from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from research_signal_context_pipelines import SignalValidationError, validate_signal
from research_signal_context_pipelines.overlay_backtest import signal_active_on
from scripts import post_shadow_signal_request as shadow_issue
from scripts import validate_latest_signal as signal_validator


ROOT = Path(__file__).resolve().parents[1]


def load_example() -> dict:
    return json.loads((ROOT / "examples" / "latest_signal.example.json").read_text(encoding="utf-8"))


def test_example_signal_is_valid() -> None:
    payload = load_example()
    validate_signal(payload)
    assert payload["horizon"] == "1-3 years"


def test_validated_signal_is_inactive_before_generated_at() -> None:
    payload = load_example()
    payload["as_of"] = "2026-05-28"
    payload["generated_at"] = "2026-05-28T22:00:00Z"
    payload["expires_at"] = "2026-06-30"
    validate_signal(payload)

    morning = dt.datetime(2026, 5, 28, 14, 30, tzinfo=dt.timezone.utc)
    assert signal_active_on(payload, dt.date(2026, 5, 28), decision_time=morning) is False
    assert signal_active_on(payload, dt.date(2026, 5, 29)) is True


def test_v2_signal_requires_versioned_model_metadata() -> None:
    payload = load_example()
    payload.update(
        {
            "schema_version": "2",
            "model_version": "shadow-v2",
            "scoring_version": "rules-v2",
        }
    )

    validate_signal(payload)


def test_v1_signal_remains_readable_without_v2_metadata() -> None:
    payload = load_example()

    validate_signal(payload)


def test_v2_signal_requires_model_and_scoring_versions() -> None:
    payload = load_example()
    payload["schema_version"] = "2"

    with pytest.raises(SignalValidationError, match="model_version"):
        validate_signal(payload)


def test_signal_requires_long_horizon_contract() -> None:
    payload = load_example()
    payload["horizon"] = "1-3 months"

    with pytest.raises(SignalValidationError, match="horizon"):
        validate_signal(payload)


def test_signal_must_be_shadow_mode() -> None:
    payload = load_example()
    payload["mode"] = "live"

    with pytest.raises(SignalValidationError, match="mode must be 'shadow'"):
        validate_signal(payload)


def test_policy_must_block_execution() -> None:
    payload = load_example()
    payload["policy"]["execution_allowed"] = True

    with pytest.raises(SignalValidationError, match="execution_allowed"):
        validate_signal(payload)


def test_confidence_must_be_bounded() -> None:
    payload = load_example()
    payload["confidence"] = 1.5

    with pytest.raises(SignalValidationError, match="between 0 and 1"):
        validate_signal(payload)


def test_signal_accepts_optional_theme_bias_and_exposure() -> None:
    payload = load_example()
    payload["theme_bias"] = {"hbm_memory": "positive", "healthcare_policy": "watch"}
    payload["symbol_theme_exposure"] = {"MU": ["hbm_memory"], "UNH": ["healthcare_policy"]}

    validate_signal(payload)


def test_signal_accepts_structured_theme_and_symbol_bias() -> None:
    payload = load_example()
    payload["theme_bias"] = {
        "hbm_memory": {
            "bias": "positive",
            "confidence": 0.62,
            "horizon": "1-3 years",
            "rationale": "HBM demand remains a long-horizon research context.",
            "risk_flags": ["cycle_risk"],
        }
    }
    payload["symbol_bias"] = {
        "MU": {
            "bias": "watch",
            "confidence": 0.55,
            "linked_themes": ["hbm_memory"],
            "rationale": "Symbol-level shadow context remains watch-only.",
        }
    }

    validate_signal(payload)


def test_signal_rejects_invalid_theme_bias() -> None:
    payload = load_example()
    payload["theme_bias"] = {"hbm_memory": "hot"}

    with pytest.raises(SignalValidationError, match="theme_bias"):
        validate_signal(payload)


def test_signal_rejects_invalid_structured_bias_confidence() -> None:
    payload = load_example()
    payload["symbol_bias"] = {"MU": {"bias": "watch", "confidence": 1.5}}

    with pytest.raises(SignalValidationError, match="confidence"):
        validate_signal(payload)


