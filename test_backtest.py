import copy
import math
import random
import unittest
from backtest import (MIN_TRADES, evaluate, curve_stats, format_report, hold_ratios, holdout, make_grid,
                      round_trips, select_config, trade_stats, verdict, walk_forward)
from engine import Config, simulate

H = 3600000


def synthetic(n, seed=1, price=100.0):
    """Random walk with slow trend cycles, so trends exist but nothing is guaranteed."""
    rng, rows = random.Random(seed), []
    for i in range(n):
        opening = price
        price *= math.exp(0.0003 * math.sin(i / 300) + rng.gauss(0, 0.006))
        rows.append(dict(time=(i + 1) * H, open=opening, close=price))
    return rows


def flat_rows(n, value=100.0):
    return [dict(time=(i + 1) * H, open=value, close=value) for i in range(n)]


class MetricsTests(unittest.TestCase):
    def test_round_trips_match_cash(self):
        cfg = Config()
        for n in (400, 500):
            sim = simulate(synthetic(n), cfg)
            trips, is_open = round_trips(sim['trades'])
            open_cost = 0.
            if is_open:
                last = sim['trades'][-1]
                open_cost = last['quantity'] * last['price'] + last['fee']
            self.assertAlmostEqual(sim['cash'] - cfg.capital, sum(t['pnl'] for t in trips) - open_cost)

    def test_trade_stats(self):
        trips = [dict(pnl=10, pnl_pct=10.), dict(pnl=20, pnl_pct=20.), dict(pnl=-5, pnl_pct=-5.), dict(pnl=-15, pnl_pct=-15.)]
        s = trade_stats(trips)
        self.assertEqual((s['trades'], s['win_rate_pct'], s['avg_win_pct'], s['avg_loss_pct'], s['payoff']),
                         (4, 50., 15., -10., 1.5))
        self.assertFalse(s['conclusive'])
        empty = trade_stats([])
        self.assertEqual((empty['trades'], empty['win_rate_pct'], empty['payoff']), (0, None, None))

    def test_curve_stats(self):
        s = curve_stats([1.1, 1.2, 0.9, 1.0])
        self.assertAlmostEqual(s['return_pct'], 0.)
        self.assertAlmostEqual(s['max_drawdown_pct'], 25.)

    def test_hold_uses_same_exposure(self):
        cfg = Config(fee=0, slippage=0, exposure=.5)
        rows = flat_rows(40)
        for r in rows[30:]:
            r['open'] = r['close'] = 200.
        ratios = hold_ratios(rows, 15, cfg)
        self.assertAlmostEqual(ratios[0], 1.0)
        self.assertAlmostEqual(ratios[-1], 1.5)  # half the capital doubled

    def test_hold_costs(self):
        cfg = Config(fee=.01, slippage=.01, exposure=1)
        ratios = hold_ratios(flat_rows(40), 15, cfg)
        self.assertAlmostEqual(ratios[-1], (1 - .01) ** 2 / (1 + .01) ** 2, places=9)  # both sides paid


class NoLookaheadTests(unittest.TestCase):
    def setUp(self):
        self.rows = synthetic(3000)
        self.base = Config()

    def test_needs_warmup(self):
        with self.assertRaises(ValueError):
            evaluate(self.rows, 10, 100, self.base)
        with self.assertRaises(ValueError):
            evaluate(self.rows, 100, 101, self.base)

    def test_candles_after_window_do_not_matter(self):
        a = evaluate(self.rows, 500, 1200, self.base)
        changed = copy.deepcopy(self.rows)
        for r in changed[1200:]:
            r['open'] *= 3
            r['close'] *= .2
        b = evaluate(changed, 500, 1200, self.base)
        self.assertEqual(a, b)

    def test_selection_ignores_test_data(self):
        grid = make_grid(self.base)
        chosen, _ = select_config(self.rows, 200, 1500, grid)
        changed = copy.deepcopy(self.rows)
        for r in changed[1500:]:
            r['close'] *= 5
        self.assertEqual(select_config(changed, 200, 1500, grid)[0], chosen)

    def test_grid_is_small_and_valid(self):
        grid = make_grid(self.base)
        self.assertEqual(len(grid), 9)
        for cfg in grid:
            cfg.validate()


