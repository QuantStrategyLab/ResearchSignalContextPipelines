#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from research_signal_context_pipelines import SignalValidationError, validate_signal  # noqa: E402
from research_signal_context_pipelines.schema import validate_signal_for_publication  # noqa: E402


DEFAULT_SIGNAL_PATH = Path("data/output/latest_signal.json")
EXPECTED_MANIFEST_TYPE = "research_signal_context"
EXPECTED_ARTIFACT_PATH = "data/output/latest_signal.json"
EXPECTED_PRODUCER_REPOSITORY = "QuantStrategyLab/ResearchSignalContextPipelines"
COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
MANIFEST_PATH = "data/output/latest_signal.manifest.json"
PAIR_PATHS = frozenset({EXPECTED_ARTIFACT_PATH, MANIFEST_PATH})
MANIFEST_KEYS = frozenset({
    "manifest_type", "schema_version", "artifact", "as_of", "generated_at",
    "expires_at", "mode", "producer", "input_digest", "policy",
})
STRUCTURE_SUCCESS = (
    "PAIR_STRUCTURE_VALID context_provenance=UNVERIFIED "
    "producer_run_provenance=UNVERIFIED model_use=UNVERIFIED"
)


def _parse_json(raw: bytes, label: str) -> Mapping[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SignalValidationError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise SignalValidationError("non-finite JSON constant")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SignalValidationError(f"invalid {label} JSON") from exc
    return _require_mapping(value, label)


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        missing = ", ".join(sorted(expected - set(value))) or "none"
        raise SignalValidationError(f"{label} keys mismatch; missing: {missing}; unknown fields are not permitted")


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SignalValidationError(f"{name} must be an object")
    return value


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SignalValidationError(f"{name} must be a non-empty string")
    return value


def validate_manifest_v2(
    signal_path: Path,
    signal: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    signal_bytes: bytes | None = None,
) -> None:
    raw = signal_path.read_bytes() if signal_bytes is None else signal_bytes
    actual_signal = _parse_json(raw, "signal")
    if json.dumps(actual_signal, sort_keys=True, allow_nan=False) != json.dumps(signal, sort_keys=True, allow_nan=False):
        raise SignalValidationError("supplied signal payload does not match signal bytes")
    manifest = _require_mapping(manifest, "manifest")
    schema_version = manifest.get("schema_version")
    if schema_version in ("1", 1):
        raise SignalValidationError("legacy_untrusted: manifest schema_version 1 cannot satisfy immutable provenance")
    if type(schema_version) is not int or schema_version != 2:
        raise SignalValidationError("manifest schema_version must be 2")
    _exact_keys(manifest, MANIFEST_KEYS, "manifest")
    if manifest.get("manifest_type") != EXPECTED_MANIFEST_TYPE:
        raise SignalValidationError(f"manifest_type must be {EXPECTED_MANIFEST_TYPE!r}")

    artifact = _require_mapping(manifest.get("artifact"), "artifact")
    _exact_keys(artifact, frozenset({"path", "sha256"}), "artifact")
    if artifact.get("path") != EXPECTED_ARTIFACT_PATH:
        raise SignalValidationError(f"artifact.path must be {EXPECTED_ARTIFACT_PATH!r}")
    artifact_sha256 = _require_string(artifact.get("sha256"), "artifact.sha256")
    if not SHA256_PATTERN.fullmatch(artifact_sha256):
        raise SignalValidationError("artifact.sha256 must be a lowercase SHA-256 hex digest")
    if artifact_sha256 != hashlib.sha256(raw).hexdigest():
        raise SignalValidationError("artifact.sha256 does not match signal bytes")

    for field in ("as_of", "generated_at", "expires_at", "mode"):
        if manifest.get(field) != signal.get(field):
            raise SignalValidationError(f"{field} must exactly match the signal")

    producer = _require_mapping(manifest.get("producer"), "producer")
    _exact_keys(producer, frozenset({"repository", "commit_sha"}), "producer")
    if producer.get("repository") != EXPECTED_PRODUCER_REPOSITORY:
        raise SignalValidationError(f"producer.repository must be {EXPECTED_PRODUCER_REPOSITORY!r}")
    producer_commit_sha = _require_string(producer.get("commit_sha"), "producer.commit_sha")
    if not COMMIT_SHA_PATTERN.fullmatch(producer_commit_sha):
        raise SignalValidationError("producer.commit_sha must be an immutable 40-hex commit")

    input_digest = _require_string(manifest.get("input_digest"), "input_digest")
    if not input_digest.startswith("sha256:") or not SHA256_PATTERN.fullmatch(input_digest.removeprefix("sha256:")):
        raise SignalValidationError("input_digest must be sha256:<64 lowercase hex>")

    policy = _require_mapping(manifest.get("policy"), "policy")
    _exact_keys(policy, frozenset({"execution_allowed"}), "policy")
    if policy.get("execution_allowed") is not False:
        raise SignalValidationError("policy.execution_allowed must be false")


def validate_pair_bytes(signal_bytes: bytes, manifest_bytes: bytes) -> Mapping[str, Any]:
    signal = _parse_json(signal_bytes, "signal")
    manifest = _parse_json(manifest_bytes, "manifest")
    validate_signal_for_publication(signal)
    validate_manifest_v2(Path(EXPECTED_ARTIFACT_PATH), signal, manifest, signal_bytes=signal_bytes)
    return manifest


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True)
    if result.returncode:
        raise SignalValidationError(f"Git {args[0]} check failed")
    return result.stdout


