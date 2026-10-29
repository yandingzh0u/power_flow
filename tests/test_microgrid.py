"""Independent MG ownership, exact Schur coupling and loss-aware PCC balance."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from gmss.core import ROOT, run_case
from gmss.coupled import DomainPlan
from gmss.microgrid import TEMPLATE, MARKER, materialize_microgrids, microgrid_states
from gmss.dat import read_dat
from original_fixtures import original_dat
from gmss.raw_solver import solve


class MicrogridTests(unittest.TestCase):
    def test_two_snapshots_four_algorithms_same_dat_ac_model(self):
        for hour, count in (('13', 2145), ('20', 2146)):
            source = ROOT / f'raw_dataset/extracted/{hour}点sim2合并anony.dat'
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            reference = None
            for algorithm in ('nr', 'damped-nr', 'bpa-style', 'partitioned-nr'):
                with self.subTest(hour=hour, algorithm=algorithm):
                    events = []
                    result = run_case(source, algorithm, emit=lambda k, e: events.append((k, e)), builtin=True)
                    scene, run = result['scenario'], result['run']
                    self.assertEqual(scene['bus_count'], count)
                    self.assertEqual(len(scene['trafos']), 97)
                    self.assertEqual(len(scene['microgrids']), 6)
                    self.assertEqual(run['source_bus_count'], count)
                    self.assertEqual(scene['layers']['microgrid_bus_count'], 24)
                    self.assertEqual(scene['layers']['interface_trafo_count'], 70)
                    values = np.array([[b['vm'], b['va'], b['p'], b['q']] for b in scene['buses']])
                    if reference is None:
                        reference = values
                    np.testing.assert_allclose(values, reference, atol=1e-5)
                    self.assertEqual(set(result['layout']), {str(b['id']) for b in scene['buses']})
                    membership = [b for p in scene['partitions'] for b in p['internal_buses']]
                    self.assertEqual(len(membership), len(set(membership)))
                    for mg in scene['microgrids']:
                        self.assertEqual(len(mg['internal_buses']), 4)
                        self.assertNotEqual(mg['partition_id'], mg['parent_partition_id'])
                        self.assertLess(mg['max_equation_residual_mva'], 1e-5)
                        self.assertLess(abs(mg['balance_p_mw']), 1e-5)
                        self.assertLess(abs(mg['balance_q_mvar']), 1e-5)
                        self.assertGreater(mg['loss_p_mw'], 0)
                        self.assertAlmostEqual(mg['import_p_mw'], .08 + mg['loss_p_mw'], places=6)
                        self.assertAlmostEqual(mg['import_q_mvar'], .055 + mg['loss_q_mvar'], places=6)
                        self.assertTrue(mg['converged'])
                    if algorithm == 'partitioned-nr':
                        self.assertEqual(run['solver_details']['microgrid_domains'], 6)
                        steps = run['solver_details']['steps']
                        for mg in scene['microgrids']:
                            self.assertTrue(all(mg['partition_id'] in s['domain_part_ids'] for s in steps))
                        iterations = [e for k, e in events if k == 'iteration']
                        self.assertEqual(len(iterations[-1]['microgrid_states']), 6)
                        self.assertGreater(iterations[0]['microgrid_states'][0]['max_equation_residual_mva'], .01)
                    json.dumps(result, allow_nan=False)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)

    def test_load_perturbation_returns_to_distribution_and_matches_newton(self):
        source = ROOT / 'raw_dataset/extracted/13点sim2合并anony.dat'
        template = json.loads(TEMPLATE.read_text())
        changed = deepcopy(template)
        changed['buses'][1]['load_p_mw'] += .04
        changed['buses'][1]['load_q_mvar'] += .015

        def run(config, algorithm):
            context = {}
            def observe(e):
                if e['kind'] == 'model':
                    context['model'] = e
                elif e['kind'] == 'iteration' and e.get('voltage') is not None:
                    context['voltage'] = e['voltage']
            with tempfile.TemporaryDirectory() as folder:
                case = Path(folder) / 'changed.dat'
                case.write_bytes(original_dat('13').read_bytes())
                materialize_microgrids(case, template=config)
                r = solve(case, algorithm=algorithm, observer=observe, include_results=True)
            return r, microgrid_states(context['model'], context['voltage']), context

        before, first, _ = run(template, 'partitioned-nr')
        after, second, context = run(changed, 'partitioned-nr')
        expected, _, _ = run(changed, 'nr')
        np.testing.assert_allclose([[b['vm_pu'], b['va_degree']] for b in after['bus_results']],
                                   [[b['vm_pu'], b['va_degree']] for b in expected['bus_results']], atol=1e-9)
        # The only BS supplies the additional MG load and physical losses.
        self.assertGreater(after['slack_buses'][0]['net_p_injection_mw'] - before['slack_buses'][0]['net_p_injection_mw'], .24)
        for a, b in zip(first, second):
            self.assertGreater(b['import_p_mw'] - a['import_p_mw'], .04)
            self.assertGreater(abs(b['pcc_va_degree'] - a['pcc_va_degree']), 1e-8)
        model = context['model']
        parts = model.get('parts')
        # raw solve builds the same protected microgrid domains without core.py.
        if parts is None:
            from gmss.partition import build_partitions
            parts = build_partitions(model)
        plan = DomainPlan(model['reduced'], model['y_reduced'], parts)
        mg_ids = {p['id'] for p in parts if p['subsystem_type'] == 'microgrid'}
        self.assertTrue(mg_ids.issubset(set(plan.domain_part_ids)))

    def test_export_and_charging_reverse_the_pcc_response(self):
        source = ROOT / 'raw_dataset/extracted/20点sim2合并anony.dat'
        template = json.loads(TEMPLATE.read_text())
        for b in template['buses']:
            b['load_p_mw'] *= .2
            b['load_q_mvar'] *= .2
        for charging in (False, True):
            config = deepcopy(template)
            if charging:
                config['devices'][0]['p_mw'] = 0
                config['devices'][1]['p_mw'] = -.08
            context = {}
            def observe(e):
                if e['kind'] == 'model': context['model'] = e
                elif e.get('voltage') is not None: context['voltage'] = e['voltage']
            with tempfile.TemporaryDirectory() as folder:
                case = Path(folder) / 'changed.dat'
                case.write_bytes(original_dat('20').read_bytes())
                materialize_microgrids(case, template=config)
                solve(case, algorithm='partitioned-nr', observer=observe)
            rows = microgrid_states(context['model'], context['voltage'])
            self.assertTrue(all(m['import_p_mw'] > 0 if charging else m['import_p_mw'] < 0 for m in rows))
            self.assertTrue(all(abs(m['balance_p_mw']) < 1e-5 for m in rows))

    def test_invalid_device_capacity_does_not_modify_the_dat(self):
        template = json.loads(TEMPLATE.read_text())
        template['devices'][0]['q_mvar'] = 10
        with tempfile.TemporaryDirectory() as folder:
            case = Path(folder) / 'case.dat'
            before = original_dat('13').read_bytes()
            case.write_bytes(before)
            with self.assertRaisesRegex(ValueError, '超出容量'):
                materialize_microgrids(case, template=template)
            self.assertEqual(case.read_bytes(), before)

    def test_materialization_is_persistent_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            case = Path(folder) / 'case.dat'
            before = original_dat('13').read_bytes()
            case.write_bytes(before)
            self.assertTrue(materialize_microgrids(case, backup_dir=Path(folder) / 'backup'))
            updated = case.read_bytes()
            _, buses, edges, _ = read_dat(case)
            self.assertEqual(len(buses), 2145)
            self.assertEqual(len(edges), 2151)
            self.assertEqual(sum('microgrid_spec' in b for b in buses), 6)
            self.assertTrue(all(b['source_line'] > 0 for b in buses))
            self.assertTrue(all(e['source_line'] > 0 for e in edges))
            self.assertFalse(materialize_microgrids(case))
            self.assertEqual(case.read_bytes(), updated)
            self.assertEqual(next((Path(folder) / 'backup').glob('*.dat')).read_bytes(), before)
            result = solve(case)
            self.assertEqual(result['bus_count'], len(buses))
            self.assertEqual(case.read_bytes(), updated)
            # Electrical cards alone retain every injection and impedance.
            # Removing subsystem comments changes ownership, never the AC model.
            plain = Path(folder) / 'plain.dat'
            plain.write_bytes(b''.join(row for row in updated.splitlines(keepends=True)
                                     if not row.startswith(MARKER.encode())))
            annotated = solve(case, include_results=True)
            unannotated = solve(plain, include_results=True)
            np.testing.assert_allclose(
                [[b['vm_pu'], b['va_degree']] for b in annotated['bus_results']],
                [[b['vm_pu'], b['va_degree']] for b in unannotated['bus_results']], atol=1e-10)
            self.assertEqual(unannotated['bus_count'], 2145)

    def test_archived_input_is_not_automatically_extended_and_layout_is_preserved(self):
        base = run_case(original_dat('13'), 'nr', builtin=True)
        source = ROOT / 'raw_dataset/extracted/13点sim2合并anony.dat'
        current = run_case(source, 'nr', builtin=True)
        self.assertEqual(base['scenario']['microgrids'], [])
        self.assertEqual(base['scenario']['bus_count'], 2121)
        self.assertEqual(len(base['scenario']['trafos']), 91)
        for node, position in base['layout'].items():
            self.assertEqual(position, current['layout'][node])
        self.assertNotEqual(base['run']['source_sha256'], current['run']['source_sha256'])

    def test_microgrid_transformer_explicit_base_does_not_follow_global_assumption(self):
        source = ROOT / 'raw_dataset/extracted/13点sim2合并anony.dat'
        _, _, first, _ = read_dat(source)
        _, _, second, _ = read_dat(source, transformer_base_mva=100.)
        for a, b in zip(first, second):
            if a.get('microgrid_id') and a['kind'] == 'T':
                self.assertEqual(a['impedance_base_mva'], 100.)
                self.assertEqual((a['r'], a['x']), (20., 80.))
                self.assertEqual((a['r'], a['x']), (b['r'], b['x']))


if __name__ == '__main__':
    unittest.main()
