"""Real HTTP, folder imports, SSE completion, failure, and result identity."""
import base64
import hashlib
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from gmss.core import ROOT
from gmss.http_api import make_server


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory=tempfile.TemporaryDirectory()
        cls.server=make_server(port=0,runtime=cls.directory.name)
        cls.server.RequestHandlerClass.func.log_message=lambda *args:None
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start();cls.base=f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.server.service.executor.shutdown(wait=True)
        cls.directory.cleanup()

    def request(self,path,payload=None):
        request=Request(self.base+path,data=json.dumps(payload).encode() if payload is not None else None,
                        headers={'Content-Type':'application/json'})
        with urlopen(request,timeout=30) as response:
            return json.loads(response.read())

    def events(self,task):
        with urlopen(self.base+f"/api/runs/{task['task_id']}/events",timeout=30) as response:
            return [json.loads(line[6:]) for line in response if line.startswith(b'data: ')]

    def test_cached_page_is_replaced_and_assets_have_revision_urls(self):
        # A browser that previously cached the pre-refactor page must get new HTML,
        # even if its saved timestamp is newer than the files on disk.
        for path in ('/', '/index.html'):
            request = Request(self.base + path, headers={
                'If-Modified-Since': 'Thu, 31 Dec 2099 23:59:59 GMT',
            })
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                html = response.read().decode('utf-8')
            self.assertNotIn('__ASSET_VERSION__', html)
            urls = re.findall(r'(?:src|href)="((?:styles\.css|data\.js|app\.js)\?v=[a-f0-9]+)"', html)
            self.assertEqual(len(urls), 3)
            for url in urls:
                with urlopen(self.base + '/' + url, timeout=5) as asset:
                    self.assertEqual(asset.status, 200)
                    self.assertEqual(asset.headers['Cache-Control'], 'no-store')
                    self.assertTrue(asset.read())

    def test_real_run_stream_and_result(self):
        task=self.request('/api/runs',{'dataset_id':'13','algorithm':'nr'})
        events=self.events(task)
        self.assertEqual(events[-1]['type'],'completed')
        self.assertEqual([e['id'] for e in events],list(range(1,len(events)+1)))
        self.assertTrue(any(e['type']=='network' for e in events))
        self.assertTrue(any(e['type']=='iteration' for e in events))
        self.assertTrue(any(e['type']=='partition' for e in events))
        result=self.request(f"/api/runs/{task['task_id']}/result")
        self.assertEqual(result['run']['task_id'],task['task_id'])
        self.assertEqual(result['run']['dataset_id'],'13')
        self.assertEqual(len(result['scenario']['trafos']),91)
        # A second run is a new calculation, not reuse of the first task.
        second=self.request('/api/runs',{'dataset_id':'13','algorithm':'damped-nr'})
        self.assertNotEqual(task['task_id'],second['task_id'])
        self.assertEqual(self.events(second)[-1]['type'],'completed')

    def test_folder_upload_preserves_bytes_and_runs_20_point_case(self):
        source=ROOT/'raw_dataset/extracted/20点sim2合并anony.dat';data=source.read_bytes()
        result=self.request('/api/datasets/import',{'files':[{'path':'my-folder/20.dat','content':base64.b64encode(data).decode()},
                                {'path':'my-folder/feeder.xls','content':base64.b64encode(b'unchanged fixture').decode()}]})
        dataset=result['datasets'][0]
        self.assertEqual(dataset['sha256'],hashlib.sha256(data).hexdigest())
        self.assertNotIn('path',dataset)
        task=self.request('/api/runs',{'dataset_id':dataset['id'],'algorithm':'nr'})
        self.assertEqual(self.events(task)[-1]['type'],'completed')
        computed=self.request(f"/api/runs/{task['task_id']}/result")
        self.assertEqual(computed['run']['source_sha256'],dataset['sha256'])
        self.assertEqual(computed['scenario']['bus_count'],2122)
        files=list(Path(self.directory.name).rglob('feeder.xls'))
        self.assertEqual(files[0].read_bytes(),b'unchanged fixture')

    def test_invalid_inputs_fail_explicitly(self):
        for path,payload in (('/api/runs',{'dataset_id':'13','algorithm':'fake'}),
                             ('/api/runs',{'dataset_id':'missing','algorithm':'nr'}),
                             ('/api/datasets/import',{'files':[{'path':'../escape.dat','content':'AA=='}]}),
                             ('/api/datasets/import',{'files':[{'path':'empty.xls','content':'AA=='}]})):
            with self.subTest(path=path,payload=payload),self.assertRaises(HTTPError) as error:
                self.request(path,payload)
            self.assertEqual(error.exception.code,400)

    def test_numerical_failure_never_emits_completed(self):
        service=self.server.service
        bad=Path(self.directory.name)/'broken.dat';bad.write_bytes(b'not a DAT case')
        service.imported.append({'id':'broken-fixture','label':'broken','filename':'broken.dat','builtin':False,'path':'broken.dat'})
        task=self.request('/api/runs',{'dataset_id':'broken-fixture','algorithm':'nr'})
        events=self.events(task)
        self.assertEqual(events[-1]['type'],'failed')
        self.assertNotIn('completed',[e['type'] for e in events])
        with self.assertRaises(HTTPError) as error:
            self.request(f"/api/runs/{task['task_id']}/result")
        self.assertEqual(error.exception.code,409)


if __name__=='__main__':unittest.main()
