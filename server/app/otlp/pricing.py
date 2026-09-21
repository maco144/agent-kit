"""
Model prices for OTLP-ingested runs.

A copy of the SDK's tables (agent_kit/providers/anthropic.py, openai.py) — the
server doesn't depend on the SDK. Keep the two in step when prices change.
"""

from __future__ import annotations

# USD per million tokens (input, output). Longest matching prefix wins, so every -pro model is listed.
# OpenAI: standard tier, developers.openai.com/api/docs/pricing, 2026-09-21.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5":    (10.00, 50.00),
    "claude-mythos-5":   (10.00, 50.00),
    "claude-opus-5":     (5.00,  25.00),
    "claude-opus-4-8":   (5.00,  25.00),
    "claude-opus-4-7":   (5.00,  25.00),
    "claude-opus-4-6":   (5.00,  25.00),
    "claude-opus-4-5":   (5.00,  25.00),
    "claude-opus-4":     (15.00, 75.00),
    "claude-sonnet-5":   (2.00,  10.00),
    "claude-sonnet-4":   (3.00,  15.00),
    "claude-haiku-4-5":  (1.00,  5.00),
    "claude-3-7-sonnet": (3.00,  15.00),
    "claude-3-5-sonnet": (3.00,  15.00),
    "claude-3-5-haiku":  (0.80,  4.00),
    "claude-3-opus":     (15.00, 75.00),
    "claude-3-haiku":    (0.25,  1.25),
    "gpt-6-astra":       (10.00, 50.00),
    "gpt-5.6-sol":       (4.00,  20.00),
    "gpt-5.6-terra":     (2.00,  12.00),
    "gpt-5.6-luna":      (0.20,  1.20),
    "gpt-5.5":           (5.00,  30.00),
    "gpt-5.5-pro":       (30.00, 180.00),
    "gpt-5.4":           (2.50,  15.00),
    "gpt-5.4-mini":      (0.75,  4.50),
    "gpt-5.4-nano":      (0.20,  1.25),
    "gpt-5.4-pro":       (30.00, 180.00),
    "gpt-5.3-codex":     (1.75,  14.00),
    "gpt-5.2":           (1.75,  14.00),
    "gpt-5.2-pro":       (21.00, 168.00),
    "gpt-5.1":           (1.25,  10.00),
    "gpt-5":             (1.25,  10.00),
    "gpt-5-mini":        (0.25,  2.00),
    "gpt-5-nano":        (0.05,  0.40),
    "gpt-5-pro":         (15.00, 120.00),
    "gpt-4.1":           (2.00,  8.00),
    "gpt-4.1-mini":      (0.40,  1.60),
    "gpt-4.1-nano":      (0.10,  0.40),
    "gpt-4o":            (2.50,  10.00),
    "gpt-4o-mini":       (0.15,  0.60),
    "gpt-4-turbo":       (10.00, 30.00),
    "gpt-4":             (30.00, 60.00),
    "gpt-3.5-turbo":     (0.50,  1.50),
    "o1":                (15.00, 60.00),
    "o1-pro":            (150.00, 600.00),
    "o1-mini":           (3.00,  12.00),
    "o3":                (2.00,  8.00),
    "o3-mini":           (1.10,  4.40),
    "o3-pro":            (20.00, 80.00),
    "o4-mini":           (1.10,  4.40),
}

# Cache reads bill at 0.1x input except where listed (longest prefix wins). OpenAI: 0.5x.
_CACHE_READ_MULTIPLIER: dict[str, float] = {
    "claude-fable-5-1": 0.025,
    "gpt-": 0.5,
    "gpt-6": 0.1,
    "gpt-5": 0.1,
    "gpt-4.1": 0.25,
    "o1": 0.5,
    "o3": 0.25,
    "o3-mini": 0.5,
    "o4-mini": 0.25,
}
_CACHE_WRITE_MULTIPLIER = 1.25


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """USD for one model call; 0.0 for unpriced models. ``input_tokens`` excludes cached tokens."""
    matches = [prefix for prefix in _PRICES if model and model.startswith(prefix)]
    if not matches:
        return 0.0
    in_rate, out_rate = _PRICES[max(matches, key=len)]
    read_matches = [prefix for prefix in _CACHE_READ_MULTIPLIER if model.startswith(prefix)]
    read_multiplier = _CACHE_READ_MULTIPLIER[max(read_matches, key=len)] if read_matches else 0.1
    return (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_read_tokens * in_rate * read_multiplier
        + cache_write_tokens * in_rate * _CACHE_WRITE_MULTIPLIER
    ) / 1_000_000
