"""
Tool output scanning — screen tool results for prompt injection and known-malicious indicators.

    from agent_kit.scanning import PatternScanner, scan_tool_output

    config = AgentConfig(hooks=Hooks(after_tool=[scan_tool_output(PatternScanner())]))

See specs/17-tool-output-scanning.md.
"""

from agent_kit.scanning.base import ENVELOPE_KEY, Scanner, TextSpan, collect_spans
from agent_kit.scanning.nullcone import NullconeScanner
from agent_kit.scanning.patterns import BUILTIN_RULES, PatternRule, PatternScanner
from agent_kit.scanning.policy import scan_tool_output
from agent_kit.types import Finding, Severity

__all__ = [
    "BUILTIN_RULES",
    "ENVELOPE_KEY",
    "Finding",
    "NullconeScanner",
    "PatternRule",
    "PatternScanner",
    "Scanner",
    "Severity",
    "TextSpan",
    "collect_spans",
    "scan_tool_output",
]
