"""LOCKED safety rules. Nothing in this project (AI, learning, versions, UI) may change them.

They are a frozen dataclass and no module offers a way to modify them. test_rules.py pins every value
and fails if one changes, so changing a rule requires a deliberate human edit of BOTH files.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Rules:
    max_risk_per_trade: float = 0.01   # 1% of capital lost if the stop is hit
    daily_loss_limit: float = 0.03     # bot stops until the human reactivates it
    weekly_loss_limit: float = 0.06
    max_open_positions: int = 3
    max_position_fraction: float = 0.34  # notional of one position, as a share of capital
    max_correlation: float = 0.85      # do not open a coin this correlated with one already held
    max_aggression: float = 1.0        # ceiling for the adjustable aggression dial
    stop_loss_required: bool = True


RULES = Rules()
