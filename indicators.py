"""Indicators on plain lists of candles ({'open','high','low','close','volume'}).

Each function only reads what it is given, so passing candles up to time t can never leak the future.
"""
import math


def sma(values, n):
    return sum(values[-n:]) / n


def rsi(closes, n=14):
    """Simple (non-Wilder) RSI over the last n changes, same as the phase-0 engine."""
    changes = [b - a for a, b in zip(closes[-n - 1:-1], closes[-n:])]
    gain = sum(x for x in changes if x > 0)
    loss = -sum(x for x in changes if x < 0)
    return 50.0 if gain + loss == 0 else 100 * gain / (gain + loss)


def atr(rows, n=14):
    """Average true range in price units over the last n candles."""
    window = rows[-n - 1:]
    trs = [max(c['high'] - c['low'], abs(c['high'] - p['close']), abs(c['low'] - p['close']))
           for p, c in zip(window, window[1:])]
    return sum(trs) / n


def volatility(closes, n=24):
    """Standard deviation of hourly log returns over the last n candles."""
    rets = [math.log(b / a) for a, b in zip(closes[-n - 1:-1], closes[-n:])]
    mean = sum(rets) / n
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / n)


def volume_ratio(rows, n=24):
    """Latest volume divided by the average of the n candles before it."""
    base = sum(r['volume'] for r in rows[-n - 1:-1]) / n
    return rows[-1]['volume'] / base if base > 0 else 0.0


def efficiency_ratio(closes, n):
    """Kaufman efficiency: net move / total path over n candles. ~1 = clean trend, ~0 = choppy."""
    path = sum(abs(b - a) for a, b in zip(closes[-n - 1:-1], closes[-n:]))
    return abs(closes[-1] - closes[-n - 1]) / path if path > 0 else 0.0


def bollinger(closes, n=20, k=2.0):
    window = closes[-n:]
    mid = sum(window) / n
    sd = math.sqrt(sum((x - mid) ** 2 for x in window) / n)
    return mid, mid - k * sd, mid + k * sd


def highest_high(rows, n):
    """Highest high of the n candles BEFORE the last one (the breakout level)."""
    return max(r['high'] for r in rows[-n - 1:-1])


def correlation(a, b):
    """Pearson correlation of two equally long series; 0 when either is constant."""
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va, vb = sum((x - ma) ** 2 for x in a), sum((y - mb) ** 2 for y in b)
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else 0.0


def returns(closes):
    return [b / a - 1 for a, b in zip(closes, closes[1:])]
