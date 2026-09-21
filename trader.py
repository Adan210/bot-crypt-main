"""Portfolio engine shared by the backtest and paper trading (spot, long only, multi-coin).

Per candle t, in this order (nothing looks ahead):
  1. entries decided at the close of t-1 fill at the OPEN of t (adverse slippage + fee), sized by the risk engine;
  2. stops and targets are checked with the candle's low/high (stop first if both are touched);
  3. equity is marked at the close; daily/weekly loss limits may halt the bot;
  4. trailing stops move using the close (effective from t+1);
  5. new signals are decided at the close, filtered by vetoes/risk rules, and queued for the next open.

State is a plain JSON-friendly dict so it can be saved and recovered after a restart.
Stops fill at the stop price, or at the open when the price gaps through it, so a gap can lose
more than the planned 1%.
"""
from dataclasses import dataclass

import extras
import risk
import signals
from rules import RULES

DAY = 86400000


@dataclass(frozen=True)
class Costs:
    fee: float = 0.001        # per side
    slippage: float = 0.0005  # adverse, per side


def new_state(capital):
    return dict(cash=float(capital), positions={}, pending=[], fees=0., equity=float(capital), peak=float(capital),
                worst=0., day=None, day_start=float(capital), week=None, week_start=float(capital),
                halted=None, mode='operate', exposure_sum=0., steps=0)


def _roll(state, t):
    """New UTC day / Monday-based week: remember the equity the loss limits are measured from."""
    day = t // DAY
    if state['day'] != day:
        state['day'], state['day_start'] = day, state['equity']
        week = (day + 3) // 7  # 1970-01-01 was a Thursday
        if state['week'] != week:
            state['week'], state['week_start'] = week, state['equity']


def _sell(state, pos, qty, fill, costs):
    price = fill * (1 - costs.slippage)
    fee = qty * price * costs.fee
    state['cash'] += qty * price - fee
    state['fees'] += fee
    pos['qty'] -= qty
    pos['proceeds'] += qty * price - fee
    return price


def _close(state, pos, t, price, why, events):
    risk0 = pos['risk']  # planned loss at the stop, costs included: a stop-out is -1R
    pnl = pos['proceeds'] - pos['cost']
    del state['positions'][pos['symbol']]
    events.append(dict(kind='trade', time=t, trade=dict(
        symbol=pos['symbol'], opened=pos['opened'], closed=t, entry=pos['entry'], exit=price, qty=pos['qty0'],
        pnl=pnl, pnl_pct=(pos['proceeds'] / pos['cost'] - 1) * 100, r=pnl / risk0 if risk0 > 0 else 0.,
        reason_entry=pos['reason'], reason_exit=why, regime=pos['regime'], context=pos['context'])))


def _mark(state, costs, closes):
    equity, invested = state['cash'], 0.
    for sym, pos in state['positions'].items():
        pos['last_close'] = closes.get(sym, pos['last_close'])
        value = pos['qty'] * pos['last_close'] * (1 - costs.slippage) * (1 - costs.fee)
        equity += value
        invested += value
    return equity, invested


def _fill_orders(state, candles, params, costs, rules, events):
    keep = []
    for order in state['pending']:
        c = candles.get(order['symbol'])
        if c is None:
            keep.append(order)  # no candle for this coin now: try at its next one
            continue
        entry = c['open'] * (1 + costs.slippage)
        qty = 0. if state['halted'] or state['mode'] != 'operate' else risk.position_size(
            state['equity'], state['cash'], entry, order['stop'], costs.fee, costs.slippage, params.aggression, rules)
        if qty <= 0:
            events.append(dict(kind='skip', time=c['time'], symbol=order['symbol'], why='Orden cancelada al abrir'))
            continue
        cost = qty * entry * (1 + costs.fee)
        state['cash'] -= cost
        state['fees'] += qty * entry * costs.fee
        state['positions'][order['symbol']] = dict(
            symbol=order['symbol'], qty0=qty, qty=qty, entry=entry, stop=order['stop'], stop0=order['stop'],
            tp_price=order['tp_price'], tp_fraction=order['tp_fraction'], tp_done=False, trail_atr=order['trail_atr'],
            atr=order['atr'], highest=entry, opened=c['time'], cost=cost, proceeds=0., last_close=c['close'],
            risk=qty * (entry * (1 + costs.fee) - order['stop'] * (1 - costs.slippage) * (1 - costs.fee)),
            reason=order['reason'], regime=order['regime'], context=order['context'])
        events.append(dict(kind='entry', time=c['time'], symbol=order['symbol'], price=entry, qty=qty,
                           stop=order['stop'], reason=order['reason']))
    state['pending'] = keep


