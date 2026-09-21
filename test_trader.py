import copy
import json
import random
import unittest
from unittest import mock
from signals import Params
from trader import DAY, Costs, emergency, new_state, reactivate, set_mode, step

H = 3600000
FREE = Costs(fee=0, slippage=0)
P = Params(aggression=1.0)


def bar(i, o, h, l, c, sym='A'):
    return {sym: dict(time=(i + 1) * H, open=o, high=h, low=l, close=c, volume=10.)}


def order(sym='A', stop=95., tp=110., frac=.5, trail=0., atr=2.):
    return dict(symbol=sym, stop=stop, tp_price=tp, tp_fraction=frac, trail_atr=trail, atr=atr,
                reason='prueba', regime='tendencia_alcista', decided=0, context={})


def opened(o=100, **kw):
    """State holding a position bought at `o` with the default order (qty 2, risk 10 of 1000)."""
    state = new_state(1000)
    state['pending'].append(order(**kw))
    events = step(state, bar(0, o, o, o, o), {}, P, FREE)
    return state, events


def trades(events):
    return [e['trade'] for e in events if e['kind'] == 'trade']


class FillAndStopTests(unittest.TestCase):
    def test_entry_fills_at_open_sized_by_risk(self):
        state, events = opened()
        pos = state['positions']['A']
        self.assertAlmostEqual(pos['qty'], 2.0)            # risk 10 / (100 - 95)
        self.assertAlmostEqual(state['cash'], 800.0)
        self.assertEqual([e['kind'] for e in events], ['entry'])
        self.assertEqual(state['pending'], [])

    def test_stop_loss_loses_exactly_the_planned_risk(self):
        state, _ = opened()
        events = step(state, bar(1, 100, 101, 94, 96), {}, P, FREE)
        (t,) = trades(events)
        self.assertEqual((t['reason_exit'], round(t['pnl'], 9), round(t['r'], 9)), ('Stop loss', -10.0, -1.0))
        self.assertAlmostEqual(state['equity'], 990.0)
        self.assertEqual(state['positions'], {})

    def test_gap_through_stop_fills_at_open_and_loses_more(self):
        state, _ = opened()
        (t,) = trades(step(state, bar(1, 90, 91, 89, 90), {}, P, FREE))
        self.assertAlmostEqual(t['exit'], 90)
        self.assertAlmostEqual(t['r'], -2.0)

    def test_stop_wins_when_candle_also_touches_target(self):
        state, _ = opened()
        (t,) = trades(step(state, bar(1, 100, 111, 94, 100), {}, P, FREE))
        self.assertEqual(t['reason_exit'], 'Stop loss')

    def test_entry_on_same_candle_can_be_stopped(self):
        state = new_state(1000)
        state['pending'].append(order())
        events = step(state, bar(0, 100, 101, 94, 96), {}, P, FREE)
        self.assertEqual([e['kind'] for e in events], ['entry', 'trade'])

    def test_costs_are_charged_on_both_sides(self):
        costs = Costs(fee=0.001, slippage=0.0005)
        state = new_state(1000)
        state['pending'].append(order())
        step(state, bar(0, 100, 100, 100, 100), {}, P, costs)
        (t,) = trades(step(state, bar(1, 100, 100, 90, 92), {}, P, costs))
        self.assertAlmostEqual(t['pnl'], -10.0, places=6)  # planned loss already includes the costs
        self.assertGreater(state['fees'], 0)

    def test_equity_is_net_liquidation_value(self):
        costs = Costs(fee=0.001, slippage=0.0005)
        state = new_state(1000)
        state['pending'].append(order())
        step(state, bar(0, 100, 100, 100, 100), {}, P, costs)
        pos = state['positions']['A']
        self.assertAlmostEqual(state['equity'], state['cash'] + pos['qty'] * 100 * (1 - .0005) * (1 - .001))


