"""Shijiazhuang original-data power-flow orchestration.

All solvers consume the same DAT cards. No load-profile writeback, impedance
adjustment, artificial Q limits, synthetic grids or undisclosed overlays.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import time

import numpy as np

from .partition import build_partitions, verify_partitions
from .raw_solver import solve
from .topology import topology

ROOT = Path(__file__).resolve().parents[1]
ALGORITHMS = {'nr': '普通牛顿法', 'damped-nr': '阻尼牛顿法', 'bpa-style': '解耦启动 + 牛顿法'}


def run_case(source, algorithm='nr', emit=None, label=None, builtin=False, partitioned=True):
    if algorithm not in ALGORITHMS:
        raise ValueError(f'不支持的算法：{algorithm}')
    source = Path(source).resolve()
    emit = emit or (lambda kind, data: None)
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    known_layout = builtin or any(hashlib.sha256(p.read_bytes()).hexdigest() == before
                                  for p in (ROOT / 'raw_dataset/extracted').glob('*.dat'))
    context = {}
    label = label or source.name
    emit('log', {'message': f'读取原始 DAT：{label}；算法：{ALGORITHMS[algorithm]}；输入 SHA-256 {before}'})

    def observe(event):
        kind = event['kind']
        if kind == 'model':
            context['model'] = event
            context['parts'] = build_partitions(event)
            emit('log', {'message': f"模型已建立：{len(event['buses'])} 母线、{len(event['branches'])} 支路；零阻抗理想连接精确合并；全部原参数保留"})
            context['initial_payload'] = topology(event, context['parts'], label, use_saved_layout=known_layout)
            emit('network', context['initial_payload'])
        else:
            voltage = event['voltage']
            context['voltage'] = voltage.copy()
            emit('iteration', {'iteration': event['iteration'], 'stage': event['stage'],
                               'residual_mva': event['max_mismatch_pu'] * event['base'],
                               'voltages': [[float(abs(v)), float(np.rad2deg(np.angle(v)))] for v in voltage],
                               'message': f"{event['stage']} 第 {event['iteration']} 步，最大不平衡 {event['max_mismatch_pu'] * event['base']:.6e} MVA"})

    result = solve(source, algorithm=algorithm, observer=observe, include_results=True)
    solve_ms = result['numerical_solve_ms']
    emit('log', {'message': f"全网潮流收敛：{result['iterations']} 次牛顿迭代；原始节点方程残差 {result['original_bus_equation_residual_mva']:.6e} MVA"})
    if partitioned:
        emit('log', {'message': f"开始 {len(context['parts'])} 个配网分区的固定边界牛顿校核；边界电压来自本次全网求解"})
        verify_partitions(context['model'], context['parts'], context['voltage'], emit)
    payload = topology(context['model'], context['parts'], label, result, use_saved_layout=known_layout, prepared_layout=context['initial_payload']['layout'])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before, '原始输入意外发生变化'
    payload['run'] = {'algorithm': algorithm, 'algorithm_label': ALGORITHMS[algorithm], 'converged': True,
                      'iterations': result['iterations'], 'residual_mva': result['original_bus_equation_residual_mva'],
                      'solver_ms': solve_ms, 'total_ms': (time.perf_counter()-started)*1000,
                      'started_at': started_at, 'completed_at': datetime.now(timezone.utc).isoformat(),
                      'source_name': source.name, 'source_sha256': before, 'source_unchanged': True,
                      'partition_failed': sum(p['converged'] is False for p in context['parts']),
                      'partition_check': 'fixed full-network boundary voltage; local AC Newton',
                      'model_policy': 'original DAT parameters; no profile writeback or added microgrid injection'}
    overloads = sum((e['loading'] or 0)>100 for e in payload['scenario']['lines']+payload['scenario']['trafos'])
    emit('log', {'message': f"本次计算结束：总耗时 {payload['run']['total_ms']:.1f} ms，{overloads} 个元件过载；越限按计算结果保留"})
    return payload
