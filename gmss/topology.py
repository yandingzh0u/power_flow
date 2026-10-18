"""Browser payloads derived from original DAT identities and computed results."""
from __future__ import annotations
from pathlib import Path
import json
import math

import networkx as nx

ROOT = Path(__file__).resolve().parents[1]
PCC_NAMES = ('QRTY2', 'QRTY1', 'OBFY1', 'FUNY2', 'LQTY1', 'LAJY1')


def topology(model, parts, label, result=None, use_saved_layout=True, prepared_layout=None):
    lookup = {b['key']: i + 1 for i, b in enumerate(model['buses'])}
    membership = {node: p['id'] for p in parts for node in p['internal_buses']}
    boundaries = {node for p in parts for node in p['boundary_buses']}
    rows = result.get('bus_results', []) if result else []
    buses = []
    for i, b in enumerate(model['buses']):
        r = rows[i] if rows else {}
        buses.append({'id': i+1, 'name': b['key'][0], 'kv': b['key'][1],
                      'type': b['subtype'].strip() or 'PQ', 'layer': 'main' if b['key'][1] >= 35 else 'distribution',
                      'partition_id': membership.get(i+1), 'is_boundary': i+1 in boundaries,
                      'vm': r.get('vm_pu'), 'va': r.get('va_degree'),
                      'p': r.get('net_p_injection_mw'), 'q': r.get('net_q_injection_mvar'),
                      'load_mw': b['load_p'], 'load_q_mvar': b['load_q'], 'sgen_mw': b['gen_p'],
                      'kind': 'slack' if b['subtype'] == 'S' else 'source' if b['gen_p'] else 'load' if b['load_p'] else 'bus'})
    busmap = {b['id']: b for b in buses}
    flows = {r['source_line']: r for r in result.get('branch_results', [])} if result else {}
    lines, trafos = [], []
    for edge in model['branches']:
        a, b = lookup[edge['from']], lookup[edge['to']]
        r = flows.get(edge['source_line'], {})
        row = {'id': edge['source_line'], 'source_line': edge['source_line'],
               'name': f"{'LINE' if edge['kind']=='L' else 'TRAFO'}-{edge['source_line']}",
               'from': a, 'to': b, 'loading': r.get('current_loading_percent'),
               'p': r.get('p_from_mw'), 'q': r.get('q_from_mvar'),
               'p_to': r.get('p_to_mw'), 'q_to': r.get('q_to_mvar'),
               'rate_mva': edge['rate_mva'], 'r_pu': edge['r'], 'x_pu': edge['x'],
               'tap': edge['tap'], 'ideal': edge['regularized'],
               'from_layer': busmap[a]['layer'], 'to_layer': busmap[b]['layer'],
               'is_cut_edge': membership.get(a) is not None and membership.get(b) is not None and membership.get(a) != membership.get(b),
               'in_service': True}
        if edge['kind'] == 'L':
            row['max_i_ka'] = edge['rate_mva'] / (math.sqrt(3) * edge['from'][1]) if edge['rate_mva'] else None
            lines.append(row)
        else:
            row.update(hv=a, lv=b, hv_kv=edge['from'][1], lv_kv=edge['to'][1], sn_mva=edge['rate_mva'],
                       p_hv=row['p'], q_hv=row['q'], hv_layer=row['from_layer'], lv_layer=row['to_layer'],
                       is_interface=row['from_layer'] != row['to_layer'], vm_hv=busmap[a]['vm'], vm_lv=busmap[b]['vm'])
            trafos.append(row)
    microgrids = []
    for name in PCC_NAMES:
        candidate = next((b for b in buses if b['name'] == name), None)
        if candidate:
            candidate['microgrid_name'] = f"PCC-{len(microgrids)+1}"
            microgrids.append({'id': len(microgrids)+1, 'name': candidate['microgrid_name'],
                               'pcc_bus': candidate['id'], 'pcc_name': name, 'partition_id': candidate['partition_id'],
                               'model_type': 'illustrative_interface', 'note': '既有等值 PCC 展示，不向原始断面叠加额外注入'})
    saved = ROOT / 'data/demo_area/layout.json'
    if prepared_layout is not None:
        layout = prepared_layout
    elif use_saved_layout and saved.exists():
        layout = json.loads(saved.read_text())
    else:
        layout = {}
    missing = {str(b['id']) for b in buses} - set(layout)
    if missing:
        graph = nx.Graph()
        graph.add_nodes_from(b['id'] for b in buses)
        graph.add_edges_from((e['from'], e['to']) for e in lines + trafos)
        # Electrical drawing only; never represented as geographic coordinates.
        # Linear-time BFS engineering layout avoids a force optimizer delaying
        # actual power-flow startup for a newly imported large folder.
        layout = {}
        for component_index, component in enumerate(nx.connected_components(graph)):
            root = next((b['id'] for b in buses if b['type'] == 'S' and b['id'] in component), min(component))
            levels = {}
            for node, depth in nx.single_source_shortest_path_length(graph.subgraph(component), root).items():
                levels.setdefault(depth, []).append(node)
            max_depth = max(levels, default=1) or 1
            for depth, members in levels.items():
                for row, node in enumerate(members):
                    layout[str(node)] = [depth/max_depth*2-1, component_index*2.5 + (row+.5)/len(members)*2-1]
    layout = {str(b['id']): layout[str(b['id'])] for b in buses}
    serial_parts = [{k: v for k, v in p.items() if k not in ('internal', 'boundary')} for p in parts]
    finite_vm = [p['max_vm_delta_vs_full_pu'] for p in parts if p.get('max_vm_delta_vs_full_pu') is not None]
    finite_va = [p['max_va_delta_vs_full_deg'] for p in parts if p.get('max_va_delta_vs_full_deg') is not None]
    scenario = {'label': label, 'bus_count': len(buses), 'line_count': len(lines), 'trafo_count': len(trafos),
                'load_count': sum(bool(b['load_mw']) for b in buses),
                'load_mw': sum(b['load_mw'] for b in buses), 'sgen_mw': sum(b['sgen_mw'] for b in buses),
                'buses': buses, 'lines': lines, 'trafos': trafos, 'microgrids': microgrids, 'partitions': serial_parts,
                'layers': {'main_bus_count': sum(b['layer']=='main' for b in buses),
                           'distribution_bus_count': sum(b['layer']=='distribution' for b in buses),
                           'interface_trafo_count': sum(t['is_interface'] for t in trafos)},
                'summary': {'parts': len(parts), 'converged': sum(p['converged'] is True for p in parts),
                            'boundary_buses': len(boundaries), 'cut_edges': sum(e['is_cut_edge'] for e in lines),
                            'boundary_trafos': sum(t['is_interface'] for t in trafos),
                            'max_vm_error': max(finite_vm, default=None), 'max_va_error': max(finite_va, default=None)},
                'result_status': 'computed' if result else 'waiting', 'coordinate_source': 'electrical_graph_layout',
                'microgrid_note': '等值 PCC 作为展示接口；本次原始 DAT 潮流不增加微网注入。'}
    return {'layout': layout, 'scenario': scenario}
