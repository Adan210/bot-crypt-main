import math
import os
import tempfile
import unittest
from data import DEFAULT_SYMBOLS, INTERVAL_MS as H, UNIVERSE, Store, check_candle, download, fetch_klines, find_gaps

T0 = 1600000000000 - 1600000000000 % H


def kline(i):
    price = 100 + i * 0.01 + 5 * math.sin(i / 10)
    t = T0 + i * H
    return [t, str(price), str(price + 1), str(price - 1), str(price + 0.5), '10', t + H - 1]


class FakeExchange:
    """Mimics Binance klines paging: sorted, from startTime, at most `limit` rows, some hours missing."""
    def __init__(self, n, missing=()):
        self.data = [kline(i) for i in range(n) if i not in set(missing)]
        self.calls = []

    def __call__(self, symbol, start, end, limit):
        self.calls.append((start, end, limit))
        return [k for k in self.data if start <= k[0] <= end][:limit]


class DataTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)  # registered first: runs after the connection is closed
        self.store = Store(os.path.join(d.name, 'market.db'))
        self.addCleanup(self.store.close)

    def run_download(self, exchange, n, now=None, **kw):
        end = T0 + n * H
        return download(self.store, 'BTCUSDT', T0, end, fetch=exchange, now_ms=now or end + 10 * H,
                        sleep=lambda s: None, **kw)

    def test_pagination_gets_everything(self):
        ex = FakeExchange(2500)
        result = self.run_download(ex, 2500)
        self.assertGreaterEqual(result['requests'], 3)
        self.assertEqual(result['inserted'], 2500)
        self.assertEqual(self.store.bounds('BTCUSDT'), (T0, T0 + 2499 * H, 2500))
        self.assertEqual(self.store.gaps('BTCUSDT'), [])
        starts = [c[0] for c in ex.calls]
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(all(c[2] == 1000 for c in ex.calls))

    def test_open_candle_is_excluded(self):
        ex = FakeExchange(2500)
        # Halfway through candle 2400: candles 0..2399 are closed, 2400 is still forming.
        self.run_download(ex, 2500, now=T0 + 2400 * H + H // 2)
        self.assertEqual(self.store.bounds('BTCUSDT')[1], T0 + 2399 * H)

    def test_repeat_is_idempotent(self):
        ex = FakeExchange(1200)
        self.run_download(ex, 1200)
        again = self.run_download(ex, 1200)
        self.assertEqual(again['inserted'], 0)
        self.assertEqual(self.store.bounds('BTCUSDT')[2], 1200)

    def test_resume_only_adds_new(self):
        self.run_download(FakeExchange(1500), 1500)
        self.assertEqual(self.run_download(FakeExchange(2000), 2000)['inserted'], 500)

    def test_gap_detection_and_longest_run(self):
        self.run_download(FakeExchange(2000, missing=range(500, 505)), 2000)
        gaps = self.store.gaps('BTCUSDT')
        self.assertEqual(gaps, [dict(after=T0 + 499 * H, before=T0 + 505 * H, missing=5)])
        rows, excluded = self.store.longest_run('BTCUSDT')
        self.assertEqual(len(rows), 1495)  # 505..1999
        self.assertEqual(excluded, 500)
        self.assertEqual(find_gaps([]), [])

    def test_empty_symbol(self):
        self.assertIsNone(self.store.bounds('ETHUSDT'))
        self.assertEqual(self.store.longest_run('ETHUSDT'), ([], 0))

    def test_no_progress_is_detected(self):
        stale = lambda symbol, start, end, limit: [kline(0)]
        with self.assertRaises(RuntimeError):
            download(self.store, 'BTCUSDT', T0 + 5 * H, T0 + 50 * H, fetch=stale, now_ms=T0 + 99 * H, sleep=lambda s: None)

    def test_invalid_candles_rejected_atomically(self):
        good = dict(time=T0, open=10., high=11., low=9., close=10.5, volume=1.)
        bad_cases = [dict(good, time=T0 + 1), dict(good, high=9.5), dict(good, low=10.2),
                     dict(good, close=float('nan')), dict(good, open=-1.), dict(good, volume=-1.)]
        for bad in bad_cases:
            with self.assertRaises(ValueError):
                check_candle(bad)
        with self.assertRaises(ValueError):
            self.store.add('BTCUSDT', [good, bad_cases[0]])
        self.assertIsNone(self.store.bounds('BTCUSDT'))

    def test_rows_range_and_symbols_are_separate(self):
        self.run_download(FakeExchange(100), 100)
        self.assertEqual(len(self.store.rows('BTCUSDT', T0 + 10 * H, T0 + 19 * H)), 10)
        self.assertEqual(self.store.rows('ETHUSDT'), [])

    def test_symbol_whitelist_and_universe(self):
        with self.assertRaises(ValueError):
            fetch_klines('DOGEBTC', T0, T0 + H)  # rejected before any network call
        self.assertEqual(len(UNIVERSE), 16)
        self.assertEqual(len(set(UNIVERSE)), 16)
        self.assertEqual(UNIVERSE[:3], DEFAULT_SYMBOLS)


if __name__ == '__main__':
    unittest.main()
