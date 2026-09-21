import dataclasses
import json
import math
import os
import tempfile
import unittest
from indicators import (atr, bollinger, correlation, efficiency_ratio, highest_high, returns, rsi, sma,
                        volatility, volume_ratio)
from signals import TUNABLE, Params, evaluate, min_history, regime
import extras

H = 3600000


def candles(closes, volume=10., spread=0.005, volumes=None):
    """Candles opening at the previous close; wicks extend `spread` beyond the body."""
    rows, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        rows.append(dict(time=(i + 1) * H, open=o, close=c, high=max(o, c) * (1 + spread),
                         low=min(o, c) * (1 - spread), volume=volumes[i] if volumes else volume))
        prev = c
    return rows


class IndicatorTests(unittest.TestCase):
    def test_basic_values(self):
        self.assertEqual(sma([1, 2, 3, 4], 2), 3.5)
        self.assertEqual(rsi(list(range(1, 30))), 100)
        self.assertEqual(rsi([5.] * 30), 50)
        self.assertEqual(rsi(list(range(30, 1, -1))), 0)
        self.assertAlmostEqual(volatility([100.] * 30), 0)

    def test_atr_and_gaps(self):
        rows = [dict(high=101., low=99., close=100.) for _ in range(20)]
        self.assertAlmostEqual(atr(rows), 2.0)
        rows.append(dict(high=111., low=109., close=110.))  # gap up: true range counts the gap
        self.assertAlmostEqual(atr(rows, 14), (13 * 2 + 11) / 14)

    def test_efficiency_ratio(self):
        self.assertAlmostEqual(efficiency_ratio([float(i) for i in range(60)], 48), 1.0)
        zigzag = [100 + (i % 2) for i in range(60)]
        self.assertLess(efficiency_ratio(zigzag, 48), 0.05)
        self.assertEqual(efficiency_ratio([7.] * 60, 48), 0.0)

    def test_bollinger_correlation_and_helpers(self):
        self.assertEqual(bollinger([5.] * 30), (5., 5., 5.))
        a = [1., 2., 3., 4., 5.]
        self.assertAlmostEqual(correlation(a, [2 * x for x in a]), 1.0)
        self.assertAlmostEqual(correlation(a, [-x for x in a]), -1.0)
        self.assertEqual(correlation(a, [3.] * 5), 0.0)
        self.assertEqual(returns([100., 110.]), [110 / 100 - 1])
        rows = candles([10, 11, 12, 30])
        self.assertEqual(highest_high(rows, 3), max(r['high'] for r in rows[:3]))  # excludes the last candle
        self.assertAlmostEqual(volume_ratio(candles([1.] * 30, volumes=[10.] * 29 + [30.])), 3.0)


class RegimeTests(unittest.TestCase):
    def test_regimes(self):
        p = Params()
        self.assertIsNone(regime(candles([100.] * 50), p))
        self.assertEqual(regime(candles([100 + 0.5 * i for i in range(200)]), p), 'tendencia_alcista')
        self.assertEqual(regime(candles([200 - 0.5 * i for i in range(200)]), p), 'tendencia_bajista')
        self.assertEqual(regime(candles([100 + 0.2 * (i % 2) for i in range(200)]), p), 'lateral')
        drift = [100 + 0.1 * i + 0.4 * (i % 2) for i in range(200)]  # in between
        self.assertEqual(regime(candles(drift), p), 'transicion')


