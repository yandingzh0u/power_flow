"""Weighted, connected distribution domains; electrical input is never changed.

METIS balances estimated Jacobian work and penalises cuts of strongly coupled
branches. A structural cost estimate selects the granularity, not a solved state.
Small disconnected feeders stay separate domains and share a CPU work queue.
"""
from __future__ import annotations
from collections import OrderedDict
from copy import deepcopy
import math
import os
import threading
import time

import numpy as np
import pymetis
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from .dat import newton
from .microgrid import bind_microgrid_partitions


_partition_cache = OrderedDict()
_partition_cache_lock = threading.Lock()


def clear_partition_cache():
    with _partition_cache_lock:
        _partition_cache.clear()


def available_cpus():
    return max(1, len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count() or 1)


def build_partitions(model, target_size=None, strategy=None):
    started = time.perf_counter()
    strategy = strategy or os.environ.get('GMSS_PARTITION_STRATEGY', 'weighted')
    if strategy not in ('weighted', 'legacy'):
        raise ValueError('Partition strategy must be weighted or legacy')
    if target_size is None and os.environ.get('GMSS_PARTITION_TARGET'):
        target_size = int(os.environ['GMSS_PARTITION_TARGET'])
    if target_size is not None and target_size < 2:
        raise ValueError('Partition target must be at least 2')
    reduced = model['reduced']
    distribution = np.flatnonzero([bus['key'][1] < 35 and not bus.get('microgrid_id') for bus in reduced])
    microgrid_groups = [[i for i, bus in enumerate(reduced) if bus.get('microgrid_id') == mg['id']]
                        for mg in model.get('microgrids', [])]
    group = model['group_index']
    original_by_group = {}
    for index, node in enumerate(group):
        original_by_group.setdefault(int(node), []).append(index + 1)
    lookup = {b['key']: i for i, b in enumerate(model['buses'])}
    starts = np.fromiter((group[lookup[e['from']]] for e in model['branches']), dtype=int)
    ends = np.fromiter((group[lookup[e['to']]] for e in model['branches']), dtype=int)
    mask = starts != ends
    a, b = starts[mask], ends[mask]
    graph = coo_matrix((np.ones(2 * len(a)), (np.r_[a, b], np.r_[b, a])),
                       shape=(len(reduced), len(reduced))).tocsr()
    graph.sort_indices()
    # Only partitioning penalties change. Ybus / branch impedances stay intact.
    coupling = np.array([1 / max(abs(complex(e['r'], e['x'])) * abs(e['tap']), 1e-12)
                         for e in model['branches']])[mask]
    weights = coo_matrix((np.r_[coupling, coupling], (np.r_[a, b], np.r_[b, a])),
                         shape=graph.shape).tocsr()
    positive = weights.data[weights.data > 0]
    scale = float(np.median(positive)) if len(positive) else 1.
    weights.data = np.maximum(1, np.rint(100 * np.sqrt(np.clip(weights.data / scale, .01, 100)))).astype(np.int64)
    degree = np.diff(graph.indptr)
    dof = np.array([0 if bus['subtype'] == 'S' else 2 if bus['subtype'] == ' ' else 1 for bus in reduced])
    work = np.maximum(1, dof * (degree + 1) ** 2).astype(np.int64)
    # Exact model structure/weights, not a filename or a previous solved state.
    cache_key = (strategy, target_size, available_cpus(), group.tobytes(), distribution.tobytes(),
                 tuple(tuple(g) for g in microgrid_groups),
                 graph.indptr.tobytes(), graph.indices.tobytes(), weights.data.tobytes(),
                 dof.tobytes(), work.tobytes(), coupling.tobytes())
    with _partition_cache_lock:
        cached = _partition_cache.get(cache_key)
        if cached is not None:
            _partition_cache.move_to_end(cache_key)
            parts, info = deepcopy(cached)
            info.update(elapsed_ms=(time.perf_counter() - started) * 1000, structure_cache_hit=True)
            model['partitioning'] = info
            return bind_microgrid_partitions(model, parts)
    low = graph[distribution][:, distribution]
    component_count, labels = connected_components(low, directed=False)
    order = np.argsort(labels, kind='stable')
    offsets = np.searchsorted(labels[order], np.arange(component_count + 1))
    components = [distribution[order[offsets[c]:offsets[c + 1]]] for c in range(component_count)]
    candidates = [int(target_size)] if target_size else ([150] if strategy == 'legacy' else [64, 128, 256])
    cache, candidates_report, best = {}, [], None
    workers = available_cpus()
    for target in candidates:
        groups = []
        for nodes in components:
            count = max(1, math.ceil(len(nodes) / target))
            if count == 1:
                groups.append(nodes.tolist())
                continue
            local_graph = graph[nodes][:, nodes].tocsr()
            local_weights = weights[nodes][:, nodes].tocsr()
            key = (count, local_graph.indptr.tobytes(), local_graph.indices.tobytes(),
                   local_weights.data.tobytes(), work[nodes].tobytes(), strategy)
            if key not in cache:
                options = pymetis.Options(seed=42, contig=1)
                result = pymetis.part_graph(count, adjacency=pymetis.CSRAdjacency(local_graph.indptr, local_graph.indices),
                    vweights=work[nodes] if strategy == 'weighted' else None,
                    eweights=local_weights.data if strategy == 'weighted' else None, options=options)
                local_groups = []
                for p in range(count):
                    members = np.flatnonzero(np.asarray(result.vertex_part) == p)
                    if not len(members):
                        continue
                    nc, lc = connected_components(local_graph[members][:, members], directed=False)
                    local_groups.extend(members[lc == c] for c in range(nc))
                cache[key] = local_groups
            groups.extend(nodes[members].tolist() for members in cache[key])
        # A microgrid is one protected physical subsystem, never a METIS shard
        # and never absorbed into its parent distribution partition.
        groups.extend(microgrid_groups)
        labels_all = np.full(len(reduced), -1, dtype=int)
        for i, members in enumerate(groups):
            labels_all[members] = i
        cut = labels_all[a] != labels_all[b]
        interface = labels_all < 0
        interface[a[cut]] = True
        interface[b[cut]] = True
        interface_unknowns = int(dof[interface].sum())
        # Proxy units, not milliseconds: local fill grows superlinearly, while
        # the coordinator is serial. Actual timings remain the acceptance test.
        costs = [float(work[g].sum()) * max(1, len(g)) ** .35 for g in groups]
        loads = np.zeros(min(workers, max(1, len(groups))))
        for cost in sorted(costs, reverse=True):
            loads[np.argmin(loads)] += cost
        score = float(loads.max(initial=0) + 4 * interface_unknowns ** 1.35)
        row = {'target_size': target, 'partition_count': len(groups), 'interface_unknowns': interface_unknowns,
               'estimated_schedule_imbalance': float(loads.max(initial=0) / max(loads.mean(), 1)),
               'weighted_cut_cost': float(coupling[cut].sum()), 'estimated_cost': score}
        candidates_report.append(row)
        if best is None or score < best[0]:
            best = score, groups, labels_all, interface, row
    _, groups, labels_all, interface, selected = best
    parts = []
    for internal in groups:
        internal = sorted(internal)
        connected = set(internal)
        boundary = sorted({b for a in internal for b in graph.indices[graph.indptr[a]:graph.indptr[a + 1]] if b not in connected})
        original_ids = sorted(i for node in internal for i in original_by_group[node])
        boundary_ids = sorted(i for node in boundary for i in original_by_group[node])
        own_boundary = [i for node in internal if interface[node] for i in original_by_group[node]]
        neighbors = sorted({int(labels_all[b]) + 1 for b in boundary if labels_all[b] >= 0})
        parts.append({'id': len(parts) + 1, 'internal': internal, 'boundary': boundary,
                      'subsystem_type': 'distribution',
                      'internal_buses': original_ids, 'boundary_buses': boundary_ids,
                      'boundary_internal_buses': own_boundary, 'neighbor_partition_ids': neighbors,
                      'has_main_interface': any(labels_all[b] < 0 for b in boundary),
                      'estimated_work': int(work[internal].sum()),
                      'display_name': f"P{len(parts) + 1:02d}", 'converged': None,
                      'graph_algorithm': 'weighted METIS + connectivity check' if strategy == 'weighted' else 'unweighted METIS + connectivity check'})
    bind_microgrid_partitions(model, parts)
    sizes = [len(p['internal_buses']) for p in parts]
    model['partitioning'] = {'strategy': strategy, 'algorithm': parts[0]['graph_algorithm'] if parts else 'no distribution domains',
        'distribution_components': int(component_count), 'partition_count': len(parts),
        'distribution_partition_count': len(parts) - len(microgrid_groups),
        'microgrid_partition_count': len(microgrid_groups),
        'target_size': selected['target_size'], 'interface_unknowns': selected['interface_unknowns'],
        'available_cpus': workers, 'size_min': min(sizes, default=0), 'size_max': max(sizes, default=0),
        'size_median': float(np.median(sizes)) if sizes else 0, 'small_domains': sum(s <= 10 for s in sizes),
        'candidates': candidates_report, 'elapsed_ms': (time.perf_counter() - started) * 1000,
        'selection': 'minimum structural cost estimate; not a claim of measured optimum',
        'edge_weight': 'sqrt(series admittance / network median), clipped, parallel branches summed',
        'vertex_weight': 'AC unknown count * (network degree + 1)^2',
        'structure_cache_hit': False,
        'policy': 'connected electrical domains; no inferred station/feeder administration; original electrical parameters unchanged'}
    # Mutable convergence/status fields are never shared with another run.
    # Bound retained network count; mutable state stays private to each run.
    with _partition_cache_lock:
        _partition_cache[cache_key] = deepcopy((parts, model['partitioning']))
        while len(_partition_cache) > 4:
            _partition_cache.popitem(last=False)
    return parts


