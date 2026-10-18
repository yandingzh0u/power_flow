"""Connected distribution partitions and actual fixed-boundary AC verification.

The full-network solution supplies Dirichlet boundary voltages. Local checks
verify that each partition reproduces it; they are not an independent global
partition-coupling convergence claim. Original ideal connections stay contracted.
"""
from __future__ import annotations
import math
import time

import networkx as nx
import numpy as np
import pymetis

from .dat import newton


def build_partitions(model, target_size=150):
    graph = nx.Graph()
    reduced = model['reduced']
    distribution = {i for i, bus in enumerate(reduced) if bus['key'][1] < 35}
    graph.add_nodes_from(range(len(reduced)))
    group = model['group_index']
    lookup = {b['key']: i for i, b in enumerate(model['buses'])}
    for edge in model['branches']:
        a, b = int(group[lookup[edge['from']]]), int(group[lookup[edge['to']]])
        if a != b:
            graph.add_edge(a, b)
    parts = []
    for component in sorted(nx.connected_components(graph.subgraph(distribution)), key=lambda v: min(v)):
        nodes = sorted(component)
        count = min(len(nodes), max(1, math.ceil(len(nodes) / target_size)))
        if count == 1:
            groups = [nodes]
        else:
            local = {node: i for i, node in enumerate(nodes)}
            adjacency = [[local[b] for b in sorted(graph[a]) if b in local] for a in nodes]
            membership = pymetis.part_graph(count, adjacency=adjacency, options=pymetis.Options(seed=42)).vertex_part
            groups = [[n for n, label in zip(nodes, membership) if label == p] for p in range(count)]
        # METIS membership is split into connected components where necessary.
        for members in groups:
            for connected in nx.connected_components(graph.subgraph(members)):
                internal = sorted(connected)
                boundary = sorted({b for a in internal for b in graph[a] if b not in connected})
                original_ids = [i + 1 for i, r in enumerate(group) if r in connected]
                boundary_ids = [i + 1 for i, r in enumerate(group) if r in boundary]
                parts.append({'id': len(parts) + 1, 'internal': internal, 'boundary': boundary,
                              'internal_buses': original_ids, 'boundary_buses': boundary_ids,
                              'display_name': f"P{len(parts) + 1:02d}", 'converged': None,
                              'graph_algorithm': 'METIS + connectivity check'})
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
