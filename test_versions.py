import os
import tempfile
import unittest
from unittest import mock
import portfolio_bt as pb
import review
import versions as V
from paper import Paper
from signals import TUNABLE, Params
from test_paper import K, SYMBOLS
from test_portfolio_bt import frequent, market
from versions import DAY, Versions


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def days(self, n):
        self.now += n * DAY / 1000


def fake_windows(good=True, trades=40, dd_extra=0.0, n=4):
    """Stand-in for portfolio_bt.windows: the candidate (tp_r != 2.0) is better/worse than the base."""
    def windows(rows, params, size, **kw):
        candidate = params.tp_r != Params().tp_r
        gain = (1.0 if good else -1.0) if candidate else 0.0
        return [dict(bot=dict(return_pct=0.2 + gain, max_drawdown_pct=2.0 + (dd_extra if candidate else 0),
                              trades=trades // n), curve=None, trades_list=[None] * (trades // n)) for _ in range(n)]

    def aggregate(items):
        return dict(bot=dict(return_pct=sum(i['bot']['return_pct'] for i in items),
                             max_drawdown_pct=max(i['bot']['max_drawdown_pct'] for i in items),
                             trades=sum(len(i['trades_list']) for i in items)))
    return windows, aggregate


class VersionTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = os.path.join(d.name, 'v.db')
        self.clock = Clock()
        self.v = Versions(self.path, clock=self.clock)
        self.addCleanup(self.v.close)
        self.v.baseline(Params())

    def gate(self, **kw):
        windows, aggregate = fake_windows(**kw)
        patches = [mock.patch.object(pb, 'windows', windows), mock.patch.object(pb, 'aggregate', aggregate)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_locked_and_invalid_changes_are_rejected(self):
        current = Params()
        for bad in ({'max_risk_per_trade': 0.5}, {'daily_loss_limit': 0.9}, {'max_open_positions': 50},
                    {'stop_loss_required': False}, {'max_correlation': 1.0}, {'inventado': 1}, {'aggression': 1.5},
                    {'aggression': 0.0}, {'tp_r': float('nan')}, {'tp_r': True}, {'tp_r': 'alto'}, {'tp_r': 99.0},
                    {'tp_r': 2.5, 'trail_atr': 3.5}, {}, None):
            with self.assertRaises(ValueError, msg=str(bad)):
                Versions.check(current, bad)
        self.assertEqual(set(TUNABLE) & {'max_risk_per_trade', 'daily_loss_limit', 'weekly_loss_limit',
                                         'max_open_positions', 'max_correlation', 'stop_loss_required'}, set())

    def test_size_of_change_and_integer_params(self):
        self.assertEqual(Versions.check(Params(), {'tp_r': 2.2}), ('tp_r', 2.2, False))       # 0.2 of a 3.0 range
        self.assertEqual(Versions.check(Params(), {'tp_r': 3.5})[2], True)                    # 1.5 of 3.0: large
        self.assertEqual(Versions.check(Params(), {'donchian': 55.4}), ('donchian', 55, False))  # int param stays int

    def test_one_candidate_at_a_time_and_weekly_cooldown(self):
        first = self.v.propose({'tp_r': 2.2}, 'motivo', 'Gemini')
        with self.assertRaises(ValueError):
            self.v.propose({'trail_atr': 3.2}, 'otro', 'Gemini')       # one at a time
        self.v._set(first, status='rejected')
        with self.assertRaises(ValueError):
            self.v.propose({'trail_atr': 3.2}, 'otro', 'Gemini')       # cooldown: at most one per week
        self.clock.days(8)
        self.assertTrue(self.v.propose({'trail_atr': 3.2}, 'otro', 'Gemini'))
        with self.assertRaises(ValueError):
            self.v.propose({'trail_atr': 3.2}, 'x', 'Gemini')

    def test_same_value_and_missing_baseline(self):
        with self.assertRaises(ValueError):
            self.v.propose({'tp_r': Params().tp_r}, 'igual', 'Gemini')
        with tempfile.TemporaryDirectory() as d:
            empty = Versions(os.path.join(d, 'e.db'))
            try:
                with self.assertRaises(ValueError):
                    empty.propose({'tp_r': 2.5}, 'x', 'Gemini')
            finally:
                empty.close()

    def test_backtest_gate_passes_a_clearly_better_candidate(self):
        self.gate(good=True)
        vid = self.v.propose({'tp_r': 2.5}, 'mejor', 'Gemini')
        evidence = self.v.evaluate(vid, rows={})
        self.assertTrue(all(evidence['checks'].values()))
        self.assertEqual(self.v.get(vid)['status'], 'backtest_ok')

    def test_backtest_gate_rejects_each_kind_of_weakness(self):
        cases = [dict(good=False), dict(good=True, trades=8), dict(good=True, dd_extra=3.0)]
        expected = ['real_improvement', 'enough_trades', 'drawdown_ok']
        for kw, failed in zip(cases, expected):
            self.gate(**kw)
            self.clock.days(8)
            vid = self.v.propose({'tp_r': 2.5}, 'x', 'Gemini')
            evidence = self.v.evaluate(vid, rows={})
            self.assertFalse(evidence['checks'][failed], (kw, evidence['checks']))
            self.assertEqual(self.v.get(vid)['status'], 'rejected')
            mock.patch.stopall()

    def test_only_backtested_versions_go_to_paper(self):
        vid = self.v.propose({'tp_r': 2.5}, 'x', 'Gemini')
        with self.assertRaises(ValueError):
            self.v.start_paper(vid, lambda p, label: None)
        self.gate(good=True)
        self.v.evaluate(vid, {})
        started = []
        self.v.start_paper(vid, lambda p, label: started.append((p.tp_r, label)))
        self.assertEqual((started, self.v.get(vid)['status']), ([(2.5, f'v{vid}')], 'paper_testing'))


class PromotionTests(unittest.TestCase):
    """Paper stage, approval, promotion and rollback on real paper accounts (returns are mocked to steer)."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = os.path.join(d.name, 'bot.db')
        self.clock = Clock()
        self.rows = pb.align({s: market(1200, i) for i, s in enumerate(SYMBOLS)})
        patch = mock.patch('trader.signals.evaluate', frequent)
        patch.start()
        self.addCleanup(patch.stop)
        self.paper = Paper(self.path)
        self.v = Versions(self.path, clock=self.clock)
        self.addCleanup(self.paper.close)
        self.addCleanup(self.v.close)
        self.v.baseline(Params())
        self.paper.init('main', Params(), {s: self.rows[s][:K] for s in SYMBOLS})

    def candidate_in_paper(self, change, days=True):
        vid = self.v.propose(change, 'motivo', 'Gemini')
        self.v._set(vid, status='backtest_ok')
        self.v.start_paper(vid, lambda params, label: self.paper.init('candidate', params, {s: self.rows[s][:K] for s in SYMBOLS}, version=label))
        end = 1200 if days else K + 100
        for name in ('main', 'candidate'):
            self.paper.process(name, {s: self.rows[s][K:end] for s in SYMBOLS})
        return vid

    def steer(self, cand=(1.0, 2.0), main=(0.5, 2.0)):
        table = {'candidate': cand, 'main': main}
        return mock.patch.object(self.paper, 'return_since', side_effect=lambda name, t: table[name])

    def test_paper_needs_time_and_trades(self):
        vid = self.candidate_in_paper({'tp_r': 2.2}, days=False)
        self.assertEqual(self.v.check_paper(vid, self.paper), 'paper_testing')   # only ~4 days: not enough evidence
        self.assertEqual(self.v.get(vid)['status'], 'paper_testing')

    def test_small_change_that_holds_up_becomes_ready_and_promotes(self):
        vid = self.candidate_in_paper({'tp_r': 2.2})
        with self.steer():
            self.assertEqual(self.v.check_paper(vid, self.paper), 'ready')
        self.v.promote(vid, self.paper)
        self.assertEqual(self.v.active()['id'], vid)
        self.assertEqual(self.paper.params('main').tp_r, 2.2)
        self.assertEqual(self.paper.summary('main')['version'], f'v{vid}')
        self.assertNotIn('candidate', self.paper.accounts())
        self.assertEqual(self.v.get(1)['status'], 'retired')

    def test_worse_candidate_is_rejected_after_enough_time(self):
        vid = self.candidate_in_paper({'tp_r': 2.2})
        with self.steer(cand=(-3.0, 2.0), main=(1.0, 2.0)):
            status = self.v.check_paper(vid, self.paper)
        self.assertEqual(status, 'rejected')
        self.assertNotIn('candidate', self.paper.accounts())
        self.assertEqual(self.v.active()['id'], 1)

    def test_large_change_needs_explicit_approval(self):
        vid = self.candidate_in_paper({'tp_r': 3.8})
        self.assertTrue(self.v.get(vid)['large'])
        with self.steer():
            self.assertEqual(self.v.check_paper(vid, self.paper), 'ready')
        with self.assertRaises(PermissionError):
            self.v.promote(vid, self.paper)
        self.assertEqual(self.v.active()['id'], 1)           # nothing changed without the human
        self.v.approve(vid)
        self.v.promote(vid, self.paper)
        self.assertEqual(self.paper.params('main').tp_r, 3.8)

    def test_rollback_restores_the_previous_version(self):
        vid = self.candidate_in_paper({'tp_r': 2.2})
        with self.steer():
            self.v.check_paper(vid, self.paper)
        self.v.promote(vid, self.paper)
        self.v.rollback(self.paper)
        self.assertEqual((self.v.active()['id'], self.paper.params('main').tp_r), (1, Params().tp_r))
        self.assertEqual(self.v.get(vid)['status'], 'rolled_back')
        with self.assertRaises(ValueError):
            self.v.rollback(self.paper)   # already at the first version

    def test_cannot_promote_unfinished_versions(self):
        vid = self.candidate_in_paper({'tp_r': 2.2}, days=False)
        with self.assertRaises(ValueError):
            self.v.promote(vid, self.paper)

    def test_every_version_is_kept(self):
        self.candidate_in_paper({'tp_r': 2.2})
        self.assertEqual([x['status'] for x in self.v.all()], ['active', 'paper_testing'])
        self.assertEqual(self.v.get(2)['parent'], 1)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = os.path.join(d.name, 'bot.db')
        self.clock = Clock()
        self.rows = pb.align({s: market(1200, i) for i, s in enumerate(SYMBOLS)})
        patch = mock.patch('trader.signals.evaluate', frequent)
        patch.start()
        self.addCleanup(patch.stop)
        self.paper, self.v = Paper(self.path), Versions(self.path, clock=self.clock)
        self.addCleanup(self.paper.close)
        self.addCleanup(self.v.close)
        self.paper.init('main', Params(), {s: self.rows[s][:K] for s in SYMBOLS})
        self.paper.process('main', {s: self.rows[s][K:1200] for s in SYMBOLS})
        self.now = self.rows['BTCUSDT'][1199]['time']
        self.started = []

    def run_review(self, proposer, now=None):
        return review.weekly_review(self.paper, self.v, proposer, lambda: {}, lambda p, label: self.started.append((p, label))
                                    or self.paper.init('candidate', p, {s: self.rows[s][:K] for s in SYMBOLS}, version=label),
                                    now or self.now)

    class Proposer:
        name = 'Gemini'

        def __init__(self, proposals=None, error=None):
            self.proposals, self.error, self.calls = proposals or [], error, 0

        def propose(self, stats, params):
            self.calls += 1
            if self.error:
                raise self.error
            return self.proposals

    def test_stats_shape(self):
        stats = review.week_stats(self.paper, 'main', self.now)
        self.assertGreater(stats['all_time']['trades'], 10)
        self.assertEqual(set(stats), {'week', 'all_time', 'by_regime', 'by_exit', 'return_pct', 'drawdown_pct',
                                      'btc_return_pct', 'exposure_pct', 'days', 'halted'})
        self.assertLessEqual(stats['week']['trades'], stats['all_time']['trades'])

    def test_too_few_trades_means_no_conclusions_and_no_ai_call(self):
        proposer = self.Proposer([dict(param='tp_r', value=2.5, reason='x')])
        outcome, _ = self.run_review(proposer, now=self.now + 400 * DAY)   # a week with zero trades
        self.assertIn('Muestra insuficiente', outcome)
        self.assertEqual(proposer.calls, 0)
        self.assertIsNone(self.v.candidate())

    def test_plan_b_and_ai_failures_change_nothing(self):
        outcome, _ = self.run_review(review.RuleProposer(), now=self.now)
        self.assertEqual(outcome, 'Sin propuestas.')
        outcome, _ = self.run_review(self.Proposer(error=TimeoutError('lento')))
        self.assertIn('no respondió', outcome)
        self.assertIsNone(self.v.candidate())
        self.assertEqual([x['status'] for x in self.v.all()], ['active'])

    def test_locked_rule_proposals_are_discarded(self):
        outcome, _ = self.run_review(self.Proposer([dict(param='max_risk_per_trade', value=0.05, reason='más ganancia')]))
        self.assertIn('descartada', outcome)
        self.assertEqual([x['status'] for x in self.v.all()], ['active'])

    def test_good_proposal_goes_to_paper_then_promotes_by_itself_if_small(self):
        windows, aggregate = fake_windows(good=True)
        with mock.patch.object(pb, 'windows', windows), mock.patch.object(pb, 'aggregate', aggregate):
            outcome, _ = self.run_review(self.Proposer([dict(param='tp_r', value=2.2, reason='objetivo más lejano')]))
        self.assertIn('paper trading', outcome)
        self.assertEqual(self.v.candidate()['status'], 'paper_testing')
        self.assertEqual(self.started[0][1], 'v2')
        for name in ('candidate',):
            self.paper.process(name, {s: self.rows[s][K:1200] for s in SYMBOLS})
        with mock.patch.object(self.paper, 'return_since', side_effect=lambda n, t: {'candidate': (2.0, 1.0), 'main': (1.0, 1.0)}[n]):
            outcome, _ = self.run_review(self.Proposer(), now=self.now + 1)
        self.assertIn('aplicado', outcome)
        self.assertEqual(self.paper.params('main').tp_r, 2.2)

    def test_large_change_waits_for_the_human(self):
        windows, aggregate = fake_windows(good=True)
        with mock.patch.object(pb, 'windows', windows), mock.patch.object(pb, 'aggregate', aggregate):
            self.run_review(self.Proposer([dict(param='tp_r', value=3.8, reason='x')]))
        self.paper.process('candidate', {s: self.rows[s][K:1200] for s in SYMBOLS})
        with mock.patch.object(self.paper, 'return_since', side_effect=lambda n, t: {'candidate': (2.0, 1.0), 'main': (1.0, 1.0)}[n]):
            outcome, _ = self.run_review(self.Proposer(), now=self.now + 1)
        self.assertIn('aprobación', outcome)
        self.assertEqual(self.paper.params('main').tp_r, Params().tp_r)

    def test_due_after_a_week(self):
        self.assertTrue(review.due(self.v, self.now))
        self.run_review(review.RuleProposer())
        self.assertFalse(review.due(self.v, self.now + 6 * DAY))
        self.assertTrue(review.due(self.v, self.now + 7 * DAY))

    def test_Gemini_proposer_parses_and_sends_no_secrets(self):
        sent = []

        class Ask:
            def ask(self, prompt):
                sent.append(prompt)
                return 'claro:\n{"proposals":[{"param":"tp_r","value":2.5,"reason":"r"}]}'
        stats = review.week_stats(self.paper, 'main', self.now)
        got = review.GeminiProposer(Ask()).propose(stats, Params())
        self.assertEqual(got, [dict(param='tp_r', value=2.5, reason='r')])
        self.assertNotIn('max_risk_per_trade', sent[0].split('Allowed parameters:')[1].split('.')[0])  # locked rules not offered


if __name__ == '__main__':
    unittest.main()
