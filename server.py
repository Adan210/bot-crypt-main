import json
import time
import urllib.request
import urllib.parse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import bot
import gate
from engine import Config, demo, simulate
from paper import Paper
from rules import RULES
from signals import Params
from versions import Versions

ROOT = Path(__file__).parent
BOT_DB = ROOT / 'bot.db'
PORT = 8000
ACCOUNT = 'main'
POST_ROUTES = ('/api/bot/init', '/api/bot/step', '/api/bot/emergency', '/api/bot/reactivate', '/api/bot/mode')


def market(symbol):
    if symbol not in ('BTCUSDT', 'ETHUSDT', 'SOLUSDT'):
        raise ValueError('Par no permitido.')
    url = 'https://api.binance.com/api/v3/klines?' + urllib.parse.urlencode(dict(symbol=symbol, interval='1h', limit=1000))
    with urllib.request.urlopen(url, timeout=15) as response:
        raw = json.load(response)
    now = time.time()*1000
    return [dict(time=int(x[0]), open=float(x[1]), close=float(x[4])) for x in raw if int(x[6]) < now]


def parse_config(q):
    return Config(**{k: (int(v) if k in ('fast','slow') else float(v)) for k,v in q.items() if k in Config.__dataclass_fields__})


def bot_state(paper):
    """Everything the dashboard shows, in one JSON-friendly dict."""
    main = paper.summary(ACCOUNT)
    versions = Versions(str(BOT_DB))
    try:
        listing = [dict(id=v['id'], status=v['status'], change=v['change'], reason=v['reason'], source=v['source'],
                        large=bool(v['large']), approved=bool(v['approved'])) for v in versions.all()]
    finally:
        versions.close()
    return dict(main=main, candidate=paper.summary('candidate') if 'candidate' in paper.accounts() else None,
                versions=listing, gate=gate.evaluate(main), rules=asdict(RULES))


class Handler(BaseHTTPRequestHandler):
    def local_only(self):
        # Blocks DNS rebinding and cross-site requests to endpoints that change state.
        allowed = {f'127.0.0.1:{PORT}', f'localhost:{PORT}'}
        origin = self.headers.get('Origin')
        return self.headers.get('Host') in allowed and (origin is None or origin in {'http://'+h for h in allowed})

    def reply(self, body, status=200, mime='application/json'):
        data = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime+'; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        if path.path == '/':
            return self.reply((ROOT/'index.html').read_text(encoding='utf-8'), mime='text/html')
        if path.path == '/api/bot':
            return self.bot(lambda paper: None)
        if path.path != '/api/run':
            return self.reply('{}', 404)
        try:
            q = dict(urllib.parse.parse_qsl(path.query))
            cfg = parse_config(q)
            cfg.validate()
            source = q.get('source', 'demo')
            if source not in ('demo','binance'):
                raise ValueError('Fuente no permitida.')
            rows = demo() if source == 'demo' else market(q.get('symbol','BTCUSDT'))
            result = simulate(rows, cfg)
            result.update(source=source, symbol=q.get('symbol','BTCUSDT'), candles=rows)
            self.reply(json.dumps(result, allow_nan=False))
        except (ValueError, TypeError) as exc:
            self.reply(json.dumps({'error':str(exc)}), 400)
        except Exception:
            self.reply(json.dumps({'error':'No se pudieron obtener los datos. Prueba más tarde o elige DEMO explícitamente.'}), 502)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path)
        if path.path not in POST_ROUTES:
            return self.reply('{}', 404)
        if not self.local_only():
            return self.reply(json.dumps({'error': 'Origen no permitido.'}), 403)
        q = dict(urllib.parse.parse_qsl(path.query))
        actions = {
            '/api/bot/step': lambda p: bot.cycle(p, ACCOUNT),
            '/api/bot/emergency': lambda p: p.emergency(ACCOUNT),
            '/api/bot/reactivate': lambda p: p.reactivate(ACCOUNT),
            '/api/bot/mode': lambda p: p.mode(ACCOUNT, q.get('mode', '')),
            '/api/bot/init': lambda p: bot.start(p, ACCOUNT, Params(aggression=float(q.get('aggression', Params().aggression)))),
        }
        self.bot(actions[path.path])

    def bot(self, action):
        paper = Paper(str(BOT_DB))
        try:
            action(paper)
            self.reply(json.dumps(bot_state(paper), allow_nan=False, default=str))
        except (ValueError, TypeError) as exc:
            self.reply(json.dumps({'error': str(exc)}), 400)
        except Exception:
            self.reply(json.dumps({'error': 'No se pudieron obtener los datos. Prueba más tarde.'}), 502)
        finally:
            paper.close()


if __name__ == '__main__':
    print(f'Crypto Lab: http://127.0.0.1:{PORT} — solo simulación. Ctrl+C para salir.')
    HTTPServer(('127.0.0.1',PORT), Handler).serve_forever()
