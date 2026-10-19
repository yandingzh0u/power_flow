"""Impedance interpretation, physical base invariance and original-data checks."""
from pathlib import Path
import tempfile
import unittest

import numpy as np

from gmss.core import ROOT
from gmss.dat import read_dat, stamp, newton
from gmss.raw_solver import solve


class ImpedanceBaseTests(unittest.TestCase):
    def test_only_t_impedance_is_converted_and_input_values_are_retained(self):
        source = ROOT / 'raw_dataset/extracted/13点sim2合并anony.dat'
        _, _, original, _ = read_dat(source)
        _, _, converted, _ = read_dat(source, transformer_base_mva=100.)
        for raw, used in zip(original, converted):
            factor = 10 if raw['kind'] == 'T' else 1
            np.testing.assert_allclose([used['r'], used['x']],
                                       np.array([raw['r'], raw['x']]) * factor)
            self.assertEqual(used['input_r_pu'], raw['input_r_pu'])
            self.assertEqual(used['input_x_pu'], raw['input_x_pu'])
            for field in ('from', 'to', 'half_y', 'tap', 'rate_mva', 'regularized'):
                self.assertEqual(used[field], raw[field])
        for invalid in (0, -100, np.nan, np.inf):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                read_dat(source, transformer_base_mva=invalid)

    def test_same_physical_transformer_gives_same_voltage_on_two_system_bases(self):
        def card(kind, fields):
            row = list(' ' * 90)
            row[:len(kind)] = kind
            for start, stop, value in fields:
                row[start-1:stop] = str(value).rjust(stop-start+1)
            return ''.join(row).rstrip()

        rows = [card('BS', [(7,14,'SOURCE'), (15,18,'10.'), (58,61,'1.0')]),
                card('B ', [(7,14,'LOAD'), (15,18,'10.'), (21,25,'80.'), (26,30,'30.')]),
                card('T ', [(7,14,'SOURCE'), (15,18,'10.'), (20,27,'LOAD'), (28,31,'10.'),
                            (34,37,'100.'), (39,44,'.01'), (45,50,'.1'),
                            (63,67,'10.'), (68,72,'10.')])]
        answers = []
        physical_admittances = []
        with tempfile.TemporaryDirectory() as directory:
            for system_base in (100, 1000):
                source = Path(directory) / f'{system_base}.dat'
                source.write_text('\n'.join([f'/MVA_BASE={system_base}\\', *rows]), encoding='gbk')
                base, buses, edges, _ = read_dat(source, transformer_base_mva=100.)
                ybus = stamp(base, buses, edges)
                physical_admittances.append(ybus.toarray() * base)
                # Use an equal physical mismatch tolerance on both bases.
                voltage, _ = newton(base, buses, ybus, tolerance=1e-10/base)
                answers.append(voltage)
        np.testing.assert_allclose(*physical_admittances, rtol=1e-13, atol=1e-12)
        np.testing.assert_allclose(*answers, rtol=1e-10, atol=1e-10)

    def test_default_solution_reports_both_loading_definitions_separately(self):
        for hour, current, apparent, target in (
                ('13', 206.36082638, 199.33434043, 74.89468623),
                ('20', 288.72305320, 273.62637358, 83.57982937)):
            with self.subTest(hour=hour):
                result = solve(ROOT / f'raw_dataset/extracted/{hour}点sim2合并anony.dat')
                self.assertAlmostEqual(result['T_loading']['max_current_loading_percent'], current, places=5)
                self.assertAlmostEqual(result['T_loading']['max_apparent_power_loading_percent'], apparent, places=5)
                self.assertAlmostEqual(result['target_GXVS2_GXVY2']['current_loading_percent'], target, places=5)


if __name__ == '__main__':
    unittest.main()
