import os
import tempfile
import unittest
import bot
from paper import Paper
from signals import Params
from test_data import FakeExchange, H, T0


class Exchange(FakeExchange):
    """FakeExchange whose candle count can grow, like a live market."""
    def grow(self, n_total):
        from test_data import kline
        have = {k[0] for k in self.data}
        self.data += [kline(i) for i in range(n_total) if T0 + i * H not in have]
        self.data.sort()


def now_after(n):
    """A clock reading just after candle n-1 has closed (candle n is still forming)."""
    return T0 + n * H + 60000


class BotTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = os.path.join(d.name, 'bot.db')
        self.ex = Exchange(500)

    def paper(self):
        paper = Paper(self.path)
        self.addCleanup(paper.close)
        return paper

    def test_closed_since_paginates_and_excludes_the_open_candle(self):
        rows = bot.closed_since('BTCUSDT', T0 - H, self.ex, now_after(500))
        self.assertEqual(len(rows), 500)
        self.assertEqual([r['time'] for r in rows], [T0 + i * H for i in range(500)])
        partial = bot.closed_since('BTCUSDT', T0 - H, self.ex, T0 + 300 * H + H // 2)  # candle 300 still forming
        self.assertEqual(len(partial), 300)
        self.assertEqual(bot.closed_since('BTCUSDT', T0 + 499 * H, self.ex, now_after(500)), [])
        long = Exchange(2600)
        self.assertEqual(len(bot.closed_since('BTCUSDT', T0 - H, long, now_after(2600))), 2600)  # several pages

    def test_start_uses_recent_candles_as_warmup_only(self):
        p = self.paper()
        bot.start(p, 'main', Params(), self.ex, now_ms=now_after(500))
        s = p.summary('main')
        self.assertEqual((s['trade_count'], s['equity'], s['positions']), (0, 1000.0, []))
        self.assertEqual(p.last_time('main'), T0 + 499 * H)
        self.assertEqual(p.symbols('main'), sorted(bot.SYMBOLS))

    def test_cycle_advances_without_duplicates(self):
        p = self.paper()
        bot.start(p, 'main', Params(), self.ex, now_ms=now_after(400))
        bot.cycle(p, 'main', self.ex, now_ms=now_after(450))
        self.assertEqual(p.last_time('main'), T0 + 449 * H)
        bot.cycle(p, 'main', self.ex, now_ms=now_after(450))  # nothing new
        self.assertEqual(p.last_time('main'), T0 + 449 * H)
        bot.cycle(p, 'main', self.ex, now_ms=now_after(500))
        self.assertEqual(p.last_time('main'), T0 + 499 * H)
        first, count = p.db.execute("SELECT MIN(time), COUNT(*) FROM candles WHERE symbol='BTCUSDT'").fetchone()
        self.assertEqual(count, (T0 + 499 * H - first) // H + 1)  # contiguous: no duplicates, no holes

    def test_loop_survives_errors_backs_off_and_recovers(self):
        p = self.paper()
        bot.start(p, 'main', Params(), self.ex, now_ms=now_after(400))
        p.close()
        calls, delays, errors = {'n': 0}, [], []

        def flaky(symbol, start, end, limit):
            calls['n'] += 1
            if calls['n'] <= 3:  # the first cycle fails on its very first request
                raise OSError('sin conexión')
            return self.ex(symbol, start, end, limit)

        rounds = {'n': 0}

        def stop():
            rounds['n'] += 1
            return rounds['n'] > 4

        bot.run_forever(lambda: Paper(self.path), ['main'], fetch=flaky, interval=300, sleep=delays.append, stop=stop,
                        on_error=errors.append)
        self.assertEqual(delays[:3], [10, 20, 40])   # growing backoff after three failures
        self.assertEqual(len(errors), 3)
        self.assertEqual(delays[3], 300)              # back to the normal interval once it works
        q = Paper(self.path)
        self.addCleanup(q.close)
        self.assertEqual(q.last_time('main'), T0 + 499 * H)  # and it caught up on everything it missed

    def test_backoff_is_capped(self):
        delays, rounds = [], {'n': 0}

        def stop():
            rounds['n'] += 1
            return rounds['n'] > 12

        def boom():
            raise RuntimeError('base de datos bloqueada')

        bot.run_forever(boom, ['main'], sleep=delays.append, stop=stop, max_backoff=900)
        self.assertEqual(max(delays), 900)
        self.assertEqual(delays[:4], [10, 20, 40, 80])

    def test_a_crash_in_the_middle_leaves_a_consistent_saved_state(self):
        p = self.paper()
        bot.start(p, 'main', Params(), self.ex, now_ms=now_after(400))
        before = p.summary('main')

        def bad_review(cands, ctx, events):
            raise RuntimeError('fallo del revisor')
        # Force a signal so the reviewer is reached, then crash inside the batch.
        import unittest.mock as mock
        cand = lambda sym, rows, params: dict(symbol=sym, side='long', regime='tendencia_alcista', close=100., atr=1.,
                                              stop=97., tp_price=105., tp_fraction=1., trail_atr=0., score=1., reason='x')
        with mock.patch('trader.signals.evaluate', cand):
            with self.assertRaises(RuntimeError):
                bot.cycle(p, 'main', self.ex, ctx=None, review=bad_review, now_ms=now_after(410))
        self.assertEqual(p.summary('main'), before)  # nothing half-applied
        bot.cycle(p, 'main', self.ex, now_ms=now_after(410))  # and the next healthy cycle simply continues
        self.assertEqual(p.last_time('main'), T0 + 409 * H)

    def test_loop_follows_a_candidate_account_when_it_appears(self):
        p = self.paper()
        bot.start(p, 'main', Params(), self.ex, now_ms=now_after(400))
        bot.start(p, 'candidate', Params(aggression=0.2), self.ex, now_ms=now_after(400))
        p.close()
        seen, rounds = [], {'n': 0}

        def stop():
            rounds['n'] += 1
            return rounds['n'] > 1
        bot.run_forever(lambda: Paper(self.path), lambda paper: [n for n in ('main', 'candidate') if n in paper.accounts()],
                        fetch=self.ex, sleep=lambda s: None, stop=stop, after_cycle=lambda paper: seen.append(paper.accounts()))
        self.assertEqual(seen, [['candidate', 'main']])
        q = Paper(self.path)
        self.addCleanup(q.close)
        self.assertEqual((q.last_time('main'), q.last_time('candidate')), (T0 + 499 * H,) * 2)

    def test_ai_is_off_without_a_key_and_on_with_one(self):
        import advisor as ai
        import review
        reviewer, proposer = bot.build_ai(self.path, {})
        self.assertIsNone(reviewer)
        self.assertIsInstance(proposer, review.RuleProposer)
        reviewer, proposer = bot.build_ai(self.path, {'GEMINI_API_KEY': 'k', 'ADVISOR_MAX_CALLS_DAY': '3'})
        self.addCleanup(reviewer.primary.limiter.close)
        self.assertIsInstance(reviewer, ai.SafeAdvisor)
        self.assertEqual(reviewer.primary.limiter.limits[0], 3)
        self.assertIsInstance(proposer, review.GeminiProposer)

    def test_describe_events(self):
        self.assertIn('COMPRA BTCUSDT', bot.describe(dict(kind='entry', symbol='BTCUSDT', price=100., stop=95., reason='r')))
        self.assertIn('DETENIDO', bot.describe(dict(kind='halt', why='Límite de pérdida diaria')))
        closed = bot.describe(dict(kind='trade', trade=dict(symbol='ETHUSDT', exit=50., pnl=-1.2, r=-1., reason_exit='Stop loss')))
        self.assertIn('-1.20', closed)


if __name__ == '__main__':
    unittest.main()
