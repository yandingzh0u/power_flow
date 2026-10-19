"""Dataset registry and isolated calculation jobs with replayable real events."""
from __future__ import annotations
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import time
import uuid

from .core import ROOT, run_case
from .dat import read_dat
from .raw_solver import validate_bpa_scope
from .expansion import MARKER, read_expansion
from .uploads import archive_files


def snapshot_metadata(filename):
    """Recognize explicitly labelled times; never infer one from file order."""
    match = re.search(r'(?<!\d)([01]?\d|2[0-3])(?:点|时)(?:([0-5]\d)分?)?', filename)
    if not match:
        match = re.search(r'(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)', filename)
    clock = f'{int(match[1]):02d}:{int(match[2] or 0):02d}' if match else None
    return {'time': clock, 'label': clock or filename, 'time_source': 'filename' if clock else None}


def file_metadata(path):
    path = PurePosixPath(path)
    if path.suffix.lower() == '.dat':
        return {'kind': 'snapshot', 'role': '运行断面 DAT', 'usage': 'select_one'}
    if '馈线' in path.name and '目录' in path.name:
        kind, role = 'feeder_catalog', '馈线分类目录'
    elif any('馈线' in part for part in path.parts[:-1]):
        kind, role = 'feeder', '10kV 馈线详细数据'
    else:
        kind, role = 'supplement', '配套数据表'
    return {'kind': kind, 'role': role, 'usage': 'reference_only'}


def finish_dataset(dataset):
    dataset['snapshots'].sort(key=lambda s: (s.get('time') or '99:99', s['filename'], s['id']))
    counts = {kind: sum(f['kind'] == kind for f in dataset['files'])
              for kind in ('snapshot', 'feeder', 'feeder_catalog', 'supplement')}
    dataset['summary'] = {'file_count': len(dataset['files']), 'snapshot_count': len(dataset['snapshots']),
                          'feeder_file_count': counts['feeder'], 'catalog_file_count': counts['feeder_catalog'],
                          'supplement_file_count': counts['supplement']}
    dataset['schema_version'] = 2
    return dataset


def public_dataset(dataset):
    return {**dataset, 'snapshots': [{k:v for k,v in s.items() if k != 'path'} for s in dataset['snapshots']]}


def normalize_datasets(records):
    """Group historical per-DAT registrations by upload folder, preserving bytes."""
    grouped = {}
    for record in records:
        if record.get('synthetic') or record.get('builtin'):
            continue
        if 'snapshots' in record:
            dataset = {**record, 'files': [{**f, **file_metadata(f['path'])} for f in record['files']],
                       'snapshots': [s for s in record['snapshots'] if not s.get('synthetic')]}
            if dataset['snapshots']:
                grouped[dataset['id']] = finish_dataset(dataset)
            continue
        folder_id = record.get('folder_id', record['id'])
        dataset_id = f'upload-{folder_id}'
        if dataset_id not in grouped:
            name = record.get('folder_name', record.get('label', '上传数据集'))
            files = [{**f, **file_metadata(f['path'])} for f in record.get('files', [])]
            grouped[dataset_id] = {'id': dataset_id, 'label': name, 'folder_name': name,
                                   'builtin': False, 'files': files, 'snapshots': []}
        snapshot = {k: record[k] for k in ('id', 'filename', 'path', 'sha256', 'bus_count', 'branch_count') if k in record}
        snapshot.update(snapshot_metadata(record['filename']))
        snapshot['relative_path'] = next((f['path'] for f in grouped[dataset_id]['files']
                                          if PurePosixPath(f['path']).name == record['filename']), record['filename'])
        grouped[dataset_id]['snapshots'].append(snapshot)
    return [finish_dataset(d) for d in grouped.values()]