def test_committed_latest_signal_covers_advisor_long_context() -> None:
    payload = json.loads((ROOT / "data" / "output" / "latest_signal.json").read_text(encoding="utf-8"))

    validate_signal(payload)

    assert payload["horizon"] == "1-3 years"
    assert payload.get("theme_bias")
    assert payload.get("symbol_theme_exposure")
    covered_symbols = set(payload.get("symbol_bias", {})) | set(payload.get("symbol_theme_exposure", {}))
    assert {"MU", "INTC", "AMD", "VRT", "DELL"} <= covered_symbols


def _write_v2_artifact_pair(tmp_path: Path) -> tuple[Path, dict]:
    signal_path = tmp_path / "latest_signal.json"
    signal = load_example()
    signal_bytes = json.dumps(signal, sort_keys=True).encode("utf-8")
    signal_path.write_bytes(signal_bytes)
    manifest = {
        "manifest_type": "research_signal_context",
        "schema_version": 2,
        "artifact": {
            "path": "data/output/latest_signal.json",
            "sha256": hashlib.sha256(signal_bytes).hexdigest(),
        },
        "as_of": signal["as_of"],
        "generated_at": signal["generated_at"],
        "expires_at": signal["expires_at"],
        "mode": signal["mode"],
        "producer": {
            "repository": "QuantStrategyLab/ResearchSignalContextPipelines",
            "commit_sha": "a" * 40,
        },
        "input_digest": "sha256:" + "b" * 64,
        "policy": {"execution_allowed": False},
    }
    return signal_path, manifest


def test_manifest_v2_rejects_stale_signal_digest(tmp_path: Path) -> None:
    signal_path, manifest = _write_v2_artifact_pair(tmp_path)
    manifest["artifact"]["sha256"] = "0" * 64

    with pytest.raises(SignalValidationError, match="artifact.sha256"):
        signal_validator.validate_manifest_v2(signal_path, load_example(), manifest)


def test_manifest_v2_rejects_missing_required_input_digest(tmp_path: Path) -> None:
    signal_path, manifest = _write_v2_artifact_pair(tmp_path)
    del manifest["input_digest"]

    with pytest.raises(SignalValidationError, match="input_digest"):
        signal_validator.validate_manifest_v2(signal_path, load_example(), manifest)


def test_manifest_v2_rejects_mutable_producer_ref(tmp_path: Path) -> None:
    signal_path, manifest = _write_v2_artifact_pair(tmp_path)
    manifest["producer"]["commit_sha"] = "main"

    with pytest.raises(SignalValidationError, match="producer.commit_sha"):
        signal_validator.validate_manifest_v2(signal_path, load_example(), manifest)


def test_manifest_v1_is_explicitly_legacy_untrusted(tmp_path: Path) -> None:
    signal_path, _ = _write_v2_artifact_pair(tmp_path)

    with pytest.raises(SignalValidationError, match="legacy_untrusted"):
        signal_validator.validate_manifest_v2(signal_path, load_example(), {"schema_version": "1"})


def test_shadow_request_binds_context_digest_to_immutable_commit(tmp_path: Path) -> None:
    context_path = tmp_path / "context.json"
    context_path.write_bytes(b'{"as_of":"2026-05-29"}')

    provenance = shadow_issue.build_immutable_provenance(context_path, "c" * 40)

    assert provenance == {
        "producer_commit_sha": "c" * 40,
        "input_digest": "sha256:" + hashlib.sha256(context_path.read_bytes()).hexdigest(),
    }


def test_confidence_rejects_nan() -> None:
    payload = load_example()
    payload["confidence"] = float("nan")

    with pytest.raises(SignalValidationError, match="finite|between 0 and 1"):
        validate_signal(payload)


def test_missing_available_at_is_allowed() -> None:
    payload = load_example()
    payload.pop("available_at", None)

    validate_signal(payload)


def test_available_at_after_generated_at_is_allowed() -> None:
    payload = load_example()
    payload["generated_at"] = "2026-05-28T12:00:00Z"
    payload["available_at"] = "2026-05-28T22:00:00Z"

    validate_signal(payload)


def test_available_at_before_generated_at_is_rejected() -> None:
    payload = load_example()
    payload["generated_at"] = "2026-05-28T22:00:00Z"
    payload["available_at"] = "2026-05-28T12:00:00Z"

    with pytest.raises(SignalValidationError, match="available_at must be >= generated_at"):
        validate_signal(payload)


