"""Risk engine: position sizing and entry permission. Always applies RULES; callers cannot loosen them."""
from indicators import correlation, returns
from rules import RULES

CORR_WINDOW = 168  # one week of hourly candles


def clamp_aggression(aggression, rules=RULES):
    """The adjustable dial can go down freely but never above the locked maximum."""
    try:
        value = float(aggression)
    except (TypeError, ValueError):
        return 0.0
    return min(max(value, 0.0), rules.max_aggression)


def position_size(equity, cash, entry, stop, fee, slippage, aggression, rules=RULES):
    """Units to buy so that hitting the stop loses at most max_risk_per_trade * aggression of equity.

    Loss per unit includes entry and exit costs. Also capped by the position-size rule and by cash.
    Returns 0 when the trade is not allowed (no valid stop below the entry).
    """
    if rules.stop_loss_required and not (0 < stop < entry):
        return 0.0
    unit_loss = entry * (1 + fee) - stop * (1 - slippage) * (1 - fee)
    if unit_loss <= 0 or equity <= 0:
        return 0.0
    qty = equity * rules.max_risk_per_trade * clamp_aggression(aggression, rules) / unit_loss
    qty = min(qty, equity * rules.max_position_fraction / (entry * (1 + fee)), cash / (entry * (1 + fee)))
    return max(qty, 0.0)


def entry_block(symbol, held, histories, rules=RULES):
    """Reason not to open `symbol` given the symbols already held or queued, or None."""
    if len(held) >= rules.max_open_positions:
        return 'Máximo de posiciones abiertas'
    mine = returns([r['close'] for r in histories[symbol][-CORR_WINDOW - 1:]])
    for other in held:
        theirs = returns([r['close'] for r in histories.get(other, [])[-CORR_WINDOW - 1:]])
        if len(mine) >= 24 and len(theirs) >= 24:
            corr = correlation(mine, theirs)
            if corr > rules.max_correlation:
                return f'Correlación {corr:.2f} con {other} (máximo {rules.max_correlation})'
    return None