class TargetAndTrailingTests(unittest.TestCase):
    def test_partial_target_then_breakeven_stop(self):
        state, _ = opened()
        events = step(state, bar(1, 105, 111, 104, 108), {}, P, FREE)
        self.assertEqual([e['kind'] for e in events], ['partial'])
        pos = state['positions']['A']
        self.assertEqual((pos['qty'], pos['tp_done'], pos['stop']), (1.0, True, 100.0))  # half sold, rest risk-free
        (t,) = trades(step(state, bar(2, 108, 109, 99, 101), {}, P, FREE))
        self.assertEqual(t['reason_exit'], 'Stop de protección')
        self.assertAlmostEqual(t['pnl'], 10.0)   # (110-100) on half, 0 on the rest
        self.assertAlmostEqual(t['r'], 1.0)

    def test_full_target(self):
        state, _ = opened(frac=1.0)
        (t,) = trades(step(state, bar(1, 105, 111, 104, 108), {}, P, FREE))
        self.assertEqual((t['reason_exit'], round(t['pnl'], 9)), ('Objetivo alcanzado', 20.0))

    def test_gap_above_target_fills_at_open(self):
        state, _ = opened(frac=1.0)
        (t,) = trades(step(state, bar(1, 115, 116, 114, 115), {}, P, FREE))
        self.assertAlmostEqual(t['exit'], 115)

    def test_trailing_stop_only_rises_and_takes_effect_next_candle(self):
        state, _ = opened(tp=500., trail=1.0, atr=2.0)  # trail = highest close - 2
        step(state, bar(1, 100, 121, 100, 120), {}, P, FREE)
        self.assertAlmostEqual(state['positions']['A']['stop'], 118.0)
        step(state, bar(2, 120, 121, 119, 119), {}, P, FREE)   # lower close: stop must not fall
        self.assertAlmostEqual(state['positions']['A']['stop'], 118.0)
        (t,) = trades(step(state, bar(3, 119, 119, 117, 117), {}, P, FREE))
        self.assertEqual((t['reason_exit'], t['exit']), ('Stop de protección', 118))


class LimitsAndControlsTests(unittest.TestCase):
    def holding(self):
        """1000 equity: 900 cash + 1 unit bought at 100, in a state whose day/week already started."""
        state = new_state(1000)
        state['cash'] = 900.
        state['positions']['A'] = dict(symbol='A', qty0=1., qty=1., entry=100., stop=1., stop0=1., tp_price=1e9,
                                       tp_fraction=1., tp_done=False, trail_atr=0., atr=1., highest=100., opened=0,
                                       cost=100., proceeds=0., last_close=100., risk=99., reason='x', regime='r', context={})
        state['day'], state['week'] = (H * 100) // DAY, ((H * 100) // DAY + 3) // 7
        return state

    def test_daily_loss_halts_and_cancels_pending(self):
        state = self.holding()
        state['pending'].append(order('B'))
        events = step(state, bar(99, 100, 100, 55, 60), {}, P, FREE)  # equity 960: -4%
        self.assertEqual(state['halted'], 'Límite de pérdida diaria')
        self.assertIn('halt', [e['kind'] for e in events])
        self.assertEqual(state['pending'], [])

    def test_weekly_loss_halts(self):
        state = self.holding()
        state['day_start'], state['week_start'] = 950., 1000.
        state['positions']['A']['last_close'] = 100
        step(state, bar(99, 100, 100, 35, 35), {}, P, FREE)  # equity 935: -1.6% today, -6.5% this week
        self.assertEqual(state['halted'], 'Límite de pérdida semanal')

    def test_halted_bot_opens_nothing_until_reactivated(self):
        state = self.holding()
        step(state, bar(99, 100, 100, 55, 60), {}, P, FREE)
        state['pending'].append(order('B'))  # even a leftover order must not open
        events = step(state, {**bar(100, 60, 60, 60, 60), **bar(100, 50, 50, 50, 50, 'B')}, {}, P, FREE)
        self.assertNotIn('B', state['positions'])
        self.assertIn('skip', [e['kind'] for e in events])
        reactivate(state)
        self.assertIsNone(state['halted'])
        self.assertEqual(state['day_start'], state['equity'])

    def test_positions_keep_their_stops_while_halted(self):
        state = self.holding()
        state['positions']['A']['stop'] = state['positions']['A']['stop0'] = 50.
        step(state, bar(99, 100, 100, 55, 60), {}, P, FREE)
        self.assertIsNotNone(state['halted'])
        (t,) = trades(step(state, bar(100, 60, 60, 40, 45), {}, P, FREE))
        self.assertEqual(t['reason_exit'], 'Stop loss')

    def test_close_only_mode(self):
        state, _ = opened()
        state['pending'].append(order('B'))
        set_mode(state, 'close_only')
        self.assertEqual(state['pending'], [])
        with mock.patch('trader.signals.evaluate', return_value=dict(symbol='B', stop=1, tp_price=9, tp_fraction=1,
                                                                    trail_atr=0, atr=1, reason='x', regime='r',
                                                                    close=5, score=1)):
            step(state, {**bar(1, 100, 101, 99, 100), **bar(1, 5, 5, 5, 5, 'B')}, {'B': [], 'A': []}, P, FREE)
        self.assertEqual(state['pending'], [])
        with self.assertRaises(ValueError):
            set_mode(state, 'yolo')

    def test_emergency_closes_everything_and_halts(self):
        state, _ = opened()
        step(state, bar(1, 100, 104, 99, 103), {}, P, FREE)
        state['pending'].append(order('B'))
        events = emergency(state, 3 * H, FREE)
        self.assertEqual(state['positions'], {})
        self.assertEqual((state['halted'], state['pending']), ('Emergencia', []))
        self.assertEqual(trades(events)[0]['reason_exit'], 'Emergencia')
        self.assertAlmostEqual(state['equity'], state['cash'])


