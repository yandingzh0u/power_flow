"""Analytic AC cases and original-DAT regressions for the BPA-style workflow."""

import csv
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from scipy.sparse import csr_matrix

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gmss.bpa import SolutionOptions, allocate_be_reactive, solve_bpa_style
from gmss.raw_solver import solve, validate_bpa_scope
from gmss.dat import read_dat
from original_fixtures import original_dat


def two_bus(kind=" ", vm=1.0, p=-80.0, q=-30.0):
    buses = [{"subtype": "S", "vm": 1.0, "va": 0.0, "p": 0.0, "q": 0.0},
             {"subtype": kind, "vm": vm, "va": 0.0, "p": p, "q": q}]
    # X = 0.1 pu, no loss or shunt, 100 MVA base.
    ybus = csr_matrix([[-10j, 10j], [10j, -10j]])
    return buses, ybus


class BpaStyleSolverTests(unittest.TestCase):
    def test_raw_iteration_instruction_and_unsupported_settings(self):
        options = SolutionOptions.from_instructions([">SOL_ITER,DECOUPLED=2,NEWTON=15<"])
        self.assertEqual(options.decoupled_half_steps, 2)
        self.assertEqual(options.max_newton_iterations, 15)
        for instruction in (">SOL_ITER,OPTIM=1<", ">SOL_ITER,NEWTON=2,NEWTON=3<",
                            ">SOL_ITER,NEWTON=-1<", ">SOL_ITER,DECOUPLED=999<"):
            with self.subTest(instruction=instruction), self.assertRaises(ValueError):
                SolutionOptions.from_instructions([instruction])

    def test_decoupled_then_newton_matches_analytic_high_voltage_solution(self):
        buses, ybus = two_bus()
        voltage, details = solve_bpa_style(100.0, buses, ybus, SolutionOptions())
        p, q, x = 0.8, 0.3, 0.1
        squared = ((1 - 2*x*q) + np.sqrt((1 - 2*x*q)**2 - 4*x*x*(p*p + q*q))) / 2
        expected = complex(squared + x*q, -x*p)
        self.assertLess(abs(voltage[1] - expected), 1e-9)
        self.assertAlmostEqual(abs(voltage[1]), np.sqrt(squared), places=9)
        self.assertFalse(details["startup_reverted"])
        self.assertEqual([s["stage"] for s in details["trace"][:3]],
                         ["initial", "decoupled_P", "decoupled_Q"])
        self.assertLess(details["trace"][-1]["max_mismatch_pu"], 1e-8)

    def test_zero_raw_q_limits_on_be_still_allow_unscheduled_q(self):
        buses, ybus = two_bus(kind="E", vm=1.04, p=0.0, q=0.0)
        voltage, _ = solve_bpa_style(100.0, buses, ybus, SolutionOptions())
        reactive = (voltage * np.conj(ybus @ voltage) * 100.0)[1].imag
        self.assertAlmostEqual(abs(voltage[1]), 1.04, places=12)
        self.assertAlmostEqual(reactive, 41.6, places=8)
        allocation = allocate_be_reactive(reactive, 0.0, 0.0)
        self.assertEqual(allocation["allocated_q_generation_mvar"], 0.0)
        self.assertAlmostEqual(allocation["unscheduled_q_mvar"], 41.6, places=8)

    def test_reversed_raw_q_range_is_reported_without_guessing_a_correction(self):
        allocation = allocate_be_reactive(-30.0, -0.5, 0.0)
        self.assertIsNone(allocation["allocated_q_generation_mvar"])
        self.assertIsNone(allocation["unscheduled_q_mvar"])

    def test_nonconvergence_and_missing_slack_are_errors(self):
        buses, ybus = two_bus()
        with self.assertRaisesRegex(RuntimeError, "did not converge"):
            solve_bpa_style(100.0, buses, ybus, SolutionOptions(0, 0))
        buses[0]["subtype"] = " "
        with self.assertRaisesRegex(ValueError, "exactly one BS"):
            solve_bpa_style(100.0, buses, ybus, SolutionOptions())

    def test_unsupported_control_command_is_not_silently_ignored(self):
        source = ROOT / "raw_dataset/extracted/20点sim2合并anony.dat"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "unsupported.dat"
            path.write_bytes(source.read_bytes() + b"\n/LTC=OFF\\\n")
            _, buses, _, _ = read_dat(path)
            with self.assertRaisesRegex(ValueError, "Unsupported DAT command"):
                validate_bpa_scope(path, buses)