def _require_commit(repo: Path, sha: str) -> None:
    if not COMMIT_SHA_PATTERN.fullmatch(sha):
        raise SignalValidationError("comparison and producer commits must be lowercase 40-hex SHAs")
    _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")


def _head_blob(repo: Path, head: str, path: str) -> bytes:
    entry = _git(repo, "ls-tree", "-z", head, "--", path).split(b"\0")
    if len(entry) != 2 or entry[-1] or b"\t" not in entry[0]:
        raise SignalValidationError("both canonical live pair files must exist")
    metadata, name = entry[0].split(b"\t", 1)
    mode, kind, sha = metadata.split()
    if name.decode() != path or kind != b"blob" or mode not in {b"100644", b"100755"}:
        raise SignalValidationError("canonical live pair members must be regular Git blobs")
    return _git(repo, "cat-file", "blob", sha.decode("ascii"))


def _require_worktree_bytes(repo: Path, path: str, expected: bytes) -> None:
    current = repo
    for part in Path(path).parts:
        current = current / part
        if current.is_symlink():
            raise SignalValidationError("live pair worktree symlinks are not permitted")
    if not current.is_file() or current.read_bytes() != expected:
        raise SignalValidationError("live pair worktree bytes do not match committed head")


def validate_changed_pair(repo: Path, *, base_sha: str, head_sha: str, event: str) -> bool:
    """Validate changed committed pair bytes; no network or repository mutation."""
    repo = repo.resolve()
    if event not in {"pull_request", "push"}:
        raise SignalValidationError("changed-pair gate requires pull_request or push")
    _require_commit(repo, base_sha)
    _require_commit(repo, head_sha)
    comparison_base = base_sha
    if event == "pull_request":
        comparison_base = _git(repo, "merge-base", base_sha, head_sha).decode("ascii").strip()
        _require_commit(repo, comparison_base)
    raw_paths = _git(
        repo, "diff", "--name-only", "--no-renames", "-z", comparison_base, head_sha,
        "--", *sorted(PAIR_PATHS),
    )
    changed = {path.decode("utf-8") for path in raw_paths.split(b"\0") if path}
    if not changed:
        return False
    if changed != PAIR_PATHS:
        raise SignalValidationError("live signal and manifest must change together")
    blobs = {path: _head_blob(repo, head_sha, path) for path in PAIR_PATHS}
    for path, raw in blobs.items():
        _require_worktree_bytes(repo, path, raw)
    manifest = validate_pair_bytes(blobs[EXPECTED_ARTIFACT_PATH], blobs[MANIFEST_PATH])
    producer = manifest["producer"]["commit_sha"]
    _require_commit(repo, producer)
    _git(repo, "merge-base", "--is-ancestor", producer, head_sha)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a long-horizon shadow signal artifact.")
    parser.add_argument("path", nargs="?", default=str(DEFAULT_SIGNAL_PATH), help="Signal JSON path")
    parser.add_argument("--allow-missing", action="store_true", help="Exit successfully when the path does not exist")
    parser.add_argument("--require-manifest", action="store_true", help="Require strict pair structure; does not authenticate a producer run")
    parser.add_argument("--changed-pair", action="store_true", help="Gate canonical live pair changes using local committed Git objects")
    parser.add_argument("--event", choices=("pull_request", "push"))
    parser.add_argument("--base-sha")
    parser.add_argument("--head-sha")
    args = parser.parse_args()

    if args.allow_missing and (args.require_manifest or args.changed_pair):
        parser.error("--allow-missing cannot bypass strict pair validation")
    if args.changed_pair:
        if not all((args.event, args.base_sha, args.head_sha)):
            parser.error("--changed-pair requires --event, --base-sha, and --head-sha")
        if args.path != str(DEFAULT_SIGNAL_PATH) or args.require_manifest:
            parser.error("--changed-pair reads only canonical committed paths")
        try:
            changed = validate_changed_pair(Path.cwd(), base_sha=args.base_sha, head_sha=args.head_sha, event=args.event)
        except (SignalValidationError, OSError) as exc:
            raise SystemExit(f"invalid signal pair structure: {exc}") from exc
        print(STRUCTURE_SUCCESS if changed else "NO_LIVE_PAIR_CHANGE retained_pair_status=DIAGNOSTIC_UNTRUSTED")
        return 0
    if any((args.event, args.base_sha, args.head_sha)):
        parser.error("Git comparison arguments require --changed-pair")

    path = Path(args.path)
    if not path.exists():
        if args.allow_missing:
            print(f"missing optional signal artifact: {path}")
            return 0
        raise SystemExit(f"signal artifact not found: {path}")

    try:
        signal_bytes = path.read_bytes()
        manifest_path = path.with_suffix(".manifest.json")
        if args.require_manifest:
            if not manifest_path.is_file():
                raise SignalValidationError("strict pair requires a sibling manifest")
            validate_pair_bytes(signal_bytes, manifest_path.read_bytes())
            print(STRUCTURE_SUCCESS)
            return 0
        payload = json.loads(signal_bytes.decode("utf-8"))
        validate_signal(payload)
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            validate_manifest_v2(path, payload, manifest, signal_bytes=signal_bytes)
    except SignalValidationError as exc:
        raise SystemExit(f"invalid or untrusted signal artifact: {exc}") from exc

    if path.with_suffix(".manifest.json").exists():
        print(f"valid signal schema and manifest structure: {path}; actual context/run provenance UNVERIFIED")
    else:
        print(f"valid signal schema only; immutable provenance unavailable: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