def test_available_at_present_but_invalid_is_rejected() -> None:
    payload = load_example()
    payload["available_at"] = ""

    with pytest.raises(SignalValidationError, match="available_at"):
        validate_signal(payload)


@pytest.mark.parametrize("location", ["root", "artifact", "producer", "policy"])
def test_manifest_v2_rejects_unknown_fields(tmp_path: Path, location: str) -> None:
    signal_path, manifest = _write_v2_artifact_pair(tmp_path)
    target = manifest if location == "root" else manifest[location]
    target["unexpected"] = "not part of the contract"
    with pytest.raises(SignalValidationError, match="keys|fields"):
        signal_validator.validate_manifest_v2(signal_path, load_example(), manifest)


@pytest.mark.parametrize("version", [2.0, "2", True])
def test_manifest_v2_requires_integer_version(tmp_path: Path, version: object) -> None:
    signal_path, manifest = _write_v2_artifact_pair(tmp_path)
    manifest["schema_version"] = version
    with pytest.raises(SignalValidationError):
        signal_validator.validate_manifest_v2(signal_path, load_example(), manifest)


def _publication_signal() -> dict:
    signal = load_example()
    signal["policy"] = {
        "execution_allowed": False,
        "portfolio_allocation_allowed": False,
        "downstream_use": "Research-only context; do not route to broker execution.",
    }
    return signal


def _write_publication_pair(directory: Path, producer: str = "a" * 40) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    signal = _publication_signal()
    signal_path = directory / "latest_signal.json"
    raw = json.dumps(signal, sort_keys=True).encode()
    signal_path.write_bytes(raw)
    manifest = {
        "manifest_type": "research_signal_context",
        "schema_version": 2,
        "artifact": {"path": "data/output/latest_signal.json", "sha256": hashlib.sha256(raw).hexdigest()},
        **{key: signal[key] for key in ("as_of", "generated_at", "expires_at", "mode")},
        "producer": {"repository": "QuantStrategyLab/ResearchSignalContextPipelines", "commit_sha": producer},
        "input_digest": "sha256:" + "b" * 64,
        "policy": {"execution_allowed": False},
    }
    manifest_path = signal_path.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    return signal_path, manifest_path


def _validator_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_latest_signal.py"), *args],
        cwd=cwd, text=True, capture_output=True, check=False,
    )


def test_strict_pair_reports_structure_only(tmp_path: Path) -> None:
    signal_path, _ = _write_publication_pair(tmp_path)
    result = _validator_cli(str(signal_path), "--require-manifest")
    assert result.returncode == 0, result.stderr
    assert "PAIR_STRUCTURE_VALID" in result.stdout
    assert "context_provenance=UNVERIFIED" in result.stdout
    assert "producer_run_provenance=UNVERIFIED" in result.stdout


def test_strict_pair_accepts_signal_v2_metadata(tmp_path: Path) -> None:
    signal_path, manifest_path = _write_publication_pair(tmp_path)
    signal = json.loads(signal_path.read_text())
    signal.update(schema_version="2", model_version="synthetic-v2", scoring_version="synthetic-rules")
    raw = json.dumps(signal, sort_keys=True).encode()
    signal_path.write_bytes(raw)
    manifest = json.loads(manifest_path.read_text())
    manifest["artifact"]["sha256"] = hashlib.sha256(raw).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    result = _validator_cli(str(signal_path), "--require-manifest")
    assert result.returncode == 0, result.stderr


def test_strict_pair_cannot_bypass_missing_manifest(tmp_path: Path) -> None:
    signal_path, manifest_path = _write_publication_pair(tmp_path)
    manifest_path.unlink()
    assert _validator_cli(str(signal_path), "--require-manifest").returncode != 0
    assert _validator_cli(str(signal_path), "--require-manifest", "--allow-missing").returncode != 0
    assert _validator_cli(str(signal_path)).returncode == 0


