"""NullconeScanner — known-malicious URLs, domains, IPs, and hashes in tool output, via the Nullcone API."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from agent_kit.exceptions import ScannerUnavailableError
from agent_kit.scanning.base import TextSpan
from agent_kit.types import Finding, Severity

logger = logging.getLogger(__name__)

_URL = re.compile(r"https?://[^\s<>\"'`)\]]+", re.IGNORECASE)
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_DOMAIN = re.compile(
    r"(?<![\w.@/-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?![\w-])", re.IGNORECASE
)
_HASH = re.compile(r"(?<![0-9a-fA-F])(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40}|[0-9a-fA-F]{32})(?![0-9a-fA-F])")
_RESERVED_NAMES = frozenset({"example.com", "example.net", "example.org", "localhost"})
_RESERVED_SUFFIXES = (".example", ".test", ".invalid", ".localhost", ".local")
# File extensions that are not top-level domains, so "report.pdf" is never looked up
_FILE_SUFFIXES = (
    ".bak", ".cfg", ".css", ".csv", ".dll", ".exe", ".gif", ".htm", ".html", ".ini", ".jpeg", ".jpg", ".js",
    ".json", ".lock", ".log", ".pdf", ".png", ".svg", ".tmp", ".toml", ".ts", ".txt", ".xml", ".yaml", ".yml",
)
_CONCURRENCY = 5
_DEFAULT_PAUSE_S = 60.0
_WARN_INTERVAL_S = 60.0


class _LookupFailed(Exception):
    pass


def _reserved_host(host: str) -> bool:
    if host in _RESERVED_NAMES or host.endswith(_RESERVED_SUFFIXES) or host.endswith(_FILE_SUFFIXES):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def _ignored(value: str, host: str, ignore: Sequence[str]) -> bool:
    return any(value == item or host == item or host.endswith("." + item) for item in (i.lower() for i in ignore))


def extract_indicators(spans: Sequence[TextSpan], ignore: Sequence[str] = ()) -> list[tuple[str, str]]:
    """(value, path of first span) for each lookup-worthy indicator, in order of first appearance."""
    found: dict[str, str] = {}
    for span in spans:
        candidates: list[tuple[int, str, str]] = []  # (position, value, host used for reserved/ignore checks)
        for match in _URL.finditer(span.text):
            url = match.group().rstrip(".,;:!?")
            try:
                parts = urlsplit(url)
                host = (parts.hostname or "").lower()
                port = parts.port
            except ValueError:
                continue
            if not host:
                continue
            netloc = host if port is None else f"{host}:{port}"  # userinfo is never sent
            candidates.append((match.start(), urlunsplit((parts.scheme.lower(), netloc, parts.path, "", "")), host))
            candidates.append((match.start(), host, host))
        for match in _IPV4.finditer(span.text):
            try:
                ipaddress.ip_address(match.group())
            except ValueError:
                continue
            candidates.append((match.start(), match.group(), match.group()))
        for match in _DOMAIN.finditer(span.text):
            candidates.append((match.start(), match.group().lower(), match.group().lower()))
        for match in _HASH.finditer(span.text):
            candidates.append((match.start(), match.group().lower(), ""))
        for _, value, host in sorted(candidates, key=lambda c: c[0]):
            if value in found:
                continue
            if host and _reserved_host(host):
                continue
            if _ignored(value, host, ignore):
                continue
            found[value] = span.path
    return list(found.items())


def _severity(score: int) -> Severity:
    if score >= 8:
        return "critical"
    if score >= 6:
        return "high"
    if score >= 4:
        return "medium"
    return "low"


def _retry_after(header: str | None) -> float:
    try:
        seconds = float(header) if header is not None else _DEFAULT_PAUSE_S
    except ValueError:
        return _DEFAULT_PAUSE_S
    return seconds if seconds >= 0 else _DEFAULT_PAUSE_S


class NullconeScanner:
    """
    Looks up indicators found in tool output against the Nullcone threat database (https://nullcone.ai).

    Sends extracted indicators — URLs without query strings or fragments, domains, IPs, hashes — to ``base_url``;
    never tool output. Fails open on errors unless ``fail_closed``; after HTTP 429 it pauses lookups for
    ``Retry-After`` seconds while cached answers keep resolving.
    """

    name = "nullcone"

    def __init__(
        self,
        base_url: str = "https://nullcone.ai/api",
        min_confidence_score: float = 0.6,
        include_unverified: bool = False,
        timeout_s: float = 2.0,
        max_indicators: int = 20,
        cache_ttl_s: float = 3600.0,
        cache_size: int = 10_000,
        ignore: Sequence[str] = (),
        fail_closed: bool = False,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._min_confidence_score = min_confidence_score
        self._include_unverified = include_unverified
        self._timeout_s = timeout_s
        self._max_indicators = max_indicators
        self._cache_ttl_s = cache_ttl_s
        self._cache_size = cache_size
        self._ignore = tuple(ignore)
        self._fail_closed = fail_closed
        self._client = http_client
        self._owns_client = http_client is None
        self._cache: OrderedDict[str, tuple[float, dict[str, Any] | None]] = OrderedDict()
        self._semaphore = asyncio.Semaphore(_CONCURRENCY)
        self._paused_until = 0.0
        self._last_warning = float("-inf")

    async def scan(self, spans: Sequence[TextSpan]) -> list[Finding]:
        indicators = extract_indicators(spans, self._ignore)[: self._max_indicators]
        rows: dict[str, dict[str, Any] | None] = {}
        pending: list[str] = []
        for value, _ in indicators:
            hit, cached = self._cached(value)
            if hit:
                rows[value] = cached
            else:
                pending.append(value)

        failed = 0
        if pending:
            results: list[dict[str, Any] | None | BaseException]
            try:
                results = list(await asyncio.wait_for(
                    asyncio.gather(*(self._lookup(v) for v in pending), return_exceptions=True), self._timeout_s
                ))
            except TimeoutError:
                results = [_LookupFailed("timeout")] * len(pending)
            for value, result in zip(pending, results):
                if isinstance(result, BaseException):
                    if not isinstance(result, _LookupFailed):
                        raise result
                    failed += 1
                else:
                    rows[value] = result
                    self._store(value, result)
        if failed:
            if self._fail_closed:
                raise ScannerUnavailableError(self.name, f"{failed} of {len(pending)} indicator lookups failed")
            self._warn(failed, len(pending))

        return [
            self._finding(value, path, row)
            for value, path in indicators
            if (row := rows.get(value)) is not None and self._counts(row)
        ]

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _lookup(self, value: str) -> dict[str, Any] | None:
        """The Nullcone row for ``value``; None for a miss. Raises _LookupFailed on errors and while paused."""
        async with self._semaphore:
            if time.monotonic() < self._paused_until:
                raise _LookupFailed("rate limited")
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=self._timeout_s, headers={"User-Agent": "agent-kit"})
            try:
                response = await self._client.get(f"{self._base_url}/v1/ioc", params={"value": value})
            except httpx.HTTPError as exc:
                raise _LookupFailed(type(exc).__name__) from exc
            if response.status_code == 404:
                return None
            if response.status_code == 429:
                self._paused_until = time.monotonic() + _retry_after(response.headers.get("Retry-After"))
                raise _LookupFailed("rate limited")
            if response.status_code >= 400:
                raise _LookupFailed(f"HTTP {response.status_code}")
            try:
                body = response.json()
            except ValueError as exc:
                raise _LookupFailed("invalid JSON") from exc
            if not isinstance(body, dict):
                raise _LookupFailed("invalid JSON")
            return None if body.get("found") is False else body

    def _counts(self, row: dict[str, Any]) -> bool:
        if row.get("is_likely_fp"):
            return False
        if float(row.get("confidence_score") or 0.0) < self._min_confidence_score:
            return False
        return self._include_unverified or row.get("confidence_tier") != "unverified"

    def _finding(self, value: str, path: str, row: dict[str, Any]) -> Finding:
        return Finding(
            scanner=self.name,
            rule=f"ioc_{row.get('ioc_type') or 'indicator'}",
            severity=_severity(int(row.get("severity") or 0)),
            message=f"known malicious indicator: {row.get('family_name') or 'unknown'}",
            location=path,
            indicator=value,
        )

    def _cached(self, value: str) -> tuple[bool, dict[str, Any] | None]:
        entry = self._cache.get(value)
        if entry is None:
            return False, None
        expires_at, row = entry
        if expires_at < time.monotonic():
            del self._cache[value]
            return False, None
        self._cache.move_to_end(value)
        return True, row

    def _store(self, value: str, row: dict[str, Any] | None) -> None:
        self._cache[value] = (time.monotonic() + self._cache_ttl_s, row)
        self._cache.move_to_end(value)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _warn(self, failed: int, attempted: int) -> None:
        now = time.monotonic()
        if now - self._last_warning >= _WARN_INTERVAL_S:
            self._last_warning = now
            logger.warning("nullcone: %d of %d indicator lookups failed; scanning without them", failed, attempted)
