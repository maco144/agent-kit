"""Shared model price lookup for provider adapters."""

from __future__ import annotations

import logging

logger = logging.getLogger("agent_kit.providers")

_warned: set[str] = set()


def lookup_rates(table: dict[str, tuple[float, float]], model: str) -> tuple[float, float] | None:
    """
    Return (input, output) USD per million tokens for the longest table prefix matching ``model``.

    Unknown models return None and log one warning per model, so a missing price
    shows up in logs instead of as a silent $0.00.
    """
    matches = [prefix for prefix in table if model.startswith(prefix)]
    if matches:
        return table[max(matches, key=len)]
    if model not in _warned:
        _warned.add(model)
        logger.warning("No pricing for model %r; cost_usd will be reported as 0.0", model)
    return None
