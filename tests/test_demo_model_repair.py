"""Control-model repairs must preserve demand, active generation and ratings."""

from types import SimpleNamespace
import unittest

import pandapower as pp

from tools.repair_demo_model import ROOT, apply_controls, repair_case, scheduled_generation, solve


def case(p_mw=0.0):
    net = pp.create_empty_network()
    source = pp.create_bus(net, 110, name="SOURCE")
    bus = pp.create_bus(net, 110, name="CONTROL")
    net.bus["bpa_name"] = net.bus.name
    pp.create_ext_grid(net, source, vm_pu=1.0)
    pp.create_line_from_parameters(net, source, bus, 10, 0.1, 0.4, 0, 1)
    pp.create_load(net, bus, p_mw=8, q_mvar=3)
    pp.create_gen(net, bus, p_mw=p_mw, vm_pu=1.04, min_q_mvar=-9999, max_q_mvar=9999)
    card = SimpleNamespace(name="CONTROL", kv=110, subtype="E", pg=p_mw, pmax=0, qfield=0, qmin=0)
    return net, [card]


class DemoModelRepairTests(unittest.TestCase):
    def test_output_cannot_replace_source_case(self):
        with self.assertRaisesRegex(ValueError, "overwrite"):
            repair_case("20", ROOT / "data/demo_area/converted", {})

    def test_zero_source_hypothesis_removes_unbounded_control_and_preserves_inputs(self):
        net, cards = case()
        pp.runpp(net)
        self.assertGreater(abs(net.res_gen.at[0, "q_mvar"]), 1)
        load, line = net.load.copy(), net.line.copy()
        changes, unresolved = apply_controls(net, cards)
        solve(net)
        self.assertEqual(len(changes), 1)
        self.assertFalse(unresolved)
        self.assertFalse(net.gen.at[0, "in_service"])
        self.assertLess(net.res_bus.at[1, "vm_pu"], 1.04)
        self.assertTrue(net.load.equals(load))
        self.assertTrue(net.line.equals(line))
        self.assertEqual(net.model_repair["status"], "hypothesis_requires_field_validation")

    def test_nonzero_active_generator_is_not_reclassified_without_parameters(self):
        net, cards = case(p_mw=1.0)
        changes, unresolved = apply_controls(net, cards)
        self.assertFalse(changes)
        self.assertEqual(len(unresolved), 1)
        self.assertTrue(net.gen.at[0, "in_service"])

    def test_explicit_fixed_q_preserves_active_generation(self):
        net, cards = case(p_mw=1.0)
        before = scheduled_generation(net)
        apply_controls(net, cards, {"CONTROL": {"mode": "fixed_q", "q_mvar": -0.2, "evidence": "test fixture fixed injection"}})
        self.assertEqual(scheduled_generation(net), before)
        solve(net)
        self.assertAlmostEqual(net.res_sgen.q_mvar.sum(), -0.2)
        self.assertAlmostEqual(net.res_sgen.p_mw.sum(), 1.0)

    def test_explicit_limited_pv_releases_voltage_when_q_is_saturated(self):
        net, cards = case(p_mw=1.0)
        apply_controls(net, cards, {"CONTROL": {"mode": "limited_pv", "min_q_mvar": 0.0, "max_q_mvar": 0.0, "vm_pu": 1.04, "evidence": "test fixture zero reactive capability"}})
        # Validate saved default PF options, not just this tool's explicit solve().
        pp.runpp(net)
        self.assertAlmostEqual(net.res_gen.at[0, "q_mvar"], 0.0)
        self.assertLess(net.res_bus.at[1, "vm_pu"], 1.04)

    def test_invalid_or_missing_capability_is_rejected(self):
        base = {"mode": "limited_pv", "min_q_mvar": 0.0, "max_q_mvar": 1.0, "evidence": "test"}
        for bad in ({"max_q_mvar": None}, {"max_q_mvar": float("nan")}, {"min_q_mvar": 2.0}, {"evidence": ""}):
            with self.subTest(bad=bad):
                net, cards = case(p_mw=1.0)
                with self.assertRaises(ValueError):
                    apply_controls(net, cards, {"CONTROL": {**base, **bad}})


if __name__ == "__main__":
    unittest.main()
