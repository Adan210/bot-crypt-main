import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock
import env
from advisor import (DEFAULT_MODEL, ClaudeAdvisor, RuleAdvisor, SafeAdvisor, UsageLimiter, cost_usd, DAY)

KEY = 'sk-ant-SECRET-123'


def cand(symbol, score=1.0):
    return dict(symbol=symbol, side='long', regime='tendencia_alcista', close=100., atr=1., stop=97., tp_price=105.,
                tp_fraction=.5, trail_atr=3., score=score, reason=f'Ruptura en {symbol}')


def reply(decisions, tokens=(500, 100), wrap=''):
    text = wrap + json.dumps(dict(decisions=decisions)) + wrap
    return dict(content=[dict(type='text', text=text)], usage=dict(input_tokens=tokens[0], output_tokens=tokens[1]))


class Clock:
    def __init__(self, day=20000):
        self.now = day * DAY / 1000

    def __call__(self):
        return self.now


class LimiterTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = os.path.join(d.name, 'usage.db')
        self.clock = Clock()

    def limiter(self, **kw):
        limiter = UsageLimiter(self.path, clock=self.clock, **kw)
        self.addCleanup(limiter.close)
        return limiter

    def test_cost_estimate(self):
        self.assertAlmostEqual(cost_usd(DEFAULT_MODEL, 1_000_000, 1_000_000), 6.0)
        self.assertAlmostEqual(cost_usd('modelo-desconocido', 0, 1_000_000), 75.0)  # unknown = most expensive known

    def test_daily_call_cap_and_next_day_reset(self):
        limiter = self.limiter(max_calls_day=2)
        self.assertIsNone(limiter.allow())
        limiter.record(0.01)
        limiter.record(0.01)
        self.assertIn('llamadas', limiter.allow())
        self.clock.now += DAY / 1000
        self.assertIsNone(limiter.allow())

    def test_daily_and_monthly_cost_caps(self):
        limiter = self.limiter(max_usd_day=0.10, max_usd_month=0.15)
        limiter.record(0.11)
        self.assertIn('costo', limiter.allow())
        self.clock.now += DAY / 1000
        self.assertIsNone(limiter.allow())  # a new day, month total 0.11 < 0.15
        limiter.record(0.05)
        self.clock.now += DAY / 1000
        self.assertIn('mensual', limiter.allow())  # 0.16 within 30 days

    def test_limits_survive_a_restart(self):
        first = self.limiter(max_calls_day=1)
        first.record(0.01)
        second = self.limiter(max_calls_day=1)  # a new object on the same file, as after restarting the bot
        self.assertIn('llamadas', second.allow())


class ClaudeAdvisorTests(unittest.TestCase):
    def advisor(self, transport, **kw):
        return ClaudeAdvisor(KEY, transport=transport, **kw)

    def test_request_shape_and_no_secrets_in_prompt(self):
        seen = {}

        def transport(url, headers, body):
            seen.update(url=url, headers=headers, body=body)
            return reply([dict(symbol='BTCUSDT', approve=True, reason='ok')])
        self.advisor(transport).review([cand('BTCUSDT')], dict(fear_greed=55, funding={'BTCUSDT': 0.0001}, news=None))
        self.assertEqual(seen['url'], 'https://api.anthropic.com/v1/messages')
        self.assertEqual(seen['headers']['x-api-key'], KEY)
        self.assertEqual((seen['body']['model'], seen['body']['temperature']), (DEFAULT_MODEL, 0))
        prompt = seen['body']['messages'][0]['content']
        self.assertNotIn(KEY, prompt + seen['body']['system'])
        self.assertIn('BTCUSDT', prompt)
        for private in ('equity', 'cash', 'capital', 'balance'):   # no account information is sent
            self.assertNotIn(private, prompt)

    def test_parses_decisions_and_ignores_unknown_symbols_and_bad_types(self):
        answer = reply([dict(symbol='BTCUSDT', approve=False, reason='euforia', confidence=0.8),
                        dict(symbol='DOGEUSDT', approve=True, reason='no ofrecida'),
                        dict(symbol='ETHUSDT', approve='sí', reason='tipo inválido')], wrap='```json\n')
        decisions = self.advisor(lambda *a: answer).review([cand('BTCUSDT'), cand('ETHUSDT')], None)
        self.assertEqual(list(decisions), ['BTCUSDT'])
        self.assertFalse(decisions['BTCUSDT']['approve'])

    def test_records_usage_and_respects_the_limiter(self):
        with tempfile.TemporaryDirectory() as d:
            limiter = UsageLimiter(os.path.join(d, 'u.db'), max_calls_day=1)
            try:
                adv = self.advisor(lambda *a: reply([dict(symbol='BTCUSDT', approve=True, reason='ok')], tokens=(1000, 200)),
                                   limiter=limiter)
                adv.review([cand('BTCUSDT')], None)
                self.assertEqual(limiter.used()['calls_today'], 1)
                self.assertAlmostEqual(limiter.used()['usd_today'], cost_usd(DEFAULT_MODEL, 1000, 200))
                with self.assertRaises(RuntimeError):
                    adv.review([cand('BTCUSDT')], None)  # cap reached: no second network call
            finally:
                limiter.close()


