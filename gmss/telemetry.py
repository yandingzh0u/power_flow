"""Iteration measurements from the AC equations, shared by logs and replay."""
from __future__ import annotations

import base64
import numpy as np
from .microgrid import microgrid_states


class IterationTrace:
    def __init__(self, model, parts):
        self.model, self.parts = model, parts
        self.group = model['group_index']
        _, self.anchors = np.unique(self.group, return_index=True)
        self.owner = np.zeros(len(model['reduced']), dtype=int)
        for part in parts:
            self.owner[part['internal']] = part['id']
        self.ns = np.array([b['subtype'] != 'S' for b in model['reduced']])
        self.pq = np.array([b['subtype'] == ' ' for b in model['reduced']])
        self.specified = np.array([complex(b['p'], b['q']) for b in model['reduced']])
        self.previous = None
        self.interfaces = []
        expansion = model.get('expansion')
        ties = {(t['from'], t['to']) for t in expansion['interties']} if expansion else set()
        for edge in model['branches']:
            if edge['regularized']:
                continue
            a, b = self.group[edge['f']], self.group[edge['t']]
            if self.owner[a] != self.owner[b] or (edge['from'][0], edge['to'][0]) in ties:
                self.interfaces.append(edge)
        self.f = np.array([e['f'] for e in self.interfaces], dtype=int)
        self.t = np.array([e['t'] for e in self.interfaces], dtype=int)
        self.yff, self.yft, self.ytf, self.ytt = [np.array([e[k] for e in self.interfaces], dtype=complex)
                                               for k in ('yff', 'yft', 'ytf', 'ytt')]
        self.rates = np.array([e['rate_mva'] for e in self.interfaces])

    def measure(self, voltage, event):
        base = self.model['base']
        v = voltage[self.anchors]
        injection = v * np.conj(self.model['y_reduced'] @ v) * base
        mismatch = injection - self.specified
        constraint = np.maximum(np.where(self.ns, abs(mismatch.real), 0),
                                np.where(self.pq, abs(mismatch.imag), 0))
        residuals = np.zeros(len(self.parts) + 1)
        np.maximum.at(residuals, self.owner, constraint)
        dv, da = np.zeros(len(self.parts) + 1), np.zeros(len(self.parts) + 1)
        if self.previous is not None:
            np.maximum.at(dv, self.owner, abs(abs(v) - abs(self.previous)))
            np.maximum.at(da, self.owner, abs(np.rad2deg(np.angle(v / self.previous))))
        self.previous = v.copy()
        coordination = event.get('coordination') or {}
        workers = dict(zip(coordination.get('domain_part_ids', []), coordination.get('domain_worker_ids', [])))
        times = dict(zip(coordination.get('domain_part_ids', []), coordination.get('domain_local_ms', [])))
        partitions = [{'id': p['id'], 'residual_mva': float(residuals[p['id']]),
                       'subsystem_type': p.get('subsystem_type', 'distribution'),
                       'delta_vm_pu': float(dv[p['id']]), 'delta_va_deg': float(da[p['id']]),
                       'worker_id': workers.get(p['id']), 'local_ms': times.get(p['id'])} for p in self.parts]
        ifrom = self.yff * voltage[self.f] + self.yft * voltage[self.t]
        ito = self.ytf * voltage[self.f] + self.ytt * voltage[self.t]
        sf = voltage[self.f] * np.conj(ifrom) * base
        st = voltage[self.t] * np.conj(ito) * base
        loading = np.divide(np.maximum(abs(ifrom), abs(ito)) * base * 100, self.rates,
                            out=np.zeros(len(self.rates)), where=self.rates > 0)
        boundaries = [[e['source_line'], float(p.real), float(p.imag), float(q.real), float(q.imag),
                       float(load) if e['rate_mva'] else None]
                      for e, p, q, load in zip(self.interfaces, sf, st, loading)]
        states = np.column_stack((abs(voltage), np.rad2deg(np.angle(voltage)),
                                  injection.real[self.group], injection.imag[self.group]))
        result = {'partition_states': partitions, 'boundary_flows': boundaries,
                  'microgrid_states': microgrid_states(self.model, voltage),
                  'node_state_scope': 'iteration voltage; P/Q is reduced-bus aggregate net injection (includes shunts)',
                  'boundary_flow_columns': ['source_line', 'p_from_mw', 'q_from_mvar', 'p_to_mw', 'q_to_mvar', 'loading_percent']}
        # A full small-grid iteration used to create two nested Python lists
        # per bus (voltage plus duplicate node state). Reuse the existing exact
        # float64 wire format at feeder-study scale as well, reducing allocation,
        # JSON serialization and garbage collection without dropping any state.
        if len(voltage) > 512:
            result.update(node_state_base64=base64.b64encode(states.astype('<f8').tobytes()).decode('ascii'),
                          node_state_encoding='little-endian float64 [vm_pu,va_deg,group_p_mw,group_q_mvar]',
                          node_state_count=len(voltage))
        else:
            result.update(voltages=states[:, :2].tolist(), node_states=states.tolist())
        return result
