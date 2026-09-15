"""
Verify agent-kit evidence bundles offline.

    from agent_kit.compliance import load_public_keys, verify_bundle
    keys = load_public_keys("https://agentkit.internal.acme.com/.well-known/agentkit-signing-keys")
    report = verify_bundle("agentkit-evidence-2026-09-01-2026-10-01.zip", keys)

Checks the manifest signature, every file's SHA-256, every run's hash chain, and every
deletion receipt's signature. Requires ``pip install agent-kit-ai[compliance]``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from agent_kit.audit.chain import _GENESIS_ROOT, _compute_leaf_hash

FORMAT = "agentkit-evidence-bundle/1"
DATA_FILES = ("runs.jsonl", "events.jsonl", "verification.json", "deletions.jsonl")
RECEIPT_FIELDS = (
    "run_id", "org_id", "project", "agent_name", "final_root_hash", "event_count",
    "chain_origin", "started_at", "completed_at", "deleted_at", "reason",
)


@dataclass
class BundleReport:
    ok: bool = False
    kid: str | None = None
    signature_valid: bool = False
    files_valid: bool = False
    runs_total: int = 0
    runs_verified: int = 0
    deletions_total: int = 0
    deletions_verified: int = 0
    errors: list[str] = field(default_factory=list)


def receipt_bytes(receipt: dict[str, Any]) -> bytes:
    """The bytes a deletion receipt's signature covers."""
    fields = {name: receipt.get(name) for name in RECEIPT_FIELDS}
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str).encode()


def load_public_keys(source: str, http_client: httpx.Client | None = None) -> dict[str, bytes]:
    """Load Ed25519 public keys by kid from a keys URL or a JSON file in the same format."""
    if source.startswith(("http://", "https://")):
        client = http_client or httpx.Client(timeout=10.0)
        response = client.get(source)
        response.raise_for_status()
        document = response.json()
    else:
        document = json.loads(Path(source).read_text())
    keys = document.get("keys", []) if isinstance(document, dict) else document
    return {
        str(k["kid"]): base64.b64decode(k["public_key"])
        for k in keys
        if isinstance(k, dict) and k.get("alg", "Ed25519") == "Ed25519"
    }


def verify_bundle(path: str | Path, public_keys: dict[str, bytes]) -> BundleReport:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise ImportError(
            "Verifying evidence bundles requires 'cryptography'. Install it with: pip install agent-kit-ai[compliance]"
        ) from exc

    report = BundleReport()

    def signature_ok(kid: str, signature_b64: str, data: bytes) -> bool:
        public = public_keys.get(kid)
        if public is None:
            return False
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(base64.b64decode(signature_b64), data)
            return True
        except (InvalidSignature, ValueError):
            return False

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "manifest.json" not in names or "manifest.sig" not in names:
            report.errors.append("missing manifest.json or manifest.sig")
            return report
        manifest_bytes = zf.read("manifest.json")
        contents = {name: zf.read(name) for name in DATA_FILES if name in names}
        sig = json.loads(zf.read("manifest.sig"))

    report.kid = str(sig.get("kid"))
    if report.kid not in public_keys:
        report.errors.append(f"unknown signing key {report.kid!r}")
    report.signature_valid = signature_ok(report.kid, str(sig.get("signature", "")), manifest_bytes)
    if not report.signature_valid and report.kid in public_keys:
        report.errors.append("manifest signature is invalid")

    manifest = json.loads(manifest_bytes)
    if manifest.get("format") != FORMAT:
        report.errors.append(f"unsupported bundle format {manifest.get('format')!r}")

    report.files_valid = True
    for name, digest in manifest.get("files", {}).items():
        if name not in contents:
            report.files_valid = False
            report.errors.append(f"{name} is missing")
        elif hashlib.sha256(contents[name]).hexdigest() != digest:
            report.files_valid = False
            report.errors.append(f"{name} sha256 does not match the manifest")

    runs = _jsonl(contents.get("runs.jsonl", b""))
    events_by_run: dict[str, list[dict[str, Any]]] = {}
    for event in _jsonl(contents.get("events.jsonl", b"")):
        events_by_run.setdefault(str(event["run_id"]), []).append(event)
    report.runs_total = len(runs)
    for run in runs:
        problem = _chain_problem(run, sorted(events_by_run.get(str(run["run_id"]), []), key=lambda e: e["seq"]))
        if problem is None:
            report.runs_verified += 1
        else:
            report.errors.append(f"run {run['run_id']}: chain {problem}")

    receipts = _jsonl(contents.get("deletions.jsonl", b""))
    report.deletions_total = len(receipts)
    for receipt in receipts:
        if signature_ok(str(receipt.get("kid")), str(receipt.get("signature", "")), receipt_bytes(receipt)):
            report.deletions_verified += 1
        else:
            report.errors.append(f"deletion receipt for run {receipt.get('run_id')} has an invalid signature")

    report.ok = (
        report.signature_valid
        and report.files_valid
        and report.runs_verified == report.runs_total
        and report.deletions_verified == report.deletions_total
        and not report.errors
    )
    return report


def _chain_problem(run: dict[str, Any], events: list[dict[str, Any]]) -> str | None:
    root = _GENESIS_ROOT
    for event in events:
        if event["prev_root"] != root:
            return f"broken at seq {event['seq']}: prev_root mismatch"
        expected = _compute_leaf_hash(
            root, event["event_type"], event["payload_hash"], datetime.fromisoformat(event["timestamp"])
        )
        if expected != event["leaf_hash"]:
            return f"broken at seq {event['seq']}: leaf hash mismatch"
        root = event["leaf_hash"]
    if len(events) != run["event_count"]:
        return f"has {len(events)} events, run records {run['event_count']}"
    if root != run["final_root_hash"]:
        return "final root does not match the run"
    return None


def _jsonl(data: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in data.decode().splitlines() if line.strip()]
