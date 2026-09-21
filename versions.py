"""Versioned strategy changes with guardrails (phase 5). Learning is slow, tested and reversible.

A change goes through: proposed -> backtest gate -> paper test on a separate 'candidate' account -> ready ->
promoted (large changes also need YOUR approval). Only one candidate at a time, one parameter at a time,
at most one proposal per week. Every version is stored, so any change can be rolled back.

Locked rules (rules.py) are not even representable here: only names in signals.TUNABLE, inside their
ranges, can be proposed.

Guards against overfitting / small samples: the backtest gate needs enough trades, a real improvement
margin, no worse drawdown and more winning windows than losing ones; then the paper stage must confirm.
"""
import json
import sqlite3
import time
from dataclasses import asdict, replace

import portfolio_bt as pb
from signals import TUNABLE, Params

MIN_TRADES = 30              # closed trades the candidate needs in the backtest
MIN_IMPROVEMENT_PP = 0.5     # candidate must beat the current version by this many percentage points
MAX_DRAWDOWN_WORSE_PP = 1.0
MIN_PAPER_DAYS = 14
MIN_PAPER_TRADES = 10
MIN_DAYS_BETWEEN_PROPOSALS = 7
LARGE_CHANGE = 0.25          # moving a parameter more than 25% of its allowed range is a "large" change
IN_FLIGHT = ('proposed', 'backtest_ok', 'paper_testing', 'ready')
DAY = 86400000
SCHEMA = '''
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, parent INTEGER, params TEXT NOT NULL, change TEXT, reason TEXT, source TEXT,
    status TEXT NOT NULL, large INTEGER NOT NULL DEFAULT 0, approved INTEGER NOT NULL DEFAULT 0, evidence TEXT,
    created INTEGER NOT NULL, updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time INTEGER NOT NULL, stats TEXT NOT NULL, outcome TEXT NOT NULL);
'''


