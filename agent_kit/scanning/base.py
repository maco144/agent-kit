"""Span collection and the Scanner protocol."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agent_kit.types import Finding

ENVELOPE_KEY = "agentkit_scan"

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class TextSpan:
    """One string from a tool result and its JSON path ("$", "$.results[2].snippet", "$error")."""

    path: str
    text: str


class Scanner(Protocol):
    name: str

    async def scan(self, spans: Sequence[TextSpan]) -> list[Finding]: ...


def collect_spans(output: Any, error: str | None, max_chars: int = 200_000) -> list[TextSpan]:
    """Every string in a tool result, dict keys included, with its JSON path; at most ``max_chars`` characters."""
    spans: list[TextSpan] = []
    remaining = max_chars

    def add(path: str, text: str) -> bool:
        nonlocal remaining
        if not text:
            return True
        spans.append(TextSpan(path, text[:remaining]))
        remaining -= min(len(text), remaining)
        return remaining > 0

    def walk(node: Any, path: str) -> bool:
        if isinstance(node, str):
            return add(path, node)
        if isinstance(node, dict):
            if ENVELOPE_KEY in node:
                return True  # already scanned and wrapped
            for key, value in node.items():
                child = _child_path(path, key)
                if isinstance(key, str) and not add(f"{child}#key", key):
                    return False
                if not walk(value, child):
                    return False
            return True
        if isinstance(node, (list, tuple)):
            return all(walk(item, f"{path}[{i}]") for i, item in enumerate(node))
        return True

    if max_chars > 0 and walk(output, "$") and error:
        add("$error", error)
    return spans


def _child_path(path: str, key: Any) -> str:
    if isinstance(key, str) and _IDENTIFIER.fullmatch(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(str(key))}]"
