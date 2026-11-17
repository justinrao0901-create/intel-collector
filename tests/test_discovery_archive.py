"""Offline archive capture: no requests are sent, original prepared results unchanged."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from lib import util, discovery_archive as archive

class Response(io.BytesIO):
    status=200
    headers={'Content-Type':'application/json'}
    def geturl(self): return 'https://source.test/data?limit=5'

class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'capture'
        self.patch=patch.object(archive,'active_root',return_value=self.root);self.patch.start();self.addCleanup(self.patch.stop)
    def test_all_bytes_saved_before_cap_and_unchanged_business_result(self):
        raw=json.dumps([{'id':i,'publishedAt':'2026-11-20T12:01:02.123456Z'} for i in range(90)]).encode()
        with patch('urllib.request.urlopen',return_value=Response(raw)):
            text,ok=util.http_get('https://source.test/data?limit=5',max_bytes=17)
        self.assertTrue(ok);self.assertEqual(text,raw[:17].decode())
        folder=next((self.root/'responses').iterdir());self.assertEqual((folder/'response.bin').read_bytes(),raw)
        self.assertEqual(len(json.loads((folder/'observations.json').read_bytes())['records']),90)
        self.assertEqual(json.loads((folder/'event.json').read_bytes())['business_read_limit_bytes'],17)
    def test_disabled_archive_keeps_original_reads(self):
        with patch.object(archive,'active_root',return_value=None),patch('urllib.request.urlopen',return_value=Response(b'abcdefgh')):
            self.assertEqual(util.http_get('https://source.test',max_bytes=3),('abc',True))
        self.assertFalse(self.root.exists())
    def test_failed_request_body_and_retry_not_lost(self):
        failed=urllib.error.HTTPError('https://source.test',503,'Unavailable',{},io.BytesIO(b'exact failure bytes'))
        with patch('urllib.request.urlopen',side_effect=[failed,Response(b'[]')]),patch('time.sleep'):
            self.assertEqual(util.http_get('https://source.test',retries=1),('[]',True))
        events=[json.loads(p.read_bytes()) for p in self.root.glob('responses/*/event.json')]
        self.assertEqual(sorted(e['status'] for e in events),[200,503]);self.assertEqual(len(events),2)
        self.assertIn(b'exact failure bytes',[p.read_bytes() for p in self.root.glob('responses/*/response.bin')])
    def test_archive_write_failure_is_not_source_retry(self):
        with patch('urllib.request.urlopen',return_value=Response(b'[]')) as request,patch.object(archive,'record',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):util.http_get('https://source.test',retries=2)
        self.assertEqual(request.call_count,1)
    def test_sealed_archive_cannot_append(self):
        self.root.mkdir();(self.root/'manifest.json').write_text('{}')
        with self.assertRaises(ValueError):archive.record('https://source.test',b'[]',status=200,headers=[],final_url='https://source.test',attempt=0,max_bytes=None)
    def test_xml_date_preserved(self):
        records,parser,_=archive.observations(b'<rss><channel><item><pubDate>2026-11-20T12:01:02.123456+08:00</pubDate></item></channel></rss>')
        self.assertEqual(len(records),1);self.assertIn('123456+08:00',records[0]['entry_xml'])

if __name__=='__main__':unittest.main()
