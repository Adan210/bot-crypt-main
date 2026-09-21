"""Telegram alerts (phase 6). Alerts are informational only: a failure here can never affect trading.

The token and chat id come from .env. The token is removed from any error text before it is stored or
shown. Every message says it is a simulation.
"""
import json
import time
import urllib.request

from env import redact

API = 'https://api.telegram.org/bot{token}/sendMessage'
ALERT_KINDS = ('entry', 'partial', 'trade', 'halt', 'manual_emergency', 'manual_reactivate')
ERROR_COOLDOWN = 1800  # seconds: the same kind of error is reported at most every 30 minutes


def post_json(url, body, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'),
                                 headers={'content-type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


class Telegram:
    def __init__(self, token, chat_id, transport=post_json):
        self.token, self.chat_id, self.transport = token, chat_id, transport
        self.last_error = None

    def send(self, text):
        """True if delivered. Never raises."""
        try:
            reply = self.transport(API.format(token=self.token),
                                   dict(chat_id=self.chat_id, text=text[:4000], disable_web_page_preview=True))
            return bool(reply.get('ok'))
        except Exception as exc:
            self.last_error = redact(f'{type(exc).__name__}: {exc}', [self.token])
            return False


class Notifier:
    """Console plus (optionally) Telegram. Hooks plug into bot.run_forever."""

    def __init__(self, telegram=None, describe=None, out=print, clock=time.time, secrets=()):
        self.telegram, self.describe, self.out, self.clock = telegram, describe or str, out, clock
        self.secrets = list(secrets) + ([telegram.token] if telegram else [])
        self._last_error = {}

    def say(self, text):
        text = redact(text, self.secrets)
        self.out(text)
        if self.telegram:
            self.telegram.send('[SIMULACIÓN] ' + text)

    def on_events(self, name, events):
        for e in events:
            if e['kind'] in ALERT_KINDS:
                self.say(f'[{name}] {self.describe(e)}')

    def on_error(self, exc):
        key = type(exc).__name__
        now = self.clock()
        if now - self._last_error.get(key, -ERROR_COOLDOWN) >= ERROR_COOLDOWN:
            self._last_error[key] = now
            self.say(f'Error del bot (se reintenta solo): {key}: {str(exc)[:150]}')


def from_env(values):
    """Telegram client if both settings exist, else None (console only)."""
    token, chat = values.get('TELEGRAM_BOT_TOKEN'), values.get('TELEGRAM_CHAT_ID')
    return Telegram(token, chat) if token and chat else None
