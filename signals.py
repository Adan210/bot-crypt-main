"""Phase 2: market regime detector and one strategy per regime (spot, long only).

The bot only trades a CLEAR signal: in a clean uptrend it buys breakouts confirmed by volume;
in a sideways market it CAN buy deep oversold dips (switched off by default, see use_range); in between
(transition) and in downtrends it stays out. Fewer trades matter because each one pays costs.

Parameters are fixed a priori, not fitted to data. Only TUNABLE ones may be changed later,
and only through the guarded process in versions.py.
"""
from dataclasses import dataclass

from indicators import atr, bollinger, efficiency_ratio, highest_high, rsi, sma, volume_ratio

WINDOW = 320  # candles of history the strategy is given


@dataclass(frozen=True)
class Params:
    # regime
    er_window: int = 48          # candles used to judge trend vs. chop
    er_trend: float = 0.30       # efficiency at or above this = trending
    er_range: float = 0.20       # efficiency at or below this = sideways
    trend_sma: int = 100         # price above this average = uptrend
    # trend strategy: breakout
    donchian: int = 48
    vol_ratio: float = 1.2
    rsi_max: float = 75.0
    stop_atr_trend: float = 2.5
    tp_r: float = 2.0            # first target, in multiples of the risk
    tp_fraction: float = 0.5     # share sold at the first target
    trail_atr: float = 3.0
    # range strategy: oversold dip
    bb_n: int = 20
    bb_k: float = 2.0
    rsi_range: float = 30.0
    stop_atr_range: float = 2.0
    min_edge: float = 0.01       # target must be at least 1% away (costs are ~0.3%)
    use_range: bool = False      # OFF: in development data it lost money (avg -0.22R over 284 trades)
    atr_n: int = 14
    # risk appetite; the locked maximum is in rules.py
    aggression: float = 0.5


# name -> (min, max). Anything not listed here can never be changed by AI or learning.
TUNABLE = {
    'er_trend': (0.2, 0.6), 'er_range': (0.05, 0.3), 'donchian': (24, 96), 'vol_ratio': (1.0, 2.5),
    'rsi_max': (60.0, 85.0), 'stop_atr_trend': (1.5, 4.0), 'tp_r': (1.0, 4.0), 'tp_fraction': (0.2, 0.8),
    'trail_atr': (2.0, 5.0), 'rsi_range': (15.0, 40.0), 'stop_atr_range': (1.0, 3.0),
    'min_edge': (0.005, 0.03), 'aggression': (0.1, 1.0),
}


def min_history(p):
    return max(p.er_window + 1, p.trend_sma, p.donchian + 1, p.bb_n, p.atr_n + 1, 25)


def regime(rows, p):
    """'tendencia_alcista', 'tendencia_bajista', 'lateral', 'transicion' or None without enough history."""
    if len(rows) < min_history(p):
        return None
    closes = [r['close'] for r in rows]
    er = efficiency_ratio(closes, p.er_window)
    if er >= p.er_trend:
        return 'tendencia_alcista' if closes[-1] > sma(closes, p.trend_sma) else 'tendencia_bajista'
    return 'lateral' if er <= p.er_range else 'transicion'


def evaluate(symbol, rows, p):
    """A long entry candidate decided at the close of rows[-1], or None. Executes at the next open."""
    kind = regime(rows, p)
    if kind not in ('tendencia_alcista', 'lateral') or (kind == 'lateral' and not p.use_range):
        return None
    closes = [r['close'] for r in rows]
    close, a, r14 = closes[-1], atr(rows, p.atr_n), rsi(closes)
    common = dict(symbol=symbol, side='long', regime=kind, close=close, atr=a)
    if kind == 'tendencia_alcista':
        level, vr = highest_high(rows, p.donchian), volume_ratio(rows)
        if close > level and vr >= p.vol_ratio and r14 < p.rsi_max:
            stop = close - p.stop_atr_trend * a
            if stop <= 0:
                return None
            return dict(common, stop=stop, tp_price=close + p.tp_r * (close - stop), tp_fraction=p.tp_fraction,
                        trail_atr=p.trail_atr, score=vr,
                        reason=f'Ruptura de máximo de {p.donchian}h en tendencia alcista, volumen x{vr:.1f}, RSI {r14:.0f}')
        return None
    mid, low, _ = bollinger(closes, p.bb_n, p.bb_k)
    if close <= low and r14 <= p.rsi_range and mid / close - 1 >= p.min_edge:
        stop = close - p.stop_atr_range * a
        if stop <= 0:
            return None
        return dict(common, stop=stop, tp_price=mid, tp_fraction=1.0, trail_atr=0.0, score=p.rsi_range - r14 + 1,
                    reason=f'Sobreventa en mercado lateral (RSI {r14:.0f}, bajo la banda), objetivo la media {mid:.2f}')
    return None
