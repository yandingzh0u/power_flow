"""Shijiazhuang original-data power-flow orchestration.

All solvers consume the same chosen impedance interpretation. Original inputs
stay immutable and each result records the user-selected T-card base assumption.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import time

import numpy as np

from .partition import build_partitions, verify_partitions
from .raw_solver import solve, DEFAULT_TRANSFORMER_BASE_MVA
from .topology import topology
from .expansion import read_expansion
from .telemetry import IterationTrace
from .microgrid import microgrid_states

ROOT = Path(__file__).resolve().parents[1]
ALGORITHMS = {'partitioned-nr': '加权分区 + 并行 Newton–Schur', 'nr': '普通牛顿法',
              'damped-nr': '阻尼牛顿法', 'bpa-style': '解耦启动 + 牛顿法'}


def run_case(source, algorithm='partitioned-nr', emit=None, label=None, builtin=False, partitioned=False,
             transformer_base_mva=DEFAULT_TRANSFORMER_BASE_MVA):
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
    expansion = read_expansion(source)
    if expansion:
        emit('log', {'message': f"研究扩展算例：{expansion['copies']} 个复制区域，{expansion['bus_count']} 母线；"
                     f"{len(expansion['interties'])} 条高压联络线，连接方式 {expansion['connection']}；原始数据文件未修改"})
        emit('log', {'message': f"全网平衡节点 {expansion['global_slack']}；其余 {len(expansion['slack_changes'])} 个原 BS 改为 BE，保留原有功给定与电压目标；新增线路参数取自源 L 卡第 {expansion['intertie_template_source_line']} 行"})
    emit('log', {'message': f'读取原始 DAT：{label}；算法：{ALGORITHMS[algorithm]}；输入 SHA-256 {before}'})

    def observe(event):
        kind = event['kind']
        if kind == 'model':
            event['expansion'] = expansion
            context['model'] = event
            policy = event['impedance_policy']
            emit('log', {'message': f"阻抗基准设置：{policy['label']}；T 卡 R/X 换算倍率 {policy['transformer_scale']:g}；原始文件字节不变",
                         'impedance_policy': policy})
            context['parts'] = build_partitions(event)
            event['parts'] = context['parts']
            context['trace'] = IterationTrace(event, context['parts'])
            emit('log', {'message': f"读取 DAT：{len(event['buses'])} 母线、{len(event['branches'])} 支路；零阻抗理想连接精确合并"})
            if event['microgrids']:
                emit('log', {'message': f"DAT 包含 {len(event['microgrids'])} 个独立微网子系统，内部母线、线路、变压器、光伏、储能及负荷直接从文件读取"})
                emit('log', {'message': '配网向微网传递 PCC 电压/相角，微网反馈随电压变化的 P/Q 及 Schur 线性响应；各微网独立消元，与配网共同协调收敛，无额外嵌套定点迭代'})
            else:
                emit('log', {'message': '当前 DAT 未定义微网子系统'})
            context['initial_payload'] = topology(event, context['parts'], label, use_saved_layout=known_layout)
            emit('network', context['initial_payload'])
            info = event['partitioning']
            emit('log', {'message': f"加权分区（{'复用已核对结构' if info['structure_cache_hit'] else '本次构造'}，{info['elapsed_ms']:.2f} ms）：{info['distribution_components']} 个配网连通块 → {info['distribution_partition_count']} 个配网域 + {info['microgrid_partition_count']} 个独立微网域；"
                         f"粒度候选 {[r['target_size'] for r in info['candidates']]}，结构成本选择 {info['target_size']}；"
                         f"边界未知量 {info['interface_unknowns']}，节点数 {info['size_min']}～{info['size_max']}（中位 {info['size_median']:g}）；"
                         f"可用逻辑 CPU {info['available_cpus']}；权重仅用于分区，不修改电气参数", 'partitioning': info})
            if algorithm == 'partitioned-nr':
                emit('log', {'message': f"进入分区并行求解：{len(context['parts'])} 个配网/微网计算域；每轮局部消元 → 边界方程协调 → 电压修正回传；从平坦初值开始，不先做全网牛顿求解"})
        elif kind == 'setup':
            emit('log', {'message': f"消元结构{'复用' if event['structure_cache_hit'] else '首次构造'}："
                         f"本次准备 {event['setup_ms']:.2f} ms；结构索引 {event['structure_bytes'] / 1024:.1f} KiB；"
                         f"配置求解线程 {event['workers']}。仅复用结构索引，数值矩阵、潮流解及初值均重新计算",
                         **{k: v for k, v in event.items() if k not in ('kind', 'voltage', 'base')}})
        elif kind == 'phase':
            messages = {'local-elimination': '各分区并行消元局部雅可比块',
                        'boundary-coordination': '汇总各区 Schur 等值方程与不平衡量，求解边界电压修正',
                        'back-substitution': '边界修正回传，各区并行回代内部电压'}
            emit('phase', {k: v for k, v in event.items() if k not in ('kind', 'voltage', 'base')} |
                 {'message': f"第 {event['iteration']} 轮 · {messages[event['stage']]}"})
        else:
            voltage = event['voltage']
            context['voltage'] = voltage.copy()
            parallel = event.get('workers')
            coordination = event.get('coordination')
            message = (f"边界协调第 {event['iteration']} 轮，{event['partition_count']} 分区 / {parallel} 个 CPU 线程，"
                       f"{event['interface_unknowns']} 个边界未知量；全网最大不平衡 {event['max_mismatch_pu'] * event['base']:.6e} MVA") if parallel else f"{event['stage']} 第 {event['iteration']} 步，最大不平衡 {event['max_mismatch_pu'] * event['base']:.6e} MVA"
            if coordination:
                message += (f"；并行消元 {coordination['local_ms']:.2f} ms，协调 {coordination['coordinator_ms']:.2f} ms，"
                            f"回代 {coordination['back_substitution_ms']:.2f} ms")
                message += (f"；边界最大修正 ΔV={coordination['boundary_max_delta_voltage_pu']:.3e} pu，"
                            f"Δθ={coordination['boundary_max_delta_angle_deg']:.3e}°；实际工作线程 {coordination['active_worker_threads']}")
            measurements = context['trace'].measure(voltage, event)
            emit('iteration', {'iteration': event['iteration'], 'stage': event['stage'],
                               'residual_mva': event['max_mismatch_pu'] * event['base'],
                               'workers': parallel, 'partition_count': event.get('partition_count'),
                               'interface_unknowns': event.get('interface_unknowns'), 'coordination': coordination,
                               'available_cpus': event.get('available_cpus'),
                               'message': message, **measurements})

    result = solve(source, algorithm=algorithm, observer=observe, include_results=True,
                   transformer_base_mva=transformer_base_mva)
    solve_ms = result['numerical_solve_ms']
    emit('log', {'message': f"全网潮流收敛：{result['iterations']} 次牛顿迭代；全部节点方程残差 {result['original_bus_equation_residual_mva']:.6e} MVA"})
    if algorithm == 'partitioned-nr':
        model = context['model']
        _, anchors = np.unique(model['group_index'], return_index=True)
        v = context['voltage'][anchors]
        mismatch = v * np.conj(model['y_reduced'] @ v) * model['base'] - np.array([complex(b['p'], b['q']) for b in model['reduced']])
        constraints = np.array([max(abs(m.real) if b['subtype'] != 'S' else 0,
                                    abs(m.imag) if b['subtype'] == ' ' else 0)
                                for m, b in zip(mismatch, model['reduced'])])
        for p in context['parts']:
            residual = float(np.max(constraints[p['internal']], initial=0))
            p.update(converged=residual < 1e-4, max_equation_residual_mva=residual,
                     iterations=result['iterations'], solve_mode='coupled Schur Newton', elapsed_ms=None)
        emit('log', {'message': f"{len(context['parts'])} 个配网/微网计算域已共同收敛；逐区检查 AC 功率方程，跨区线路保留；没有额外全网参考求解"})
    elif partitioned:
        emit('log', {'message': f"开始 {len(context['parts'])} 个配网分区的固定边界牛顿校核；边界电压来自本次全网求解"})
        verify_partitions(context['model'], context['parts'], context['voltage'], emit)
    payload = topology(context['model'], context['parts'], label, result, use_saved_layout=known_layout, prepared_layout=context['initial_payload']['layout'])
    mg_states = microgrid_states(context['model'], context['voltage'])
    for mg, row in zip(payload['scenario']['microgrids'], mg_states):
        mg.update(row, converged=row['max_equation_residual_mva'] < 1e-4)
    if mg_states:
        emit('log', {'message': f"微网方程残差最大 {max(m['max_equation_residual_mva'] for m in mg_states):.3e} MVA；"
                     f"PCC 总购电 {sum(m['import_p_mw'] for m in mg_states):.6f} MW，"
                     f"总无功交换 {sum(m['import_q_mvar'] for m in mg_states):.6f} Mvar（正值为配网供给微网）"})
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before, '原始输入意外发生变化'
    payload['run'] = {'algorithm': algorithm, 'algorithm_label': ALGORITHMS[algorithm], 'converged': True,
                      'iterations': result['iterations'], 'residual_mva': result['original_bus_equation_residual_mva'],
                      'solver_ms': solve_ms, 'total_ms': (time.perf_counter()-started)*1000,
                      'started_at': started_at, 'completed_at': datetime.now(timezone.utc).isoformat(),
                      'source_name': source.name, 'source_sha256': before, 'source_unchanged': True,
                      'partition_failed': sum(p['converged'] is False for p in context['parts']),
                      'partition_check': 'coupled AC Schur residual' if algorithm == 'partitioned-nr' else ('fixed full-network boundary voltage; local AC Newton' if partitioned else 'not requested'),
                      'solver_details': result.get('solver_details'),
                      'partitioning': context['model'].get('partitioning'),
                      'workers': result.get('solver_details', {}).get('workers', 1),
                      'boundary_iterations': result['iterations'] if algorithm == 'partitioned-nr' else 0,
                      'microgrid_count': len(mg_states),
                      'source_bus_count': result['source_bus_count'], 'source_branch_count': result['source_branch_count'],
                      'microgrid_template_sha256': [m['template_sha256'] for m in context['model']['microgrids']],
                      'microgrid_boundary_iterations': result['iterations'] if algorithm == 'partitioned-nr' and mg_states else 0,
                      'timing_scope': 'solver includes domain plan and iteration callbacks; total includes DAT parsing, partitioning and display payloads',
                      'impedance_policy': result['impedance_policy'],
                      'model_policy': result['impedance_policy']['label'] + '；网架、负荷及微网设备直接读取 DAT；不回写 Excel 负荷曲线'}
    if expansion:
        payload['run']['model_policy'] += '；' + expansion['policy']
        payload['run']['expansion'] = expansion
    overloads = sum((e['loading'] or 0)>100 for e in payload['scenario']['lines']+payload['scenario']['trafos'])
    emit('log', {'message': f"最高电流负载率：线路 {result['L_loading']['max_current_loading_percent'] or 0:.2f}%；"
                 f"T 支路 {result['T_loading']['max_current_loading_percent'] or 0:.2f}%；"
                 f"T 支路最高视在功率容量比 {result['T_loading']['max_apparent_power_loading_percent'] or 0:.2f}%（独立口径）"})
    emit('log', {'message': f"本次建模与计算结束：后端 {payload['run']['total_ms']:.1f} ms（含数值求解 {solve_ms:.1f} ms，不含排队、结果保存及网络），{overloads} 个元件过载；越限按计算结果保留"})
    return payload
