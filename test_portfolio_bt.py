import copy
import json
import math
import random
import unittest
from unittest import mock
import portfolio_bt as pb
from signals import Params
from trader import Costs

H = 3600000


def market(n, seed, price=100.0):
    rng, rows = random.Random(seed), []
    for i in range(n):
        o = price
        price *= math.exp(0.0004 * math.sin(i / 200) + rng.gauss(0, 0.007))
        rows.append(dict(time=(i + 1) * H, open=o, high=max(o, price) * 1.002, low=min(o, price) * 0.998,
                         close=price, volume=rng.uniform(5, 20)))
    return rows


def frequent(sym, rows, p):
    """Stand-in signal so tests always have trades: a candidate every 11th candle."""
    step = rows[-1]['time'] // H
    if step % 11:
        return None
    close = rows[-1]['close']
    return dict(symbol=sym, side='long', regime='tendencia_alcista', close=close, atr=close * 0.01,
                stop=close * 0.97, tp_price=close * 1.05, tp_fraction=0.5, trail_atr=2.0, score=step % 7,
                reason='señal de prueba')


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.rows = pb.align({s: market(2000, i) for i, s in enumerate(('BTCUSDT', 'ETHUSDT', 'SOLUSDT'))})
        self.p = Params()

    def run_with_signals(self, rows=None, i0=300, i1=1800):
        with mock.patch('trader.signals.evaluate', frequent):
            return pb.run(rows or self.rows, self.p, i0, i1)

    def test_align_keeps_only_common_times(self):
        a, b = market(100, 1), market(100, 2)
        del b[10]
        aligned = pb.align({'A': a, 'B': b})
        self.assertEqual(len(aligned['A']), 99)
        self.assertEqual([r['time'] for r in aligned['A']], [r['time'] for r in aligned['B']])

    def test_run_is_deterministic_and_trades(self):
        a, b = self.run_with_signals(), self.run_with_signals()
        self.assertGreater(len(a['trades']), 20)
        self.assertEqual((a['trades'], a['ratios']), (b['trades'], b['ratios']))
        self.assertEqual(len(a['ratios']), 1500)
        json.dumps(a['state'])  # the whole state stays serialisable

    def test_candles_after_the_window_do_not_matter(self):
        base = self.run_with_signals()
        changed = copy.deepcopy(self.rows)
        for rows in changed.values():
            for r in rows[1800:]:
                r['open'] *= 5
                r['close'] *= 0.1
                r['low'] *= 0.1
        other = self.run_with_signals(changed)
        self.assertEqual((base['trades'], base['ratios']), (other['trades'], other['ratios']))

    def test_locked_rules_hold_over_a_long_run(self):
        result = self.run_with_signals()
        self.assertLessEqual(result['state']['peak'] - 1000, 1e9)  # sanity: state present
        losses = [t['r'] for t in result['trades']]
        self.assertGreater(min(losses), -2.5)  # stop-outs near 1R; only inter-candle gaps can exceed it slightly
        for i0 in range(0, 1500, 150):  # never more than 3 positions open at once, sampled across the run
            r = self.run_with_signals(i0=300, i1=300 + i0 + 1)
            self.assertLessEqual(len(r['state']['positions']), 3)

    def test_summary_shape_and_exposure_floor(self):
        result = self.run_with_signals()
        s = pb.summarize(result, self.rows['BTCUSDT'], 300, 1800)
        self.assertEqual(set(s), {'bot', 'btc_same_exposure', 'btc_full', 'curve'})
        self.assertEqual(len(s['curve']['bot']), len(s['curve']['btc_full']))
        self.assertAlmostEqual(s['bot']['exposure_pct'], result['exposure'] * 100)
        empty = dict(trades=[], ratios=[1.0] * 1500, state=dict(fees=0., halted=None), exposure=0.0)
        flat = pb.summarize(empty, self.rows['BTCUSDT'], 300, 1800)
        self.assertEqual(flat['bot']['trades'], 0)
        self.assertAlmostEqual(flat['btc_same_exposure']['return_pct'], 0, places=2)  # ~0 exposure: ~0 return

    def test_windows_are_consecutive_and_independent(self):
        with mock.patch('trader.signals.evaluate', frequent):
            items = pb.windows(self.rows, self.p, 500, start=300, end=1800)
        self.assertEqual(len(items), 3)
        for a, b in zip(items, items[1:]):
            self.assertEqual(b['first'] - a['last'], H)

    def test_aggregate_chains_returns(self):
        with mock.patch('trader.signals.evaluate', frequent):
            items = pb.windows(self.rows, self.p, 500, start=300, end=1800)
        total = pb.aggregate(items)
        product = 1.0
        for w in items:
            product *= 1 + w['bot']['return_pct'] / 100
        self.assertAlmostEqual(total['bot']['return_pct'], (product - 1) * 100, places=6)
        self.assertEqual(total['bot']['trades'], sum(w['bot']['trades'] for w in items))

    def test_verdict_and_report(self):
        def s(ret, dd, trades, ref=2., ref_dd=1.):
            return dict(bot=dict(return_pct=ret, max_drawdown_pct=dd, trades=trades),
                        btc_same_exposure=dict(return_pct=ref, max_drawdown_pct=ref_dd))
        self.assertTrue(pb.verdict(s(5., .5, 50)).startswith('SUPERA'))
        self.assertIn('menos', pb.verdict(s(5., .5, 50)))
        self.assertTrue(pb.verdict(s(1., .5, 50)).startswith('NO SUPERA'))
        self.assertIn('NO CONCLUYENTE', pb.verdict(s(5., .5, 3)))
        with mock.patch('trader.signals.evaluate', frequent):
            items = pb.windows(self.rows, self.p, 500, start=300, end=1800)
        text = pb.report('Prueba', items, self.p)
        for needle in ('VEREDICTO', 'no ajustados', 'Bitcoin', 'Ventana 3', 'TOTAL encadenado'):
            self.assertIn(needle, text)

    def test_default_bot_never_trades_the_range_strategy(self):
        result = pb.run(self.rows, self.p, 300, 1800)
        self.assertTrue(all(t['regime'] == 'tendencia_alcista' for t in result['trades']))


if __name__ == '__main__':
    unittest.main()