class Versions:
    def __init__(self, path, clock=time.time):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.clock = clock

    def close(self):
        self.db.close()

    def _now(self):
        return int(self.clock() * 1000)

    def _row(self, r):
        return None if r is None else dict(r, params=Params(**json.loads(r['params'])),
                                           evidence=json.loads(r['evidence'] or '{}'))

    def get(self, version_id):
        row = self._row(self.db.execute('SELECT * FROM versions WHERE id=?', (version_id,)).fetchone())
        if not row:
            raise ValueError('Versión inexistente.')
        return row

    def all(self):
        return [self._row(r) for r in self.db.execute('SELECT * FROM versions ORDER BY id')]

    def active(self):
        return self._row(self.db.execute("SELECT * FROM versions WHERE status='active'").fetchone())

    def candidate(self):
        return self._row(self.db.execute(
            f"SELECT * FROM versions WHERE status IN ({','.join('?' * len(IN_FLIGHT))}) ORDER BY id DESC LIMIT 1", IN_FLIGHT).fetchone())

    def _set(self, version_id, **fields):
        fields['updated'] = self._now()
        if 'evidence' in fields:
            fields['evidence'] = json.dumps(fields['evidence'])
        with self.db:
            self.db.execute(f"UPDATE versions SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                            (*fields.values(), version_id))

    def baseline(self, params):
        """Register the starting parameters as the first active version (idempotent)."""
        if self.active():
            return self.active()
        now = self._now()
        with self.db:
            self.db.execute("INSERT INTO versions (params, reason, source, status, created, updated) VALUES (?, 'Inicial', 'humano', 'active', ?, ?)",
                            (json.dumps(asdict(params)), now, now))
        return self.active()

    @staticmethod
    def check(current, change):
        """Validate a one-parameter change. Returns (name, value, large) or raises ValueError."""
        if not isinstance(change, dict) or len(change) != 1:
            raise ValueError('Se cambia un solo parámetro a la vez.')
        (name, value), = change.items()
        if name not in TUNABLE:
            raise ValueError(f'"{name}" no es ajustable: las reglas bloqueadas y otros parámetros no se pueden cambiar.')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) == float('inf'):
            raise ValueError('Valor numérico finito requerido.')
        low, high = TUNABLE[name]
        if not low <= value <= high:
            raise ValueError(f'{name} debe estar entre {low} y {high}.')
        if isinstance(getattr(Params(), name), int):
            value = int(round(value))
        old = getattr(current, name)
        return name, value, abs(value - old) / (high - low) > LARGE_CHANGE

    def propose(self, change, reason, source):
        active = self.active()
        if not active:
            raise ValueError('Falta la versión inicial.')
        name, value, large = self.check(active['params'], change)
        if value == getattr(active['params'], name):
            raise ValueError('El valor propuesto es el actual.')
        if self.candidate():
            raise ValueError('Ya hay un candidato en prueba: uno a la vez.')
        last = self.db.execute("SELECT MAX(created) FROM versions WHERE source!='humano'").fetchone()[0]
        if last and self._now() - last < MIN_DAYS_BETWEEN_PROPOSALS * DAY:
            raise ValueError(f'Máximo una propuesta cada {MIN_DAYS_BETWEEN_PROPOSALS} días.')
        now = self._now()
        with self.db:
            cur = self.db.execute(
                "INSERT INTO versions (parent, params, change, reason, source, status, large, created, updated) "
                "VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?, ?)",
                (active['id'], json.dumps(asdict(replace(active['params'], **{name: value}))),
                 json.dumps({name: value}), str(reason)[:300], source, int(large), now, now))
        return cur.lastrowid

    def evaluate(self, version_id, rows, window_candles=2160):
        """Backtest gate over stored history: candidate vs the version it would replace."""
        v = self.get(version_id)
        if v['status'] != 'proposed':
            raise ValueError('Solo se evalúa una propuesta nueva.')
        base = pb.windows(rows, self.get(v['parent'])['params'], window_candles)
        cand = pb.windows(rows, v['params'], window_candles)
        if not base or len(base) != len(cand):
            raise ValueError('Datos insuficientes para evaluar.')
        b, c = pb.aggregate(base)['bot'], pb.aggregate(cand)['bot']
        wins = sum(x['bot']['return_pct'] > y['bot']['return_pct'] for x, y in zip(cand, base))
        losses = sum(x['bot']['return_pct'] < y['bot']['return_pct'] for x, y in zip(cand, base))
        checks = dict(
            enough_trades=c['trades'] >= MIN_TRADES,
            real_improvement=c['return_pct'] - b['return_pct'] >= MIN_IMPROVEMENT_PP,
            drawdown_ok=c['max_drawdown_pct'] <= b['max_drawdown_pct'] + MAX_DRAWDOWN_WORSE_PP,
            more_windows_won=wins > losses)
        evidence = dict(base=dict(return_pct=b['return_pct'], drawdown_pct=b['max_drawdown_pct'], trades=b['trades']),
                        candidate=dict(return_pct=c['return_pct'], drawdown_pct=c['max_drawdown_pct'], trades=c['trades']),
                        windows=len(cand), windows_won=wins, windows_lost=losses, checks=checks)
        self._set(version_id, status='backtest_ok' if all(checks.values()) else 'rejected', evidence=evidence)
        return evidence

    def start_paper(self, version_id, start_candidate):
        v = self.get(version_id)
        if v['status'] != 'backtest_ok':
            raise ValueError('Solo pasa a paper trading lo que superó el backtest.')
        start_candidate(v['params'], f"v{version_id}")
        self._set(version_id, status='paper_testing', evidence=dict(v['evidence'], paper_started=self._now()))

    def check_paper(self, version_id, paper):
        """Compare candidate and main over the candidate's own lifetime. Returns the (possibly new) status."""
        v = self.get(version_id)
        if v['status'] != 'paper_testing':
            return v['status']
        cand = paper.summary('candidate')
        started = v['evidence']['paper_started']
        if not cand['initialized'] or cand['days'] < MIN_PAPER_DAYS:
            return 'paper_testing'
        cand_ret, cand_dd = paper.return_since('candidate', cand['started'])
        main_ret, main_dd = paper.return_since('main', cand['started'])
        checks = dict(enough_trades=cand['trade_count'] >= MIN_PAPER_TRADES, not_worse=cand_ret >= main_ret,
                      drawdown_ok=cand_dd <= main_dd + MAX_DRAWDOWN_WORSE_PP)
        evidence = dict(v['evidence'], paper=dict(days=cand['days'], trades=cand['trade_count'], candidate_return=cand_ret,
                                                  main_return=main_ret, candidate_drawdown=cand_dd, main_drawdown=main_dd,
                                                  checks=checks))
        if all(checks.values()):
            status = 'ready'
        elif cand['days'] >= 2 * MIN_PAPER_DAYS and cand['trade_count'] >= MIN_PAPER_TRADES:
            status = 'rejected'
        else:
            status = 'paper_testing'  # keep waiting: not enough evidence either way yet
        self._set(version_id, status=status, evidence=evidence)
        if status == 'rejected':
            paper.remove('candidate')
        return status

    def approve(self, version_id):
        """The human's explicit OK for a large change."""
        self._set(version_id, approved=1)

    def promote(self, version_id, paper):
        v = self.get(version_id)
        if v['status'] != 'ready':
            raise ValueError('Solo se promueve lo que terminó la prueba en paper trading.')
        if v['large'] and not v['approved']:
            raise PermissionError('Cambio grande: requiere tu aprobación explícita.')
        old = self.active()
        paper.set_params('main', v['params'], f'v{version_id}')
        paper.remove('candidate')
        self._set(old['id'], status='retired')
        self._set(version_id, status='active')

    def rollback(self, paper):
        """Go back to the version the active one replaced."""
        current = self.active()
        if not current or not current['parent']:
            raise ValueError('No hay versión anterior.')
        previous = self.get(current['parent'])
        paper.set_params('main', previous['params'], f"v{previous['id']}")
        self._set(current['id'], status='rolled_back')
        self._set(previous['id'], status='active')
