import json
import time
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from engine import Config, demo, simulate

ROOT = Path(__file__).parent


def market(symbol):
    if symbol not in ('BTCUSDT', 'ETHUSDT', 'SOLUSDT'):
        raise ValueError('Par no permitido.')
    url = 'https://api.binance.com/api/v3/klines?' + urllib.parse.urlencode(dict(symbol=symbol, interval='1h', limit=1000))
    with urllib.request.urlopen(url, timeout=15) as response:
        raw = json.load(response)
    now = time.time()*1000
    return [dict(time=int(x[0]), open=float(x[1]), close=float(x[4])) for x in raw if int(x[6]) < now]


class Handler(BaseHTTPRequestHandler):
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
        if path.path != '/api/run':
            return self.reply('{}', 404)
        try:
            q = dict(urllib.parse.parse_qsl(path.query))
            cfg = Config(**{k: (int(v) if k in ('fast','slow') else float(v)) for k,v in q.items() if k in Config.__dataclass_fields__})
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


if __name__ == '__main__':
    print('Crypto Lab: http://127.0.0.1:8000 — solo simulación. Ctrl+C para salir.')
    HTTPServer(('127.0.0.1',8000), Handler).serve_forever()