class JobService:
    def __init__(self, runtime=None, raw_dir=None):
        self.runtime = Path(runtime or ROOT / 'runtime')
        self.raw_dir = Path(raw_dir or ROOT / 'raw_dataset/extracted')
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.jobs = {}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='powerflow')
        self.registry_path = self.runtime / 'datasets.json'
        self.imported = normalize_datasets(json.loads(self.registry_path.read_text()) if self.registry_path.exists() else [])
        self.retained_jobs = max(1, int(os.environ.get('GMSS_RETAIN_JOBS', '2')))

    def datasets(self):
        # Historical registrations remain on disk, but are never default inputs.
        return [public_dataset(d) for d in self.imported]

    def resolve(self, dataset_id, snapshot_id=None):
        if not dataset_id or dataset_id in ('13', '20'):
            raise ValueError('请先上传原始数据文件夹，再选择本次 DAT 断面')
        for dataset in self.imported:
            if dataset['id'] == dataset_id:
                if not snapshot_id:
                    raise ValueError('请选择这套数据集的计算时刻或运行断面')
                snapshot = next((s for s in dataset['snapshots'] if s['id'] == snapshot_id), None)
                if snapshot is None:
                    raise ValueError('所选断面不属于这套数据集，请重新选择计算时刻')
                path = self.runtime / snapshot['path']
                if path.is_file() and read_expansion(path):
                    raise ValueError('复制扩展算例已停用，请上传原始 DAT 数据')
                return dataset, snapshot, path
        raise ValueError('未找到所选数据集，请重新导入')

    def import_archive(self, name, data):
        files = archive_files(name, data)
        return self.import_folder([{'path': str(path), 'content': base64.b64encode(content).decode('ascii')}
                                   for path, content in files])

    def import_folder(self, files):
        if not isinstance(files, list) or not 1 <= len(files) <= 500:
            raise ValueError('文件夹必须包含 1～500 个 DAT/XLS/XLSX 文件')
        decoded, seen, total = [], set(), 0
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get('path'), str) or not isinstance(item.get('content'), str):
                raise ValueError('上传文件必须包含 path 和 base64 content')
            path = PurePosixPath(str(item['path']).replace('\\', '/'))
            if path.is_absolute() or not path.parts or any(p in ('..', '.') or p.startswith('.') for p in path.parts) or ':' in str(path):
                raise ValueError('上传文件路径无效')
            if path.suffix.lower() not in ('.dat', '.xls', '.xlsx'):
                raise ValueError('仅导入 DAT 和配套 XLS/XLSX；文档不参与计算')
            if str(path) in seen:
                raise ValueError('上传路径重复')
            seen.add(str(path))
            data = base64.b64decode(item['content'], validate=True)
            total += len(data)
            if total > 32 * 1024**2 or len(data) > 16 * 1024**2:
                raise ValueError('上传总大小限 32 MiB，单文件限 16 MiB')
            if path.suffix.lower() == '.dat' and data.startswith(MARKER.encode('ascii')):
                raise ValueError('复制扩展算例已停用，请上传原始两千余节点的 DAT 数据')
            decoded.append((path, data))
        if not any(p.suffix.lower()=='.dat' for p,_ in decoded):
            raise ValueError('文件夹中没有 DAT 潮流文件')
        roots = {p.parts[0] for p,_ in decoded if len(p.parts) > 1}
        if len(roots) > 1 or (roots and any(len(p.parts) == 1 for p,_ in decoded)):
            raise ValueError('一次只能上传一个数据集文件夹，请保留共同的顶层目录')
        folder_name = next(iter(roots), '上传数据集')
        folder_id = uuid.uuid4().hex
        root = self.runtime / 'uploads' / folder_id
        root.mkdir(parents=True)
        snapshots = []
        file_list = [{'path': str(p), 'size_bytes': len(data),
                      'sha256': hashlib.sha256(data).hexdigest(),
                      **file_metadata(p)} for p, data in decoded]
        try:
            for relative, data in decoded:
                target = root / str(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                if target.suffix.lower() == '.dat':
                    _, buses, branches, _ = read_dat(target)
                    if not buses or not branches:
                        raise ValueError(f'{relative.name} 没有可计算的节点或支路')
                    validate_bpa_scope(target, buses)
                    snapshots.append({'id': f'snapshot-{uuid.uuid4().hex}', 'filename': relative.name,
                                      **snapshot_metadata(relative.name), 'relative_path': str(relative),
                                      'bus_count': len(buses), 'branch_count': len(branches),
                                      'path': str(target.relative_to(self.runtime)),
                                      'sha256': hashlib.sha256(data).hexdigest()})
            dataset = finish_dataset({'id': f'upload-{folder_id}', 'label': folder_name, 'folder_name': folder_name,
                                      'builtin': False, 'files': file_list, 'snapshots': snapshots})
            with self.lock:
                registry = self.imported + [dataset]
                temporary = self.registry_path.with_suffix('.tmp')
                temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2)+'\n')
                temporary.replace(self.registry_path)
                self.imported = registry
        except Exception:
            shutil.rmtree(root)
            raise
        return public_dataset(dataset)

    def create(self, dataset_id, algorithm, snapshot_id=None):
        from .core import ALGORITHMS
        if algorithm not in ALGORITHMS:
            raise ValueError('请选择支持的潮流算法')
        dataset, snapshot, source = self.resolve(dataset_id, snapshot_id)
        if not source.is_file():
            raise ValueError('数据文件不存在')
        task_id = uuid.uuid4().hex
        task = {'id': task_id, 'dataset_id': dataset_id, 'dataset_name': dataset['label'],
                'snapshot_id': snapshot['id'], 'snapshot_time': snapshot['time'], 'snapshot_label': snapshot['label'],
                'dataset_file_count': len(dataset['files']), 'reference_file_count': sum(f['usage']=='reference_only' for f in dataset['files']),
                'algorithm': algorithm, 'status': 'queued',
                'events': [], 'event_frames': [], 'condition': threading.Condition(), 'result': None, 'started': time.perf_counter()}
        with self.lock:
            self.jobs[task_id] = task
        self.emit(task, 'log', {'message': '任务已创建，等待求解器', 'status': 'queued'})
        self.emit(task, 'log', {'message': f"数据集：{dataset['label']}；计算时刻／断面：{snapshot['label']}；输入 DAT：{snapshot['filename']}"})
        if task['reference_file_count']:
            self.emit(task, 'log', {'message': f"同一数据集的 {task['reference_file_count']} 份 Excel 已按原字节保存，当前未接入求解模型；本次仅使用所选 DAT"})
        self.executor.submit(self._work, task, source, f"{dataset['label']} · {snapshot['label']}", False)
        return task

    def emit(self, task, kind, data):
        with task['condition']:
            if kind in ('completed', 'failed'):
                task['status'] = kind
            event = {'id': len(task['events'])+1, 'type': kind,
                     'timestamp': datetime.now(timezone.utc).isoformat(),
                     'elapsed_ms': (time.perf_counter()-task['started'])*1000, **data}
            # Encode once on publication. SSE subscribers only write bytes,
            # instead of re-encoding the full network/state while the numeric
            # worker is running (and duplicating that CPU cost per subscriber).
            data = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            task['event_frames'].append(f"id: {event['id']}\ndata: {data}\n\n".encode())
            task['events'].append(event)
            task['condition'].notify_all()

    def _work(self, task, source, label, builtin):
        work_started = time.perf_counter()
        task['status'] = 'running'
        try:
            result = run_case(source, task['algorithm'], lambda k,d: self.emit(task,k,d), label, builtin)
            calculation_done = time.perf_counter()
            result['run'].update(task_id=task['id'], **{k:task[k] for k in (
                'dataset_id', 'dataset_name', 'snapshot_id', 'snapshot_time', 'snapshot_label',
                'dataset_file_count', 'reference_file_count')}, excel_integrated=False)
            task_dir = self.runtime / 'runs' / task['id']
            task_dir.mkdir(parents=True)
            (task_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':'))+'\n')
            ready = time.perf_counter()
            task['timing'] = {'queue_ms': (work_started-task['started'])*1000,
                              'execution_ms': (calculation_done-work_started)*1000,
                              'persistence_ms': (ready-calculation_done)*1000,
                              'ready_ms': (ready-task['started'])*1000}
            (task_dir / 'timing.json').write_text(json.dumps(task['timing'])+'\n')
            task['result'] = result
            self.emit(task, 'completed', {'run': self.result(task)['run'], 'message': '本次真实计算结果已就绪'})
        except Exception as error:
            self.emit(task, 'failed', {'message': f'计算失败：{error}', 'error': str(error)})
        finally:
            task_dir = self.runtime / 'runs' / task['id']
            task_dir.mkdir(parents=True, exist_ok=True)
            with (task_dir / 'events.jsonl').open('wb') as output:
                for frame in task['event_frames']:
                    # Drop the SSE id/data prefix and one of its two trailing
                    # newlines, retaining the exact JSON published to clients.
                    output.write(frame[frame.index(b'\n') + len(b'\ndata: '):-1])
            self.prune_completed()

    def prune_completed(self):
        """Bound 100k payloads in memory; retain every result and event on disk.

        Replace the registry entry rather than clearing the old dict: an SSE
        subscriber already holding it can finish without missing any events.
        """
        with self.lock:
            resident = [t for t in self.jobs.values() if t['status'] in ('completed','failed') and not t.get('archived')]
            for old in resident[:-self.retained_jobs]:
                self.jobs[old['id']] = {**old, 'result': None, 'events': [], 'event_frames': [], 'archived': True}

    def result(self, task):
        if task.get('archived'):
            result = json.loads((self.runtime/'runs'/task['id']/'result.json').read_text(encoding='utf-8'))
        else:
            result = task['result']
        if result is not None and task.get('timing'):
            return {**result, 'run': {**result['run'], 'service_timing': task['timing']}}
        return result

    def events(self, task):
        if task.get('archived'):
            with (self.runtime/'runs'/task['id']/'events.jsonl').open(encoding='utf-8') as source:
                return [json.loads(line) for line in source]
        return task['events']

    def get(self, task_id):
        with self.lock:
            if task_id not in self.jobs:
                raise KeyError('计算任务不存在或服务已重启')
            return self.jobs[task_id]