def test_manifest_binds_the_supplied_payload_to_exact_file_bytes(tmp_path: Path) -> None:
    signal_path, manifest = _write_v2_artifact_pair(tmp_path)
    other = load_example()
    other["confidence"] = 0.01
    with pytest.raises(SignalValidationError, match="signal bytes|payload"):
        signal_validator.validate_manifest_v2(signal_path, other, manifest)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unknown", True),
        ("available_at", "2026-05-28T22:00:00Z"),
        ("generated_at", "2026-05-28T12:00:00"),
        ("generated_at", "2020-01-01T12:00:00Z"),
        ("expires_at", "2020-01-01"),
        ("confidence", True),
        ("confidence", float("nan")),
        ("confidence", float("inf")),
    ],
)
def test_strict_publication_rejects_unsafe_shape_or_time(field: str, value: object) -> None:
    signal = _publication_signal()
    signal[field] = value
    with pytest.raises(SignalValidationError):
        signal_validator.validate_signal_for_publication(signal)


@pytest.mark.parametrize("location", ["evidence", "policy", "bias"])
def test_strict_publication_rejects_unknown_nested_fields(location: str) -> None:
    signal = _publication_signal()
    if location == "bias":
        signal["candidate_bias"] = {"TEST": {"bias": "watch", "unknown": True}}
    else:
        signal[location]["unknown"] = True
    with pytest.raises(SignalValidationError):
        signal_validator.validate_signal_for_publication(signal)


def test_strict_publication_rejects_non_string_gaps_and_unsafe_policy() -> None:
    signal = _publication_signal()
    signal["evidence"]["data_gaps"] = [123]
    with pytest.raises(SignalValidationError):
        signal_validator.validate_signal_for_publication(signal)
    signal = _publication_signal()
    signal["policy"]["downstream_use"] = "Live account orders"
    with pytest.raises(SignalValidationError):
        signal_validator.validate_signal_for_publication(signal)


def test_strict_pair_rejects_duplicate_keys(tmp_path: Path) -> None:
    signal_path, manifest_path = _write_publication_pair(tmp_path)
    raw = signal_path.read_bytes()
    duplicated = b'{"confidence":0.01,' + raw[1:]
    signal_path.write_bytes(duplicated)
    manifest = json.loads(manifest_path.read_text())
    manifest["artifact"]["sha256"] = hashlib.sha256(duplicated).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    assert _validator_cli(str(signal_path), "--require-manifest").returncode != 0


def test_strict_pair_rejects_duplicate_manifest_keys(tmp_path: Path) -> None:
    signal_path, manifest_path = _write_publication_pair(tmp_path)
    raw = manifest_path.read_bytes()
    manifest_path.write_bytes(b'{"schema_version":1,' + raw[1:])
    assert _validator_cli(str(signal_path), "--require-manifest").returncode != 0


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _pair_repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Synthetic fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    live = repo / "data/output"
    live.mkdir(parents=True)
    (live / "latest_signal.json").write_text('{"retained_legacy":true}')
    (live / "latest_signal.manifest.json").write_text('{"schema_version":"1"}')
    (repo / "README.md").write_text("synthetic repository\n")
    return repo, _commit(repo, "synthetic baseline")


def _changed_pair(repo: Path, base: str, head: str, event: str = "pull_request") -> subprocess.CompletedProcess:
    return _validator_cli("--changed-pair", "--event", event, "--base-sha", base, "--head-sha", head, cwd=repo)


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_changed_pair_validates_exact_head_objects(tmp_path: Path, event: str) -> None:
    repo, base = _pair_repository(tmp_path)
    _write_publication_pair(repo / "data/output", base)
    head = _commit(repo, "valid synthetic pair")
    result = _changed_pair(repo, base, head, event)
    assert result.returncode == 0, result.stderr
    assert "PAIR_STRUCTURE_VALID" in result.stdout


def test_unrelated_change_does_not_relabel_or_block_retained_legacy_pair(tmp_path: Path) -> None:
    repo, base = _pair_repository(tmp_path)
    (repo / "README.md").write_text("unrelated change\n")
    head = _commit(repo, "unrelated")
    result = _changed_pair(repo, base, head)
    assert result.returncode == 0, result.stderr
    assert "NO_LIVE_PAIR_CHANGE" in result.stdout
    assert "PAIR_STRUCTURE_VALID" not in result.stdout


@pytest.mark.parametrize("action", ["signal_only", "manifest_only", "delete", "rename"])
def test_changed_pair_rejects_one_side_deletion_or_rename(tmp_path: Path, action: str) -> None:
    repo, base = _pair_repository(tmp_path)
    live = repo / "data/output"
    if action == "signal_only":
        (live / "latest_signal.json").write_text(json.dumps(_publication_signal()))
    elif action == "manifest_only":
        (live / "latest_signal.manifest.json").write_text('{"schema_version":2}')
    elif action == "delete":
        (live / "latest_signal.json").unlink()
        (live / "latest_signal.manifest.json").unlink()
    else:
        (live / "latest_signal.json").rename(live / "renamed.json")
    head = _commit(repo, action)
    assert _changed_pair(repo, base, head).returncode != 0