def verify_partitions(model, parts, voltage, emit):
    ybus = model['y_reduced']
    group = model['group_index']
    reduced_v = np.zeros(len(model['reduced']), dtype=complex)
    for index, node in enumerate(group):
        reduced_v[node] = voltage[index]
    for part in parts:
        started = time.perf_counter()
        inside, boundary = part['internal'], part['boundary']
        selected = inside + boundary
        buses = [dict(model['reduced'][i]) for i in selected]
        for offset, node in enumerate(selected):
            if node in boundary:
                buses[offset].update(subtype='S', vm=float(abs(reduced_v[node])),
                                     va=float(np.rad2deg(np.angle(reduced_v[node]))))
        emit('partition', {'partition_id': part['id'], 'status': 'running', 'message': f"{part['display_name']}：校核 {len(part['internal_buses'])} 个节点，固定 {len(boundary)} 个边界电压"})
        try:
            if not boundary and not any(b['subtype'] == 'S' for b in buses):
                raise ValueError('Partition has no fixed boundary or slack reference')
            local, history = newton(model['base'], buses, ybus[selected][:, selected].tocsr(),
                                    damped=True, max_iterations=30, check_islands=False)
            expected = reduced_v[inside]
            vm_error = float(np.max(abs(abs(local[:len(inside)]) - abs(expected)), initial=0))
            va_delta = np.rad2deg(np.angle(local[:len(inside)] / expected))
            va_error = float(np.max(abs(va_delta), initial=0))
            part.update(converged=vm_error < 1e-5 and va_error < 1e-3,
                        max_vm_delta_vs_full_pu=vm_error, max_va_delta_vs_full_deg=va_error,
                        iterations=len(history)-1, error=None)
        except (ValueError, RuntimeError, ArithmeticError) as error:
            part.update(converged=False, error=str(error), max_vm_delta_vs_full_pu=None,
                        max_va_delta_vs_full_deg=None, iterations=None)
        part['elapsed_ms'] = (time.perf_counter()-started)*1000
        emit('partition', {'partition_id': part['id'], 'status': 'completed' if part['converged'] else 'failed',
                          'message': f"{part['display_name']}：{'通过' if part['converged'] else '未通过'}，{part['elapsed_ms']:.1f} ms",
                          'result': {k: v for k, v in part.items() if k not in ('internal','boundary')}})
    return parts
