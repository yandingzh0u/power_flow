"""Physical-limit and incomplete-result regressions for local PF retries."""

import unittest
from unittest.mock import patch

import numpy as np
import pandapower as pp
from pandapower.auxiliary import LoadflowNotConverged

from gmss.core import _run_local_pf_ladder, _summarize_demo_distribution_partition_result


def voltage_control_case(with_pq_bus=False):
    net = pp.create_empty_network()
    upstream = pp.create_bus(net, 110.0)
    downstream = pp.create_bus(net, 110.0)
    pp.create_ext_grid(net, upstream, vm_pu=1.0)
    pp.create_line_from_parameters(net, upstream, downstream, 10.0, 0.1, 0.4, 0.0, 1.0)
    load_bus = downstream
    if with_pq_bus:
        load_bus = pp.create_bus(net, 110.0)
        pp.create_line_from_parameters(net, downstream, load_bus, 1.0, 0.1, 0.4, 0.0, 1.0)
    pp.create_load(net, load_bus, p_mw=8.0, q_mvar=3.0)
    pp.create_gen(net, downstream, p_mw=1.0, vm_pu=1.04, min_q_mvar=0.0, max_q_mvar=0.0)
    return net


class PowerflowModelConsistencyTests(unittest.TestCase):
    def test_retry_preserves_unlimited_pv_model(self):
        net = voltage_control_case(with_pq_bus=True)
        pp.runpp(net, enforce_q_lims=False)
        expected_vm = float(net.res_bus.at[1, "vm_pu"])
        expected_q = float(net.res_gen.at[0, "q_mvar"])
        calls = []

        def fail_first_then_solve(local, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise LoadflowNotConverged("force the numerical fallback")
            return pp.runpp(local, **kwargs)

        with patch("gmss.core.runpp_safe", side_effect=fail_first_then_solve):
            _run_local_pf_ladder(net)
        self.assertGreater(expected_q, 1.0)
        self.assertAlmostEqual(net.res_bus.at[1, "vm_pu"], expected_vm, places=7)
        self.assertAlmostEqual(net.res_gen.at[0, "q_mvar"], expected_q, places=5)
        self.assertEqual(len(calls), 2)

    def test_all_voltage_controlled_case_can_retry_without_pq_variables(self):
        net = voltage_control_case()
        pp.runpp(net, enforce_q_lims=False)
        expected_q = float(net.res_gen.at[0, "q_mvar"])
        calls = []

        def fail_first_then_solve(local, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise LoadflowNotConverged("force the numerical fallback")
            return pp.runpp(local, **kwargs)

        with patch("gmss.core.runpp_safe", side_effect=fail_first_then_solve):
            _run_local_pf_ladder(net)
        self.assertAlmostEqual(net.res_gen.at[0, "q_mvar"], expected_q, places=5)

    def test_retry_preserves_finite_reactive_limits(self):
        net = voltage_control_case()
        pp.runpp(net, enforce_q_lims=True)
        expected_vm = float(net.res_bus.at[1, "vm_pu"])
        self.assertLess(expected_vm, 1.04)
        calls = []

        def fail_first_then_solve(local, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise LoadflowNotConverged("force the numerical fallback")
            return pp.runpp(local, **kwargs)

        with patch("gmss.core.runpp_safe", side_effect=fail_first_then_solve):
            _run_local_pf_ladder(net)
        self.assertAlmostEqual(net.res_gen.at[0, "q_mvar"], 0.0, places=7)
        self.assertAlmostEqual(net.res_bus.at[1, "vm_pu"], expected_vm, places=7)
        self.assertEqual(len(calls), 2)

    def test_all_retries_preserve_policy_even_after_solver_updates_options(self):
        net = voltage_control_case()
        pp.runpp(net, enforce_q_lims=False)
        observed = []

        def fail(local, **kwargs):
            observed.append(kwargs["enforce_q_lims"])
            local["_options"]["enforce_q_lims"] = True
            raise LoadflowNotConverged("forced failure")

        with patch("gmss.core.runpp_safe", side_effect=fail):
            with self.assertRaises(LoadflowNotConverged):
                _run_local_pf_ladder(net)
        self.assertEqual(observed, [False, False, False])

    def test_missing_or_nan_local_results_are_not_zero_error_success(self):
        net = voltage_control_case()
        pp.runpp(net)
        part = {"id": 1, "internal_buses": [0, 1]}
        valid = {int(b): (float(r.vm_pu), float(r.va_degree)) for b, r in net.res_bus.iterrows()}
        result = _summarize_demo_distribution_partition_result(net, part, valid)
        self.assertTrue(result["converged"])
        self.assertAlmostEqual(result["max_vm_delta_vs_full_pu"], 0.0)
        for bad in ({0: valid[0]}, {**valid, 1: (np.nan, 0.0)}):
            with self.subTest(local_result=bad):
                result = _summarize_demo_distribution_partition_result(net, part, bad)
                self.assertFalse(result["converged"])
                self.assertIsNone(result["max_vm_delta_vs_full_pu"])
                self.assertTrue(result["error"])
        net.res_bus.at[1, "vm_pu"] = np.nan
        self.assertFalse(_summarize_demo_distribution_partition_result(net, part, valid)["converged"])


if __name__ == "__main__":
    unittest.main()
