import glob
import os
import re
import tempfile
import unittest
from unittest import mock
import alerts
import bot
import gate
import portfolio_bt as pb
import report
from paper import Paper
from signals import Params
from test_paper import K, SYMBOLS
from test_portfolio_bt import frequent, market

TOKEN = '123456:SECRET-TOKEN'
ROOT = os.path.dirname(os.path.abspath(__file__))


class TelegramTests(unittest.TestCase):
    def test_send_success_and_payload(self):
        seen = {}

        def transport(url, body):
            seen.update(url=url, body=body)
            return {'ok': True}
        self.assertTrue(alerts.Telegram(TOKEN, '42', transport).send('hola ' * 2000))
        self.assertIn(TOKEN, seen['url'])
        self.assertEqual(seen['body']['chat_id'], '42')
        self.assertLessEqual(len(seen['body']['text']), 4000)

    def test_send_never_raises_and_never_leaks_the_token(self):
        def transport(url, body):
            raise OSError(f'fallo al conectar con {url}')
        tg = alerts.Telegram(TOKEN, '42', transport)
        self.assertFalse(tg.send('x'))
        self.assertNotIn(TOKEN, tg.last_error)
        self.assertFalse(alerts.Telegram(TOKEN, '42', lambda u, b: {'ok': False}).send('x'))

    def test_from_env(self):
        self.assertIsNone(alerts.from_env({}))
        self.assertIsNone(alerts.from_env({'TELEGRAM_BOT_TOKEN': 't'}))
        self.assertIsNotNone(alerts.from_env({'TELEGRAM_BOT_TOKEN': 't', 'TELEGRAM_CHAT_ID': '1'}))


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.out, self.sent, self.now = [], [], [1000.0]
        tg = alerts.Telegram(TOKEN, '1', lambda url, body: self.sent.append(body['text']) or {'ok': True})
        self.notifier = alerts.Notifier(tg, describe=bot.describe, out=self.out.append, clock=lambda: self.now[0])

    def test_only_important_events_are_alerted_and_marked_as_simulation(self):
        events = [dict(kind='entry', time=1, symbol='BTCUSDT', price=100., stop=95., reason='r'),
                  dict(kind='skip', time=1, symbol='ETHUSDT', why='x'),
                  dict(kind='ai_review', time=1, symbol='ETHUSDT', approve=False, reason='r', source='claude'),
                  dict(kind='trade', time=2, trade=dict(symbol='BTCUSDT', exit=94., pnl=-5., r=-1., reason_exit='Stop loss')),
                  dict(kind='halt', time=3, why='Límite de pérdida diaria')]
        self.notifier.on_events('main', events)
        self.assertEqual(len(self.sent), 3)
        self.assertTrue(all(m.startswith('[SIMULACIÓN]') for m in self.sent))
        self.assertIn('COMPRA', self.sent[0])
        self.assertIn('DETENIDO', self.sent[2])

    def test_console_only_without_telegram(self):
        quiet = alerts.Notifier(None, describe=bot.describe, out=self.out.append)
        quiet.on_events('main', [dict(kind='halt', time=1, why='x')])
        self.assertEqual(len(self.out), 1)

    def test_same_error_is_reported_once_per_half_hour(self):
        for _ in range(5):
            self.notifier.on_error(OSError('sin red'))
        self.assertEqual(len(self.sent), 1)
        self.notifier.on_error(ValueError('otro tipo'))
        self.assertEqual(len(self.sent), 2)
        self.now[0] += alerts.ERROR_COOLDOWN
        self.notifier.on_error(OSError('sin red'))
        self.assertEqual(len(self.sent), 3)

    def test_secrets_are_scrubbed_from_messages(self):
        self.notifier.on_error(RuntimeError(f'clave {TOKEN}'))
        self.assertNotIn(TOKEN, ' '.join(self.sent + self.out))


