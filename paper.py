"""Persistent paper trading on SQLite (phase 4). No orders, no credentials, no network.

The caller supplies closed candles. This module stores them (deduplicated by coin and time), runs the
same engine as the backtest (trader.step) and saves the full state after every batch, so open positions,
queued orders, loss-limit halts and the trade log all survive a restart. Several accounts can coexist
(e.g. 'main' and a 'candidate' configuration under test) sharing the same candles.
"""
import json
import sqlite3
from dataclasses import asdict

from backtest import curve_stats
from data import check_candle
from rules import RULES
from signals import WINDOW, Params
from trader import Costs, emergency as trader_emergency, new_state, reactivate as trader_reactivate, set_mode, step

DAY = 86400000
SCHEMA = '''
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL, time INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
    low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, PRIMARY KEY (symbol, time));
CREATE TABLE IF NOT EXISTS accounts (
    name TEXT PRIMARY KEY, params TEXT NOT NULL, costs TEXT NOT NULL, symbols TEXT NOT NULL, state TEXT NOT NULL,
    last_time INTEGER NOT NULL, started INTEGER NOT NULL, capital REAL NOT NULL, version TEXT);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, symbol TEXT NOT NULL, opened INTEGER NOT NULL,
    closed INTEGER NOT NULL, entry REAL NOT NULL, exit REAL NOT NULL, qty REAL NOT NULL, pnl REAL NOT NULL,
    pnl_pct REAL NOT NULL, r REAL NOT NULL, reason_entry TEXT, reason_exit TEXT, regime TEXT, context TEXT);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, time INTEGER NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equity (
    account TEXT NOT NULL, time INTEGER NOT NULL, equity REAL NOT NULL, btc_close REAL, PRIMARY KEY (account, time));
'''
CANDLE = ('time', 'open', 'high', 'low', 'close', 'volume')


