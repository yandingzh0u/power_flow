"""Reproducible research cases made from unchanged Shijiazhuang network cards."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import networkx as nx

from .dat import read_dat
from .raw_solver import validate_bpa_scope

ROOT = Path(__file__).resolve().parents[1]
MARKER = '.SJZ_EXPANSION '


def split_bus_name(meta, name):
    width = meta.get('prefix_width', 1)
    alias = name[width:]
    return name[:width], meta.get('bus_aliases', {}).get(alias, alias)


def read_expansion(path):
    with Path(path).open('rb') as stream:
        first = stream.readline()
    if not first.startswith(MARKER.encode('ascii')):
        return None
    meta = json.loads(first[len(MARKER):].decode('ascii'))
    if meta.get('kind') != 'synthetic_replication' or meta.get('version') != 1:
        raise ValueError('不支持的扩展算例元数据')
    return meta


def build_expansion(source, destination, copies=5, connection='ring'):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not isinstance(copies, int) or isinstance(copies, bool) or not 2 <= copies <= 99:
        raise ValueError('复制份数必须为 2～99 的整数')
    if connection not in ('ring', 'chain') or (connection == 'ring' and copies < 3):
        raise ValueError('连接方式为 ring（至少 3 份）或 chain')
    if destination == source or destination.is_relative_to(ROOT / 'raw_dataset'):
        raise ValueError('扩展文件必须写入独立目录，禁止覆盖原始数据')
    original = source.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    base, buses, branches, instructions = read_dat(source)
    validate_bpa_scope(source, buses)
    keys = {b['key'] for b in buses}
    if len(keys) != len(buses) or any(not k[0].isascii() or len(k[0]) > 7 for k in keys):
        raise ValueError('源母线必须唯一，名称为最多 7 字符的 ASCII 文本')
    graph = nx.Graph()
    graph.add_nodes_from(keys)
    graph.add_edges_from((e['from'], e['to']) for e in branches)
    slacks = [b for b in buses if b['subtype'] == 'S']
    if not nx.is_connected(graph) or len(slacks) != 1:
        raise ValueError('源算例必须是只有一个平衡节点的连通交流网络')
    template = next((e for e in branches if e['kind'] == 'L' and
                     e['from'][1] == e['to'][1] and e['from'][1] >= 200 and
                     e['r'] >= 0 and e['x'] > 0 and e['rate_mva'] > 0), None)
    if template is None:
        raise ValueError('源算例缺少可作联络线模板的同电压高压线路')
    prefixes = [chr(65 + i) for i in range(copies)] if copies <= 26 else [f'{i+1:02d}' for i in range(copies)]
    width = len(prefixes[0])
    aliases = {}
    used = {key[0] for key in keys}
    for index, name in enumerate(sorted(used)):
        alias = name
        if len(name) + width > 8:
            number = index
            alias = f'Z{number:05d}'
            while alias in used:
                number += 1
                alias = f'Z{number:05d}'
            used.add(alias)
        aliases[name] = alias

    def copied_name(prefix, name):
        return prefix + aliases[name]
    pairs = list(zip(prefixes, prefixes[1:]))
    if connection == 'ring':
        pairs.append((prefixes[-1], prefixes[0]))
    lines = original.decode('gbk').splitlines()
    records = [line for line in lines if line[:2] in ('B ', 'BE', 'BS', 'L ', 'T ')]
    ties = [{'from': copied_name(a, template['to'][0]), 'to': copied_name(b, template['from'][0]),
             'kv': template['from'][1], 'r_pu': template['r'], 'x_pu': template['x'],
             'half_g_pu': template['half_y'].real, 'half_b_pu': template['half_y'].imag,
             'rate_mva': float(template['rate_mva'])} for a, b in pairs]
    slack = slacks[0]
    meta = {'kind': 'synthetic_replication', 'version': 1,
            'source_name': source.name, 'source_sha256': digest, 'base_mva': base,
            'copies': copies, 'connection': connection, 'area_prefixes': prefixes,
            'source_bus_count': len(buses), 'source_branch_count': len(branches),
            'bus_count': copies * len(buses), 'branch_count': copies * len(branches) + len(ties),
            'global_slack': copied_name(prefixes[0], slack['key'][0]),
            'slack_changes': [{'bus': copied_name(a, slack['key'][0]), 'from': 'BS', 'to': 'BE',
                               'scheduled_p_mw': slack['p'], 'voltage_target_pu': slack['vm']}
                              for a in prefixes[1:]],
            'intertie_template_source_line': template['source_line'], 'interties': ties,
            'policy': 'Research hypothesis: duplicate internal cards; rename buses; retain one BS; '
                      'convert other BS to BE preserving original P and voltage targets; add template L ties. '
                      'No load scaling, impedance adjustment or rating enlargement.'}
    if width > 1:
        meta.update(prefix_width=width, bus_aliases={alias: name for name, alias in aliases.items() if alias != name})
    output = [MARKER + json.dumps(meta, separators=(',', ':'), ensure_ascii=True),
              '(POWERFLOW,CASEID=SJZ_EXPANDED)', f'/MVA_BASE={base}\\', '/NETWORK_DATA\\']

    def replace_name(row, start, stop, name):
        if len(name) > stop - start:
            raise ValueError('复制后的母线名称超出 DAT 字段宽度')
        row[start:stop] = name.ljust(stop - start)

    for prefix in prefixes:
        output.append(f'. Area {prefix}: identical internal network cards, names prefixed by {prefix}')
        for line in records:
            row = list(line.ljust(90))
            replace_name(row, 6, 14, copied_name(prefix, line[6:14].strip()))
            if line[0] in 'LT':
                replace_name(row, 19, 27, copied_name(prefix, line[19:27].strip()))
            elif line[:2] == 'BS' and prefix != prefixes[0]:
                row[1] = 'E'
                row[61:65] = '    '  # BE keeps a voltage target, not a fixed angle.
            output.append(''.join(row).rstrip())
    output.append('. Synthetic inter-area ties: electrical parameters copied from the declared source L card')
    for tie in ties:
        row = list(lines[template['source_line'] - 1].ljust(90))
        replace_name(row, 6, 14, tie['from'])
        replace_name(row, 19, 27, tie['to'])
        output.append(''.join(row).rstrip())
    output.extend(['/SOLUTION\\', *instructions, '(END)'])
    data = ('\n'.join(output) + '\n').encode('gbk')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    manifest = {**meta, 'generated_file': destination.name,
                'generated_sha256': hashlib.sha256(data).hexdigest()}
    destination.with_suffix('.manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if source.read_bytes() != original:
        raise RuntimeError('原始输入意外发生变化')
    return manifest


def expanded_layout(meta, buses):
    """Separate area drawings while preserving each original electrical layout."""
    saved = ROOT / 'data/demo_area/layout.json'
    source = next((p for p in (ROOT / 'raw_dataset/extracted').glob('*.dat')
                   if hashlib.sha256(p.read_bytes()).hexdigest() == meta['source_sha256']), None)
    if source is None or not saved.exists():
        return None
    _, original_buses, _, _ = read_dat(source)
    original_layout = json.loads(saved.read_text())
    positions = {b['key']: original_layout[str(i + 1)] for i, b in enumerate(original_buses)}
    xs, ys = zip(*positions.values())
    center = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-9)
    radius = max(3, meta['copies'] * 1.3 / math.pi)
    centers = {prefix: (radius * math.cos(math.pi/2 + 2*math.pi*i/meta['copies']),
                        radius * math.sin(math.pi/2 + 2*math.pi*i/meta['copies']))
               for i, prefix in enumerate(meta['area_prefixes'])}
    layout = {}
    for bus in buses:
        name = bus['name']
        area, original_name = split_bus_name(meta, name)
        p = positions.get((original_name, bus['kv']))
        if p is None or area not in centers:
            return None
        cx, cy = centers[area]
        layout[str(bus['id'])] = [cx + 2.2*(p[0] - center[0])/span,
                                 cy + 2.2*(p[1] - center[1])/span]
    return layout