class OriginalDatRegressionTests(unittest.TestCase):
    def test_both_original_cases_preserve_data_and_balance_all_raw_buses(self):
        for hour, expected_loading in (("13", 397.89571637), ("20", 489.25963521)):
            with self.subTest(hour=hour), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / f'{hour}点.dat'
                source.write_bytes(original_dat(hour).read_bytes())
                before = hashlib.sha256(source.read_bytes()).hexdigest()
                output = Path(folder)
                result = solve(source, output, "bpa-style", transformer_base_mva=None)
                baseline = solve(source, output, transformer_base_mva=None)
                self.assertEqual(baseline["solver"], "standalone polar Newton-Raphson")
                self.assertAlmostEqual(baseline["target_GXVS2_GXVY2"]["current_loading_percent"], expected_loading, places=5)
                # Re-export the mode under test after checking legacy behavior.
                result = solve(source, output, "bpa-style", transformer_base_mva=None)
                self.assertEqual(result["source_sha256"], before)
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
                self.assertFalse(result["PSD_BPA_executed"])
                self.assertEqual(result['impedance_policy']['mode'],'system_base')
                self.assertEqual(result["ideal_zero_impedance_L_cards_contracted"], 26)
                self.assertEqual(result["decoupled_half_steps_executed"], 2)
                self.assertFalse(result["solver_details"]["startup_reverted"])
                self.assertLessEqual(result["solver_details"]["newton_iterations"], 15)
                self.assertLess(result["original_bus_equation_residual_mva"], 1e-4)
                self.assertAlmostEqual(result["target_GXVS2_GXVY2"]["current_loading_percent"], expected_loading, places=5)
                self.assertLess(result["pure_newton_comparison"]["max_complex_voltage_difference_pu"], 1e-6)
                self.assertLess(result["pure_newton_comparison"]["max_ordinary_branch_end_power_difference_mva"], 1e-3)
                target = result["BE_reactive_allocation"]["target_GXVY2"]
                self.assertEqual(target["allocated_q_generation_mvar"], 0.0)
                self.assertLess(target["unscheduled_q_mvar"], -30.0)
                # Independent KCL accounting from exported branch-end powers,
                # including reconstructed ideal-connection flows (no shunts in
                # these two original cases). No converted model is involved.
                with (output / f"raw_{hour}_branch.csv").open(encoding="utf-8-sig") as stream:
                    branches = list(csv.DictReader(stream))
                with (output / f"raw_{hour}_bus.csv").open(encoding="utf-8-sig") as stream:
                    buses = list(csv.DictReader(stream))
                self.assertEqual(len(branches), result["branch_count"])
                self.assertEqual(len(buses), result["bus_count"])
                self.assertEqual(sum(b["kind"] == "T" for b in branches), 91)
                summed = {(b["name"], float(b["base_kv"])): 0j for b in buses}
                for branch in branches:
                    for side in ("from", "to"):
                        key = branch[f"{side}_name"], float(branch[f"{side}_kv"])
                        summed[key] += complex(float(branch[f"p_{side}_mw"]), float(branch[f"q_{side}_mvar"]))
                for bus in buses:
                    actual = complex(float(bus["net_p_injection_mw"]), float(bus["net_q_injection_mvar"]))
                    self.assertLess(abs(summed[bus["name"], float(bus["base_kv"])] - actual), 1e-6)
                # Output must remain portable JSON (no NaN/Infinity).
                json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
