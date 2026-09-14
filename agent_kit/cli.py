"""agent-kit command-line tools."""

from __future__ import annotations

import argparse
import sys
import zipfile

import httpx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-kit")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="Verify a signed agent-kit evidence bundle offline")
    verify.add_argument("bundle", help="Path to the evidence bundle zip")
    source = verify.add_mutually_exclusive_group(required=True)
    source.add_argument("--keys-url", help="agent-kit signing keys URL (…/.well-known/agentkit-signing-keys)")
    source.add_argument("--public-key", help="JSON file of signing keys in the keys-URL format")
    args = parser.parse_args(argv)

    try:
        from agent_kit.compliance import load_public_keys, verify_bundle

        keys = load_public_keys(args.keys_url or args.public_key)
        report = verify_bundle(args.bundle, keys)
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, httpx.HTTPError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def line(ok: bool, text: str) -> None:
        print(f"{'✔' if ok else '✘'} {text}")

    line(report.signature_valid, f"manifest signature (kid {report.kid})")
    line(report.files_valid, "file hashes match the manifest")
    line(report.runs_verified == report.runs_total, f"audit chains: {report.runs_verified}/{report.runs_total} verified")
    line(report.deletions_verified == report.deletions_total,
         f"deletion receipts: {report.deletions_verified}/{report.deletions_total} verified")
    for error in report.errors:
        print(f"  - {error}")
    print("VERIFIED" if report.ok else "FAILED")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