def walk(seed, n=400):
    rng, price, rows = random.Random(seed), 100., []
    for i in range(n):
        o, price = price, price * (1 + rng.gauss(0, 0.01))
        rows.append(dict(time=(i + 1) * H, open=o, high=max(o, price), low=min(o, price), close=price, volume=10.))
    return rows


def fake_candidate(symbol, score):
    return dict(symbol=symbol, side='long', regime='lateral', close=100., atr=1., stop=97., tp_price=105.,
                tp_fraction=1., trail_atr=0., score=score, reason=f'señal {symbol}')


class SignalFlowTests(unittest.TestCase):
    def setUp(self):
        self.rows = {s: walk(i) for i, s in enumerate('ABCD')}
        self.rows['E'] = [dict(r, open=r['open'] * 2, high=r['high'] * 2, low=r['low'] * 2, close=r['close'] * 2)
                          for r in self.rows['A']]  # E is a perfect copy of A: correlation 1

    def run_step(self, scores, ctx=None, review=None, symbols='ABCD', state=None):
        state = state or new_state(1000)
        i = 300
        candles = {s: self.rows[s][i] for s in symbols}
        hist = {s: self.rows[s][:i + 1] for s in symbols}
        evaluate = lambda sym, rows, p: fake_candidate(sym, scores[sym]) if sym in scores else None
        with mock.patch('trader.signals.evaluate', evaluate):
            events = step(state, candles, hist, P, FREE, ctx=ctx, review=review)
        return state, events

    def test_max_open_positions_and_best_score_first(self):
        state, events = self.run_step(dict(A=1, B=4, C=3, D=2))
        self.assertEqual([o['symbol'] for o in state['pending']], ['B', 'C', 'D'])
        self.assertTrue(any('Máximo' in e.get('why', '') for e in events))

    def test_correlated_coin_is_skipped(self):
        state, events = self.run_step(dict(A=5, E=4), symbols='AE')
        self.assertEqual([o['symbol'] for o in state['pending']], ['A'])
        self.assertTrue(any('Correlación' in e.get('why', '') for e in events))

    def test_veto_blocks_entry_and_is_recorded(self):
        state, events = self.run_step(dict(A=1), ctx=dict(calendar='FOMC'))
        self.assertEqual(state['pending'], [])
        self.assertTrue(any('FOMC' in e.get('why', '') for e in events))

    def test_reviewer_can_remove_but_not_add_or_alter(self):
        def reviewer(cands, ctx, events):
            forged = dict(cands[0], stop=1e-9, tp_price=1e9, symbol=cands[0]['symbol'])
            return [forged, fake_candidate('D', 99.)]  # alters a stop and tries to add a coin it was not offered
        state, _ = self.run_step(dict(A=3, B=2), review=reviewer)
        self.assertEqual([o['symbol'] for o in state['pending']], ['A'])
        self.assertEqual(state['pending'][0]['stop'], 97.)  # the original, untouched
        state, _ = self.run_step(dict(A=3, B=2), review=lambda c, x, e: [])
        self.assertEqual(state['pending'], [])

    def test_no_new_order_for_held_or_pending_symbol(self):
        state, _ = self.run_step(dict(A=1))
        state, _ = self.run_step(dict(A=1), state=state)
        self.assertEqual(len(state['pending']), 1)

    def test_state_survives_a_json_round_trip(self):
        state, _ = self.run_step(dict(A=3, B=2))
        restored = json.loads(json.dumps(state))
        self.assertEqual(restored, state)
        a, b = copy.deepcopy(state), restored
        for st in (a, b):
            step(st, {s: self.rows[s][301] for s in 'AB'}, {s: self.rows[s][:302] for s in 'AB'}, P, Costs())
        self.assertEqual(a, b)

    def test_a_stop_out_never_loses_more_than_the_planned_risk_without_gaps(self):
        state, worst_r, closed = new_state(1000), 0., 0
        pick = lambda sym, rows, p: fake_candidate(sym, 1) if len(rows) % 7 == 0 else None
        with mock.patch('trader.signals.evaluate', pick):
            for i in range(30, 399):
                events = step(state, {s: self.rows[s][i] for s in 'ABCD'}, {s: self.rows[s][:i + 1] for s in 'ABCD'}, P, Costs())
                for t in trades(events):
                    closed += 1
                    worst_r = min(worst_r, t['r'])
        self.assertGreater(closed, 10)               # the run really traded
        self.assertLess(worst_r, -0.95)              # and really hit stops
        self.assertGreater(worst_r, -1.001)          # yet no stop-out lost more than 1R (= 1% of equity at most)


if __name__ == '__main__':
    unittest.main()
