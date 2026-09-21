"""Backtest of the full bot (signals + risk engine) over several coins, always against Bitcoin buy and hold.

Parameters are FIXED (not fitted to the data). Results are reported over independent consecutive
windows so one lucky stretch cannot hide behind a total. The Bitcoin reference uses the bot's own
average exposure and costs ("same exposure"), and 100% invested is shown as well.
"""
from types import SimpleNamespace

from backtest import MIN_TRADES, curve_stats, hold_ratios, trade_stats, _day, _n
from rules import RULES
from signals import WINDOW, Params, min_history
from trader import Costs, new_state, step


def align(rows_by_symbol):
    """Keep only candle times present for every coin, so all series share one timeline."""
    common = set.intersection(*(set(r['time'] for r in rows) for rows in rows_by_symbol.values()))
    return {s: [r for r in rows if r['time'] in common] for s, rows in rows_by_symbol.items()}


def run(rows, params, i0, i1, capital=1000.0, costs=Costs(), ctx=None, review=None, rules=RULES):
    """Run the bot over aligned candles [i0, i1). Earlier candles only feed the indicators."""
    state, trades, ratios = new_state(capital), [], []
    for i in range(i0, i1):
        candles = {s: r[i] for s, r in rows.items()}
        histories = {s: r[max(0, i + 1 - WINDOW):i + 1] for s, r in rows.items()}
        for event in step(state, candles, histories, params, costs, ctx, rules, review):
            if event['kind'] == 'trade':
                trades.append(event['trade'])
        ratios.append(state['equity'] / capital)
    return dict(trades=trades, ratios=ratios, state=state,
                exposure=state['exposure_sum'] / state['steps'] if state['steps'] else 0.)


def summarize(result, btc_rows, i0, i1, costs=Costs(), capital=1000.0):
    """Bot metrics plus Bitcoin buy and hold at the same average exposure and at 100%."""
    trades = result['trades']
    stats = {**curve_stats(result['ratios']), **trade_stats(trades),
             'avg_r': sum(t['r'] for t in trades) / len(trades) if trades else None,
             'exposure_pct': result['exposure'] * 100, 'fees': result['state']['fees'],
             'halted': result['state']['halted']}
    seg = btc_rows[i0:i1]

    def hold(exposure):
        cfg = SimpleNamespace(exposure=exposure, fee=costs.fee, slippage=costs.slippage)
        return hold_ratios(seg, 0, cfg)
    curve = dict(bot=result['ratios'], btc_same_exposure=hold(max(result['exposure'], 1e-6)), btc_full=hold(1.0))
    return dict(bot=stats, btc_same_exposure=curve_stats(curve['btc_same_exposure']),
                btc_full=curve_stats(curve['btc_full']), curve=curve)


def verdict(s):
    bot, ref = s['bot'], s['btc_same_exposure']
    beats = bot['return_pct'] > ref['return_pct']
    text = (f"{'SUPERA' if beats else 'NO SUPERA'} a Bitcoin comprar y mantener con la misma exposición "
            f"({bot['return_pct'] - ref['return_pct']:+.2f} puntos), con "
            f"{'menos' if bot['max_drawdown_pct'] < ref['max_drawdown_pct'] else 'más o igual'} caída máxima.")
    if bot['trades'] < MIN_TRADES:
        text += f" NO CONCLUYENTE: solo {bot['trades']} operaciones (mínimo {MIN_TRADES})."
    return text


def windows(rows, params, size, start=None, end=None, costs=Costs(), review=None):
    """Independent consecutive windows of `size` candles in [start, end) with fresh capital each (no fitting)."""
    first = next(iter(rows.values()))
    i = start if start is not None else max(min_history(params), 200)
    end = end if end is not None else len(first)
    out = []
    while i + size <= end:
        result = run(rows, params, i, i + size, costs=costs, review=review)
        out.append(dict(first=first[i]['time'], last=first[i + size - 1]['time'],
                        **summarize(result, rows['BTCUSDT'], i, i + size, costs), trades_list=result['trades']))
        i += size
    return out


def report(name, items, params):
    """Per-window table plus totals over all windows, honest about not being fitted."""
    lines = [f'=== {name} ===',
             f"Parámetros fijos (no ajustados): riesgo {RULES.max_risk_per_trade * params.aggression:.2%} por operación, "
             f"máx. {RULES.max_open_positions} posiciones, límites {RULES.daily_loss_limit:.0%} diario / {RULES.weekly_loss_limit:.0%} semanal."]
    for i, w in enumerate(items, 1):
        b, r = w['bot'], w['btc_same_exposure']
        lines.append(f"Ventana {i}: {_day(w['first'])} a {_day(w['last'])} · bot {_n(b['return_pct'], '%')} "
                     f"(caída {_n(b['max_drawdown_pct'], '%')}, {b['trades']} op., exposición {_n(b['exposure_pct'], '%')}) · "
                     f"BTC misma exposición {_n(r['return_pct'], '%')} (caída {_n(r['max_drawdown_pct'], '%')})"
                     + (f" · DETENIDO: {b['halted']}" if b['halted'] else ''))
    total = aggregate(items)
    b, r, full = total['bot'], total['btc_same_exposure'], total['btc_full']
    lines += [f"TOTAL encadenado ({len(items)} ventanas): bot {_n(b['return_pct'], '%')}, caída máx. {_n(b['max_drawdown_pct'], '%')}, "
              f"{b['trades']} operaciones, aciertos {_n(b['win_rate_pct'], '%')}, "
              f"ganancia prom. {_n(b['avg_win_pct'], '%')} vs pérdida prom. {_n(b['avg_loss_pct'], '%')}, R prom. {_n(b['avg_r'])}",
              f"  BTC comprar y mantener con la misma exposición: {_n(r['return_pct'], '%')}, caída {_n(r['max_drawdown_pct'], '%')}",
              f"  BTC comprar y mantener 100%: {_n(full['return_pct'], '%')}, caída {_n(full['max_drawdown_pct'], '%')}",
              f"  Ventanas donde el bot supera a la referencia: {sum(w['bot']['return_pct'] > w['btc_same_exposure']['return_pct'] for w in items)} de {len(items)}",
              'VEREDICTO: ' + verdict(total),
              'Aviso: simulación con costos supuestos; el pasado no garantiza el futuro.']
    return '\n'.join(lines)


def _chain(parts):
    level, out = 1.0, []
    for part in parts:
        out.extend(level * x for x in part)
        level = out[-1]
    return out


def aggregate(items):
    """Chain windows into one result (each window starts flat with fresh capital)."""
    trades = [t for w in items for t in w['trades_list']]

    def chained(key):
        return curve_stats(_chain([w['curve'][key] for w in items]))
    exposure = sum(w['bot']['exposure_pct'] for w in items) / len(items)
    return dict(bot={**chained('bot'), **trade_stats(trades), 'exposure_pct': exposure,
                     'avg_r': sum(t['r'] for t in trades) / len(trades) if trades else None},
                btc_same_exposure=chained('btc_same_exposure'), btc_full=chained('btc_full'))
