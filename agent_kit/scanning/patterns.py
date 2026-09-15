"""PatternScanner — local rules for prompt-injection techniques in tool output. No network, no dependencies."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from agent_kit.scanning.base import TextSpan
from agent_kit.types import Finding, Severity


@dataclass(frozen=True)
class PatternRule:
    name: str
    severity: Severity
    message: str  # fixed description; never the matched text
    check: Callable[[str], bool]


def _char_class(*ranges: tuple[int, int]) -> str:
    """A regex character class from code point ranges (keeps invisible characters out of this file)."""
    return "[" + "".join(re.escape(chr(lo)) + "-" + re.escape(chr(hi)) for lo, hi in ranges) + "]"


_TAG_CHARS = re.compile(_char_class((0xE0000, 0xE007F)))
_ROLE_TOKEN = re.compile(
    r"<\|(?:im_start|im_end|system)\|>"
    r"|\[/?INST\]"
    r"|<<\s*/?SYS\s*>>"
    r"|^[ \t]*(?:</?system>|</tool_result>)",
    re.IGNORECASE | re.MULTILINE,
)
_OVERRIDE = re.compile(
    r"\b(?:ignore|disregard|forget)\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|earlier|preceding)\s+"
    r"(?:instructions?|prompts?|rules?|directions?|context)\b"
    r"|\boverride\s+(?:the\s+)?(?:system|safety|security)\s+(?:prompt|instructions?|rules?|filters?)\b"
    r"|\bnew\s+system\s+(?:prompt|instructions?)\s*:",
    re.IGNORECASE,
)
_BIDI = re.compile(_char_class((0x202A, 0x202E), (0x2066, 0x2069)))
_ZERO_WIDTH_RUN = re.compile(_char_class((0x200B, 0x200D), (0x2060, 0x2060), (0xFEFF, 0xFEFF)) + "{3,}")
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_MARKDOWN_URL = re.compile(r"!?\[[^\]\n]*\]\((https?://[^\s)]+)\)", re.IGNORECASE)
_PERSONA = re.compile(
    r"\byou\s+are\s+now\s+(?:(?-i:DAN)\b|in\s+developer\s+mode|jailbroken|unrestricted)"
    r"|\bdeveloper\s+mode\s+(?:enabled|activated)"
    r"|\bact\s+as\s+if\s+you\s+have\s+no\s+(?:restrictions|rules|guidelines)",
    re.IGNORECASE,
)


def _encoded_payload(text: str) -> bool:
    for match in _BASE64_RUN.finditer(text):
        blob = match.group()
        try:
            decoded = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if _OVERRIDE.search(decoded) or _ROLE_TOKEN.search(decoded):
            return True
    return False


def _exfil_markdown(text: str) -> bool:
    for match in _MARKDOWN_URL.finditer(text):
        query = urlsplit(match.group(1)).query
        if not query:
            continue
        if "{" in query or "}" in query or "%7b" in query.lower():
            return True
        if any(len(value) >= 16 for _, value in parse_qsl(query, keep_blank_values=True)):
            return True
    return False


BUILTIN_RULES: tuple[PatternRule, ...] = (
    PatternRule("unicode_tags", "critical", "invisible Unicode tag characters", lambda t: bool(_TAG_CHARS.search(t))),
    PatternRule("role_token", "critical", "chat-template control token", lambda t: bool(_ROLE_TOKEN.search(t))),
    PatternRule(
        "instruction_override", "high", "text telling the model to discard its instructions",
        lambda t: bool(_OVERRIDE.search(t)),
    ),
    PatternRule(
        "hidden_text", "high", "bidirectional overrides or zero-width character runs",
        lambda t: bool(_BIDI.search(t) or _ZERO_WIDTH_RUN.search(t)),
    ),
    PatternRule("encoded_payload", "high", "encoded text that decodes to injected instructions", _encoded_payload),
    PatternRule("exfil_markdown", "medium", "markdown link or image carrying data in its URL", _exfil_markdown),
    PatternRule("persona_switch", "medium", "jailbreak persona or mode switch", lambda t: bool(_PERSONA.search(t))),
)


class PatternScanner:
    """Built-in rules for injection techniques (spec 17). ``disable`` drops rules by name; ``extra_rules`` adds."""

    name = "patterns"

    def __init__(self, extra_rules: Sequence[PatternRule] = (), disable: Sequence[str] = ()) -> None:
        unknown = set(disable) - {rule.name for rule in BUILTIN_RULES}
        if unknown:
            raise ValueError(f"unknown pattern rules: {sorted(unknown)}")
        self.rules = [rule for rule in BUILTIN_RULES if rule.name not in set(disable)] + list(extra_rules)

    async def scan(self, spans: Sequence[TextSpan]) -> list[Finding]:
        return [
            Finding(scanner=self.name, rule=rule.name, severity=rule.severity, message=rule.message, location=span.path)
            for span in spans
            for rule in self.rules
            if rule.check(span.text)
        ]
