# Spec 10 — Compliance Exports: Signed Evidence Bundles and Retention

Status: **approved design** · Written 2026-09-14 · Roadmap item: 3.3 (`specs/06-harness-roadmap.md`)

## Goal

Regulated buyers can hand an auditor a file that proves what their agents did and that nobody
altered the record: a signed evidence bundle of audit chains for a period, verifiable offline with
agent-kit's published public keys. Audit data is kept for a defined retention period, protected by
legal holds, and disposed of with signed deletion receipts.

Positioning: evidence that **supports** record-keeping obligations (EU AI Act Article 12 logging,
SOC 2 audit evidence). agent-kit makes no claim of certification or legal compliance.

**Done means:** against a running server, `GET /v1/compliance/export` for a period returns a zip that
`agent-kit verify bundle.zip --keys-url <server>/.well-known/agentkit-signing-keys` verifies
end to end; altering any byte of a run's events makes verification fail; a run past retention is
purged by the worker with a receipt that verifies; a legal hold prevents that purge.

## Decisions

1. **Ed25519 signatures with published keys.** The server signs each bundle's manifest; public keys
   are served unauthenticated with key IDs so rotation never invalidates old bundles. Verifiers get
   keys out-of-band (keys URL or pinned file) — a bundle never supplies its own key.
2. **Sign bytes, not JSON.** The signature covers the exact `manifest.json` bytes in the bundle, and
   the manifest pins every other file by SHA-256. No canonicalisation ambiguity.
3. **Retention = tier policy + enterprise override + legal holds + receipts.** Receipts outlive the
   data so disposal is itself auditable.
4. **Audit data only.** Retention covers audit runs and events. Metrics retention is separate and
   unchanged (platform spec §7). Payloads are never on the server, so bundles contain hashes.
5. **Synchronous, capped exports.** One request returns one bundle of at most 10,000 runs; larger
   scopes are split by the caller. No job queue or object storage.

## Server

### Data — migration `007_compliance`

```
signing_keys
  kid          VARCHAR(64) PK          -- e.g. "ak-2026-09-14-3f9a"
  public_key   VARCHAR(64) NOT NULL    -- base64 raw 32-byte Ed25519 public key
  private_key  VARCHAR(128) NULL       -- base64 seed; NULL for env-supplied keys (never stored)
  source       VARCHAR(16) NOT NULL    -- env | generated
  active       BOOLEAN NOT NULL
  created_at   DATETIME NOT NULL
  retired_at   DATETIME NULL

legal_holds
  id           VARCHAR(36) PK
  org_id       VARCHAR(36) NOT NULL    INDEX (org_id, released_at)
  project      VARCHAR(255) NULL       -- exactly one of project / run_id is set
  run_id       VARCHAR(36) NULL
  reason       VARCHAR(500) NOT NULL
  created_at   DATETIME NOT NULL
  released_at  DATETIME NULL

deletion_receipts
  id              VARCHAR(36) PK
  org_id          VARCHAR(36) NOT NULL  INDEX (org_id, deleted_at)
  run_id          VARCHAR(36) NOT NULL
  project, agent_name              VARCHAR(255)
  final_root_hash VARCHAR(64) NOT NULL
  event_count     INTEGER NOT NULL
  chain_origin    VARCHAR(16) NOT NULL
  started_at, completed_at         DATETIME NULL
  deleted_at      DATETIME NOT NULL
  reason          VARCHAR(32) NOT NULL  -- retention
  kid             VARCHAR(64) NOT NULL
  signature       VARCHAR(128) NOT NULL -- base64 Ed25519 over the receipt's canonical bytes

organizations.audit_retention_days  INTEGER NULL   -- enterprise override; NULL = tier default
```

### Signing — `app/compliance/signing.py`