class EntryTests(unittest.TestCase):
    def uptrend(self, jump, volume):
        closes = [100 + 0.5 * i for i in range(199)]
        closes.append(closes[-1] + jump)
        return candles(closes, volumes=[10.] * 199 + [volume])

    def test_breakout_with_volume(self):
        p = Params(rsi_max=100.1)  # isolate the breakout logic from RSI (a clean line has RSI 100)
        c = evaluate('BTCUSDT', self.uptrend(3, 30.), p)
        self.assertIsNotNone(c)
        self.assertEqual((c['symbol'], c['side'], c['regime']), ('BTCUSDT', 'long', 'tendencia_alcista'))
        self.assertLess(c['stop'], c['close'])
        self.assertGreater(c['tp_price'], c['close'])
        self.assertAlmostEqual(c['tp_price'] - c['close'], p.tp_r * (c['close'] - c['stop']))
        self.assertIn('Ruptura', c['reason'])

    def test_no_signal_without_volume_or_breakout(self):
        p = Params(rsi_max=100.1)
        self.assertIsNone(evaluate('BTCUSDT', self.uptrend(3, 10.), p))      # no volume confirmation
        self.assertIsNone(evaluate('BTCUSDT', self.uptrend(0.5, 30.), p))    # ordinary step, no breakout
        self.assertIsNone(evaluate('BTCUSDT', self.uptrend(3, 30.), Params()))  # RSI too hot for default params

    def test_oversold_dip_in_range(self):
        closes = [100 + 0.2 * (i % 2) for i in range(200)]
        closes[-1] = 98.0
        c = evaluate('ETHUSDT', candles(closes), Params(rsi_range=35, use_range=True))
        self.assertIsNotNone(c)
        self.assertEqual((c['regime'], c['tp_fraction'], c['trail_atr']), ('lateral', 1.0, 0.0))
        self.assertGreater(c['tp_price'] / c['close'] - 1, Params().min_edge)
        self.assertLess(c['stop'], c['close'])

    def test_no_dip_signal_when_target_too_close(self):
        closes = [100 + 0.2 * (i % 2) for i in range(200)]
        closes[-1] = 99.85  # barely below the band: not enough room to cover costs
        self.assertIsNone(evaluate('ETHUSDT', candles(closes), Params(rsi_range=100, min_edge=0.02, use_range=True)))

    def test_range_strategy_is_off_by_default(self):
        closes = [100 + 0.2 * (i % 2) for i in range(200)]
        closes[-1] = 98.0
        self.assertIsNone(evaluate('ETHUSDT', candles(closes), Params(rsi_range=35)))

    def test_stays_out_of_downtrend_and_transition(self):
        self.assertIsNone(evaluate('BTCUSDT', candles([200 - 0.5 * i for i in range(200)]), Params()))
        drift = [100 + 0.1 * i + 0.4 * (i % 2) for i in range(200)]
        self.assertIsNone(evaluate('BTCUSDT', candles(drift), Params()))

    def test_not_enough_history(self):
        self.assertIsNone(evaluate('BTCUSDT', candles([100.] * (min_history(Params()) - 1)), Params()))

    def test_future_candles_change_nothing(self):
        rows = self.uptrend(3, 30.)
        p = Params(rsi_max=100.1)
        before = evaluate('BTCUSDT', rows, p)
        rows.append(dict(rows[-1], close=1.0, high=1.0, low=1.0, time=10 ** 12))
        self.assertEqual(evaluate('BTCUSDT', rows[:-1], p), before)

    def test_selective_on_random_walk(self):
        import random
        rng, price, closes = random.Random(3), 100., []
        for _ in range(6000):
            price *= math.exp(rng.gauss(0, 0.006))
            closes.append(price)
        rows = candles(closes, volumes=[rng.uniform(5, 15) for _ in closes])
        p, hits = Params(), 0
        for i in range(400, len(rows)):
            hits += evaluate('X', rows[max(0, i - 319):i + 1], p) is not None
        self.assertLess(hits / (len(rows) - 400), 0.02)  # signals are rare: under 2% of candles


class ParamsTests(unittest.TestCase):
    def test_tunable_names_exist_and_defaults_are_inside(self):
        fields = {f.name for f in dataclasses.fields(Params)}
        for name, (lo, hi) in TUNABLE.items():
            self.assertIn(name, fields)
            self.assertLessEqual(lo, getattr(Params(), name))
            self.assertGreaterEqual(hi, getattr(Params(), name))

    def test_params_are_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Params().aggression = 1.0


class ExtrasTests(unittest.TestCase):
    RSS = ('<rss><channel><item><title>Bitcoin steady</title></item>'
           '<item><title>Major exchange HACK drains funds</title></item></channel></rss>')

    def test_parsers(self):
        self.assertEqual(extras.parse_fear_greed(json.dumps({'data': [{'value': '72'}]})), 72)
        self.assertEqual(extras.parse_funding(json.dumps({'lastFundingRate': '0.0001'})), 0.0001)
        titles = extras.parse_rss_titles(self.RSS)
        self.assertEqual(titles[0], 'Bitcoin steady')
        self.assertIn('HACK', extras.news_risk(titles))
        self.assertIsNone(extras.news_risk(['Bitcoin steady', 'ETF inflows']))

    def test_calendar_window_and_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'events.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump([dict(time='2026-09-24T18:00:00Z', name='FOMC', impact='high'),
                           dict(time='2026-09-24T12:00:00Z', name='Minor', impact='low')], f)
            events = extras.load_events(path)
            self.assertEqual([e['name'] for e in events], ['FOMC'])  # low impact ignored
            t = events[0]['ms']
            self.assertEqual(extras.blackout(events, t - 2 * H), 'FOMC')
            self.assertEqual(extras.blackout(events, t + H), 'FOMC')
            self.assertIsNone(extras.blackout(events, t - 2 * H - 1))
            self.assertIsNone(extras.blackout(events, t + H + 1))
            self.assertEqual(extras.load_events(os.path.join(d, 'missing.json')), [])

    def test_gather_fails_soft(self):
        def get(url):
            if 'alternative.me' in url:
                return json.dumps({'data': [{'value': '95'}]})
            if 'BTCUSDT' in url:
                return json.dumps({'lastFundingRate': '0.001'})
            raise OSError('blocked')
        ctx = extras.gather(['BTCUSDT', 'ETHUSDT'], 5, get=get)
        self.assertEqual((ctx['fear_greed'], ctx['funding'], ctx['news']), (95, {'BTCUSDT': 0.001}, None))

    def test_veto(self):
        self.assertIsNone(extras.veto('BTCUSDT', None))
        self.assertIsNone(extras.veto('BTCUSDT', dict(fear_greed=None, funding={}, news=None, calendar=None)))
        self.assertIn('FOMC', extras.veto('BTCUSDT', dict(calendar='FOMC')))
        self.assertIn('hack', extras.veto('BTCUSDT', dict(news='exchange hack')))
        self.assertIn('Euforia', extras.veto('BTCUSDT', dict(fear_greed=92)))
        self.assertIn('Funding', extras.veto('BTCUSDT', dict(funding={'BTCUSDT': 0.001})))
        self.assertIsNone(extras.veto('ETHUSDT', dict(funding={'BTCUSDT': 0.001})))  # per symbol


if __name__ == '__main__':
    unittest.main()