class Paper:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # ---- accounts ---------------------------------------------------------------------------------
    def accounts(self):
        return [r['name'] for r in self.db.execute('SELECT name FROM accounts ORDER BY name')]

    def _load(self, name):
        row = self.db.execute('SELECT * FROM accounts WHERE name=?', (name,)).fetchone()
        if not row:
            raise ValueError('La cuenta simulada no está iniciada.')
        return dict(name=name, params=Params(**json.loads(row['params'])), costs=Costs(**json.loads(row['costs'])),
                    symbols=json.loads(row['symbols']), state=json.loads(row['state']), last_time=row['last_time'],
                    started=row['started'], capital=row['capital'], version=row['version'])

    def last_time(self, name):
        return self._load(name)['last_time']

    def params(self, name):
        return self._load(name)['params']

    def symbols(self, name):
        return self._load(name)['symbols']

    def _save(self, acc):
        self.db.execute('UPDATE accounts SET state=?, last_time=? WHERE name=?',
                        (json.dumps(acc['state']), acc['last_time'], acc['name']))

    def _candles(self, symbol, since=None, until=None, limit=None):
        sql, args = 'SELECT time, open, high, low, close, volume FROM candles WHERE symbol=?', [symbol]
        if since is not None:
            sql, args = sql + ' AND time>?', args + [since]
        if until is not None:
            sql, args = sql + ' AND time<=?', args + [until]
        if limit:
            sql += ' ORDER BY time DESC LIMIT ?'
            args.append(limit)
            return [dict(r) for r in reversed(self.db.execute(sql, args).fetchall())]
        return [dict(r) for r in self.db.execute(sql + ' ORDER BY time', args)]

    def _store_candles(self, rows_by_symbol):
        for symbol, rows in rows_by_symbol.items():
            for c in rows:
                check_candle(c)
            self.db.executemany('INSERT OR IGNORE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?)',
                                [(symbol, *(c[k] for k in CANDLE)) for c in rows])

    def init(self, name, params, warmup, capital=1000.0, costs=Costs(), version='v0'):
        """Start a fresh account. Warmup candles only feed the indicators: no trades in the past."""
        if not (0 < params.aggression <= RULES.max_aggression):
            raise ValueError('Agresividad fuera del tope bloqueado.')
        if capital <= 0:
            raise ValueError('Capital inválido.')
        symbols = sorted(warmup)
        if 'BTCUSDT' not in symbols:
            raise ValueError('BTCUSDT es obligatorio: es la referencia de comparación.')
        last = min(rows[-1]['time'] for rows in warmup.values() if rows)
        with self.db:
            self._store_candles(warmup)
            for table in ('trades', 'events', 'equity'):
                self.db.execute(f'DELETE FROM {table} WHERE account=?', (name,))
            self.db.execute('INSERT OR REPLACE INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                            (name, json.dumps(asdict(params)), json.dumps(asdict(costs)), json.dumps(symbols),
                             json.dumps(new_state(capital)), last, last, capital, version))

    # ---- processing candles -----------------------------------------------------------------------
    def process(self, name, rows_by_symbol=None, ctx=None, review=None):
        """Store new closed candles, then advance the account through every candle time it has not seen.

        A coin with a gap is skipped for that time; a coin that simply has not published yet makes the
        account wait, so all coins stay aligned. The whole batch is atomic. Returns the engine events.
        """
        acc = self._load(name)
        symbols, state = acc['symbols'], acc['state']
        with self.db:
            self._store_candles(rows_by_symbol or {})
            fresh = {s: {c['time']: c for c in self._candles(s, since=acc['last_time'])} for s in symbols}
            latest = {s: max(fresh[s], default=acc['last_time']) for s in symbols}
            times = sorted({t for s in symbols for t in fresh[s]})
            history = {s: self._candles(s, until=acc['last_time'], limit=WINDOW) for s in symbols}
            all_events, todo = [], []
            for t in times:
                now = {s: fresh[s][t] for s in symbols if t in fresh[s]}
                if any(s not in now and latest[s] <= t for s in symbols):
                    break  # some coin has not published this candle yet: wait for it
                todo.append((t, now))
            for k, (t, now) in enumerate(todo):
                for s, c in now.items():
                    history[s] = (history[s] + [c])[-WINDOW:]
                live = k == len(todo) - 1  # market context only describes "now", never the past
                events = step(state, now, {s: history[s] for s in symbols}, acc['params'], acc['costs'],
                              ctx if live else None, RULES, review if live else None)
                acc['last_time'] = t
                btc = now.get('BTCUSDT') or (history['BTCUSDT'][-1] if history['BTCUSDT'] else None)
                self.db.execute('INSERT OR REPLACE INTO equity VALUES (?, ?, ?, ?)',
                                (name, t, state['equity'], btc['close'] if btc else None))
                self._log(name, events)
                all_events += events
            self._save(acc)
        return all_events

    def _log(self, name, events):
        for e in events:
            if e['kind'] == 'trade':
                t = e['trade']
                self.db.execute(
                    'INSERT INTO trades (account, symbol, opened, closed, entry, exit, qty, pnl, pnl_pct, r, reason_entry, '
                    'reason_exit, regime, context) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (name, t['symbol'], t['opened'], t['closed'], t['entry'], t['exit'], t['qty'], t['pnl'], t['pnl_pct'],
                     t['r'], t['reason_entry'], t['reason_exit'], t['regime'], json.dumps(t['context'])))
            else:
                self.db.execute('INSERT INTO events (account, time, kind, data) VALUES (?, ?, ?, ?)',
                                (name, e['time'], e['kind'], json.dumps({k: v for k, v in e.items() if k not in ('kind', 'time')})))

    # ---- manual controls (each one is logged and saved) --------------------------------------------
    def _control(self, name, kind, fn):
        acc = self._load(name)
        with self.db:
            events = fn(acc) or []
            events.append(dict(kind=kind, time=acc['last_time']))
            self._log(name, events)
            self.db.execute('INSERT OR REPLACE INTO equity VALUES (?, ?, ?, ?)', (
                name, acc['last_time'], acc['state']['equity'],
                (self._candles('BTCUSDT', until=acc['last_time'], limit=1) or [dict(close=None)])[0]['close']))
            self._save(acc)
        return events

    def emergency(self, name):
        """Close everything at the last known prices and stop until reactivated by hand."""
        return self._control(name, 'manual_emergency',
                             lambda a: trader_emergency(a['state'], a['last_time'], a['costs']))

    def reactivate(self, name):
        return self._control(name, 'manual_reactivate', lambda a: trader_reactivate(a['state']))

    def mode(self, name, mode):
        return self._control(name, f'manual_mode_{mode}', lambda a: set_mode(a['state'], mode))

    def set_params(self, name, params, version):
        """Swap the strategy parameters of a running account (positions and history are kept)."""
        self._load(name)
        if not (0 < params.aggression <= RULES.max_aggression):
            raise ValueError('Agresividad fuera del tope bloqueado.')
        with self.db:
            self.db.execute('UPDATE accounts SET params=?, version=? WHERE name=?',
                            (json.dumps(asdict(params)), version, name))

    def remove(self, name):
        with self.db:
            for table in ('accounts', 'trades', 'events', 'equity'):
                self.db.execute(f'DELETE FROM {table} WHERE {"name" if table == "accounts" else "account"}=?', (name,))

    def return_since(self, name, t):
        """(return %, worst drawdown %) of an account from time t on, measured on its equity curve."""
        acc = self._load(name)
        row = self.db.execute('SELECT equity FROM equity WHERE account=? AND time<=? ORDER BY time DESC LIMIT 1',
                              (name, t)).fetchone()
        start = row['equity'] if row else acc['capital']
        peak, worst = start, 0.0
        curve = [r['equity'] for r in self.db.execute('SELECT equity FROM equity WHERE account=? AND time>? ORDER BY time', (name, t))]
        for equity in curve:
            peak = max(peak, equity)
            worst = max(worst, 1 - equity / peak)
        end = curve[-1] if curve else start
        return (end / start - 1) * 100, worst * 100

    # ---- reading ------------------------------------------------------------------------------------
    def trades(self, name, since=0):
        return [dict(r, context=json.loads(r['context'] or '{}')) for r in self.db.execute(
            'SELECT * FROM trades WHERE account=? AND closed>=? ORDER BY closed', (name, since))]

    def events(self, name, limit=50):
        return [dict(r, data=json.loads(r['data'])) for r in self.db.execute(
            'SELECT time, kind, data FROM events WHERE account=? ORDER BY id DESC LIMIT ?', (name, limit))]

    def summary(self, name, curve_points=400, trade_limit=200):
        if name not in self.accounts():
            return dict(initialized=False)
        acc = self._load(name)
        state, capital = acc['state'], acc['capital']
        curve = [dict(r) for r in self.db.execute(
            'SELECT time, equity, btc_close FROM equity WHERE account=? ORDER BY time', (name,))]
        step_ = max(1, len(curve) // curve_points)
        trades = self.trades(name)
        wins = [t for t in trades if t['pnl'] > 0]
        losses = [t for t in trades if t['pnl'] <= 0]
        exposure = state['exposure_sum'] / state['steps'] if state['steps'] else 0.
        btc = [c['btc_close'] for c in curve if c['btc_close']]
        btc_return = (btc[-1] / btc[0] - 1) * 100 if len(btc) > 1 else 0.
        costs = acc['costs']
        factor = (1 - costs.slippage) * (1 - costs.fee) / ((1 + costs.slippage) * (1 + costs.fee))

        def hold(exposure):  # Bitcoin bought at the start with the same costs, partly invested
            if len(btc) < 2:
                return dict(return_pct=0., max_drawdown_pct=0.)
            return curve_stats([1 - exposure + exposure * (c / btc[0]) * factor for c in btc])
        positions = [dict(symbol=p['symbol'], qty=p['qty'], entry=p['entry'], stop=p['stop'], last=p['last_close'],
                          pnl=p['qty'] * (p['last_close'] - p['entry']), reason=p['reason'])
                     for p in state['positions'].values()]
        return dict(
            initialized=True, name=name, version=acc['version'], symbols=acc['symbols'], params=asdict(acc['params']),
            capital=capital, equity=state['equity'], cash=state['cash'], return_pct=(state['equity'] / capital - 1) * 100,
            drawdown_pct=state['worst'] * 100, fees=state['fees'], halted=state['halted'], mode=state['mode'],
            positions=positions, pending=[o['symbol'] for o in state['pending']], last_time=acc['last_time'],
            started=acc['started'], days=(acc['last_time'] - acc['started']) / DAY, exposure_pct=exposure * 100,
            btc_return_pct=btc_return, btc_same_exposure=hold(exposure), btc_full=hold(1.0),
            btc_same_exposure_pct=hold(exposure)['return_pct'],
            trade_count=len(trades), win_rate_pct=len(wins) / len(trades) * 100 if trades else None,
            avg_win_pct=sum(t['pnl_pct'] for t in wins) / len(wins) if wins else None,
            avg_loss_pct=sum(t['pnl_pct'] for t in losses) / len(losses) if losses else None,
            curve=curve[::step_] + ([curve[-1]] if curve and (len(curve) - 1) % step_ else []),
            trades=trades[-trade_limit:], events=self.events(name, 30))

