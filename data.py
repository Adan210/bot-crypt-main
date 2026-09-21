"""Historical candles: paginated download from Binance public market data, SQLite storage
and gap validation. Only the public klines endpoint is used: no orders, no credentials.
"""
import json
import math
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

INTERVAL_MS = 3600000  # only 1h candles for now
PAGE = 1000            # Binance klines maximum per request
DEFAULT_SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
# Ready to grow to 16 coins: add names to --symbols, nothing else changes.
UNIVERSE = DEFAULT_SYMBOLS + ('BNBUSDT', 'XRPUSDT', 'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'LINKUSDT',
                              'DOTUSDT', 'LTCUSDT', 'TRXUSDT', 'ATOMUSDT', 'UNIUSDT', 'NEARUSDT', 'BCHUSDT')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL, time INTEGER NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL,
    PRIMARY KEY (symbol, time));
'''
FIELDS = ('time', 'open', 'high', 'low', 'close', 'volume')


def check_candle(c):
    """Reject candles that cannot be real: bad numbers, misaligned time, inconsistent high/low."""
    prices = [c[k] for k in ('open', 'high', 'low', 'close')]
    if (not all(math.isfinite(p) and p > 0 for p in prices) or not math.isfinite(c['volume']) or c['volume'] < 0
            or c['time'] % INTERVAL_MS != 0 or c['high'] < max(c['open'], c['close'])
            or c['low'] > min(c['open'], c['close'])):
        raise ValueError(f"Vela inválida en {c['time']}.")


def find_gaps(times):
    """Missing 1h candles between consecutive sorted times. Returns [{after, before, missing}]."""
    return [dict(after=a, before=b, missing=(b - a) // INTERVAL_MS - 1)
            for a, b in zip(times, times[1:]) if b - a != INTERVAL_MS]


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def add(self, symbol, rows):
        """Insert candles, ignoring ones already stored. All-or-nothing. Returns how many were new."""
        for c in rows:
            check_candle(c)
        before = self.db.total_changes
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?)',
                                [(symbol, *(c[k] for k in FIELDS)) for c in rows])
        return self.db.total_changes - before

    def rows(self, symbol, start=None, end=None):
        cur = self.db.execute('SELECT time, open, high, low, close, volume FROM candles '
                              'WHERE symbol=? AND time>=? AND time<=? ORDER BY time',
                              (symbol, start if start is not None else 0, end if end is not None else 2**62))
        return [dict(r) for r in cur]

    def bounds(self, symbol):
        """(first time, last time, count) or None when there is no data."""
        row = self.db.execute('SELECT MIN(time), MAX(time), COUNT(*) FROM candles WHERE symbol=?', (symbol,)).fetchone()
        return tuple(row) if row[2] else None

    def gaps(self, symbol):
        times = [r[0] for r in self.db.execute('SELECT time FROM candles WHERE symbol=? ORDER BY time', (symbol,))]
        return find_gaps(times)

    def longest_run(self, symbol):
        """Longest gap-free stretch, so indicators never mix across missing candles.

        Returns (rows, excluded) where excluded is how many stored candles were left out.
        """
        rows = self.rows(symbol)
        runs, current = [], rows[:1]
        for prev, row in zip(rows, rows[1:]):
            if row['time'] - prev['time'] == INTERVAL_MS:
                current.append(row)
            else:
                runs.append(current)
                current = [row]
        runs.append(current)
        best = max(runs, key=len)
        return best, len(rows) - len(best)


def fetch_klines(symbol, start_ms, end_ms, limit=PAGE, retries=3):
    """One page of raw klines (Binance format). Retries on rate limits and temporary errors."""
    if symbol not in UNIVERSE:
        raise ValueError('Par no permitido.')
    url = 'https://api.binance.com/api/v3/klines?' + urllib.parse.urlencode(
        dict(symbol=symbol, interval='1h', startTime=int(start_ms), endTime=int(end_ms), limit=limit))
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (418, 429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
        time.sleep(2 ** attempt * 2)


def candle_from_kline(k):
    return dict(time=int(k[0]), open=float(k[1]), high=float(k[2]), low=float(k[3]),
                close=float(k[4]), volume=float(k[5]))


def download(store, symbol, start_ms, end_ms, fetch=fetch_klines, now_ms=None, sleep=time.sleep, pause=0.2):
    """Download [start_ms, end_ms] page by page and store only closed candles.

    Safe to repeat: stored candles are ignored. Returns dict(requests, inserted).
    """
    now = time.time() * 1000 if now_ms is None else now_ms
    end_ms = min(end_ms, now)
    cursor = start_ms - start_ms % INTERVAL_MS
    requests = inserted = 0
    while cursor < end_ms:
        raw = fetch(symbol, cursor, end_ms, PAGE)
        requests += 1
        if not raw:
            break  # nothing newer (or the coin did not exist yet)
        last_open = int(raw[-1][0])
        if last_open < cursor:
            raise RuntimeError('La paginación no avanza; se detiene para evitar un bucle infinito.')
        closed = [candle_from_kline(k) for k in raw if int(k[6]) < now]  # close time in the past
        inserted += store.add(symbol, closed)
        cursor = last_open + INTERVAL_MS
        sleep(pause)
    return dict(requests=requests, inserted=inserted)
