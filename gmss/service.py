"""Dataset registry and isolated calculation jobs with replayable real events."""
from __future__ import annotations
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import threading
import time
import uuid

from .core import ROOT, run_case
from .dat import read_dat
from .raw_solver import validate_bpa_scope


class JobService:
    def __init__(self, runtime=None, raw_dir=None):
        self.runtime = Path(runtime or ROOT / 'runtime')
        self.raw_dir = Path(raw_dir or ROOT / 'raw_dataset/extracted')
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.jobs = {}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='powerflow')
        self.registry_path = self.runtime / 'datasets.json'
        self.imported = json.loads(self.registry_path.read_text()) if self.registry_path.exists() else []

    def datasets(self):
        return [{'id': hour, 'label': f'石家庄 {hour}:00', 'filename': f'{hour}点sim2合并anony.dat', 'builtin': True}
                for hour in ('13','20')] + [{k:v for k,v in d.items() if k != 'path'} for d in self.imported]

    def resolve(self, dataset_id):
        if dataset_id in ('13', '20'):
            return self.raw_dir / f'{dataset_id}点sim2合并anony.dat', f'石家庄 {dataset_id}:00', True
        for d in self.imported:
            if d['id'] == dataset_id:
                return self.runtime / d['path'], d['label'], False
        raise ValueError('未找到所选数据集，请重新导入')

    def import_folder(self, files):
        if not isinstance(files, list) or not 1 <= len(files) <= 500:
            raise ValueError('文件夹必须包含 1～500 个 DAT/XLS/XLSX 文件')
        decoded, seen, total = [], set(), 0
        for item in files:
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
            decoded.append((path, data))
        if not any(p.suffix.lower()=='.dat' for p,_ in decoded):
            raise ValueError('文件夹中没有 DAT 潮流文件')
        folder_id = uuid.uuid4().hex
        root = self.runtime / 'uploads' / folder_id
        root.mkdir(parents=True)
        entries = []
        for relative, data in decoded:
            target = root / str(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if target.suffix.lower() == '.dat':
                _, buses, branches, _ = read_dat(target)
                if not buses or not branches:
                    raise ValueError(f'{relative.name} 没有可计算的节点或支路')
                validate_bpa_scope(target, buses)
                entries.append({'id': f'upload-{uuid.uuid4().hex}', 'label': f'导入 · {relative.name}',
                                'filename': relative.name, 'builtin': False,
                                'path': str(target.relative_to(self.runtime)),
                                'sha256': hashlib.sha256(data).hexdigest()})
        with self.lock:
            self.imported.extend(entries)
            self.registry_path.write_text(json.dumps(self.imported, ensure_ascii=False, indent=2)+'\n')
        return [{k:v for k,v in e.items() if k!='path'} for e in entries]

    def create(self, dataset_id, algorithm):
        from .core import ALGORITHMS
        if algorithm not in ALGORITHMS:
            raise ValueError('请选择支持的潮流算法')
        source, label, builtin = self.resolve(dataset_id)
        if not source.is_file():
            raise ValueError('数据文件不存在')
        task_id = uuid.uuid4().hex
        task = {'id': task_id, 'dataset_id': dataset_id, 'algorithm': algorithm, 'status': 'queued',
                'events': [], 'condition': threading.Condition(), 'result': None, 'started': time.perf_counter()}
        with self.lock:
            self.jobs[task_id] = task
        self.emit(task, 'log', {'message': '任务已创建，等待求解器', 'status': 'queued'})
        self.executor.submit(self._work, task, source, label, builtin)
        return task

    def emit(self, task, kind, data):
        with task['condition']:
            if kind in ('completed', 'failed'):
                task['status'] = kind
            event = {'id': len(task['events'])+1, 'type': kind,
                     'timestamp': datetime.now(timezone.utc).isoformat(),
                     'elapsed_ms': (time.perf_counter()-task['started'])*1000, **data}
            task['events'].append(event)
            task['condition'].notify_all()

    def _work(self, task, source, label, builtin):
        task['status'] = 'running'
        try:
            result = run_case(source, task['algorithm'], lambda k,d: self.emit(task,k,d), label, builtin)
            result['run'].update(task_id=task['id'], dataset_id=task['dataset_id'])
            task_dir = self.runtime / 'runs' / task['id']
            task_dir.mkdir(parents=True)
            (task_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, allow_nan=False)+'\n')
            task['result'] = result
            self.emit(task, 'completed', {'run': result['run'], 'message': '本次真实计算结果已就绪'})
        except Exception as error:
            self.emit(task, 'failed', {'message': f'计算失败：{error}', 'error': str(error)})
        finally:
            task_dir = self.runtime / 'runs' / task['id']
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / 'events.jsonl').write_text(''.join(json.dumps(e, ensure_ascii=False, allow_nan=False)+'\n' for e in task['events']))

    def get(self, task_id):
        with self.lock:
            if task_id not in self.jobs:
                raise KeyError('计算任务不存在或服务已重启')
            return self.jobs[task_id]
