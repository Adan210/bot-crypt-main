"""Backtest with costs, chronological train/test split and walk-forward.

Every result is compared with buy and hold using the SAME exposure and costs. Parameters are
chosen only on training data; the test data is never used to pick them.
Limits: one position at a time, no stop loss or risk engine yet (phase 3).
"""
from dataclasses import asdict, replace
from datetime import datetime, timezone

from engine import simulate

MIN_TRADES = 30        # fewer closed trades than this: the result is marked inconclusive
MIN_TRAIN_TRADES = 5   # a candidate that barely trades in training is not trusted
FAST_GRID = (5, 10, 20)
SLOW_GRID = (30, 50, 100)


def warmup(cfg):
    """Candles the indicators need before the first possible signal."""
    return max(cfg.slow, 15)


def make_grid(base):
    """Small on purpose: every extra candidate makes a lucky training result more likely."""
    return [replace(base, fast=f, slow=s) for f in FAST_GRID for s in SLOW_GRID if f < s]


def round_trips(trades):
    """Pair each COMPRA with the next VENTA. Fees on both sides are inside pnl.

    An open position at the end is not counted as a trade; it is flagged instead.
    """
    trips, entry = [], None
    for t in trades:
        if t['side'] == 'COMPRA':
            entry = t
        elif entry:
            cost = entry['quantity'] * entry['price'] + entry['fee']
            proceeds = t['quantity'] * t['price'] - t['fee']
            trips.append(dict(entry=entry['time'], exit=t['time'], pnl=proceeds - cost,
                              pnl_pct=(proceeds / cost - 1) * 100, reason=t['reason']))
            entry = None
    return trips, entry is not None


def trade_stats(trips, open_position=False):
    wins = [t['pnl_pct'] for t in trips if t['pnl'] > 0]
    losses = [t['pnl_pct'] for t in trips if t['pnl'] <= 0]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    return dict(trades=len(trips), win_rate_pct=len(wins) / len(trips) * 100 if trips else None,
                avg_win_pct=avg_win, avg_loss_pct=avg_loss,
                payoff=avg_win / abs(avg_loss) if avg_win is not None and avg_loss else None,
                conclusive=len(trips) >= MIN_TRADES, open_position=open_position)


def curve_stats(ratios):
    """Return and worst peak-to-trough drop of an equity curve expressed as multiples of capital."""
    peak, worst = 1.0, 0.0
    for r in ratios:
        peak = max(peak, r)
        worst = max(worst, 1 - r / peak)
    return dict(return_pct=(ratios[-1] - 1) * 100, max_drawdown_pct=worst * 100)


def hold_ratios(rows, start, cfg):
    """Buy and hold with the strategy's exposure and costs, entering at the same first candle."""
    entry = rows[start]['open'] * (1 + cfg.slippage) * (1 + cfg.fee)
    units = cfg.exposure / entry
    return [1 - cfg.exposure + units * r['close'] * (1 - cfg.slippage) * (1 - cfg.fee) for r in rows[start:]]


def evaluate(rows, i0, i1, cfg, btc_by_time=None):
    """Run the strategy over rows[i0:i1]. Earlier candles are used only to warm up indicators.

    btc_by_time ({time: candle}) adds a Bitcoin buy-and-hold reference over the same candles.
    """
    start = warmup(cfg)
    if i0 < start:
        raise ValueError('Falta historial previo para calentar los indicadores.')
    if i1 - i0 < 2:
        raise ValueError('Ventana demasiado corta.')
    seg = rows[i0 - start:i1]
    sim = simulate(seg, cfg)
    trips, is_open = round_trips(sim['trades'])
    btc = None
    if btc_by_time is not None:
        btc_seg = [btc_by_time.get(r['time']) for r in seg]
        if None not in btc_seg:
            btc = hold_ratios(btc_seg, start, cfg)
    return dict(config=asdict(cfg), first=rows[i0]['time'], last=rows[i1 - 1]['time'], trips=trips,
                open_position=is_open, ratios=[c['equity'] / cfg.capital for c in sim['curve'][start:]],
                hold=hold_ratios(seg, start, cfg), btc=btc)


def summarize(ev):
    return dict(strategy={**curve_stats(ev['ratios']), **trade_stats(ev['trips'], ev['open_position'])},
                hold=curve_stats(ev['hold']), btc=curve_stats(ev['btc']) if ev['btc'] else None)


def select_config(rows, i0, i1, grid):
    """Best candidate by training return, ignoring ones with too few trades. (None, None) if none qualify."""
    best = None
    for cfg in grid:
        s = summarize(evaluate(rows, i0, i1, cfg))['strategy']
        if s['trades'] >= MIN_TRAIN_TRADES and (best is None or s['return_pct'] > best[1]['return_pct']):
            best = (cfg, s)
    return best or (None, None)


def flat(length):
    """Stand-in for 'stay in cash' when no candidate is trusted in training."""
    return dict(trips=[], open_position=False, ratios=[1.0] * length)


def _lead(grid):
    return max(warmup(c) for c in grid)


