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


def simulate(rows, cfg):
    cfg.validate()
    if len(rows) < cfg.slow + 2:
        raise ValueError('No hay suficientes velas para las medias elegidas.')
    previous = -1
    for row in rows:
        if not all(math.isfinite(row[k]) and row[k] > 0 for k in ('time','open','close')) or row['time'] <= previous:
            raise ValueError('Velas inválidas o desordenadas.')
        previous = row['time']
    cash, units, peak = cfg.capital, 0., cfg.capital
    history, trades, curve = [], [], []
    halted, pending_halt, desired = False, False, False
    fees, worst = 0., 0.
    start = max(cfg.slow, 15)
    benchmark_units = cfg.capital / (rows[start]['open'] * (1+cfg.slippage) * (1+cfg.fee))
    for i, row in enumerate(rows):
        # A signal at close t can only execute at open t+1.
        if pending_halt:
            halted = True
        reason = 'Límite de caída' if halted else 'Señal'
        side = None
        if units and (not desired or halted):
            price = row['open'] * (1-cfg.slippage)
            quantity = units
            fee = quantity * price * cfg.fee
            cash += quantity * price - fee
            units, side = 0., 'VENTA'
        elif desired and not units and not halted:
            budget = cash * cfg.exposure
            price = row['open'] * (1+cfg.slippage)
            quantity = budget / (price * (1+cfg.fee))
            fee = quantity * price * cfg.fee
            cash -= budget
            units, side = quantity, 'COMPRA'
        if side:
            fees += fee
            trades.append(dict(time=row['time'], side=side, price=price, quantity=quantity, fee=fee, reason=reason))
        # Equity is estimated net liquidation value, including hypothetical exit costs.
        equity = cash + units * row['close'] * (1-cfg.slippage) * (1-cfg.fee)
        peak = max(peak, equity)
        dd = 1-equity/peak
        worst = max(worst, dd)
        if dd >= cfg.max_drawdown:
            pending_halt = True
        benchmark = cfg.capital if i < start else benchmark_units * row['close'] * (1-cfg.slippage) * (1-cfg.fee)
        curve.append(dict(time=row['time'], equity=equity, benchmark=benchmark, close=row['close']))
        history.append(row['close'])
        desired = signal(history, cfg)
    return dict(config=asdict(cfg), curve=curve, trades=trades, cash=cash, units=units,
                equity=equity, return_pct=(equity/cfg.capital-1)*100, drawdown_pct=worst*100,
                fees=fees, halted=halted or pending_halt, benchmark_pct=(benchmark/cfg.capital-1)*100)
