"""Real OpenSSL software-fixture tests; no source/network/model requests."""
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]/'lib/discovery_archive.py'
class EncryptedArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve();self.repo=self.base/'repo'
        for name in ('lib','config','prepared'):(self.repo/name).mkdir(parents=True)
        shutil.copyfile(SOURCE,self.repo/'lib/discovery_archive.py')
        (self.repo/'config/settings.json').write_text('{}')
        self.key=self.base/'synthetic-private.pem';self.cert=self.repo/'config/discovery-certificate.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-subj','/CN=Software-only-archive-test',
                        '-keyout',str(self.key),'-out',str(self.cert),'-days','1'],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        os.chmod(self.key,0o600)
        spec=importlib.util.spec_from_file_location('synthetic_archive',self.repo/'lib/discovery_archive.py')
        self.archive=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.archive)
        env={'DISCOVERY_ARCHIVE_PROFILE':'payment','DISCOVERY_ARCHIVE_DATE':'2026-11-28','GITHUB_RUN_ID':'100','GITHUB_RUN_ATTEMPT':'1','GITHUB_SHA':'f'*40}
        self.env=patch.dict(os.environ,env);self.env.start();self.addCleanup(self.env.stop)
        self.archive.record('https://private-source.test/raw',b'FULL RAW PRIVATE CONTENT',status=200,
                            headers=[('Private-Header','secret-fixture')],final_url='https://private-source.test/raw',attempt=0,max_bytes=4)
        (self.repo/'prepared/latest-payment.json').write_text(json.dumps({'date':'2026-11-28'}))
    def decrypt(self,folder):
        output=self.base/'decrypted.tar.gz'
        subprocess.run(['openssl','cms','-decrypt','-binary','-inform','DER','-in',str(folder/'evidence.cms'),
                        '-recip',str(self.cert),'-inkey',str(self.key),'-out',str(output)],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(output.read_bytes())),mode='r:') as tar:
            return {member.name:tar.extractfile(member).read() for member in tar.getmembers()}
    def test_roundtrip_full_private_population(self):
        pointer=self.archive.seal();folder=self.repo/'prepared'/Path(pointer['ciphertext_path']).parent
        self.assertEqual({p.name for p in folder.iterdir()},{'evidence.cms','envelope.json'})
        for path in folder.iterdir():self.assertNotIn(b'private-source.test',path.read_bytes())
        files=self.decrypt(folder);manifest=json.loads(files['manifest.json'])
        self.assertEqual(set(files),{row['path'] for row in manifest['files']}|{'manifest.json'})
        for row in manifest['files']:self.assertEqual(self.archive.digest(files[row['path']]),row['sha256'])
        self.assertTrue(any(value==b'FULL RAW PRIVATE CONTENT' for value in files.values()))
        self.assertEqual(manifest['status'],'sealed')
    def test_failure_still_encrypts_all_evidence(self):
        with self.assertRaises(ValueError):self.archive.seal('failure')
        folder=self.repo/'prepared/discovery/payment/2026-11-28/100-1'
        self.assertEqual({p.name for p in folder.iterdir()},{'evidence.cms','envelope.json'})
        self.assertFalse((self.repo/'prepared/latest-discovery-payment.json').exists())
        files=self.decrypt(folder);self.assertEqual(json.loads(files['manifest.json'])['status'],'failed')
        self.assertTrue(any(value==b'FULL RAW PRIVATE CONTENT' for value in files.values()))
    def test_public_plaintext_guard(self):
        pointer=self.archive.seal();folder=self.repo/'prepared'/Path(pointer['ciphertext_path']).parent
        (folder/'accidental-response.bin').write_bytes(b'not allowed public')
        with self.assertRaises(ValueError):self.archive.assert_public_tree(self.repo)
    def test_public_envelope_rejects_content_fields(self):
        pointer=self.archive.seal();folder=self.repo/'prepared'/Path(pointer['ciphertext_path']).parent
        path=folder/'envelope.json';doc=json.loads(path.read_bytes());doc['raw_body']='not allowed';path.write_text(json.dumps(doc))
        with self.assertRaises(ValueError):self.archive.assert_public_tree(self.repo)
    def test_bad_certificate_never_publishes_plaintext(self):
        self.cert.write_text('NOT A CERTIFICATE')
        with self.assertRaises(ValueError):self.archive.seal()
        public=self.repo/'prepared/discovery'
        self.assertFalse(any(p.is_file() for p in public.rglob('*')))
        self.assertTrue(any(p.name=='response.bin' for p in (self.repo/'.discovery-private').rglob('*')))

if __name__=='__main__':unittest.main()
