"""Domain elimination, real boundary feedback, parallelism and AC invariants."""
from copy import deepcopy
import unittest

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import spsolve

from gmss.coupled import DomainPlan, _domain_plan, clear_structure_cache, solve_coupled
from gmss.dat import newton, stamp


def loop_case():
    buses = [{'key': (str(i), 110 if i == 0 else 10), 'subtype': 'S' if i == 0 else ' ',
              'vm': 1.02 if i == 0 else 1., 'va': 0., 'p': -3. - i, 'q': -1., 'gs': 0., 'bs': 0.}
             for i in range(9)]
    buses[3].update(subtype='E', vm=1.015, p=10.)
    edges = [{'from': buses[a]['key'], 'to': buses[b]['key'], 'r': .01, 'x': .08,
              'half_y': .001j, 'tap': 1.01 if a == 0 else 1.}
             for a, b in ((0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8), (8, 0), (4, 8))]
    y = stamp(100., buses, edges)
    return buses, y, [{'internal': [1, 2, 3, 4]}, {'internal': [5, 6, 7, 8]}]


class CoupledTests(unittest.TestCase):
    def setUp(self):
        clear_structure_cache()

    def test_cyclic_cut_edges_pv_taps_and_boundary_feedback_match_newton(self):
        buses, y, parts = loop_case()
        original = deepcopy(buses)
        events = []
        phases=[]
        def observe(row,value):
            if row.get('kind') == 'phase':
                phases.append((row, value))
            elif row.get('kind') != 'setup':
                events.append((row, value))
        v, details = solve_coupled(100., buses, y, parts, observe, workers=2)
        reference, history = newton(100., buses, y)
        np.testing.assert_allclose(v, reference, atol=1e-10)
        np.testing.assert_allclose(details['history'], history, atol=1e-10)
        self.assertEqual(buses, original)
        self.assertEqual(details['initialization'].split(';')[0], 'flat PQ voltage')
        self.assertTrue(all(s['linear_residual_pu']<1e-10 for s in details['steps']))
        # The two endpoints of the real 4--5 cut both belong to the coordinator.
        plan = DomainPlan(buses, y, parts)
        self.assertTrue({4, 5}.issubset(set(plan.interface_buses)))
        self.assertGreater(abs(events[0][1][4]-events[-1][1][4]), 1e-4)
        self.assertTrue(all(e['boundary_exchange'] for e, _ in events[1:]))
        self.assertEqual(len(phases),details['newton_iterations']*3)
        self.assertTrue(all(len(s['domain_worker_ids'])==details['local_domains'] for s in details['steps']))
        changed = deepcopy(buses)
        changed[6]['p'] -= 20.
        w, _ = solve_coupled(100., changed, y, parts, workers=2)
        target, _ = newton(100., changed, y)
        np.testing.assert_allclose(w, target, atol=1e-10)
        # A load change in partition 2 changes the solution inside partition 1.
        self.assertGreater(abs(w[2]-v[2]), 1e-4)

    def test_structure_reuse_binds_new_admittances_and_never_caches_a_solution(self):
        buses, y, parts = loop_case()
        before = y.copy()
        a = _domain_plan(buses, y, parts)
        b = _domain_plan(buses, y, parts)
        self.assertFalse(a.structure_cache_hit)
        self.assertTrue(b.structure_cache_hit)
        self.assertTrue(np.shares_memory(a.pivots, b.pivots))
        self.assertFalse(a.pivots.flags.writeable)
        self.assertFalse(np.shares_memory(a.values, b.values))
        self.assertFalse(np.shares_memory(a.rhs, b.rhs))
        a.values[:] = 123.
        a.rhs[:] = 456.
        self.assertTrue((b.values == 0).all())
        self.assertTrue((b.rhs == 0).all())
        self.assertTrue(y.indices.flags.writeable)
        np.testing.assert_array_equal(y.toarray(), before.toarray())
        changed_y = y * 1.1
        changed = deepcopy(buses)
        changed[6]['p'] -= 12.
        changed[0]['vm'] += .01
        reused = _domain_plan(changed, changed_y, parts)
        self.assertTrue(reused.structure_cache_hit)
        np.testing.assert_array_equal(reused.ydata, changed_y.tocoo().data)
        events = []
        actual, detail = solve_coupled(100., changed, changed_y, parts,
                                      lambda e, v: events.append((e, v)), workers=2)
        expected, history = newton(100., changed, changed_y)
        np.testing.assert_allclose(actual, expected, atol=1e-10)
        initial = next(v for e, v in events if e.get('iteration') == 0)
        np.testing.assert_allclose(abs(initial[1:3]), 1.)
        np.testing.assert_allclose(detail['history'], history, atol=1e-10)
        self.assertTrue(detail['structure_cache_hit'])

    def test_structure_cache_invalidates_types_cut_membership_and_sparse_pattern(self):
        buses, y, parts = loop_case()
        _domain_plan(buses, y, parts)
        controlled = deepcopy(buses)
        controlled[4].update(subtype='E', vm=1.01)
        self.assertFalse(_domain_plan(controlled, y, parts).structure_cache_hit)
        moved = [{'internal': [1, 2, 3]}, {'internal': [4, 5, 6, 7, 8]}]
        self.assertFalse(_domain_plan(buses, y, moved).structure_cache_hit)
        changed_y = y.tolil()
        changed_y[2, 7] = .01j
        changed_y[7, 2] = .01j
        self.assertFalse(_domain_plan(buses, changed_y.tocsr(), parts).structure_cache_hit)

    def test_local_pivot_fallback_preserves_the_schur_equations(self):
        buses, y, parts = loop_case()
        plan = DomainPlan(buses, y, parts)
        voltage = np.ones(len(buses), dtype=complex)
        data = plan.jacobian_data(voltage, abs(voltage), y @ voltage)
        # Force a zero first interior pivot while retaining an invertible block.
        node = int(plan.domains[0][4][0])
        data[(plan.row == node) & (plan.col == node)] = 0.
        residual = np.linspace(.1, .8, plan.unknowns)
        step, detail = plan.step(data, residual)
        matrix = coo_matrix((data, (plan.row, plan.col)), shape=(plan.unknowns, plan.unknowns)).tocsc()
        np.testing.assert_allclose(step, spsolve(matrix, -residual), atol=1e-9)
        self.assertGreater(detail['pivoted_local_fallbacks'], 0)

    def test_no_distribution_and_single_domain_and_failure_are_explicit(self):
        buses, y, _ = loop_case()
        for parts in ([], [{'internal': list(range(len(buses)))}]):
            v, _ = solve_coupled(100., buses, y, parts, workers=1)
            ref, _ = newton(100., buses, y)
            np.testing.assert_allclose(v, ref, atol=1e-10)
        with self.assertRaisesRegex(RuntimeError, 'did not converge'):
            solve_coupled(100., buses, y, [], workers=1, max_iterations=0)
        with self.assertRaises(ValueError):
            solve_coupled(100., buses, y, [], workers=-1)
        buses[0]['subtype'] = ' '
        with self.assertRaisesRegex(ValueError, 'exactly one BS'):
            solve_coupled(100., buses, y, [], workers=1)


if __name__ == '__main__':
    unittest.main()