class GateTests(unittest.TestCase):
    def summary(self, **kw):
        base = dict(initialized=True, days=100, trade_count=250, return_pct=5.0, drawdown_pct=2.0, halted=None,
                    btc_same_exposure=dict(return_pct=3.0, max_drawdown_pct=4.0))
        return dict(base, **kw)

    def test_all_criteria_must_hold(self):
        self.assertTrue(gate.evaluate(self.summary())['ok'])
        for override in (dict(days=89), dict(trade_count=200), dict(return_pct=2.9), dict(drawdown_pct=4.0),
                         dict(halted='Límite de pérdida diaria')):
            result = gate.evaluate(self.summary(**override))
            self.assertFalse(result['ok'], override)
            self.assertEqual(sum(not c['ok'] for c in result['checks']), 1, override)

    def test_text_and_uninitialized(self):
        self.assertIn('NO se considera dinero real', gate.format_gate(gate.evaluate(self.summary(days=1))))
        self.assertIn('CRITERIOS CUMPLIDOS', gate.format_gate(gate.evaluate(self.summary())))
        self.assertFalse(gate.evaluate(dict(initialized=False))['ok'])
        self.assertIn('no contiene código para enviar órdenes', gate.NOTE)

    def test_project_has_no_code_to_place_real_orders(self):
        forbidden = re.compile(r'/api/v3/order|newOrder|X-MBX-APIKEY|createOrder|/fapi/v1/order|hmac|signature=', re.I)
        for path in glob.glob(os.path.join(ROOT, '*.py')):
            if os.path.basename(path).startswith('test_'):
                continue
            with open(path, encoding='utf-8') as f:
                self.assertIsNone(forbidden.search(f.read()), path)


class ReportTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        rows = pb.align({s: market(1200, i) for i, s in enumerate(SYMBOLS)})
        patch = mock.patch('trader.signals.evaluate', frequent)
        patch.start()
        self.addCleanup(patch.stop)
        self.paper = Paper(os.path.join(d.name, 'bot.db'))
        self.addCleanup(self.paper.close)
        self.paper.init('main', Params(), {s: rows[s][:K] for s in SYMBOLS})
        self.paper.process('main', {s: rows[s][K:1200] for s in SYMBOLS})

    def test_daily_report_content(self):
        day = self.paper.last_time('main') // report.DAY - 1
        text = report.daily_report(self.paper, 'main', day)
        for needle in ('Reporte diario', 'Bitcoin', 'Capital neto', 'Operaciones cerradas ese día', 'no garantizan',
                       'Criterios para considerar dinero real', 'sin dinero real'):
            self.assertIn(needle, text)

    def test_one_report_per_day_and_notification(self):
        sent = []
        folder = os.path.join(self.dir, 'reports')
        first = report.write_daily(self.paper, 'main', folder, notify=sent.append)
        self.assertIsNotNone(first)
        self.assertEqual(len(os.listdir(folder)), 1)
        self.assertIsNone(report.write_daily(self.paper, 'main', folder, notify=sent.append))  # already written
        self.assertEqual(len(sent), 1)

    def test_no_report_before_a_day_has_completed(self):
        with tempfile.TemporaryDirectory() as d2:
            fresh = Paper(os.path.join(d2, 'x.db'))
            try:
                rows = pb.align({s: market(400, i) for i, s in enumerate(SYMBOLS)})
                fresh.init('main', Params(), {s: rows[s][:K] for s in SYMBOLS})
                fresh.process('main', {s: rows[s][K:K + 5] for s in SYMBOLS})   # only a few hours of live data
                self.assertIsNone(report.due_day(fresh, 'main', os.path.join(d2, 'reports')))
            finally:
                fresh.close()

    def test_summary_has_btc_references_with_costs(self):
        s = self.paper.summary('main')
        for key in ('btc_same_exposure', 'btc_full'):
            self.assertEqual(set(s[key]), {'return_pct', 'max_drawdown_pct'})
        self.assertLess(abs(s['btc_same_exposure']['return_pct']), abs(s['btc_full']['return_pct']) + 1e-9)


if __name__ == '__main__':
    unittest.main()
