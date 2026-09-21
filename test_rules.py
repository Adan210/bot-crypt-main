import dataclasses
import glob
import os
import re
import unittest
from risk import clamp_aggression, entry_block, position_size
from rules import RULES, Rules
from signals import TUNABLE

ROOT = os.path.dirname(os.path.abspath(__file__))


class LockedRulesTests(unittest.TestCase):
    def test_values_are_pinned(self):
        """Changing a locked rule must be a deliberate human edit of rules.py AND this test."""
        self.assertEqual(dataclasses.asdict(RULES), dict(
            max_risk_per_trade=0.01, daily_loss_limit=0.03, weekly_loss_limit=0.06, max_open_positions=3,
            max_position_fraction=0.34, max_correlation=0.85, max_aggression=1.0, stop_loss_required=True))

    def test_rules_are_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            RULES.max_risk_per_trade = 0.5

    def test_no_code_tries_to_modify_rules(self):
        pattern = re.compile(r'RULES\.\w+\s*=[^=]|setattr\(\s*RULES|object\.__setattr__|Rules\.__dict__|\.max_risk_per_trade\s*=[^=]')
        for path in glob.glob(os.path.join(ROOT, '*.py')):
            if os.path.basename(path).startswith('test_'):
                continue
            with open(path, encoding='utf-8') as f:
                self.assertIsNone(pattern.search(f.read()), path)

    def test_tunable_never_includes_locked_rules(self):
        locked = {f.name for f in dataclasses.fields(Rules)}
        self.assertFalse(locked & set(TUNABLE))
        self.assertFalse({k for k in TUNABLE if k.startswith(('max_', 'daily', 'weekly', 'stop_loss'))})

    def test_aggression_dial_has_a_locked_ceiling(self):
        self.assertEqual(clamp_aggression(5), 1.0)
        self.assertEqual(clamp_aggression(-1), 0.0)
        self.assertEqual(clamp_aggression('x'), 0.0)
        self.assertEqual(clamp_aggression(None), 0.0)
        self.assertEqual(clamp_aggression(0.4), 0.4)
        self.assertEqual(TUNABLE['aggression'][1], RULES.max_aggression)


class SizingTests(unittest.TestCase):
    def test_risk_is_one_percent_at_most(self):
        qty = position_size(1000, 1000, 100, 95, 0, 0, 1.0)
        self.assertAlmostEqual(qty * (100 - 95), 10.0)  # loses 10 = 1% of 1000 if the stop hits

    def test_costs_are_inside_the_risk(self):
        fee, slip = 0.001, 0.0005
        qty = position_size(1000, 1000, 100, 95, fee, slip, 1.0)
        loss = qty * (100 * (1 + fee) - 95 * (1 - slip) * (1 - fee))
        self.assertAlmostEqual(loss, 10.0)

    def test_aggression_scales_risk_but_cannot_exceed_cap(self):
        half = position_size(1000, 1000, 100, 95, 0, 0, 0.5)
        full = position_size(1000, 1000, 100, 95, 0, 0, 1.0)
        self.assertAlmostEqual(half * 2, full)
        self.assertEqual(position_size(1000, 1000, 100, 95, 0, 0, 50), full)

    def test_position_size_and_cash_caps(self):
        self.assertAlmostEqual(position_size(1000, 1000, 100, 99.9, 0, 0, 1.0) * 100, 340)  # 34% of capital
        self.assertAlmostEqual(position_size(1000, 100, 100, 95, 0, 0, 1.0), 1.0)          # only 100 in cash

    def test_stop_loss_is_mandatory(self):
        for stop in (100, 101, 0, -5):
            self.assertEqual(position_size(1000, 1000, 100, stop, 0, 0, 1.0), 0.0)
        self.assertEqual(position_size(0, 0, 100, 95, 0, 0, 1.0), 0.0)


class EntryBlockTests(unittest.TestCase):
    @staticmethod
    def series(seed, n=200):
        import random
        rng, price, rows = random.Random(seed), 100., []
        for _ in range(n):
            price *= 1 + rng.gauss(0, 0.01)
            rows.append(dict(close=price))
        return rows

    def test_max_positions(self):
        hist = {s: self.series(i) for i, s in enumerate('ABCD')}
        self.assertIsNone(entry_block('D', {'A', 'B'}, hist))
        self.assertIn('Máximo', entry_block('D', {'A', 'B', 'C'}, hist))

    def test_correlated_coin_is_blocked(self):
        a = self.series(1)
        hist = {'A': a, 'B': [dict(close=r['close'] * 3) for r in a], 'C': self.series(99)}
        self.assertIn('Correlación', entry_block('B', {'A'}, hist))
        self.assertIsNone(entry_block('C', {'A'}, hist))

    def test_short_history_does_not_block(self):
        hist = {'A': self.series(1, 10), 'B': self.series(2, 10)}
        self.assertIsNone(entry_block('B', {'A'}, hist))


if __name__ == '__main__':
    unittest.main()