- Key selection at first use: if `AGENTKIT_SIGNING_KEY` (base64 32-byte seed) is set, derive the key,
  `kid` = `AGENTKIT_SIGNING_KEY_ID` or `"ak-" + first 12 hex of sha256(public key)`; upsert its public
  half into `signing_keys` (`source=env`, `private_key` NULL) and mark it the only active key.
- Otherwise use the active `generated` key, creating one if none exists, and log a warning that
  production deployments should set `AGENTKIT_SIGNING_KEY`.
- `sign(db, data: bytes) -> (kid, signature_b64)`; `public_keys(db) -> list[dict]` (all keys, active
  and retired).
- Receipt canonical bytes: `json.dumps(fields, sort_keys=True, separators=(",", ":"))` UTF-8, over
  `run_id, org_id, project, agent_name, final_root_hash, event_count, chain_origin, started_at,
  completed_at, deleted_at, reason` (datetimes ISO-8601, `null` when absent).

### Evidence bundle — `app/compliance/bundle.py`

`build_bundle(org, db, from_, to, project=None, agent_name=None) -> bytes` (zip, deflated).

Runs in scope: org's `audit_runs` with `started_at` (else `created_at`) in `[from, to)`, matching
`project` / `agent_name` when given, ordered by start time. More than 10,000 → `BundleTooLarge`.

| File | Contents |
|---|---|
| `runs.jsonl` | One line per run: `run_id, project, agent_name, chain_origin, started_at, completed_at, event_count, final_root_hash, integrity` |
| `events.jsonl` | One line per chain link, grouped by run in `seq` order: `run_id, seq, event_id, event_type, actor, payload_hash, prev_root, leaf_hash, timestamp` |
| `verification.json` | `{"runs": [{"run_id", "verified", "broken_seq"}], "verified": n, "failed": n}` — chains re-verified at export time |
| `deletions.jsonl` | Receipts with `deleted_at` in `[from, to)` and matching scope, each with `kid` and `signature` |
| `manifest.json` | Below |
| `manifest.sig` | JSON `{"kid", "alg": "Ed25519", "signature"}` over the exact `manifest.json` bytes |

```json
{
  "format": "agentkit-evidence-bundle/1",
  "generated_at": "2026-09-14T15:02:11Z",
  "org": {"id": "…", "name": "…", "tier": "enterprise"},
  "scope": {"from": "…", "to": "…", "project": null, "agent_name": null},
  "retention": {"audit_retention_days": 2555, "source": "override"},
  "legal_holds": [{"id": "…", "project": "claims", "run_id": null, "reason": "…", "created_at": "…"}],
  "counts": {"runs": 142, "events": 2210, "deletions": 3},
  "files": {"runs.jsonl": "<sha256>", "events.jsonl": "<sha256>", "verification.json": "<sha256>", "deletions.jsonl": "<sha256>"},
  "signing": {"kid": "ak-…", "alg": "Ed25519", "keys_url": "/.well-known/agentkit-signing-keys"}
}
```

### Retention — `app/compliance/retention.py`

- `effective_retention(org) -> (days, source)`: `free` 7 · `pro` 90 · `enterprise` 365; enterprise
  `audit_retention_days` override → `source="override"`.
- `purge_expired(db, now, batch_size=500) -> int`: for each org, runs whose `completed_at` (else
  `created_at`) `< now - days`, excluding runs under an active hold (`run_id` match, or `project`
  match). For each: create and sign a receipt, delete its `audit_events`, delete the run — in one
  transaction per batch. Called by the background worker each cycle, so automatic disposal requires
  `ENABLE_ALERT_WORKER=1` on exactly one server process (as for alerting).

### API — `app/routers/compliance.py`