class SafeAdvisorTests(unittest.TestCase):
    def run_safe(self, transport, candidates=None):
        events = []
        safe = SafeAdvisor(ClaudeAdvisor(KEY, transport=transport))
        kept = safe(candidates or [cand('BTCUSDT'), cand('ETHUSDT')], None, events)
        return kept, events

    def test_ai_can_reject_and_reasons_are_recorded(self):
        kept, events = self.run_safe(lambda *a: reply([dict(symbol='BTCUSDT', approve=False, reason='euforia'),
                                                       dict(symbol='ETHUSDT', approve=True, reason='ok')]))
        self.assertEqual([c['symbol'] for c in kept], ['ETHUSDT'])
        self.assertEqual([(e['symbol'], e['approve'], e['source']) for e in events],
                         [('BTCUSDT', False, 'claude'), ('ETHUSDT', True, 'claude')])

    def test_silence_from_the_ai_means_the_rules_stand(self):
        kept, _ = self.run_safe(lambda *a: reply([dict(symbol='BTCUSDT', approve=False, reason='x')]))
        self.assertEqual([c['symbol'] for c in kept], ['ETHUSDT'])

    def test_every_failure_falls_back_to_plan_b(self):
        failures = [urllib.error.HTTPError('u', 429, 'quota', {}, None), urllib.error.URLError('sin red'),
                    TimeoutError('lento'), ValueError('json roto'), KeyError('content')]
        for exc in failures:
            def transport(*a, exc=exc):
                raise exc
            kept, events = self.run_safe(transport)
            self.assertEqual(len(kept), 2, exc)                       # trading is never blocked by the AI
            self.assertTrue(all(e['source'] == 'reglas' and e['fallback_note'] for e in events))
        kept, events = self.run_safe(lambda *a: dict(content=[dict(type='text', text='no soy json')]))
        self.assertEqual((len(kept), events[0]['source']), (2, 'reglas'))

    def test_api_key_never_leaks_into_notes(self):
        def transport(*a):
            raise RuntimeError(f'error de autenticación con {KEY}')
        _, events = self.run_safe(transport)
        self.assertNotIn(KEY, json.dumps(events))
        self.assertIn('***', events[0]['fallback_note'])

    def test_plan_b_alone(self):
        events = []
        kept = SafeAdvisor(RuleAdvisor())([cand('BTCUSDT')], None, events)
        self.assertEqual((len(kept), events[0]['source']), (1, 'reglas'))

    def test_cannot_add_or_alter_candidates_through_the_trader(self):
        """End to end with the real trader hook: an AI that lies about symbols/values changes nothing."""
        import trader
        from signals import Params
        H = 3600000
        candles = {s: dict(time=H * 5, open=100., high=101., low=99., close=100., volume=1.) for s in ('A', 'B')}
        hist = {s: [dict(time=H * i, open=100. + i % 3, high=102., low=98., close=100. + (i * 7 + (s == 'B')) % 5,
                         volume=1.) for i in range(60)] for s in ('A', 'B')}
        safe = SafeAdvisor(ClaudeAdvisor(KEY, transport=lambda *a: reply(
            [dict(symbol='A', approve=True, reason='ok'), dict(symbol='Z', approve=True, reason='inventada')])))
        state = trader.new_state(1000)
        with mock.patch('trader.signals.evaluate', lambda sym, rows, p: cand(sym, 2.0 if sym == 'A' else 1.0)):
            trader.step(state, candles, hist, Params(), trader.Costs(), None, review=safe)
        self.assertEqual(sorted(o['symbol'] for o in state['pending']), ['A', 'B'])
        self.assertTrue(all(o['stop'] == 97. for o in state['pending']))


class EnvTests(unittest.TestCase):
    def test_load_and_redact(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, '.env')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('# comentario\nANTHROPIC_API_KEY="abc123"\nTELEGRAM_CHAT_ID = 42\nOTRA=x\n\nsin_igual\n')
            values = env.load(path)
            self.assertEqual((values['ANTHROPIC_API_KEY'], values['TELEGRAM_CHAT_ID'], values['OTRA']), ('abc123', '42', 'x'))
            self.assertEqual(env.load(os.path.join(d, 'missing')).get('ANTHROPIC_API_KEY') or 'none', os.environ.get('ANTHROPIC_API_KEY') or 'none')
        self.assertEqual(env.redact('token=abc123 y abc123', ['abc123', None, '']), 'token=*** y ***')

    def test_env_file_is_ignored_by_git(self):
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, '.gitignore'), encoding='utf-8') as f:
            lines = f.read().split()
        self.assertIn('.env', lines)
        self.assertIn('*.db', lines)


if __name__ == '__main__':
    unittest.main()
