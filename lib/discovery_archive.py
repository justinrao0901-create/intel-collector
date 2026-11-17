"""Additive, opt-in response archive; never alters the collector's candidate pool."""
from __future__ import annotations
import hashlib
import json
import gzip
import subprocess
import tarfile
import tempfile
import os
from pathlib import Path
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

LATEST = {
    'daily': ['latest-daily.json', 'latest-manifest.json', 'latest-coverage.json', 'latest-health.json'],
    'weekly': ['latest-weekly-papers.json', 'latest-papers-health.json'],
    'payment': ['latest-payment.json'], 'monthly': ['latest-monthly.json'],
}
LIMITATIONS = [
    'Only actual configured requests are observed; this is not the entire source universe.',
    'Existing query windows, pagination, API defaults and source caps remain unchanged; exact code/config are archived.',
    'All response bytes are archived before decoding, date filtering, per-source caps, merging and ranking.',
    'HTTP retries remain separate observations; semantic deduplication requires explicit downstream decisions.',
    'Publication fields retain their original precision; no publication or historical-availability inference is added.',
    'Legacy collector TLS verification remains disabled; the archive does not upgrade source authenticity.',
    'Scraped HTML anchors include navigation; downstream exclusions must be explicit.',
]

def digest(data): return hashlib.sha256(data).hexdigest()
def encoded(obj): return (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode()
def write_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream: stream.write(data)
def utcnow(): return datetime.now(timezone.utc).isoformat()

def active_root():
    profile = os.environ.get('DISCOVERY_ARCHIVE_PROFILE')
    if not profile: return None
    if profile not in LATEST: raise ValueError('unknown archive profile')
    end = os.environ['DISCOVERY_ARCHIVE_DATE']
    datetime.strptime(end, '%Y-%m-%d')
    run = os.environ['GITHUB_RUN_ID']+'-'+os.environ.get('GITHUB_RUN_ATTEMPT', '1')
    if not re.fullmatch(r'[0-9]+-[0-9]+', run): raise ValueError('invalid immutable workflow run id')
    return Path(__file__).resolve().parents[1]/'.discovery-private'/profile/end/run

class Links(HTMLParser):
    def __init__(self): super().__init__(); self.records=[]; self.current=None
    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'a':
            record={'attributes': list(attrs), 'text': ''}
            self.records.append(record); self.current=record
    def handle_data(self, data):
        if self.current is not None: self.current['text']+=data
    def handle_endtag(self, tag):
        if tag.lower()=='a': self.current=None

def observations(raw):
    """Lossless JSON records or complete XML entries; unknown formats remain documents."""
    text=raw.decode('utf-8', errors='replace')
    try:
        value=json.loads(text)
        if isinstance(value,list): return value, 'json-list', []
        if isinstance(value,dict):
            for key in ('hits','results','studies','papers','data'):
                if isinstance(value.get(key),list):
                    return value[key], 'json-'+key, ['All enclosing response fields, including other arrays, are retained in response.bin; only the named collection is expanded here.']
            return [value], 'json-document', ['Object response retained as one complete record; may be enrichment or nested collections.']
        return [value], 'json-scalar', ['Scalar response is not assumed to be a candidate.']
    except (ValueError,UnicodeError): pass
    try:
        root=ET.fromstring(raw)
        entries=[e for e in root.iter() if e.tag.rsplit('}',1)[-1] in ('item','entry')]
        if entries or root.tag.rsplit('}',1)[-1].lower() in ('rss','feed','RDF'):
            records=[]
            for entry in entries:
                records.append({'entry_xml': ET.tostring(entry,encoding='unicode'),
                                'fields':[{'tag':e.tag,'text':''.join(e.itertext()),'attributes':dict(e.attrib)} for e in entry]})
            return records, 'xml-feed', []
    except ET.ParseError: pass
    if '<html' in text.lower() or '<a ' in text.lower():
        parser=Links(); parser.feed(text)
        if parser.records: return parser.records, 'html-anchors', ['Anchor inventory is unfiltered; scripts and non-anchor discoveries require separate interpretation of raw bytes.']
    return [{'document':text}], 'unparsed-document', ['Unrecognized response retained in full; candidate extraction requires explicit review.']

def record(url, raw, *, status, headers, final_url, attempt, max_bytes, error=None):
    root=active_root()
    if root is None: return
    if (root/'manifest.json').exists(): raise ValueError('cannot append to sealed archive')
    capture=root/'responses'/uuid.uuid4().hex
    capture.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(root, 0o700)
    # Save bytes first, before parsing or writing any summaries.
    write_new(capture/'response.bin', raw)
    records, parser, limitations=observations(raw) if error is None else ([], 'failed-response', ['Request failed; exact body and error are retained.'])
    inventory={'schema_version':1,'records':records}
    write_new(capture/'observations.json', encoded(inventory))
    event={'schema_version':1,'url':url,'query':urllib.parse.urlsplit(url).query,
           'final_url':final_url,'status':status,'headers':list(headers),'attempt':attempt,
           'retrieved_at':utcnow(),'error':error,'response_sha256':digest(raw),'response_bytes':len(raw),
           'business_read_limit_bytes':max_bytes,'archive_truncated':False,'response_complete':error is None,'parser':parser,
           'observation_count':len(records),'limitations':limitations,
           'observation_ids':[digest((url+'\n'+digest(raw)+'\n'+str(i)).encode()) for i in range(len(records))]}
    write_new(capture/'event.json', encoded(event))

def assert_public_tree(repo):
    """Do not publish any accidental plaintext from an older/partial archive tree."""
    public=repo/'prepared/discovery'
    allowed={'schema_version','profile','date','status','run_id','run_attempt','format','ciphertext_path',
             'ciphertext_sha256','ciphertext_bytes','compressed_sha256','compressed_bytes',
             'manifest_sha256','manifest_bytes','certificate_sha256'}
    folders=set()
    for path in public.rglob('*'):
        if path.is_symlink():raise ValueError('public archive symlink forbidden')
        if path.is_file():
            if path.name not in {'evidence.cms','envelope.json'}:raise ValueError('plaintext or unexpected public archive member')
            folders.add(path.parent)
    for folder in folders:
        if {p.name for p in folder.iterdir()}!={'evidence.cms','envelope.json'}:
            raise ValueError('public archive must contain exactly ciphertext and safe envelope')
        envelope=json.loads((folder/'envelope.json').read_bytes())
        if set(envelope)!=allowed or envelope['schema_version']!=2 or envelope['profile'] not in LATEST:
            raise ValueError('unsafe public envelope schema')
        if envelope['format']!='openssl-cms-aes-256-gcm-tar-gzip-v1' or envelope['status'] not in {'sealed','failed'}:
            raise ValueError('unsafe public envelope format')
        datetime.strptime(envelope['date'],'%Y-%m-%d')
        if not re.fullmatch(r'[0-9]+',envelope['run_id']) or not re.fullmatch(r'[0-9]+',envelope['run_attempt']):
            raise ValueError('unsafe public workflow identity')
        expected=Path('discovery')/envelope['profile']/envelope['date']/(envelope['run_id']+'-'+envelope['run_attempt'])/'evidence.cms'
        if envelope['ciphertext_path']!=str(expected) or repo/'prepared'/expected!=folder/'evidence.cms':
            raise ValueError('public envelope path mismatch')
        for key in ('ciphertext_sha256','compressed_sha256','manifest_sha256','certificate_sha256'):
            if not re.fullmatch(r'[0-9a-f]{64}',envelope[key]):raise ValueError('unsafe public digest')
        for key in ('ciphertext_bytes','compressed_bytes','manifest_bytes'):
            if type(envelope[key]) is not int or envelope[key]<=0:raise ValueError('unsafe public byte count')
        cipher=(folder/'evidence.cms').read_bytes()
        if digest(cipher)!=envelope['ciphertext_sha256'] or len(cipher)!=envelope['ciphertext_bytes']:
            raise ValueError('public ciphertext changed')

def _encrypt(root, manifest, repo):
    """Only ciphertext and non-content envelope metadata may leave private staging."""
    profile=manifest['profile'];end=manifest['date']
    run=manifest['run_id']+'-'+manifest['run_attempt']
    public=repo/'prepared'/'discovery'/profile/end/run
    public.mkdir(parents=True,exist_ok=False)
    cert=repo/'config'/'discovery-certificate.pem'
    if cert.is_symlink() or not cert.is_file(): raise ValueError('public discovery certificate required')
    # Public certificate only; a private key is never deployed to the collector.
    certificate=cert.read_bytes()
    if b'PRIVATE KEY' in certificate or b'BEGIN CERTIFICATE' not in certificate:
        raise ValueError('only public X509 certificate may be committed')
    files=sorted(p for p in root.rglob('*') if p.is_file())
    if any(p.is_symlink() or p.resolve()!=p for p in files): raise ValueError('archive symlink forbidden')
    with tempfile.TemporaryDirectory(prefix='encrypt-',dir=repo/'.discovery-private') as tmp:
        compressed=Path(tmp)/'evidence.tar.gz';cipher=Path(tmp)/'evidence.cms'
        with compressed.open('xb') as raw:
            os.chmod(compressed,0o600)
            with gzip.GzipFile(fileobj=raw,mode='wb',mtime=0,filename='') as zipped:
                with tarfile.open(fileobj=zipped,mode='w|',format=tarfile.USTAR_FORMAT) as tar:
                    for path in files:
                        info=tarfile.TarInfo(str(path.relative_to(root)))
                        info.size=path.stat().st_size;info.mode=0o600;info.mtime=0
                        with path.open('rb') as stream:tar.addfile(info,stream)
        result=subprocess.run(['openssl','cms','-encrypt','-binary','-aes-256-gcm','-outform','DER',
                               '-in',str(compressed),'-out',str(cipher),str(cert)],
                              stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=180)
        if result.returncode or not cipher.is_file() or not cipher.stat().st_size:
            raise ValueError('CMS encryption failed; no plaintext may be uploaded')
        encrypted=cipher.read_bytes();write_new(public/'evidence.cms',encrypted)
        pointer={'schema_version':2,'profile':profile,'date':end,'status':manifest['status'],
                 'run_id':manifest['run_id'],'run_attempt':manifest['run_attempt'],
                 'format':'openssl-cms-aes-256-gcm-tar-gzip-v1',
                 'ciphertext_path':str((public/'evidence.cms').relative_to(repo/'prepared')),
                 'ciphertext_sha256':digest(encrypted),'ciphertext_bytes':len(encrypted),
                 'compressed_sha256':digest(compressed.read_bytes()),'compressed_bytes':compressed.stat().st_size,
                 'manifest_sha256':digest((root/'manifest.json').read_bytes()),
                 'manifest_bytes':(root/'manifest.json').stat().st_size,
                 'certificate_sha256':digest(certificate)}
    write_new(public/'envelope.json',encoded(pointer))
    assert_public_tree(repo)
    if manifest['status']=='sealed':
        target=repo/'prepared'/('latest-discovery-'+profile+'.json')
        temporary=target.with_suffix('.tmp');temporary.write_bytes(encoded(pointer));temporary.replace(target)
    return pointer

def seal(collection_status='success'):
    root=active_root()
    if root is None: raise ValueError('archive environment required')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if (root/'manifest.json').exists(): raise ValueError('archive already sealed')
    repo=Path(__file__).resolve().parents[1]
    profile=os.environ['DISCOVERY_ARCHIVE_PROFILE'];end=os.environ['DISCOVERY_ARCHIVE_DATE']
    events=[];prepared={};failures=[]
    if collection_status!='success': failures.append('collector job did not complete successfully: '+collection_status)
    try:
        for folder in sorted((root/'responses').iterdir()):
            if not folder.is_dir() or {p.name for p in folder.iterdir()} != {'response.bin','observations.json','event.json'}:
                raise ValueError('incomplete response capture')
            event=json.loads((folder/'event.json').read_bytes())
            raw=(folder/'response.bin').read_bytes();inv=json.loads((folder/'observations.json').read_bytes())
            if digest(raw)!=event['response_sha256'] or len(inv['records'])!=event['observation_count']:
                raise ValueError('changed archive capture')
            events.append(str((folder/'event.json').relative_to(root)))
        if not events: raise ValueError('no requests captured')
        for name in LATEST[profile]:
            raw=(repo/'prepared'/name).read_bytes();doc=json.loads(raw)
            actual=doc.get('date',doc.get('end'))
            if actual!=end or ('end' in doc and doc['end']!=end): raise ValueError('prepared date mismatch: '+name)
            prepared[name]={'sha256':digest(raw),'bytes':len(raw)}
            write_new(root/'prepared'/name,raw)
    except Exception as error:
        # Failure details remain encrypted; never fall back to a plaintext artifact.
        failures.append(repr(error))
    for pattern in ('lib/*.py','config/*.json','config/discovery-certificate.pem','run_*.py',
                    '.github/workflows/*.yml','tools/seal_discovery.py'):
        for path in sorted(repo.glob(pattern)):
            write_new(root/'inputs'/path.relative_to(repo),path.read_bytes())
    files=[]
    for path in sorted(root.rglob('*')):
        if path.is_file():
            if path.is_symlink():raise ValueError('archive symlink forbidden')
            raw=path.read_bytes();files.append({'path':str(path.relative_to(root)),'sha256':digest(raw),'bytes':len(raw)})
    manifest={'schema_version':1,'profile':profile,'date':end,'run_id':os.environ['GITHUB_RUN_ID'],
              'run_attempt':os.environ.get('GITHUB_RUN_ATTEMPT','1'),'collector_commit':os.environ.get('GITHUB_SHA'),
              'sealed_at':utcnow(),'status':'failed' if failures else 'sealed','validation_errors':failures,
              'prepared':prepared,'events':events,'files':files,'limitations':LIMITATIONS,
              'quality_verified':False,'source_universe_complete':False}
    write_new(root/'manifest.json',encoded(manifest))
    pointer=_encrypt(root,manifest,repo)
    if failures: raise ValueError('discovery validation failed; encrypted failure evidence retained')
    return pointer
