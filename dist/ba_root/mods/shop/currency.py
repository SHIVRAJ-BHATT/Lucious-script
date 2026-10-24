# Released under the MIT License. See LICENSE for details.
"""LUCIOUSBELLY (LB): the server's currency name and formatting."""

CURRENCY_NAME = "LUCIOUSBELLY"
SYMBOL = "LB"


def fmt(amount: int) -> str:
    return f"{int(amount):,} {SYMBOL}"
