"""Connected copies, original-card invariants, and full AC expansion regression."""
import hashlib
from pathlib import Path
import tempfile
import unittest

import networkx as nx
import numpy as np

from gmss.core import ROOT, run_case
from gmss.dat import read_dat
from gmss.expansion import build_expansion, read_expansion, split_bus_name
from original_fixtures import original_dat


class ExpansionTests(unittest.TestCase):
    def test_copies_preserve_internal_parameters_and_form_one_island(self):
        for hour, copies in (('13', 5), ('20', 5), ('13', 50), ('20', 50)):
            source = original_dat(hour)
            before = source.read_bytes()
            base, original, edges, _ = read_dat(source)
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'expanded.dat'
                meta = build_expansion(source, target, copies=copies)
                actual_base, buses, branches, _ = read_dat(target)
                self.assertEqual(base, actual_base)
                self.assertEqual(len(buses), copies * len(original))
                self.assertEqual(len(branches), copies * len(edges) + copies)
                self.assertEqual(len({b['key'] for b in buses}), len(buses))
                self.assertEqual(sum(b['subtype'] == 'S' for b in buses), 1)
                self.assertEqual(read_expansion(target)['source_sha256'], hashlib.sha256(before).hexdigest())
                for copy_index, prefix in enumerate(meta['area_prefixes']):
                    for old, new in zip(original, buses[copy_index*len(original):(copy_index+1)*len(original)]):
                        self.assertEqual(split_bus_name(meta, new['key'][0]), (prefix, old['key'][0]))
                        self.assertLessEqual(len(new['key'][0]), 8)
                        self.assertEqual(new['key'][1], old['key'][1])
                        for field in ('p', 'q', 'load_p', 'load_q', 'gen_p', 'vm', 'gs', 'bs'):
                            self.assertEqual(new[field], old[field], field)
                        self.assertEqual(new['subtype'], 'E' if copy_index and old['subtype'] == 'S' else old['subtype'])
                    for old, new in zip(edges, branches[copy_index*len(edges):(copy_index+1)*len(edges)]):
                        for endpoint in ('from', 'to'):
                            self.assertEqual(split_bus_name(meta, new[endpoint][0]), (prefix, old[endpoint][0]))
                            self.assertEqual(new[endpoint][1], old[endpoint][1])
                        for field in ('r', 'x', 'half_y', 'tap', 'rate_mva', 'regularized'):
                            self.assertEqual(new[field], old[field], field)
                graph = nx.Graph()
                graph.add_nodes_from(b['key'] for b in buses)
                graph.add_edges_from((e['from'], e['to']) for e in branches)
                self.assertEqual(nx.number_connected_components(graph), 1)
                self.assertEqual(len(meta['slack_changes']), copies - 1)
            self.assertEqual(before, source.read_bytes())

    def test_ring_chain_and_original_overwrite_protection(self):
        source = original_dat('13')
        with self.assertRaises(ValueError):
            build_expansion(source, source)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'chain.dat'
            meta = build_expansion(source, target, copies=3, connection='chain')
            self.assertEqual(len(meta['interties']), 2)
            _, buses, branches, _ = read_dat(target)
            graph = nx.Graph()
            graph.add_nodes_from(b['key'] for b in buses)
            graph.add_edges_from((e['from'], e['to']) for e in branches)
            self.assertTrue(nx.is_connected(graph))
            for copies, connection in ((2, 'ring'), (1, 'chain'), (100, 'chain'), (5, 'unknown')):
                with self.assertRaises(ValueError):
                    build_expansion(source, target, copies, connection)

    def test_ten_thousand_bus_cases_converge_and_exchange_power(self):
        for hour, count in (('13', 10605), ('20', 10610)):
            source = ROOT / f'data/synthetic/sjz_{hour}_x5_ring.dat'
            reference = None
            for algorithm in ('nr', 'damped-nr', 'bpa-style', 'partitioned-nr'):
                with self.subTest(hour=hour, algorithm=algorithm):
                    result = run_case(source, algorithm)
                    scene, run = result['scenario'], result['run']
                    self.assertTrue(run['converged'])
                    self.assertTrue(run['source_unchanged'])
                    self.assertLess(run['residual_mva'], 1e-4)
                    self.assertEqual(scene['bus_count'], count)
                    self.assertEqual(len(scene['areas']), 5)
                    self.assertEqual(len(scene['partitions']),run['partitioning']['partition_count'])
                    self.assertGreaterEqual(len(scene['partitions']),run['partitioning']['distribution_components'])
                    self.assertEqual(run['partition_failed'], 0)
                    self.assertEqual(len(scene['microgrids']), 0)
                    self.assertEqual(set(result['layout']), {str(b['id']) for b in scene['buses']})
                    ties = [e for e in scene['lines'] if e['is_intertie']]
                    self.assertEqual(len(ties), 5)
                    self.assertTrue(all(abs(e['p']) > 1 for e in ties))
                    self.assertGreater(max(e['loading'] for e in scene['trafos']), 100)
                    values = np.array([[b['vm'], b['va']] for b in scene['buses']])
                    if reference is None:
                        reference = values
                    else:
                        np.testing.assert_allclose(values, reference, atol=1e-6)

    def test_hundred_thousand_bus_cases_and_coupled_domains_converge(self):
        for hour, count, edge_count in (('13', 106050, 106400), ('20', 106100, 106500)):
            with self.subTest(hour=hour):
                source = ROOT / f'data/synthetic/sjz_{hour}_x50_ring.dat'
                before = hashlib.sha256(source.read_bytes()).hexdigest()
                result = run_case(source)
                scene, run = result['scenario'], result['run']
                self.assertTrue(run['converged'])
                self.assertEqual(run['source_sha256'], before)
                self.assertTrue(run['source_unchanged'])
                self.assertLess(run['residual_mva'], 1e-4)
                self.assertEqual(scene['bus_count'], count)
                self.assertEqual(len(scene['lines']) + len(scene['trafos']), edge_count)
                self.assertEqual(len(scene['areas']), 50)
                self.assertEqual(len(scene['partitions']),run['partitioning']['partition_count'])
                self.assertEqual(scene['summary']['converged'], len(scene['partitions']))
                self.assertEqual(run['algorithm'], 'partitioned-nr')
                self.assertEqual(run['boundary_iterations'], 6)
                self.assertGreater(run['workers'], 1)
                self.assertTrue(all(s['active_worker_threads']>1 for s in run['solver_details']['steps']))
                self.assertEqual(run['partition_failed'], 0)
                self.assertEqual(len(scene['microgrids']), 0)
                self.assertEqual(sum(b['type'] == 'S' for b in scene['buses']), 1)
                self.assertEqual(set(result['layout']), {str(b['id']) for b in scene['buses']})
                self.assertEqual(sum(e['is_intertie'] for e in scene['lines']), 50)
                self.assertGreater(max(e['loading'] for e in scene['trafos']), 100)
                for area in scene['areas']:
                    self.assertEqual(area['bus_count'], count // 50)
                self.assertEqual({b['area_id'] for b in scene['buses']}, {f'{i:02d}' for i in range(1, 51)})
                self.assertEqual(sum(b['original_name'] == 'VOE21' for b in scene['buses']), 50)
                if hour == '20':
                    self.assertEqual(sum(b['original_name'] == 'GA  B14' for b in scene['buses']), 50)


if __name__ == '__main__':
    unittest.main()