class WalkForwardTests(unittest.TestCase):
    def setUp(self):
        self.rows = synthetic(3000)
        self.base = Config()

    def test_folds_are_ordered_and_do_not_overlap(self):
        result = walk_forward(self.rows, self.base, train_len=700, test_len=300)
        self.assertEqual(len(result['folds']), 7)
        for f in result['folds']:
            self.assertLess(f['train_span'][1], f['test_span'][0])
        for a, b in zip(result['folds'], result['folds'][1:]):
            self.assertEqual(b['test_span'][0] - a['test_span'][1], H)  # next test starts right after
        self.assertLessEqual(result['folds'][-1]['test_span'][1], self.rows[-1]['time'])

    def test_total_is_chained_product(self):
        result = walk_forward(self.rows, self.base, 700, 300)
        for key in ('strategy', 'hold'):
            product = 1.
            for f in result['folds']:
                product *= 1 + f['test'][key]['return_pct'] / 100
            self.assertAlmostEqual(result['total'][key]['return_pct'], (product - 1) * 100, places=6)
        self.assertEqual(result['total']['strategy']['trades'],
                         sum(f['test']['strategy']['trades'] for f in result['folds']))

    def test_deterministic(self):
        self.assertEqual(walk_forward(self.rows, self.base, 700, 300), walk_forward(self.rows, self.base, 700, 300))

    def test_choice_never_depends_on_its_own_test_window(self):
        full = walk_forward(self.rows, self.base, 700, 300)
        for k in (0, 2, 4):
            test_start = 200 + 700 + 300 * k  # first candle of fold k's test window
            changed = copy.deepcopy(self.rows)
            for r in changed[test_start:]:
                r['open'] *= 4
                r['close'] *= 4
            other = walk_forward(changed, self.base, 700, 300)
            for i in range(k + 1):  # folds up to k: training data untouched, so same choice
                self.assertEqual((other['folds'][i]['chosen'], other['folds'][i]['train']),
                                 (full['folds'][i]['chosen'], full['folds'][i]['train']))
            self.assertEqual(full['folds'][:k], other['folds'][:k])  # earlier folds fully identical

    def test_flat_market_stays_in_cash(self):
        result = walk_forward(flat_rows(2000), self.base, 700, 300)
        self.assertEqual(result['skipped_folds'], len(result['folds']))
        self.assertEqual(result['total']['strategy']['return_pct'], 0)
        self.assertEqual(result['total']['strategy']['trades'], 0)
        self.assertIn('NO CONCLUYENTE', verdict(result['total']))

    def test_too_short(self):
        with self.assertRaises(ValueError):
            walk_forward(self.rows[:500], self.base, 700, 300)

    def test_bitcoin_reference(self):
        btc = {r['time']: r for r in synthetic(3000, seed=7)}
        result = walk_forward(self.rows, self.base, 700, 300, btc_by_time=btc)
        self.assertIsNotNone(result['total']['btc'])
        missing = dict(btc)
        del missing[self.rows[1500]['time']]
        self.assertIsNone(walk_forward(self.rows, self.base, 700, 300, btc_by_time=missing)['total']['btc'])


class HoldoutAndReportTests(unittest.TestCase):
    def test_holdout_split(self):
        rows = synthetic(3000)
        result = holdout(rows, Config(), test_fraction=.3)
        self.assertLess(result['train_span'][1], result['test_span'][0])
        self.assertEqual(result['test_span'][1], rows[-1]['time'])
        with self.assertRaises(ValueError):
            holdout(rows[:150], Config())

    def test_verdict_wording(self):
        def summary(ret, dd, trades, hold_ret=5., hold_dd=10.):
            return dict(strategy=dict(return_pct=ret, max_drawdown_pct=dd, trades=trades, conclusive=trades >= MIN_TRADES),
                        hold=dict(return_pct=hold_ret, max_drawdown_pct=hold_dd))
        good = verdict(summary(8., 5., 50))
        self.assertTrue(good.startswith('SUPERA') and 'menos caída' in good and 'NO CONCLUYENTE' not in good)
        self.assertTrue(verdict(summary(2., 5., 50)).startswith('NO SUPERA'))
        self.assertIn('NO CONCLUYENTE', verdict(summary(8., 5., 3)))
        self.assertIn('más o igual', verdict(summary(8., 12., 50)))

    def test_report_text(self):
        rows = synthetic(3000)
        text = format_report('BTCUSDT', walk_forward(rows, Config(), 700, 300))
        self.assertIn('BTCUSDT', text)
        self.assertIn('VEREDICTO:', text)
        self.assertIn('Comprar y mantener', text)
        text = format_report('BTCUSDT', holdout(rows, Config()))
        self.assertIn('VEREDICTO:', text)


if __name__ == '__main__':
    unittest.main()