def test_worktree_replacement_cannot_rescue_invalid_committed_pair(tmp_path: Path) -> None:
    repo, base = _pair_repository(tmp_path)
    signal_path, manifest_path = _write_publication_pair(repo / "data/output", base)
    manifest = json.loads(manifest_path.read_text())
    manifest["artifact"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    head = _commit(repo, "invalid committed pair")
    _write_publication_pair(repo / "data/output", base)
    assert _changed_pair(repo, base, head).returncode != 0


def test_changed_pair_rejects_worktree_mutation_after_valid_commit(tmp_path: Path) -> None:
    repo, base = _pair_repository(tmp_path)
    signal_path, _ = _write_publication_pair(repo / "data/output", base)
    head = _commit(repo, "valid pair")
    signal_path.write_bytes(signal_path.read_bytes() + b"\n")
    assert _changed_pair(repo, base, head).returncode != 0


def test_pr_uses_merge_base_but_push_uses_exact_before(tmp_path: Path) -> None:
    repo, common = _pair_repository(tmp_path)
    _git(repo, "checkout", "-q", "-b", "target")
    _write_publication_pair(repo / "data/output", common)
    target_head = _commit(repo, "target changed its pair")
    _git(repo, "checkout", "-q", "-b", "feature", common)
    (repo / "README.md").write_text("feature changes no live inputs\n")
    feature_head = _commit(repo, "feature documentation")
    result = _changed_pair(repo, target_head, feature_head, "pull_request")
    assert result.returncode == 0, result.stderr
    assert "NO_LIVE_PAIR_CHANGE" in result.stdout
    assert _changed_pair(repo, target_head, feature_head, "push").returncode != 0


def test_changed_pair_rejects_regular_file_replaced_by_symlink(tmp_path: Path) -> None:
    repo, base = _pair_repository(tmp_path)
    signal_path, _ = _write_publication_pair(repo / "data/output", base)
    raw = signal_path.read_bytes()
    saved = signal_path.with_name("saved.json")
    saved.write_bytes(raw)
    signal_path.unlink()
    signal_path.symlink_to("saved.json")
    head = _commit(repo, "symlink pair")
    assert _changed_pair(repo, base, head).returncode != 0


def test_changed_pair_rejects_invalid_or_missing_commit(tmp_path: Path) -> None:
    repo, base = _pair_repository(tmp_path)
    assert _changed_pair(repo, "main", base).returncode != 0
    assert _changed_pair(repo, "f" * 40, base).returncode != 0


def test_changed_pair_rejects_unresolved_producer_lineage(tmp_path: Path) -> None:
    repo, base = _pair_repository(tmp_path)
    _write_publication_pair(repo / "data/output", "f" * 40)
    head = _commit(repo, "unknown producer")
    assert _changed_pair(repo, base, head).returncode != 0


def test_changed_pair_rejects_existing_nonancestor_producer(tmp_path: Path) -> None:
    repo, base = _pair_repository(tmp_path)
    _git(repo, "checkout", "-q", "-b", "producer-side")
    (repo / "README.md").write_text("sibling history\n")
    producer = _commit(repo, "sibling producer declaration")
    _git(repo, "checkout", "-q", "-b", "publication", base)
    _write_publication_pair(repo / "data/output", producer)
    head = _commit(repo, "not descended from producer")
    assert _changed_pair(repo, base, head).returncode != 0


def test_changed_pair_rejects_missing_comparison_arguments(tmp_path: Path) -> None:
    repo, _ = _pair_repository(tmp_path)
    assert _validator_cli("--changed-pair", cwd=repo).returncode != 0


def test_ci_gates_changed_pair_in_existing_read_only_test_job() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "--changed-pair" in workflow
    assert "fetch-depth: 0" in workflow
    assert "github.event.pull_request.base.sha" in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert "github.event.before" in workflow
    assert "contents: read" in workflow
    assert "pull-requests: write" not in workflow
    assert "python -m pytest -q" in workflow
