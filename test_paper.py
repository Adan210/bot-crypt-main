import copy
import os
import tempfile
import unittest
from unittest import mock
import portfolio_bt as pb
from paper import Paper
from signals import Params
from test_portfolio_bt import frequent, market
from trader import Costs

SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
K = 340  # warmup candles


class PaperTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)  # registered first: runs after connections close
        self.path = os.path.join(d.name, 'bot.db')
        self.rows = pb.align({s: market(1200, i) for i, s in enumerate(SYMBOLS)})
        self.p = Params()
        patch = mock.patch('trader.signals.evaluate', frequent)
        patch.start()
        self.addCleanup(patch.stop)

    def open(self):
        paper = Paper(self.path)
        self.addCleanup(paper.close)
        return paper

    def start(self, paper, name='main', params=None, rows=None):
        rows = rows or self.rows
        paper.init(name, params or self.p, {s: rows[s][:K] for s in SYMBOLS})

    def feed(self, paper, i0, i1, name='main', **kw):
        return paper.process(name, {s: self.rows[s][i0:i1] for s in SYMBOLS}, **kw)

    def test_matches_the_backtest_exactly(self):
        paper = self.open()
        self.start(paper)
        self.feed(paper, K, 1200)
        reference = pb.run(self.rows, self.p, K, 1200)
        got = paper.trades('main')
        self.assertGreater(len(got), 10)
        self.assertEqual([round(t['pnl'], 9) for t in got], [round(t['pnl'], 9) for t in reference['trades']])
        self.assertEqual([t['symbol'] for t in got], [t['symbol'] for t in reference['trades']])
        self.assertAlmostEqual(paper.summary('main')['equity'], reference['state']['equity'], places=9)

    def test_duplicate_and_overlapping_candles_are_ignored(self):
        paper = self.open()
        self.start(paper)
        self.feed(paper, K, 700)
        before = paper.summary('main')
        self.assertEqual(self.feed(paper, K, 700), [])
        self.assertEqual(self.feed(paper, 0, 700), [])
        self.assertEqual(paper.summary('main'), before)
        count = paper.db.execute('SELECT COUNT(*) FROM candles').fetchone()[0]
        self.assertEqual(count, 700 * 3)

    def test_open_positions_and_orders_survive_a_restart(self):
        one = self.open()
        self.start(one)
        self.feed(one, K, 1200)
        split = next(i for i in range(K + 50, 1100) if self.positions_at(i))
        first = self.open()
        self.start(first)
        self.feed(first, K, split)
        held = first.summary('main')
        self.assertTrue(held['positions'])
        first.close()
        second = self.open_fresh()
        recovered = second.summary('main')
        self.assertEqual((recovered['positions'], recovered['pending'], recovered['equity']),
                         (held['positions'], held['pending'], held['equity']))
        self.feed(second, split, 1200)
        self.assertEqual(second.summary('main')['equity'], one.summary('main')['equity'])
        self.assertEqual(len(second.trades('main')), len(one.trades('main')))

    def open_fresh(self):
        paper = Paper(self.path)  # a brand new connection, as after restarting the program
        self.addCleanup(paper.close)
        return paper

    def positions_at(self, i):
        return pb.run(self.rows, self.p, K, i)['state']['positions']

    def test_invalid_batch_changes_nothing(self):
        paper = self.open()
        self.start(paper)
        before = (paper.summary('main'), paper.db.execute('SELECT COUNT(*) FROM candles').fetchone()[0])
        rows = {s: copy.deepcopy(self.rows[s][K:K + 10]) for s in SYMBOLS}
        rows['ETHUSDT'][5]['high'] = rows['ETHUSDT'][5]['low'] / 2  # impossible candle
        with self.assertRaises(ValueError):
            paper.process('main', rows)
        after = (paper.summary('main'), paper.db.execute('SELECT COUNT(*) FROM candles').fetchone()[0])
        self.assertEqual(before, after)

    def test_waits_for_a_slow_coin_and_skips_a_real_gap(self):
        paper = self.open()
        self.start(paper)
        rows = {s: self.rows[s][K:K + 5] for s in SYMBOLS}
        rows['SOLUSDT'] = rows['SOLUSDT'][:3]        # SOL has not published candles 4 and 5 yet
        paper.process('main', rows)
        self.assertEqual(paper._load('main')['last_time'], self.rows['BTCUSDT'][K + 2]['time'])
        paper.process('main', {'SOLUSDT': self.rows['SOLUSDT'][K + 3:K + 5]})  # it catches up
        self.assertEqual(paper._load('main')['last_time'], self.rows['BTCUSDT'][K + 4]['time'])
        gap = {s: self.rows[s][K + 5:K + 9] for s in SYMBOLS}
        del gap['SOLUSDT'][1]                        # SOL is missing one candle but has later ones: a gap, not a wait
        paper.process('main', gap)
        self.assertEqual(paper._load('main')['last_time'], self.rows['BTCUSDT'][K + 8]['time'])

    def test_manual_controls_are_persisted(self):
        paper = self.open()
        self.start(paper)
        split = next(i for i in range(K + 50, 1100) if self.positions_at(i))
        self.feed(paper, K, split)
        paper.emergency('main')
        s = paper.summary('main')
        self.assertEqual((s['positions'], s['pending'], s['halted']), ([], [], 'Emergencia'))
        self.assertIn('Emergencia', [t['reason_exit'] for t in paper.trades('main')])
        again = self.open_fresh()
        self.feed(again, split, 1200)
        self.assertEqual(again.summary('main')['halted'], 'Emergencia')     # still stopped after a restart
        self.assertEqual(again.summary('main')['positions'], [])            # and nothing new opened
        again.reactivate('main')
        again.mode('main', 'close_only')
        self.assertEqual((again.summary('main')['halted'], again.summary('main')['mode']), (None, 'close_only'))
        kinds = [e['kind'] for e in again.events('main', 10)]
        self.assertIn('manual_reactivate', kinds)

    def test_trade_log_has_reason_price_result_and_market_state(self):
        paper = self.open()
        self.start(paper)
        self.feed(paper, K, 900, ctx=dict(fear_greed=55, funding={}, news=None, calendar=None))
        trades = paper.trades('main')
        self.assertTrue(trades)
        t = trades[0]
        for key in ('reason_entry', 'reason_exit', 'entry', 'exit', 'pnl', 'r', 'regime'):
            self.assertIsNotNone(t[key])
        self.assertIn('atr_pct', t['context'])
        s = paper.summary('main')
        self.assertTrue(all(c['btc_close'] for c in s['curve']))
        self.assertIn('btc_same_exposure_pct', s)

    def test_context_is_used_only_for_the_latest_candle(self):
        paper = self.open()
        self.start(paper)
        events = self.feed(paper, K, 704, ctx=dict(calendar='FOMC'))  # candle 704 is a multiple of 11: signals fire
        vetoes = [e for e in events if e['kind'] == 'skip' and 'FOMC' in e.get('why', '')]
        self.assertTrue(vetoes)
        self.assertEqual({e['time'] for e in vetoes}, {self.rows['BTCUSDT'][703]['time']})

    def test_accounts_are_independent_and_share_candles(self):
        paper = self.open()
        self.start(paper, 'main')
        self.start(paper, 'candidate', Params(aggression=0.2))
        self.feed(paper, K, 800, 'main')
        self.feed(paper, K, 800, 'candidate')
        self.assertEqual(paper.accounts(), ['candidate', 'main'])
        self.assertEqual(paper.db.execute('SELECT COUNT(*) FROM candles').fetchone()[0], 800 * 3)
        main, cand = paper.summary('main'), paper.summary('candidate')
        self.assertNotEqual(main['equity'], cand['equity'])
        self.assertEqual(cand['params']['aggression'], 0.2)

    def test_init_validation_and_reset(self):
        paper = self.open()
        with self.assertRaises(ValueError):
            paper.init('x', self.p, {s: self.rows[s][:K] for s in ('ETHUSDT', 'SOLUSDT')})  # BTC is the reference
        with self.assertRaises(ValueError):
            paper.init('x', Params(aggression=5.0), {s: self.rows[s][:K] for s in SYMBOLS})  # above locked cap
        with self.assertRaises(ValueError):
            paper.process('nobody')
        self.assertEqual(paper.summary('nobody'), {'initialized': False})
        self.start(paper)
        self.feed(paper, K, 800)
        self.start(paper)  # reset wipes the previous run of that account
        s = paper.summary('main')
        self.assertEqual((s['trade_count'], s['equity'], s['curve']), (0, 1000.0, []))


if __name__ == '__main__':
    unittest.main()