| Route | Behaviour |
|---|---|
| `GET /.well-known/agentkit-signing-keys` | **No auth.** `{"keys": [{"kid", "alg": "Ed25519", "public_key", "created_at", "retired_at", "active"}]}` |
| `GET /v1/compliance/export?from=&to=&project=&agent_name=` | `application/zip`, `Content-Disposition: attachment; filename="agentkit-evidence-<from>-<to>.zip"`. `400` if `from >= to` or over 10,000 runs |
| `GET /v1/compliance/retention` | `{"tier", "audit_retention_days", "source", "configurable"}` |
| `PUT /v1/compliance/retention` | `{"audit_retention_days": 1..2555 \| null}`; enterprise only (`403` otherwise); `400` out of range |
| `GET /v1/compliance/holds` | Active and released holds |
| `POST /v1/compliance/holds` | `{"project" \| "run_id", "reason"}` — exactly one scope; `400` otherwise |
| `POST /v1/compliance/holds/{id}/release` | Sets `released_at` |
| `GET /v1/compliance/deletions?from=&to=` | Receipts with signatures |

## SDK

### `agent_kit/compliance.py`

```python
@dataclass
class BundleReport:
    ok: bool
    kid: str | None
    signature_valid: bool
    files_valid: bool
    runs_total: int
    runs_verified: int
    deletions_total: int
    deletions_verified: int
    errors: list[str]

def load_public_keys(source: str) -> dict[str, bytes]      # keys URL (http/https) or JSON file path
def verify_bundle(path: str | Path, public_keys: dict[str, bytes]) -> BundleReport
```

Checks, all performed and reported (not short-circuited): manifest signature with the named `kid`;
every file's SHA-256 against the manifest; each run's chain re-derived from `events.jsonl`
(`prev_root` linkage, `leaf_hash` recomputation via `agent_kit.audit.chain`) with the final root and
event count matching `runs.jsonl`; each deletion receipt's signature over its canonical bytes.
Unknown `kid` → signature invalid.

### CLI — `agent-kit verify`

`[project.scripts] agent-kit = "agent_kit.cli:main"`.

```
agent-kit verify BUNDLE (--keys-url URL | --public-key FILE)
```

Prints one line per check (✔/✘) and a summary; exit `0` when `ok`, `1` otherwise, `2` on usage or
unreadable input. Requires the new extra `agent-kit[compliance]` (`cryptography>=41`); without it the
command exits `2` with an install hint.

## Failure handling

- Export and purge queries are always org-scoped.
- A purge batch that fails rolls back entirely — no run deleted without its receipt.
- Missing `AGENTKIT_SIGNING_KEY` never blocks exports; the generated key is persisted (its seed stored
  in the database) so bundles stay verifiable across restarts. Anyone with database access can sign
  with a generated key — production deployments set the env var.
- Setting a new `AGENTKIT_SIGNING_KEY` retires the previous active key; retired keys remain published.

## Testing

- **Server** `tests/test_compliance.py`: env vs generated key selection and rotation (bundles signed by
  a retired key still verify); keys endpoint without auth; bundle files, manifest hashes, scope
  filters, `400` on inverted range and over-cap; verification.json reflects a tampered stored chain;
  retention per tier, enterprise `PUT` validation, `403` for non-enterprise; holds validation and
  release; purge deletes expired unheld runs, keeps held ones (project and run holds), writes receipts
  whose signatures verify; deletions endpoint.
- **SDK** `tests/test_compliance.py`: build a bundle in-test with a throwaway key; `verify_bundle`
  passes, then fails for a modified manifest, modified `events.jsonl` (hash and chain errors), wrong
  key, missing file, forged receipt; `load_public_keys` from file and from an `httpx` mock; CLI exit
  codes. Tests skip when `cryptography` is absent.
- **End to end:** running server with ingested SDK runs → export → `agent-kit verify` via keys URL
  passes; flip one byte in `events.jsonl` → fails; expired run purged by the worker function with a
  verifiable receipt; held run survives.

## Out of scope

External anchoring (RFC 3161 timestamps, transparency logs); async export jobs and object storage;
capturing prompt/tool content; KMS/HSM-backed keys; dashboard UI; metrics retention.
