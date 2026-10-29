"""Real HTTP, folder imports, SSE completion, failure, and result identity."""
import base64
import hashlib
import gzip
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote
from io import BytesIO
import tarfile
import zipfile

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

    def upload_original(self, hour='13'):
        source = ROOT / f'raw_dataset/extracted/{hour}点sim2合并anony.dat'
        return self.request('/api/datasets/import', {'files': [{
            'path': f'original/{source.name}',
            'content': base64.b64encode(source.read_bytes()).decode(),
        }]})['dataset']

    def submit_dataset(self, dataset, algorithm=None, snapshot=None):
        snapshot = snapshot or dataset['snapshots'][0]
        payload = {'dataset_id': dataset['id'], 'snapshot_id': snapshot['id']}
        if algorithm is not None:
            payload['algorithm'] = algorithm
        return self.request('/api/runs', payload)

    def test_one_folder_registers_one_dataset_with_two_times_and_all_reference_files(self):
        folder = ROOT / 'raw_dataset/extracted'
        sources = [p for p in sorted(folder.rglob('*')) if p.is_file()]
        before = {d['id'] for d in self.request('/api/datasets')['datasets']}
        response = self.request('/api/datasets/import', {'files': [
            {'path': f'{folder.name}/{p.relative_to(folder).as_posix()}',
             'content': base64.b64encode(p.read_bytes()).decode()} for p in sources]})
        self.assertEqual(set(response), {'dataset'})
        dataset = response['dataset']
        self.assertEqual(dataset['label'], 'extracted')
        self.assertNotIn('bus_count', dataset)
        self.assertEqual([(s['label'],s['bus_count']) for s in dataset['snapshots']], [('13:00',2145),('20:00',2146)])
        self.assertTrue(all('path' not in s for s in dataset['snapshots']))
        self.assertEqual(dataset['summary'], {'file_count':21,'snapshot_count':2,'feeder_file_count':18,
                                               'catalog_file_count':1,'supplement_file_count':0})
        references = [f for f in dataset['files'] if f['kind']!='snapshot']
        self.assertTrue(all(f['usage']=='reference_only' for f in references))
        after = {d['id'] for d in self.request('/api/datasets')['datasets']}
        self.assertEqual(after - before, {dataset['id']})
        uploaded = Path(self.directory.name)/'uploads'/dataset['id'].removeprefix('upload-')
        for metadata,source in zip(dataset['files'],sources):
            self.assertEqual((uploaded/metadata['path']).read_bytes(),source.read_bytes())
            self.assertEqual(metadata['sha256'],hashlib.sha256(source.read_bytes()).hexdigest())
        for snapshot in dataset['snapshots']:
            task = self.submit_dataset(dataset, 'nr', snapshot)
            self.assertEqual(self.events(task)[-1]['type'],'completed')
            result = self.request(f"/api/runs/{task['task_id']}/result")
            run = result['run']
            self.assertEqual(run['dataset_id'],dataset['id'])
            self.assertEqual(run['snapshot_id'],snapshot['id'])
            self.assertEqual(run['snapshot_time'],snapshot['time'])
            self.assertEqual(run['source_sha256'],snapshot['sha256'])
            self.assertEqual(run['reference_file_count'],19)
            self.assertFalse(run['excel_integrated'])
            self.assertEqual(result['scenario']['bus_count'],snapshot['bus_count'])

    def test_snapshot_selection_is_required_and_cannot_cross_datasets(self):
        first, second = self.upload_original('13'), self.upload_original('20')
        for payload in ({'dataset_id':first['id']},
                        {'dataset_id':first['id'],'snapshot_id':second['snapshots'][0]['id']},
                        {'dataset_id':first['snapshots'][0]['id'],'snapshot_id':first['snapshots'][0]['id']}):
            with self.subTest(payload=payload), self.assertRaises(HTTPError) as error:
                self.request('/api/runs',payload)
            self.assertEqual(error.exception.code,400)

    def test_archive_import_preserves_dataset_and_can_run(self):
        folder = ROOT/'raw_dataset/extracted'
        sources = sorted(p for p in folder.rglob('*') if p.is_file())
        for suffix in ('.zip', '.tar.gz'):
            output = BytesIO()
            if suffix == '.zip':
                with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
                    for source in sources:
                        archive.writestr('石家庄/'+source.relative_to(folder).as_posix(), source.read_bytes())
            else:
                with tarfile.open(fileobj=output, mode='w:gz') as archive:
                    for source in sources:
                        content=source.read_bytes()
                        member=tarfile.TarInfo('石家庄/'+source.relative_to(folder).as_posix());member.size=len(content)
                        archive.addfile(member,BytesIO(content))
            request=Request(self.base+'/api/datasets/import-archive?name='+quote('原始数据'+suffix),
                            data=output.getvalue(),headers={'Content-Type':'application/octet-stream'})
            with urlopen(request,timeout=30) as response:
                self.assertEqual(response.status,201)
                dataset=json.load(response)['dataset']
            self.assertEqual(dataset['label'],'石家庄')
            self.assertEqual(dataset['summary']['file_count'],21)
            self.assertEqual([s['time'] for s in dataset['snapshots']],['13:00','20:00'])
            uploaded=Path(self.directory.name)/'uploads'/dataset['id'].removeprefix('upload-')
            for source in sources:
                self.assertEqual((uploaded/'石家庄'/source.relative_to(folder)).read_bytes(),source.read_bytes())
            task=self.submit_dataset(dataset,'nr')
            self.assertEqual(self.events(task)[-1]['type'],'completed')
            self.assertTrue(self.request(f"/api/runs/{task['task_id']}/result")['run']['source_unchanged'])

    def test_queue_and_backend_timing_are_disjoint_and_survive_archiving(self):
        dataset=self.upload_original()
        entered,release=threading.Event(),threading.Event()
        def blocker():
            entered.set();release.wait(10)
        self.server.service.executor.submit(blocker)
        self.assertTrue(entered.wait(2))
        try:
            task=self.submit_dataset(dataset,'nr')
            time.sleep(.03)  # Test-only queue wait; the running worker is held by an event.
        finally:
            release.set()
        events=self.events(task)
        run=self.request(f"/api/runs/{task['task_id']}/result")['run']
        timing=run['service_timing']
        self.assertGreaterEqual(timing['queue_ms'],25)
        self.assertGreaterEqual(timing['execution_ms'],run['total_ms'])
        self.assertLessEqual(run['solver_ms'],timing['execution_ms'])
        self.assertAlmostEqual(timing['queue_ms']+timing['execution_ms']+timing['persistence_ms'],timing['ready_ms'],places=7)
        self.assertEqual(events[-1]['run']['service_timing'],timing)
        self.assertEqual(json.loads((Path(self.directory.name)/'runs'/task['task_id']/'timing.json').read_text()),timing)
        archived={**self.server.service.get(task['task_id']),'archived':True,'result':None}
        self.assertEqual(self.server.service.result(archived)['run']['service_timing'],timing)

    def test_invalid_archive_does_not_register_any_dataset(self):
        before=self.request('/api/datasets')
        request=Request(self.base+'/api/datasets/import-archive?name=bad.zip',data=b'broken zip',
                        headers={'Content-Type':'application/octet-stream'})
        with self.assertRaises(HTTPError) as error:
            urlopen(request,timeout=5)
        self.assertEqual(error.exception.code,400)
        self.assertIn('压缩包',json.load(error.exception)['error'])
        self.assertEqual(self.request('/api/datasets'),before)

    def test_historical_per_dat_registrations_group_by_folder_after_restart(self):
        from gmss.service import JobService
        with tempfile.TemporaryDirectory() as directory:
            files = [{'path':f'old/{h}点.dat','size_bytes':1} for h in ('13','20')]
            records = [{'id':f'old-{h}', 'folder_id':'one-folder','folder_name':'old', 'files':files,
                        'filename':f'{h}点.dat','path':f'{h}.dat','bus_count':2121,'branch_count':2127}
                       for h in ('20','13')]
            registry = Path(directory,'datasets.json');original=json.dumps(records);registry.write_text(original)
            service = JobService(runtime=directory)
            try:
                datasets = service.datasets()
                self.assertEqual(len(datasets),1)
                self.assertEqual(datasets[0]['id'],'upload-one-folder')
                self.assertEqual([s['time'] for s in datasets[0]['snapshots']],['13:00','20:00'])
                self.assertTrue(all('path' not in s for s in datasets[0]['snapshots']))
                self.assertEqual(registry.read_text(),original)
            finally:
                service.executor.shutdown(wait=True)

    def test_unlabelled_and_duplicate_times_remain_distinct_snapshots(self):
        data=(ROOT/'raw_dataset/extracted/13点sim2合并anony.dat').read_bytes()
        dataset=self.request('/api/datasets/import', {'files':[
            {'path':f'custom/{name}','content':base64.b64encode(data).decode()}
            for name in ('network.dat','13点-a.dat','13点-b.dat')]})['dataset']
        self.assertEqual(len(dataset['snapshots']),3)
        self.assertEqual([s['time'] for s in dataset['snapshots']],['13:00','13:00',None])
        self.assertEqual(len({s['id'] for s in dataset['snapshots']}),3)
        self.assertEqual(dataset['snapshots'][-1]['label'],'network.dat')

    def test_multi_root_and_partial_invalid_uploads_do_not_register_or_leave_files(self):
        content=base64.b64encode((ROOT/'raw_dataset/extracted/13点sim2合并anony.dat').read_bytes()).decode()
        before=self.request('/api/datasets')['datasets']
        files_before=set(Path(self.directory.name).rglob('*.dat'))
        for files in ([{'path':'one/a.dat','content':content},{'path':'two/b.dat','content':content}],
                      [{'path':'same/a.dat','content':content},{'path':'same/b.dat','content':'YmFk'}]):
            with self.assertRaises(HTTPError) as error:
                self.request('/api/datasets/import',{'files':files})
            self.assertEqual(error.exception.code,400)
            self.assertEqual(self.request('/api/datasets')['datasets'],before)
            self.assertEqual(set(Path(self.directory.name).rglob('*.dat')),files_before)

    def test_fresh_service_has_no_default_or_synthetic_datasets(self):
        from gmss.service import JobService
        with tempfile.TemporaryDirectory() as directory:
            service = JobService(runtime=directory, raw_dir=Path(directory) / 'absent')
            try:
                self.assertEqual(service.datasets(), [])
                for dataset in ('', '13', '20', 'sjz_13_x50_ring'):
                    with self.subTest(dataset=dataset), self.assertRaises(ValueError):
                        service.resolve(dataset)
            finally:
                service.executor.shutdown(wait=True)

    def test_historical_synthetic_registrations_stay_hidden_after_restart(self):
        from gmss.service import JobService
        with tempfile.TemporaryDirectory() as directory:
            historical = {'id':'upload-old-synthetic','label':'old copy',
                          'path':'old-copy.dat','synthetic':True,'builtin':False}
            Path(directory,'datasets.json').write_text(json.dumps([historical]))
            service = JobService(runtime=directory)
            try:
                self.assertEqual(service.datasets(), [])
                with self.assertRaises(ValueError):
                    service.resolve(historical['id'])
            finally:
                service.executor.shutdown(wait=True)

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
            urls = re.findall(r'(?:src|href)="((?:styles\.css|data\.js|trace\.js|app\.js)\?v=[a-f0-9]+)"', html)
            self.assertEqual(len(urls), 3)
            self.assertNotIn('src="data.js', html)
            self.assertNotIn('已保存结果', html)
            self.assertIn('id="folderName">尚未上传', html)
            self.assertIn('id="runBtn" disabled', html)
            self.assertNotIn('id="datasetSelect"', html)
            for url in urls:
                with urlopen(self.base + '/' + url, timeout=5) as asset:
                    self.assertEqual(asset.status, 200)
                    self.assertEqual(asset.headers['Cache-Control'], 'no-store')
                    self.assertTrue(asset.read())

    def test_real_run_stream_and_result(self):
        dataset=self.upload_original()
        task=self.submit_dataset(dataset)
        events=self.events(task)
        self.assertEqual(events[-1]['type'],'completed')
        self.assertEqual([e['id'] for e in events],list(range(1,len(events)+1)))
        self.assertTrue(any(e['type']=='network' for e in events))
        self.assertTrue(any(e['type']=='iteration' for e in events))
        iterations=[e for e in events if e['type']=='iteration']
        self.assertTrue(all(e['stage']=='partition-schur' for e in iterations))
        self.assertTrue(any(e['coordination'] for e in iterations))
        result=self.request(f"/api/runs/{task['task_id']}/result")
        self.assertEqual(result['run']['task_id'],task['task_id'])
        self.assertEqual(result['run']['dataset_id'],dataset['id'])
        self.assertEqual(result['run']['algorithm'],'partitioned-nr')
        self.assertEqual(result['run']['impedance_policy']['transformer_base_mva'],100.)
        self.assertTrue(result['run']['impedance_policy']['is_assumption'])
        self.assertEqual(result['scenario']['impedance_policy'],result['run']['impedance_policy'])
        self.assertEqual(result['scenario']['summary']['converged'],len(result['scenario']['partitions']))
        self.assertEqual(result['run']['partitioning']['strategy'],'weighted')
        phases=[e for e in events if e['type']=='phase']
        self.assertEqual(len(phases),result['run']['iterations']*3)
        self.assertEqual([e['stage'] for e in phases[:3]],['local-elimination','boundary-coordination','back-substitution'])
        self.assertTrue(all('partition_states' in e and 'boundary_flows' in e for e in iterations))
        trace=self.request(f"/api/runs/{task['task_id']}/trace")
        self.assertEqual(trace['events'],events)
        self.assertEqual(len(result['scenario']['trafos']),97)
        # A second run is a new calculation, not reuse of the first task.
        second=self.submit_dataset(dataset,'damped-nr')
        self.assertNotEqual(task['task_id'],second['task_id'])
        self.assertEqual(self.events(second)[-1]['type'],'completed')
        ordinary=self.request(f"/api/runs/{second['task_id']}/result")
        self.assertEqual(ordinary['run']['partition_check'],'not requested')
        self.assertEqual(ordinary['run']['boundary_iterations'],0)

    def test_microgrids_are_read_from_dat_by_default_without_duplicate_equipment(self):
        dataset = self.upload_original()
        snapshot = dataset['snapshots'][0]
        task = self.submit_dataset(dataset)
        events = self.events(task)
        self.assertEqual(events[-1]['type'], 'completed')
        result = self.request(f"/api/runs/{task['task_id']}/result")
        self.assertNotIn('microgrid_mode', result['run'])
        self.assertEqual(result['run']['microgrid_count'], 6)
        self.assertEqual(result['scenario']['bus_count'], snapshot['bus_count'])
        self.assertEqual(len(result['scenario']['trafos']), 97)
        for e in (e for e in events if e['type'] == 'iteration'):
            self.assertEqual(len(e['microgrid_states']), 6)
        final = [e for e in events if e['type'] == 'iteration'][-1]
        for state, mg in zip(final['microgrid_states'], result['scenario']['microgrids']):
            self.assertAlmostEqual(state['import_p_mw'], mg['import_p_mw'])
            self.assertLess(mg['max_equation_residual_mva'], 1e-5)
        repeated = self.submit_dataset(dataset)
        self.assertEqual(self.events(repeated)[-1]['type'], 'completed')
        again = self.request(f"/api/runs/{repeated['task_id']}/result")
        self.assertEqual(again['scenario']['bus_count'], snapshot['bus_count'])
        self.assertEqual(again['run']['microgrid_count'], 6)
        self.assertEqual(again['run']['source_sha256'], snapshot['sha256'])
        self.assertEqual(again['run']['source_sha256'], result['run']['source_sha256'])
        html = (ROOT / 'web_sjz/index.html').read_text()
        self.assertNotIn('microgridMode', html)
        self.assertNotIn('microgridModelNote', html)

    def test_compressed_catalog_keeps_all_data_and_respects_encoding(self):
        self.upload_original()
        expected = self.request('/api/datasets')
        for encoding, compressed in (('gzip', True), ('gzip;q=0', False), ('identity', False)):
            with self.subTest(encoding=encoding):
                request = Request(self.base + '/api/datasets', headers={'Accept-Encoding': encoding})
                with urlopen(request, timeout=5) as response:
                    data = response.read()
                    self.assertEqual(response.headers.get('Content-Encoding') == 'gzip', compressed)
                    self.assertEqual(int(response.headers['Content-Length']), len(data))
                    self.assertEqual(response.headers['Vary'], 'Accept-Encoding')
                self.assertEqual(json.loads(gzip.decompress(data) if compressed else data), expected)

    def test_synthetic_presets_and_marked_uploads_are_disabled(self):
        datasets = self.request('/api/datasets')['datasets']
        self.assertTrue(all(not d.get('builtin') and not d.get('synthetic') for d in datasets))
        self.assertFalse(any(d['id'] in ('13','20','sjz_13_x5_ring','sjz_13_x50_ring') for d in datasets))
        # Reject the expansion marker before writing any uploaded files.
        before = set(Path(self.directory.name).rglob('*.dat'))
        content = base64.b64encode(b'.SJZ_EXPANSION {"kind":"synthetic_replication"}\n').decode()
        with self.assertRaises(HTTPError) as error:
            self.request('/api/datasets/import', {'files': [{'path':'copied/expanded.dat','content':content}]})
        self.assertEqual(error.exception.code,400)
        self.assertIn('复制扩展算例已停用',json.loads(error.exception.read())['error'])
        self.assertEqual(set(Path(self.directory.name).rglob('*.dat')),before)
        self.assertEqual(self.request('/api/datasets')['datasets'],datasets)

    def test_folder_upload_preserves_bytes_and_runs_20_point_case(self):
        source=ROOT/'raw_dataset/extracted/20点sim2合并anony.dat';data=source.read_bytes()
        result=self.request('/api/datasets/import',{'files':[{'path':'my-folder/20.dat','content':base64.b64encode(data).decode()},
                                {'path':'my-folder/feeder.xls','content':base64.b64encode(b'unchanged fixture').decode()}]})
        dataset=result['dataset'];snapshot=dataset['snapshots'][0]
        self.assertEqual(snapshot['sha256'],hashlib.sha256(data).hexdigest())
        self.assertNotIn('path',dataset)
        self.assertEqual(snapshot['bus_count'],2146)
        self.assertEqual(snapshot['branch_count'],2153)
        self.assertEqual(dataset['folder_name'],'my-folder')
        self.assertEqual(len(dataset['files']),2)
        task=self.submit_dataset(dataset,'nr')
        self.assertEqual(self.events(task)[-1]['type'],'completed')
        computed=self.request(f"/api/runs/{task['task_id']}/result")
        self.assertEqual(computed['run']['source_sha256'],snapshot['sha256'])
        self.assertEqual(computed['scenario']['bus_count'],2146)
        files=list(Path(self.directory.name).rglob('feeder.xls'))
        self.assertEqual(files[0].read_bytes(),b'unchanged fixture')

    def test_invalid_inputs_fail_explicitly(self):
        for path,payload in (('/api/runs',{'dataset_id':'13','algorithm':'fake'}),
                             ('/api/runs',{}),
                             ('/api/runs',{'dataset_id':None}),
                             ('/api/runs',{'dataset_id':'13'}),
                             ('/api/runs',{'dataset_id':'20'}),
                             ('/api/runs',{'dataset_id':'sjz_13_x50_ring'}),
                             ('/api/runs',{'dataset_id':'missing','algorithm':'nr'}),
                             ('/api/datasets/import',{'files':[{'path':'../escape.dat','content':'AA=='}]}),
                             ('/api/datasets/import',{'files':[{'path':'empty.xls','content':'AA=='}]})):
            with self.subTest(path=path,payload=payload),self.assertRaises(HTTPError) as error:
                self.request(path,payload)
            self.assertEqual(error.exception.code,400)

    def test_numerical_failure_never_emits_completed(self):
        dataset=self.upload_original()
        _,_,bad=self.server.service.resolve(dataset['id'],dataset['snapshots'][0]['id'])
        bad.write_bytes(b'not a DAT case')
        task=self.submit_dataset(dataset,'nr')
        events=self.events(task)
        self.assertEqual(events[-1]['type'],'failed')
        self.assertNotIn('completed',[e['type'] for e in events])
        with self.assertRaises(HTTPError) as error:
            self.request(f"/api/runs/{task['task_id']}/result")
        self.assertEqual(error.exception.code,409)

    def test_archived_jobs_keep_results_and_sse_without_retaining_large_payloads(self):
        service=self.server.service
        previous=service.retained_jobs;service.retained_jobs=1
        dataset=self.upload_original()
        try:
            first=self.submit_dataset(dataset,'nr')
            original_events=self.events(first)
            held_reference=service.get(first['task_id'])
            for _ in range(2):
                task=self.submit_dataset(dataset,'nr')
                self.assertEqual(self.events(task)[-1]['type'],'completed')
            archived=service.get(first['task_id'])
            self.assertTrue(archived['archived'])
            self.assertIsNone(archived['result'])
            self.assertEqual(archived['events'],[])
            self.assertEqual(held_reference['events'],original_events)
            self.assertEqual(self.events(first),original_events)
            result=self.request(f"/api/runs/{first['task_id']}/result")
            self.assertEqual(result['scenario']['bus_count'],2145)
            trace=self.request(f"/api/runs/{first['task_id']}/trace")
            self.assertEqual(trace['events'],original_events)
        finally:
            service.retained_jobs=previous


if __name__=='__main__':unittest.main()
