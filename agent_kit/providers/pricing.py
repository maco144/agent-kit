"""Shared model price lookup for provider adapters."""

from __future__ import annotations

import logging

logger = logging.getLogger("agent_kit.providers")

_warned: set[str] = set()
_custom: dict[str, tuple[float, float]] = {}
_custom_cached: dict[str, float] = {}


def set_price(
    model_prefix: str,
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
    cached_input_usd_per_mtok: float | None = None,
) -> None:
    """
    Price a model the built-in tables don't know (or override one). Longest prefix wins.

    ``cached_input_usd_per_mtok`` is the rate for prompt-cache reads; without it the provider's
    default discount applies (Anthropic 0.1x input, OpenAI 0.5x).
    """
    _custom[model_prefix] = (input_usd_per_mtok, output_usd_per_mtok)
    if cached_input_usd_per_mtok is None:
        _custom_cached.pop(model_prefix, None)
    else:
        _custom_cached[model_prefix] = cached_input_usd_per_mtok


def clear_prices() -> None:
    """Remove every price registered with set_price()."""
    _custom.clear()
    _custom_cached.clear()


def cached_input_rate(model: str) -> float | None:
    """USD per million cache-read tokens registered with set_price(), or None for the provider default."""
    matches = [prefix for prefix in _custom_cached if model.startswith(prefix)]
    return _custom_cached[max(matches, key=len)] if matches else None


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


def lookup_multiplier(table: dict[str, float], model: str) -> float | None:
    """The value for the longest prefix of ``model`` in ``table``, or None."""
    matches = [prefix for prefix in table if model.startswith(prefix)]
    return table[max(matches, key=len)] if matches else None
