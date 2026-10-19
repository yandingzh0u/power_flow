"""Electrical cuts, unchanged equations and truthful iteration measurements."""
from copy import deepcopy
import base64
from pathlib import Path
import unittest

import numpy as np
from scipy.sparse.csgraph import connected_components

from gmss.core import ROOT, run_case
from gmss.dat import stamp
from gmss.partition import build_partitions, available_cpus, clear_partition_cache


class WeightedPartitionTests(unittest.TestCase):
    def setUp(self):
        clear_partition_cache()

    def test_strong_connections_stay_inside_connected_domains(self):
        buses = [{'key': (str(i), 110 if i == 0 else 10), 'subtype': 'S' if i == 0 else ' ',
                  'p': 0., 'q': 0., 'vm': 1., 'va': 0., 'gs': 0., 'bs': 0.} for i in range(9)]
        edges = [{'from': buses[i]['key'], 'to': buses[i+1]['key'], 'r': .0001,
                  'x': .5 if i == 4 else .001, 'tap': 1., 'half_y': 0j} for i in range(8)]
        y = stamp(100, buses, edges)
        model = {'buses': buses, 'reduced': buses, 'branches': edges,
                 'group_index': np.arange(9), 'y_reduced': y}
        original = deepcopy(edges)
        parts = build_partitions(model, target_size=4)
        membership = {node: p['id'] for p in parts for node in p['internal']}
        self.assertEqual(set(membership),set(range(1,9)))
        self.assertEqual(len(parts),2)
        self.assertNotEqual(membership[4],membership[5])
        graph=y.copy();graph.data=np.ones(len(graph.data))
        for part in parts:
            self.assertEqual(connected_components(graph[part['internal']][:,part['internal']],directed=False)[0],1)
        self.assertEqual(edges,original)
        self.assertEqual(model['partitioning']['strategy'],'weighted')
        self.assertEqual(model['partitioning']['available_cpus'],available_cpus())
        self.assertFalse(model['partitioning']['structure_cache_hit'])
        parts[0]['converged'] = True
        reused = build_partitions(model, target_size=4)
        self.assertTrue(model['partitioning']['structure_cache_hit'])
        self.assertIsNone(reused[0]['converged'])
        reused[0]['internal_buses'].clear()
        self.assertTrue(build_partitions(model, target_size=4)[0]['internal_buses'])
        model['branches'][4]['x'] = .001
        build_partitions(model, target_size=4)
        self.assertFalse(model['partitioning']['structure_cache_hit'])

    def test_iteration_boundary_powers_and_group_injections_match_final_equations(self):
        events=[]
        result=run_case(ROOT/'raw_dataset/extracted/20点sim2合并anony.dat',
                        emit=lambda k,e:events.append((k,e)),builtin=True)
        last=[e for k,e in events if k=='iteration'][-1]
        flows={e['id']:e for e in result['scenario']['lines']+result['scenario']['trafos']}
        for row in last['boundary_flows']:
            expected=flows[row[0]]
            np.testing.assert_allclose(row[1:],[expected[k] for k in ('p','q','p_to','q_to','loading')],atol=1e-8)
        self.assertEqual(last['node_state_count'], len(result['scenario']['buses']))
        states = np.frombuffer(base64.b64decode(last['node_state_base64']), dtype='<f8').reshape(-1, 4)
        for bus,values in zip(result['scenario']['buses'], states):
            np.testing.assert_allclose(values[:2],[bus['vm'],bus['va']],atol=1e-10)
            if bus['merged_group_count']==1:
                np.testing.assert_allclose(values[2:],[bus['p'],bus['q']],atol=1e-5)
        self.assertTrue(all(p['residual_mva']<1e-4 for p in last['partition_states']))
        self.assertTrue(any(p['worker_id'] is not None for p in last['partition_states']))
        before=[e for k,e in events if k=='iteration'][0]
        self.assertGreater(max(p['residual_mva'] for p in before['partition_states']),1)


if __name__=='__main__':
    unittest.main()
