"""Microgrid equipment stored as real B/L/T cards in the input DAT.

Runtime only reads equipment and subsystem annotations; it never overlays buses
or injections. Parameter provenance and one-time migration live with the data.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / 'data/microgrid/default_template.json'
MARKER = '.GMSS_MICROGRID '


def _device_check(device):
    p, q, sn = device['p_mw'], device['q_mvar'], device['sn_mva']
    if (device['control'] != 'PQ' or not all(math.isfinite(x) for x in (p, q, sn))
            or sn <= 0 or math.hypot(p, q) > sn + 1e-12
            or abs(p) > device['max_p_mw'] or (device['kind'] == 'pv' and p < 0)):
        raise ValueError('微网设备 PQ 给定超出容量或控制方式不受支持')


def _number(value, width):
    """DAT implicit decimals require an explicit dot, including whole values."""
    for precision in range(width, 0, -1):
        result = format(value, f'.{precision}g')
        if 'e' not in result.lower() and '.' not in result:
            result += '.'
        if result.startswith('0.') and len(result) > 2:
            result = result[1:]
        elif result.startswith('-0.') and len(result) > 3:
            result = '-' + result[2:]
        if len(result) <= width:
            return result
    raise ValueError(f'DAT 字段宽度不足：{value}')


def _card(kind, fields):
    row = list(' ' * 90)
    row[:len(kind)] = kind
    for start, end, value in fields:
        width = end - start + 1
        word = value if isinstance(value, str) else _number(value, width)
        if len(word) > width:
            raise ValueError(f'DAT 字段超长：{word}')
        row[start - 1:end] = word.rjust(width)
    return ''.join(row).rstrip()


def materialize_microgrids(path, backup_dir=None, template=None):
    """One-time dataset edit, with an immutable content-addressed backup.

    Already annotated DATs are left byte-for-byte unchanged. All electrical
    parameters are in standard cards; annotations only name the subsystems,
    record device limits/provenance and make the added T-card base explicit.
    """
    from .dat import read_dat
    path = Path(path)
    original = path.read_bytes()
    text = original.decode('gbk')
    if any(line.startswith(MARKER) for line in text.splitlines()):
        return False
    config = deepcopy(template) if template is not None else json.loads(TEMPLATE.read_text())
    for device in config['devices']:
        _device_check(device)
    base, buses, branches, _ = read_dat(path)
    pccs = [b for b in buses if b['key'][0] in config['pcc_names']]
    if not pccs:
        return False
    keys = {b['key'] for b in buses}
    records = []
    digest = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    kv = config['voltage_kv']
    roles = {'IN': 'IN', 'LOAD': 'LD', 'PV': 'PV', 'BESS': 'BT'}
    for number, pcc in enumerate(pccs, 1):
        if pcc['key'][1] >= 35:
            raise ValueError('微网要求配网 PCC 电压低于 35 kV')
        name = f'MG{number:02d}'
        local = {b['name']: f"{name}-{roles[b['name']]}" for b in config['buses']}
        for item in config['buses']:
            key = (local[item['name']], kv)
            if key in keys:
                raise ValueError(f'微网新增母线与已有标识冲突：{key}')
            keys.add(key)
            devices = [d for d in config['devices'] if d['bus'] == item['name']]
            fields = [(7, 14, key[0]), (15, 18, kv),
                      (21, 25, item['load_p_mw']), (26, 30, item['load_q_mvar']),
                      (43, 47, sum(d['p_mw'] for d in devices)),
                      (48, 52, sum(d['q_mvar'] for d in devices))]
            records.append(_card('B ', fields))
        trafo = config['transformer']
        if trafo.get('phase_shift_deg', 0) != 0 or trafo['tap'] != 1:
            raise ValueError('微网 DAT 写入器目前要求零相移和额定分接')
        # Store new T-card impedances on an explicit 100 MVA reference.
        records.append(_card('T ', [(7, 14, pcc['key'][0]), (15, 18, pcc['key'][1]),
            (20, 27, local['IN']), (28, 31, kv), (34, 37, trafo['sn_mva']),
            (39, 44, trafo['r_pu_own_base'] * 100 / trafo['sn_mva']),
            (45, 50, trafo['x_pu_own_base'] * 100 / trafo['sn_mva']),
            (63, 67, pcc['key'][1]), (68, 72, kv)]))
        cable = config['cable']
        for line in config['lines']:
            records.append(_card('L ', [(7, 14, local[line['from']]), (15, 18, kv),
                (20, 27, local[line['to']]), (28, 31, kv),
                (34, 37, cable['max_i_ka'] * 1000),
                (39, 44, cable['r_ohm_per_km'] * line['length_km'] * base / kv ** 2),
                (45, 50, cable['x_ohm_per_km'] * line['length_km'] * base / kv ** 2)]))
        spec = {'id': number, 'name': name, 'pcc_name': pcc['key'][0], 'pcc_kv': pcc['key'][1],
                'voltage_kv': kv, 'bus_roles': local, 'transformer_base_mva': 100.,
                'model_type': 'multi_bus_microgrid', 'template_id': config['id'],
                'template_sha256': digest, 'operation_mode': 'grid_connected',
                'devices': [dict(d, bus=local[d['bus']]) for d in config['devices']],
                'scope': config['scope'], 'note': config['provenance'], 'sources': config['sources']}
        records.append(MARKER + json.dumps(spec, ensure_ascii=True, separators=(',', ':')))
    newline = b'\r\n' if b'\r\n' in original else b'\n'
    rows = original.splitlines(keepends=True)
    position = next((i for i, row in enumerate(rows) if row.startswith(b'/SOLUTION')), None)
    if position is None:
        raise ValueError('DAT 缺少 /SOLUTION，不能确定网络卡片写入位置')
    updated = b''.join(rows[:position]) + newline.join(r.encode('ascii') for r in records) + newline + b''.join(rows[position:])
    # Parse and validate the complete bytes before replacing the working file.
    temporary = path.with_name(path.name + '.microgrid-tmp')
    try:
        temporary.write_bytes(updated)
        _, parsed, parsed_edges, _ = read_dat(temporary)
        if len(parsed) != len(buses) + 4 * len(pccs) or len(parsed_edges) != len(branches) + 4 * len(pccs):
            raise ValueError('写入后 DAT 设备数量不一致')
        if backup_dir is not None:
            backup = Path(backup_dir) / (hashlib.sha256(original).hexdigest() + '.dat')
            backup.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists() and backup.read_bytes() != original:
                raise ValueError('原始 DAT 备份校验失败')
            if not backup.exists():
                backup.write_bytes(original)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def attach_microgrid_metadata(text, buses, branches, base):
    """Annotate equipment already parsed from DAT; never append equipment."""
    lookup = {b['key']: i for i, b in enumerate(buses)}
    occupied, ids = set(), set()
    for line in text.splitlines():
        if not line.startswith(MARKER):
            continue
        spec = json.loads(line[len(MARKER):])
        if spec['id'] in ids:
            raise ValueError('DAT 微网标识重复')
        ids.add(spec['id'])
        kv = spec['voltage_kv']
        pcc = (spec['pcc_name'], spec['pcc_kv'])
        members = {(name, kv) for name in spec['bus_roles'].values()}
        if pcc not in lookup or not members.issubset(lookup) or occupied & members or pcc in members:
            raise ValueError('DAT 微网母线定义缺失或重叠')
        occupied.update(members)
        for key in members:
            buses[lookup[key]]['microgrid_id'] = spec['id']
        inlet = (spec['bus_roles']['IN'], kv)
        interface = [e for e in branches if e['kind'] == 'T' and e['from'] == pcc and e['to'] == inlet]
        if len(interface) != 1:
            raise ValueError('DAT 微网接入变压器定义不唯一')
        for edge in branches:
            if (edge['from'] in members and edge['to'] in members) or edge is interface[0]:
                edge['microgrid_id'] = spec['id']
        edge = interface[0]
        scale = base / spec['transformer_base_mva']
        edge.update(r=edge['input_r_pu'] * scale, x=edge['input_x_pu'] * scale,
                    impedance_base_mva=spec['transformer_base_mva'], impedance_scale=scale)
        buses[lookup[inlet]]['microgrid_spec'] = spec


def microgrids_from_dat(buses, branches):
    """Use actual card P/Q, not annotation schedules, to report MG balance."""
    lookup = {b['key']: i + 1 for i, b in enumerate(buses)}
    systems = []
    for bus in buses:
        if 'microgrid_spec' not in bus:
            continue
        spec = deepcopy(bus['microgrid_spec'])
        kv = spec['voltage_kv']
        keys = [(name, kv) for name in spec['bus_roles'].values()]
        indices = [lookup[key] for key in keys]
        pcc = (spec['pcc_name'], spec['pcc_kv'])
        inlet = (spec['bus_roles']['IN'], kv)
        interface = next(e for e in branches if e['from'] == pcc and e['to'] == inlet)
        for device in spec['devices']:
            device['bus_id'] = lookup[(device['bus'], kv)]
            b = buses[device['bus_id'] - 1]
            device.update(p_mw=b['gen_p'], q_mvar=b['q'] + b['load_q'])
            _device_check(device)
        members = [buses[i - 1] for i in indices]
        spec.update(pcc_bus=lookup[pcc], internal_buses=indices,
                    interface_branch_id=interface['source_line'],
                    internal_branch_ids=[e['source_line'] for e in branches if e['from'] in keys and e['to'] in keys],
                    scheduled_load_mw=sum(b['load_p'] for b in members),
                    scheduled_load_q_mvar=sum(b['load_q'] for b in members),
                    scheduled_generation_mw=sum(b['gen_p'] for b in members),
                    scheduled_generation_q_mvar=sum(b['q'] + b['load_q'] for b in members))
        systems.append(spec)
    return sorted(systems, key=lambda m: m['id'])


def bind_microgrid_partitions(model, parts):
    """Resolve disjoint electrical ownership after ideal-bus contraction."""
    owner = {i: p['id'] for p in parts for i in p['internal_buses']}
    by_id = {p['id']: p for p in parts}
    for mg in model.get('microgrids', []):
        ids = {owner[i] for i in mg['internal_buses']}
        if len(ids) != 1 or owner[mg['pcc_bus']] in ids:
            raise ValueError('微网内部必须属于独立计算域，不能与配网 PCC 混合')
        mg['partition_id'] = ids.pop()
        mg['parent_partition_id'] = owner[mg['pcc_bus']]
        by_id[mg['partition_id']].update(subsystem_type='microgrid', microgrid_id=mg['id'],
                                        graph_algorithm='explicit microgrid subsystem; protected internal topology',
                                        parent_partition_id=mg['parent_partition_id'], display_name=mg['name'])
    return parts


def microgrid_states(model, voltage):
    """Measure the actual PCC response and unsatisfied subsystem AC equations.

    Positive import is distribution -> MG at the HV transformer terminal.
    A subsystem's boundary mismatch is the independent P/Q nodal residual at
    its inlet (transformer + internal feeder), not the sum of lossy terminals.
    """
    systems = model.get('microgrids', [])
    if not systems:
        return []
    import numpy as np
    group = model['group_index']
    _, anchors = np.unique(group, return_index=True)
    v = voltage[anchors]
    injection = v * np.conj(model['y_reduced'] @ v) * model['base']
    specified = np.array([complex(b['p'], b['q']) for b in model['reduced']])
    residual = injection - specified
    edges = {e['source_line']: e for e in model['branches']}
    rows = []
    for mg in systems:
        e = edges[mg['interface_branch_id']]
        vf, vt = voltage[e['f']], voltage[e['t']]
        sf = vf * np.conj(e['yff'] * vf + e['yft'] * vt) * model['base']
        st = vt * np.conj(e['ytf'] * vf + e['ytt'] * vt) * model['base']
        indices = group[np.array(mg['internal_buses']) - 1]
        inlet = group[e['t']]
        own = [edges[i] for i in mg['internal_branch_ids']]
        losses = sf + st
        for line in own:
            a, b = voltage[line['f']], voltage[line['t']]
            losses += (a * np.conj(line['yff'] * a + line['yft'] * b)
                       + b * np.conj(line['ytf'] * a + line['ytt'] * b)) * model['base']
        balance = sf - complex(mg['scheduled_load_mw'] - mg['scheduled_generation_mw'],
                               mg['scheduled_load_q_mvar'] - mg['scheduled_generation_q_mvar']) - losses
        rows.append({'id': mg['id'], 'partition_id': mg.get('partition_id'),
                     'parent_partition_id': mg.get('parent_partition_id'), 'pcc_bus': mg['pcc_bus'],
                     'pcc_vm_pu': float(abs(vf)), 'pcc_va_degree': float(np.rad2deg(np.angle(vf))),
                     'inlet_vm_pu': float(abs(vt)), 'inlet_va_degree': float(np.rad2deg(np.angle(vt))),
                     'import_p_mw': float(sf.real), 'import_q_mvar': float(sf.imag),
                     'received_p_mw': float(-st.real), 'received_q_mvar': float(-st.imag),
                     'loss_p_mw': float(losses.real), 'loss_q_mvar': float(losses.imag),
                     'boundary_mismatch_p_mw': float(residual[inlet].real),
                     'boundary_mismatch_q_mvar': float(residual[inlet].imag),
                     'max_equation_residual_mva': float(np.max(np.maximum(abs(residual[indices].real), abs(residual[indices].imag)))),
                     'balance_p_mw': float(balance.real), 'balance_q_mvar': float(balance.imag),
                     'voltage_min_pu': float(abs(v[indices]).min()),
                     'exchange': 'distribution sends PCC V/angle; microgrid returns voltage-dependent P/Q and Schur linear response'})
    return rows
