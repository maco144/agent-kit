"""Shared model price lookup for provider adapters."""

from __future__ import annotations

import logging

logger = logging.getLogger("agent_kit.providers")

_warned: set[str] = set()
_custom: dict[str, tuple[float, float]] = {}


def set_price(model_prefix: str, input_usd_per_mtok: float, output_usd_per_mtok: float) -> None:
    """Price a model the built-in tables don't know (or override one). Longest prefix wins."""
    _custom[model_prefix] = (input_usd_per_mtok, output_usd_per_mtok)


def clear_prices() -> None:
    """Remove every price registered with set_price()."""
    _custom.clear()


def lookup_rates(table: dict[str, tuple[float, float]], model: str) -> tuple[float, float] | None:
    """
    Return (input, output) USD per million tokens for the longest table prefix matching ``model``.

    Unknown models return None and log one warning per model, so a missing price
    shows up in logs instead of as a silent $0.00.
    """
    for prices in (_custom, table):
        matches = [prefix for prefix in prices if model.startswith(prefix)]
        if matches:
            return prices[max(matches, key=len)]
    if model not in _warned:
        _warned.add(model)
        logger.warning("No pricing for model %r; cost_usd will be reported as 0.0", model)
    return None
