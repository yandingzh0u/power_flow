"""Upload original DAT files, exercise queued calculations and SSE subscribers.

Run against a local server. Numerical benchmarks are deliberately separate:
this measures API/telemetry overhead and correctness under concurrent requests.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import hashlib
import json
from pathlib import Path
import statistics
import threading
import time
from urllib.request import Request, urlopen


def request(base, path, payload=None):
    req = Request(base + path, data=json.dumps(payload).encode() if payload is not None else None,
                  headers={'Content-Type': 'application/json', 'Accept-Encoding': 'gzip'})
    with urlopen(req, timeout=180) as response:
        raw = response.read()
        if response.headers.get('Content-Encoding') == 'gzip':
            raw = gzip.decompress(raw)
        return json.loads(raw)


def consume(base, task_id):
    started = time.perf_counter()
    count = iterations = phases = 0
    digest = hashlib.sha256()
    with urlopen(f'{base}/api/runs/{task_id}/events', timeout=180) as response:
        for line in response:
            if not line.startswith(b'data: '):
                continue
            digest.update(line)
            event = json.loads(line[6:])
            count += 1
            assert event['id'] == count, 'SSE event gap/duplicate'
            iterations += event['type'] == 'iteration'
            phases += event['type'] == 'phase'
            if event['type'] == 'failed':
                raise AssertionError(event['message'])
            last = event
    assert last['type'] == 'completed'
    return {'events': count, 'iteration_states': iterations, 'phase_events': phases,
            'event_sha256': digest.hexdigest(), 'subscriber_ms': (time.perf_counter()-started)*1000}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:4173')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--subscribers', type=int, default=2)
    parser.add_argument('--server-pid', type=int)
    parser.add_argument('--folder', type=Path, default=Path(__file__).resolve().parents[1] / 'raw_dataset/extracted')
    parser.add_argument('--output', type=Path, default=Path('runtime/benchmarks/original_http_stress.json'))
    args = parser.parse_args()
    if not 1 <= args.jobs <= 16 or not 1 <= args.subscribers <= 4:
        parser.error('jobs must be 1..16, subscribers 1..4')
    files = [{'path': f'{args.folder.name}/{p.relative_to(args.folder).as_posix()}',
              'content': base64.b64encode(p.read_bytes()).decode('ascii')}
             for p in sorted(args.folder.rglob('*')) if p.is_file() and p.suffix.lower() in ('.dat','.xls','.xlsx')]
    dataset = request(args.base, '/api/datasets/import', {'files': files})['dataset']
    snapshots = dataset['snapshots']
    print('Uploaded one dataset:',dataset['label'],[(s['label'],s['bus_count']) for s in snapshots],flush=True)
    pid=args.server_pid or (int(Path('runtime/server.pid').read_text()) if Path('runtime/server.pid').exists() else None)
    peaks={'rss_mib':0.,'threads':0};stop=threading.Event()
    def monitor():
        while not stop.is_set():
            if pid:
                try:
                    values=dict(line.split(':',1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
                    peaks['rss_mib']=max(peaks['rss_mib'],int(values['VmRSS'].split()[0])/1024)
                    peaks['threads']=max(peaks['threads'],int(values['Threads']))
                except (FileNotFoundError,KeyError):
                    pass
            stop.wait(.1)
    monitor_thread=threading.Thread(target=monitor,daemon=True);monitor_thread.start()
    tasks=[]; started=time.perf_counter()
    for i in range(args.jobs):
        snapshot=snapshots[i % len(snapshots)]
        task=request(args.base,'/api/runs',{'dataset_id':dataset['id'],'snapshot_id':snapshot['id'],'algorithm':'partitioned-nr'})
        tasks.append({'dataset':dataset['label'],'dataset_id':dataset['id'],'snapshot':snapshot['filename'],
                      'input':snapshot,'task_id':task['task_id'],'subscribers':[]})
    with ThreadPoolExecutor(max_workers=args.jobs*args.subscribers) as pool:
        pending={pool.submit(consume,args.base,t['task_id']): t for t in tasks for _ in range(args.subscribers)}
        for future in as_completed(pending):
            task=pending[future];task['subscribers'].append(future.result())
            print(task['dataset'],task['snapshot'],task['task_id'],'subscriber completed',flush=True)
    for task in tasks:
        assert len({s['event_sha256'] for s in task['subscribers']})==1
        result=request(args.base,f"/api/runs/{task['task_id']}/result")
        run,scene=result['run'],result['scenario']
        assert run['converged'] and run['source_unchanged'] and run['residual_mva']<1e-4
        assert scene['bus_count']==task['input']['bus_count'] and run['partition_failed']==0
        assert run['source_sha256']==task['input']['sha256']
        assert run['dataset_id']==dataset['id'] and run['snapshot_id']==task['input']['id']
        assert run['snapshot_time']==task['input']['time'] and not run['excel_integrated']
        assert len(scene['lines'])+len(scene['trafos'])==task['input']['branch_count']
        assert all(p['max_equation_residual_mva']<1e-4 for p in scene['partitions'])
        assert task['subscribers'][0]['phase_events']==run['iterations']*3
        task.update(run=run,buses=scene['bus_count'],branches=len(scene['lines'])+len(scene['trafos']),
                    partitions=len(scene['partitions']),max_loading=max(e['loading'] or 0 for e in scene['lines']+scene['trafos']))
        print(task['dataset'],task['snapshot'],'validated',round(run['total_ms'],2),'ms',flush=True)
        del result
    report={'jobs':args.jobs,'subscribers_per_job':args.subscribers,'wall_ms':(time.perf_counter()-started)*1000,
            'median_backend_total_ms':statistics.median(t['run']['total_ms'] for t in tasks),
            'scope':'concurrent submissions, single solver job queue, parallel domains within each job; no artificial delays',
            'tasks':tasks,'server_peak':peaks}
    stop.set();monitor_thread.join(timeout=1)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(args.output,report['wall_ms'],'ms; all checks passed',flush=True)


if __name__=='__main__':
    main()
