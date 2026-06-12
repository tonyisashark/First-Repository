"""kalshi_bot -- an autonomous, risk-managed Kalshi trading bot.

The bot sizes every trade as a fraction of *current* portfolio equity, so
profits automatically increase position sizes (and losses shrink them):
that is the compounding mechanism. Three independent strategies feed a
shared risk layer; nothing trades without passing exposure caps, drawdown
circuit breakers, and a kill switch.
"""

__version__ = "1.0.0"