def _manage(state, candles, costs, events):
    for sym, pos in list(state['positions'].items()):
        c = candles.get(sym)
        if c is None:
            continue
        if c['low'] <= pos['stop']:  # stop wins if the same candle also reaches the target
            price = _sell(state, pos, pos['qty'], min(c['open'], pos['stop']), costs)
            _close(state, pos, c['time'], price, 'Stop loss' if pos['stop'] <= pos['stop0'] else 'Stop de protección', events)
        elif not pos['tp_done'] and c['high'] >= pos['tp_price']:
            fill = max(pos['tp_price'], c['open'])
            if pos['tp_fraction'] >= 1:
                _close(state, pos, c['time'], _sell(state, pos, pos['qty'], fill, costs), 'Objetivo alcanzado', events)
            else:
                price = _sell(state, pos, pos['qty'] * pos['tp_fraction'], fill, costs)
                pos['tp_done'], pos['stop'] = True, max(pos['stop'], pos['entry'])  # rest is now risk-free
                events.append(dict(kind='partial', time=c['time'], symbol=sym, price=price, qty=pos['qty0'] * pos['tp_fraction']))


def _trail(state, candles):
    for sym, pos in state['positions'].items():
        c = candles.get(sym)
        if c and pos['trail_atr'] > 0:
            pos['highest'] = max(pos['highest'], c['close'])
            pos['stop'] = max(pos['stop'], pos['highest'] - pos['trail_atr'] * pos['atr'])


def _check_limits(state, rules, events, t):
    day_loss, week_loss = 1 - state['equity'] / state['day_start'], 1 - state['equity'] / state['week_start']
    why = ('Límite de pérdida diaria' if day_loss >= rules.daily_loss_limit else
           'Límite de pérdida semanal' if week_loss >= rules.weekly_loss_limit else None)
    if why and not state['halted']:
        state['halted'], state['pending'] = why, []
        events.append(dict(kind='halt', time=t, why=why))


def _new_orders(state, candles, histories, params, ctx, rules, review, events, t):
    found = []
    held = set(state['positions']) | {o['symbol'] for o in state['pending']}
    for sym, rows in histories.items():
        if sym in held or sym not in candles:
            continue
        cand = signals.evaluate(sym, rows, params)
        if not cand:
            continue
        why = extras.veto(sym, ctx)
        if why:
            events.append(dict(kind='skip', time=t, symbol=sym, why=why))
        else:
            found.append(cand)
    found.sort(key=lambda c: c['score'], reverse=True)
    if review and found:
        # The reviewer (e.g. AI) may only REMOVE candidates: whatever it returns is matched back to the
        # originals, so it cannot add a coin or alter a stop, target or size.
        originals = {c['symbol']: c for c in found}
        found = [originals[c['symbol']] for c in review(list(found), ctx, events) if c.get('symbol') in originals]
        for e in events:
            if e.get('time') == 0:
                e['time'] = t  # events the reviewer added carry the candle time
    for cand in found:
        why = risk.entry_block(cand['symbol'], held, histories, rules)
        if why:
            events.append(dict(kind='skip', time=t, symbol=cand['symbol'], why=why))
            continue
        held.add(cand['symbol'])
        state['pending'].append(dict(
            symbol=cand['symbol'], stop=cand['stop'], tp_price=cand['tp_price'], tp_fraction=cand['tp_fraction'],
            trail_atr=cand['trail_atr'], atr=cand['atr'], reason=cand['reason'], regime=cand['regime'],
            decided=t, context=dict(ctx or {}, close=cand['close'], atr_pct=cand['atr'] / cand['close'] * 100)))


def step(state, candles, histories, params, costs=Costs(), ctx=None, rules=RULES, review=None):
    """Advance one candle time. `candles` {symbol: candle at t}; `histories` {symbol: candles up to and including t}."""
    t = max(c['time'] for c in candles.values())
    events = []
    _roll(state, t)
    _fill_orders(state, candles, params, costs, rules, events)
    _manage(state, candles, costs, events)
    equity, invested = _mark(state, costs, {s: c['close'] for s, c in candles.items()})
    state['equity'] = equity
    state['peak'] = max(state['peak'], equity)
    state['worst'] = max(state['worst'], 1 - equity / state['peak'])
    state['exposure_sum'] += invested / equity if equity > 0 else 0.
    state['steps'] += 1
    _check_limits(state, rules, events, t)
    _trail(state, candles)
    if not state['halted'] and state['mode'] == 'operate':
        _new_orders(state, candles, histories, params, ctx, rules, review, events, t)
    return events


def reactivate(state):
    """Manual restart after a halt. Loss limits are measured again from the current equity."""
    state['halted'] = None
    state['day_start'] = state['week_start'] = state['equity']


def set_mode(state, mode):
    if mode not in ('operate', 'close_only'):
        raise ValueError('Modo desconocido.')
    state['mode'] = mode
    if mode == 'close_only':
        state['pending'] = []


def emergency(state, t, costs=Costs()):
    """Emergency button: close everything at the last known prices and halt until reactivated."""
    events = []
    for pos in list(state['positions'].values()):
        price = _sell(state, pos, pos['qty'], pos['last_close'], costs)
        _close(state, pos, t, price, 'Emergencia', events)
    state['pending'], state['halted'] = [], 'Emergencia'
    state['equity'] = state['cash']
    events.append(dict(kind='halt', time=t, why='Emergencia'))
    return events
