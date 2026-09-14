"""Offline verification of agent-kit evidence bundles."""

from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from agent_kit.audit.chain import AuditChain  # noqa: E402
from agent_kit.cli import main  # noqa: E402
from agent_kit.compliance import load_public_keys, receipt_bytes, verify_bundle  # noqa: E402

KID = "ak-test"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def make_bundle(tmp_path: Path, key: Ed25519PrivateKey, *, tamper: str | None = None) -> Path:
    """Build a bundle exactly as the server does (format agentkit-evidence-bundle/1)."""
    runs, events = [], []
    for n in range(2):
        chain = AuditChain()
        for i in range(3):
            chain.append(f"event_{i}", actor="agent", payload={"n": n, "i": i})
        run_id = f"run-{n}"
        runs.append({"run_id": run_id, "project": "prod", "agent_name": "support", "chain_origin": "client",
                     "started_at": None, "completed_at": None, "event_count": len(chain),
                     "final_root_hash": chain.root_hash(), "integrity": "verified"})
        for seq, e in enumerate(chain.events()):
            events.append({"run_id": run_id, "seq": seq, "event_id": e.event_id, "event_type": e.event_type,
                           "actor": e.actor, "payload_hash": e.payload_hash, "prev_root": e.prev_root,
                           "leaf_hash": e.leaf_hash, "timestamp": e.timestamp.isoformat()})

    receipt = {"run_id": "old-run", "org_id": "org", "project": "prod", "agent_name": "support",
               "final_root_hash": "a" * 64, "event_count": 4, "chain_origin": "client",
               "started_at": "2026-01-01T00:00:00", "completed_at": "2026-01-01T00:01:00",
               "deleted_at": "2026-09-14T00:00:00", "reason": "retention"}
    receipt_sig = key.sign(receipt_bytes(receipt))
    if tamper == "receipt":
        receipt["event_count"] = 5
    deletions = [{**receipt, "kid": KID, "signature": b64(receipt_sig)}]

    if tamper == "event":
        events[1]["payload_hash"] = "0" * 64

    def jsonl(rows: list[dict[str, Any]]) -> bytes:
        return "".join(json.dumps(r) + "\n" for r in rows).encode()

    files = {
        "runs.jsonl": jsonl(runs),
        "events.jsonl": jsonl(events),
        "verification.json": json.dumps({"runs": [], "verified": 2, "failed": 0}).encode(),
        "deletions.jsonl": jsonl(deletions),
    }
    manifest = {"format": "agentkit-evidence-bundle/1", "counts": {"runs": 2, "events": 6, "deletions": 1},
                "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
                "signing": {"kid": KID, "alg": "Ed25519"}}
    if tamper == "event":  # attacker also fixes the file hash, but can't re-sign
        manifest["files"]["events.jsonl"] = hashlib.sha256(files["events.jsonl"]).hexdigest()
    manifest_bytes = json.dumps(manifest, indent=2).encode()
    signature = key.sign(manifest_bytes)
    if tamper == "manifest":
        manifest_bytes = manifest_bytes.replace(b'"runs": 2', b'"runs": 3')
    if tamper == "hash-only":
        files["runs.jsonl"] += b"\n"

    path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", manifest_bytes)
        zf.writestr("manifest.sig", json.dumps({"kid": KID, "alg": "Ed25519", "signature": b64(signature)}))
        for name, content in files.items():
            if tamper == "missing" and name == "events.jsonl":
                continue
            zf.writestr(name, content)
    return path


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def keys_for(key: Ed25519PrivateKey) -> dict[str, bytes]:
    return {KID: key.public_key().public_bytes_raw()}


def test_valid_bundle_verifies(tmp_path, key):
    report = verify_bundle(make_bundle(tmp_path, key), keys_for(key))
    assert report.ok, report.errors
    assert (report.kid, report.signature_valid, report.files_valid) == (KID, True, True)
    assert (report.runs_total, report.runs_verified, report.deletions_total, report.deletions_verified) == (2, 2, 1, 1)


@pytest.mark.parametrize(("tamper", "expect"), [
    ("manifest", "signature"),
    ("event", "chain"),
    ("hash-only", "sha256"),
    ("missing", "missing"),
    ("receipt", "receipt"),
])
def test_tampering_is_detected(tmp_path, key, tamper, expect):
    report = verify_bundle(make_bundle(tmp_path, key, tamper=tamper), keys_for(key))
    assert not report.ok
    assert any(expect in error for error in report.errors), report.errors


def test_wrong_or_unknown_key_fails(tmp_path, key):
    path = make_bundle(tmp_path, key)
    other = Ed25519PrivateKey.generate()
    assert not verify_bundle(path, {KID: other.public_key().public_bytes_raw()}).signature_valid
    unknown = verify_bundle(path, {"ak-other": key.public_key().public_bytes_raw()})
    assert not unknown.signature_valid and any("unknown signing key" in e for e in unknown.errors)


def keys_document(key: Ed25519PrivateKey) -> dict[str, Any]:
    return {"keys": [{"kid": KID, "alg": "Ed25519", "public_key": b64(key.public_key().public_bytes_raw()), "active": True}]}


def test_load_public_keys_from_file_and_url(tmp_path, key):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps(keys_document(key)))
    assert load_public_keys(str(path)) == keys_for(key)

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=keys_document(key)))
    assert load_public_keys("https://cloud.test/.well-known/agentkit-signing-keys",
                            http_client=httpx.Client(transport=transport)) == keys_for(key)


def test_cli_exit_codes(tmp_path, key, capsys):
    keys_path = tmp_path / "keys.json"
    keys_path.write_text(json.dumps(keys_document(key)))
    good = make_bundle(tmp_path, key)

    assert main(["verify", str(good), "--public-key", str(keys_path)]) == 0
    assert "✔" in capsys.readouterr().out

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad = make_bundle(bad_dir, key, tamper="event")
    assert main(["verify", str(bad), "--public-key", str(keys_path)]) == 1
    assert "✘" in capsys.readouterr().out

    assert main(["verify", str(tmp_path / "nope.zip"), "--public-key", str(keys_path)]) == 2
    with pytest.raises(SystemExit) as usage:
        main(["verify", str(good)])
    assert usage.value.code == 2
