"""Educational spot simulator. No exchange order methods or credentials."""
import math
import random
from dataclasses import dataclass, asdict


@dataclass
class Config:
    capital: float = 100
    fast: int = 10
    slow: int = 30
    fee: float = 0.001
    slippage: float = 0.0005
    exposure: float = 0.25
    max_drawdown: float = 0.10

    def validate(self):
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in asdict(self).values()):
            raise ValueError('Parámetros numéricos finitos requeridos.')
        if not (1 <= self.capital <= 1e7 and isinstance(self.fast, int) and isinstance(self.slow, int)
                and 2 <= self.fast < self.slow <= 200 and 0 <= self.fee <= .05
                and 0 <= self.slippage <= .05 and 0 < self.exposure <= 1
                and 0 < self.max_drawdown <= .8):
            raise ValueError('Parámetros fuera de rango.')


def demo(n=500):
    rng = random.Random(42)
    price, rows = 30000., []
    for i in range(n):
        opening = price
        price *= math.exp(.0002 + .006 * math.sin(i / 22) + rng.gauss(0, .008))
        rows.append({'time': 1704067200000 + i * 3600000, 'open': opening, 'close': price})
    return rows


def signal(closes, cfg):
    if len(closes) < max(cfg.slow, 15):
        return False
    changes = [b-a for a,b in zip(closes[-15:-1], closes[-14:])]
    gain = sum(max(x, 0) for x in changes)
    loss = sum(max(-x, 0) for x in changes)
    rsi = 50 if gain + loss == 0 else 100 * gain / (gain + loss)
    return sum(closes[-cfg.fast:])/cfg.fast > sum(closes[-cfg.slow:])/cfg.slow and rsi < 70


def validate_rows(rows, previous=-1):
    for row in rows:
        if not all(math.isfinite(row[k]) and row[k] > 0 for k in ('time','open','close')) or row['time'] <= previous:
            raise ValueError('Velas inválidas o desordenadas.')
        previous = row['time']


def new_state(cfg):
    return dict(cash=cfg.capital, units=0., peak=cfg.capital, halted=False,
                pending_halt=False, desired=False, fees=0., worst=0.)


def step(state, row, cfg, history):
    """Process one candle. Mutates state and history (closes up to and including this candle).

    A signal at close t can only execute at open t+1. Returns (equity, trade or None).
    """
    if state['pending_halt']:
        state['halted'] = True
    halted = state['halted']
    reason = 'Límite de caída' if halted else 'Señal'
    trade = None
    if state['units'] and (not state['desired'] or halted):
        price = row['open'] * (1-cfg.slippage)
        quantity = state['units']
        fee = quantity * price * cfg.fee
        state['cash'] += quantity * price - fee
        state['units'] = 0.
        trade = dict(time=row['time'], side='VENTA', price=price, quantity=quantity, fee=fee, reason=reason)
    elif state['desired'] and not state['units'] and not halted:
        budget = state['cash'] * cfg.exposure
        price = row['open'] * (1+cfg.slippage)
        quantity = budget / (price * (1+cfg.fee))
        fee = quantity * price * cfg.fee
        state['cash'] -= budget
        state['units'] = quantity
        trade = dict(time=row['time'], side='COMPRA', price=price, quantity=quantity, fee=fee, reason=reason)
    if trade:
        state['fees'] += trade['fee']
    # Equity is estimated net liquidation value, including hypothetical exit costs.
    equity = state['cash'] + state['units'] * row['close'] * (1-cfg.slippage) * (1-cfg.fee)
    state['peak'] = max(state['peak'], equity)
    dd = 1-equity/state['peak']
    state['worst'] = max(state['worst'], dd)
    if dd >= cfg.max_drawdown:
        state['pending_halt'] = True
    history.append(row['close'])
    state['desired'] = signal(history, cfg)
    return equity, trade


def simulate(rows, cfg):
    cfg.validate()
    if len(rows) < cfg.slow + 2:
        raise ValueError('No hay suficientes velas para las medias elegidas.')
    validate_rows(rows)
    state = new_state(cfg)
    history, trades, curve = [], [], []
    start = max(cfg.slow, 15)
    benchmark_units = cfg.capital / (rows[start]['open'] * (1+cfg.slippage) * (1+cfg.fee))
    for i, row in enumerate(rows):
        equity, trade = step(state, row, cfg, history)
        if trade:
            trades.append(trade)
        benchmark = cfg.capital if i < start else benchmark_units * row['close'] * (1-cfg.slippage) * (1-cfg.fee)
        curve.append(dict(time=row['time'], equity=equity, benchmark=benchmark, close=row['close']))
    return dict(config=asdict(cfg), curve=curve, trades=trades, cash=state['cash'], units=state['units'],
                equity=equity, return_pct=(equity/cfg.capital-1)*100, drawdown_pct=state['worst']*100,
                fees=state['fees'], halted=state['halted'] or state['pending_halt'],
                benchmark_pct=(benchmark/cfg.capital-1)*100)