def holdout(rows, base, test_fraction=0.3, btc_by_time=None):
    """One chronological split: choose on the first part, report only on the last part."""
    grid = make_grid(base)
    lead, split = _lead(grid), int(len(rows) * (1 - test_fraction))
    if split - lead < 200 or len(rows) - split < 50:
        raise ValueError('Muy pocas velas para separar entrenamiento y prueba.')
    cfg, train = select_config(rows, lead, split, grid)
    ev = evaluate(rows, split, len(rows), cfg or base, btc_by_time)
    if cfg is None:
        ev.update(flat(len(ev['ratios'])))
    return dict(kind='holdout', chosen=asdict(cfg) if cfg else None, train=train,
                train_span=(rows[lead]['time'], rows[split - 1]['time']),
                test_span=(ev['first'], ev['last']), test=summarize(ev))


def _chain(parts):
    """Join per-fold curves (each starting at 1.0) into one continuous curve."""
    level, out = 1.0, []
    for part in parts:
        out.extend(level * x for x in part)
        level = out[-1]
    return out


def walk_forward(rows, base, train_len, test_len, btc_by_time=None):
    """Roll forward: choose on `train_len` candles, trade the next `test_len`, then slide by `test_len`.

    Each test period starts flat with fresh capital; results are chained. Never looks past its test window.
    """
    grid = make_grid(base)
    a, folds, evs = _lead(grid), [], []
    while a + train_len + test_len <= len(rows):
        tr1 = a + train_len
        cfg, train = select_config(rows, a, tr1, grid)
        ev = evaluate(rows, tr1, tr1 + test_len, cfg or base, btc_by_time)
        if cfg is None:
            ev.update(flat(test_len))
        evs.append(ev)
        folds.append(dict(train_span=(rows[a]['time'], rows[tr1 - 1]['time']), test_span=(ev['first'], ev['last']),
                          chosen=asdict(cfg) if cfg else None, train=train, test=summarize(ev)))
        a += test_len
    if not folds:
        raise ValueError('Muy pocas velas para hacer walk-forward con esas ventanas.')
    trips = [t for ev in evs for t in ev['trips']]
    have_btc = all(ev['btc'] for ev in evs)
    total = dict(strategy={**curve_stats(_chain([e['ratios'] for e in evs])), **trade_stats(trips)},
                 hold=curve_stats(_chain([e['hold'] for e in evs])),
                 btc=curve_stats(_chain([e['btc'] for e in evs])) if have_btc else None)
    return dict(kind='walk_forward', folds=folds, total=total, skipped_folds=sum(f['chosen'] is None for f in folds))


def verdict(summary):
    """Plain sentence: does the strategy beat buy and hold (same exposure)?"""
    s, h = summary['strategy'], summary['hold']
    diff = s['return_pct'] - h['return_pct']
    text = (f"{'SUPERA' if diff > 0 else 'NO SUPERA'} a comprar y mantener con la misma exposición "
            f"({diff:+.2f} puntos de retorno), con {'menos' if s['max_drawdown_pct'] < h['max_drawdown_pct'] else 'más o igual'} caída máxima.")
    if not s['conclusive']:
        text += f" NO CONCLUYENTE: solo {s['trades']} operaciones cerradas (mínimo {MIN_TRADES}); puede ser azar."
    return text


def _n(x, suffix=''):
    return '—' if x is None else f'{x:.2f}{suffix}'


def _day(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%d')


def _block(title, s, indent='  '):
    st, h, b = s['strategy'], s['hold'], s['btc']
    lines = [f'{title}',
             f"{indent}Estrategia: retorno {_n(st['return_pct'], '%')} · caída máx. {_n(st['max_drawdown_pct'], '%')} · "
             f"operaciones {st['trades']} · aciertos {_n(st['win_rate_pct'], '%')} · "
             f"ganancia prom. {_n(st['avg_win_pct'], '%')} vs pérdida prom. {_n(st['avg_loss_pct'], '%')}",
             f"{indent}Comprar y mantener (misma exposición): retorno {_n(h['return_pct'], '%')} · caída máx. {_n(h['max_drawdown_pct'], '%')}"]
    if b:
        lines.append(f"{indent}Comprar y mantener BTC (misma exposición): retorno {_n(b['return_pct'], '%')} · caída máx. {_n(b['max_drawdown_pct'], '%')}")
    return lines


def format_report(symbol, result):
    out = [f'=== {symbol} · {"Prueba fuera de muestra (holdout)" if result["kind"] == "holdout" else "Walk-forward"} ===']
    if result['kind'] == 'holdout':
        c = result['chosen']
        out.append(f"Entrenamiento {_day(result['train_span'][0])} a {_day(result['train_span'][1])}: "
                   + (f"elegida media {c['fast']}/{c['slow']} (retorno en entrenamiento {_n(result['train']['return_pct'], '%')})" if c
                      else 'ningún candidato con operaciones suficientes; se queda en efectivo'))
        out += _block(f"Prueba {_day(result['test_span'][0])} a {_day(result['test_span'][1])} (datos no usados para elegir):", result['test'])
        final = result['test']
    else:
        for i, f in enumerate(result['folds'], 1):
            c = f['chosen']
            out += _block(f"Tramo {i}: prueba {_day(f['test_span'][0])} a {_day(f['test_span'][1])} · "
                          + (f"media {c['fast']}/{c['slow']}" if c else 'en efectivo'), f['test'])
        out += _block(f"TOTAL encadenado fuera de muestra ({len(result['folds'])} tramos, {result['skipped_folds']} en efectivo):", result['total'])
        final = result['total']
    out.append('VEREDICTO: ' + verdict(final))
    out.append('Aviso: sin stop loss ni motor de riesgo todavía; el pasado no garantiza el futuro.')
    return '\n'.join(out)
