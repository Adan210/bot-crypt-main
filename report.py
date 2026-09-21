"""Daily report (phase 6): what happened in one completed UTC day, always next to Bitcoin.

Written to reports/YYYY-MM-DD.md (git-ignored) and, if configured, sent by Telegram. One report per day:
if the file already exists nothing is sent twice.
"""
from datetime import datetime, timezone
from pathlib import Path

import gate

DAY = 86400000
FOLDER = Path(__file__).parent / 'reports'


def _stamp(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%d')


def daily_report(paper, name, day):
    """Markdown for UTC day number `day` (ms // DAY)."""
    start, end = day * DAY, (day + 1) * DAY
    s = paper.summary(name)
    rows = paper.db.execute('SELECT time, equity, btc_close FROM equity WHERE account=? AND time>=? AND time<? ORDER BY time',
                            (name, start, end)).fetchall()
    before = paper.db.execute('SELECT equity, btc_close FROM equity WHERE account=? AND time<? ORDER BY time DESC LIMIT 1',
                              (name, start)).fetchone()
    first_equity = before['equity'] if before else s['capital']
    first_btc = before['btc_close'] if before and before['btc_close'] else (rows[0]['btc_close'] if rows else None)
    day_ret = (rows[-1]['equity'] / first_equity - 1) * 100 if rows else 0.0
    btc_ret = (rows[-1]['btc_close'] / first_btc - 1) * 100 if rows and first_btc and rows[-1]['btc_close'] else 0.0
    trades = [t for t in paper.trades(name) if start <= t['closed'] < end]
    lines = [f'# Reporte diario {_stamp(start)} (simulación, sin dinero real)', '',
             f"- Capital neto estimado: {s['equity']:.2f} USDT ({s['return_pct']:+.2f}% desde el inicio)",
             f"- Ese día: bot {day_ret:+.2f}% · Bitcoin {btc_ret:+.2f}%",
             f"- Caída máxima desde el inicio: {s['drawdown_pct']:.2f}% · Bitcoin misma exposición {s['btc_same_exposure']['max_drawdown_pct']:.2f}%",
             f"- Desde el inicio: bot {s['return_pct']:+.2f}% · Bitcoin misma exposición {s['btc_same_exposure']['return_pct']:+.2f}% · Bitcoin 100% {s['btc_full']['return_pct']:+.2f}%",
             f"- Estado: {'DETENIDO: ' + s['halted'] if s['halted'] else 'activo'} · modo {s['mode']} · versión {s['version']}",
             f"- Operaciones cerradas ese día: {len(trades)} · total {s['trade_count']}", '']
    if trades:
        lines.append('## Operaciones del día')
        lines += [f"- {t['symbol']}: {t['pnl']:+.2f} USDT ({t['r']:+.2f}R) · entrada {t['entry']:.2f}, salida {t['exit']:.2f} · "
                  f"{t['reason_exit']} · {t['reason_entry']}" for t in trades]
        lines.append('')
    if s['positions']:
        lines.append('## Posiciones abiertas')
        lines += [f"- {p['symbol']}: entrada {p['entry']:.2f}, stop {p['stop']:.2f}, resultado abierto {p['pnl']:+.2f} USDT" for p in s['positions']]
        lines.append('')
    g = gate.evaluate(s)
    lines += ['## Criterios para considerar dinero real', f"{sum(c['ok'] for c in g['checks'])} de {len(g['checks'])} cumplidos "
              f"({'todos' if g['ok'] else 'faltan'}). Detalle: py bot.py gate", '',
              'Las simulaciones no garantizan ganancias.']
    return '\n'.join(lines)


def due_day(paper, name, folder=FOLDER):
    """The most recent completed day that has no report yet, or None."""
    day = paper.last_time(name) // DAY - 1   # yesterday relative to the latest candle: fully processed
    if day < paper.summary(name)['started'] // DAY:
        return None
    return None if (Path(folder) / f'{_stamp(day * DAY)}.md').exists() else day


def write_daily(paper, name, folder=FOLDER, notify=None):
    """Create the pending daily report (if any). Returns its text or None."""
    day = due_day(paper, name, folder)
    if day is None:
        return None
    text = daily_report(paper, name, day)
    Path(folder).mkdir(parents=True, exist_ok=True)
    (Path(folder) / f'{_stamp(day * DAY)}.md').write_text(text, encoding='utf-8')
    if notify:
        notify(text)
    return text
