"""Source-only tests: synthetic prices and in-memory GitHub, never a live write."""
from __future__ import annotations

import base64
import copy
import datetime as dt
import io
import json
from pathlib import Path
from types import SimpleNamespace
import urllib.error

import pytest

from research_signal_context_pipelines.price_history import PriceRow
from research_signal_context_pipelines import theme_momentum
from scripts import publish_theme_snapshot_pr as publisher


BASE = "a" * 40
BASE_TREE = "b" * 40
NEW_TREE = "c" * 40
HEAD = "d" * 40
ARTIFACT_ID = "9876"
ARTIFACT_DIGEST = "f" * 64
APP_SLUG = "synthetic-research-publisher"
INSTALLATION_ID = "168603414"
NOW = dt.datetime(2026, 10, 6, 12, tzinfo=dt.UTC)
STARTED = "2026-10-06T10:00:00Z"
COMPLETED = "2026-10-06T11:00:00Z"
TOKEN = "synthetic-test-token-never-real"


def trusted_env():
    return {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": publisher.REPOSITORY,
        "GITHUB_REPOSITORY_ID": publisher.REPOSITORY_ID,
        "GITHUB_WORKFLOW_REF": publisher.WORKFLOW_REF,
        "GITHUB_SHA": BASE,
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "2",
    }


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not contact a network service")

    monkeypatch.setattr(publisher.urllib.request, "build_opener", forbidden)
    monkeypatch.setattr(publisher.urllib.request, "urlopen", forbidden)


@pytest.fixture
def approved(monkeypatch):
    # This is test-local only. Source and workflow remain fail-closed.
    monkeypatch.setattr(publisher, "PUBLICATION_CONTENT_APPROVED", True)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    (root / "config").mkdir(parents=True)
    (root / publisher.CONFIG_PATHS[0]).write_text(
        "taxonomy_version,theme_id,theme_name,sector,horizon,description,source_policy\n"
        "synthetic-v1,alpha,Alpha,technology,6-24 months,test,primary evidence required\n"
        "synthetic-v1,beta,Beta,energy,6-24 months,test,primary evidence required\n"
    )
    (root / publisher.CONFIG_PATHS[1]).write_text(
        "symbol,theme_ids,exposure_confidence,rationale\n"
        "AAA,alpha,high,synthetic\nBBB,alpha,high,synthetic\nCCC,beta,high,synthetic\n"
    )
    themes = publisher.load_theme_taxonomy(root / publisher.CONFIG_PATHS[0])
    exposures = publisher.load_symbol_theme_exposure(root / publisher.CONFIG_PATHS[1], known_theme_ids=themes)
    rows = [
        PriceRow(date=NOW.date() - dt.timedelta(days=279 - index), symbol=symbol, close=100 + step * index)
        for symbol, step in (("AAA", 0.2), ("BBB", 0.1))
        for index in range(280)
    ]
    snapshot = publisher.build_theme_momentum_snapshot(
        rows, themes=themes, exposures=exposures, as_of=NOW.date(),
        generated_at=dt.datetime(2026, 10, 6, 10, 30, tzinfo=dt.UTC),
    )
    snapshot["source_artifacts"] = {
        "prices": publisher.PUBLIC_PRICE_SOURCE,
        "theme_taxonomy": publisher.CONFIG_PATHS[0],
        "theme_exposures": publisher.CONFIG_PATHS[1],
    }
    raw = publisher.json_bytes(snapshot)
    candidate = root / publisher.DATA_PATH
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(raw)
    return SimpleNamespace(
        root=root, snapshot=snapshot, raw=raw, candidate=candidate,
        context=publisher.context_from_env(trusted_env()), directory=tmp_path / "package",
    )


@pytest.fixture
def package(source, approved):
    source.hashes = publisher.prepare_package(
        source.candidate, source.directory, source.context, STARTED, COMPLETED, root=source.root, now=NOW,
    )
    source.receipt = json.loads((source.directory / publisher.RECEIPT_NAME).read_bytes())
    return source


def validate(package, **overrides):
    arguments = dict(
        directory=package.directory, context=package.context, **package.hashes,
        artifact_id=ARTIFACT_ID, artifact_digest=ARTIFACT_DIGEST, root=package.root, now=NOW,
    )
    arguments.update(overrides)
    return publisher.validate_package(**arguments)


def change(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


def rewrite_receipt(package, receipt):
    raw = publisher.json_bytes(receipt)
    (package.directory / publisher.RECEIPT_NAME).write_bytes(raw)
    package.hashes["receipt_sha256"] = publisher.sha256(raw)


def test_default_gate_is_literal_false_and_env_cannot_approve(monkeypatch, tmp_path, capsys):
    assert publisher.PUBLICATION_CONTENT_APPROVED is False
    for key, value in {**trusted_env(), "PUBLICATION_CONTENT_APPROVED": "true", "PUBLISH_DRAFT_PR": "true"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(publisher, "verify_checkout", lambda *_: pytest.fail("admission must run first"))
    result_path = tmp_path / "result.json"
    assert publisher.main(["publish", "--result", str(result_path)]) == 1
    result = json.loads(result_path.read_bytes())
    assert result["reason_code"] == "CONTENT_APPROVAL_REQUIRED"
    assert result["published"] is False
    assert result["branch_created"] is False and result["pr_created"] is False
    assert "CONTENT_APPROVAL_REQUIRED" in capsys.readouterr().out


def test_cli_has_no_content_approval_override():
    with pytest.raises(SystemExit) as error:
        publisher.main(["admission", "--publication-content-approved"])
    assert error.value.code == 2


@pytest.mark.parametrize("operation", ["prepare", "validate", "publish"])
def test_all_publication_entrypoints_gate_before_io(source, operation):
    with pytest.raises(publisher.PublicationError, match="^CONTENT_APPROVAL_REQUIRED$"):
        if operation == "prepare":
            publisher.prepare_package(source.candidate, source.directory, source.context, STARTED, COMPLETED)
        elif operation == "validate":
            publisher.validate_package(source.directory, source.context, "", "", "", "")
        else:
            publisher.publish_candidate(None, b"", {}, source.context, "", "", "", "", {})
    assert not source.directory.exists()


@pytest.mark.parametrize("field,value,code", [
    ("GITHUB_EVENT_NAME", "pull_request", "UNTRUSTED_CONTEXT"),
    ("GITHUB_REF", "refs/heads/topic", "UNTRUSTED_CONTEXT"),
    ("GITHUB_REPOSITORY", "attacker/fork", "UNTRUSTED_CONTEXT"),
    ("GITHUB_REPOSITORY_ID", "1", "UNTRUSTED_CONTEXT"),
    ("GITHUB_WORKFLOW_REF", publisher.WORKFLOW_REF.replace("main", "topic"), "UNTRUSTED_CONTEXT"),
    ("GITHUB_SHA", "A" * 40, "SOURCE_SHA_INVALID"),
    ("GITHUB_SHA", "abc123", "SOURCE_SHA_INVALID"),
    ("GITHUB_RUN_ID", "0", "RUN_INVALID"),
    ("GITHUB_RUN_ID", "1\n", "RUN_INVALID"),
    ("GITHUB_RUN_ATTEMPT", "01", "RUN_INVALID"),
    ("GITHUB_RUN_ATTEMPT", "", "RUN_INVALID"),
])
def test_context_rejects_untrusted_event_identity_and_run(field, value, code):
    env = {**trusted_env(), field: value}
    with pytest.raises(publisher.PublicationError, match=f"^{code}$"):
        publisher.context_from_env(env)


def test_checkout_must_equal_exact_source_commit(source, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=HEAD + "\n")

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    with pytest.raises(publisher.PublicationError, match="CHECKOUT_SHA_MISMATCH"):
        publisher.verify_checkout(source.context, root=source.root)
    assert calls[0][0] == ["git", "rev-parse", "HEAD"]
    assert calls[0][1]["cwd"] == source.root


@pytest.mark.parametrize("field,value", [("repository_id", "1"), ("expected_base_sha", HEAD), ("run_id", "01"), ("run_attempt", 2)])
def test_direct_admission_also_rejects_invalid_context(source, approved, field, value):
    context = {**source.context, field: value}
    with pytest.raises(publisher.PublicationError):
        publisher.admission(context)


def test_builder_candidate_package_roundtrip_preserves_raw_bytes(package):
    raw, receipt = validate(package)
    assert raw == package.raw == (package.directory / publisher.CANDIDATE_NAME).read_bytes()
    assert receipt["source_sha"] == receipt["expected_base_sha"] == BASE
    assert receipt["run_id"] == "12345" and receipt["run_attempt"] == "2"
    assert receipt["candidate"] == {"path": publisher.DATA_PATH, "sha256": publisher.sha256(raw), "size_bytes": len(raw)}
    assert receipt["config_sha256"] == publisher.config_hashes(package.root)
    assert package.snapshot["data_quality"]["missing_price_symbols"] == ["CCC"]
    assert package.snapshot["data_quality"]["unranked_themes"] == ["beta"]


@pytest.mark.parametrize("filename", [publisher.CANDIDATE_NAME, publisher.RECEIPT_NAME])
def test_package_hash_binds_raw_bytes_not_equivalent_json(package, filename):
    path = package.directory / filename
    path.write_bytes(path.read_bytes() + b" \n")
    with pytest.raises(publisher.PublicationError, match="PACKAGE_HASH_MISMATCH"):
        validate(package)


def test_receipt_must_be_canonical_even_when_its_updated_hash_matches(package):
    raw = json.dumps(package.receipt, separators=(",", ":")).encode()
    (package.directory / publisher.RECEIPT_NAME).write_bytes(raw)
    package.hashes["receipt_sha256"] = publisher.sha256(raw)
    with pytest.raises(publisher.PublicationError, match="RECEIPT_NONCANONICAL"):
        validate(package)


@pytest.mark.parametrize("field", ["repository", "repository_id", "workflow_ref", "source_sha", "expected_base_sha", "run_id", "run_attempt"])
def test_receipt_is_bound_to_same_trusted_source_run_and_base(package, field):
    receipt = copy.deepcopy(package.receipt)
    receipt[field] += "changed"
    rewrite_receipt(package, receipt)
    with pytest.raises(publisher.PublicationError, match="PACKAGE_CONTEXT_MISMATCH"):
        validate(package)


@pytest.mark.parametrize("path,value,code", [
    (("schema_version",), True, "PACKAGE_SCHEMA_INVALID"),
    (("kind",), "other_package", "PACKAGE_SCHEMA_INVALID"),
    (("candidate", "path"), "data/output/other.json", "PACKAGE_CANDIDATE_MISMATCH"),
    (("candidate", "size_bytes"), 1, "PACKAGE_CANDIDATE_MISMATCH"),
    (("candidate", "sha256"), "0" * 64, "PACKAGE_CANDIDATE_MISMATCH"),
    (("config_sha256", publisher.CONFIG_PATHS[0]), "0" * 64, "CONFIG_HASH_MISMATCH"),
])
def test_receipt_has_strict_candidate_and_config_bindings(package, path, value, code):
    receipt = copy.deepcopy(package.receipt)
    change(receipt, path, value)
    rewrite_receipt(package, receipt)
    with pytest.raises(publisher.PublicationError, match=code):
        validate(package)


def test_receipt_rejects_additional_fields(package):
    receipt = {**package.receipt, "override": True}
    rewrite_receipt(package, receipt)
    with pytest.raises(publisher.PublicationError, match="SCHEMA_KEYS"):
        validate(package)


def test_config_change_after_preparation_is_rejected(package):
    path = package.root / publisher.CONFIG_PATHS[1]
    path.write_bytes(path.read_bytes() + b"DDD,beta,high,new synthetic member\n")
    with pytest.raises(publisher.PublicationError, match="CONFIG_HASH_MISMATCH"):
        validate(package)


@pytest.mark.parametrize("overrides,code", [
    ({"candidate_sha256": "A" * 64}, "PACKAGE_HASH_INVALID"),
    ({"receipt_sha256": "bad"}, "PACKAGE_HASH_INVALID"),
    ({"artifact_id": "0"}, "ARTIFACT_BINDING_INVALID"),
    ({"artifact_id": "12\n"}, "ARTIFACT_BINDING_INVALID"),
    ({"artifact_digest": "sha256:" + ARTIFACT_DIGEST}, "ARTIFACT_BINDING_INVALID"),
])
def test_package_rejects_invalid_hash_and_artifact_binding_shapes(package, overrides, code):
    with pytest.raises(publisher.PublicationError, match=code):
        validate(package, **overrides)


@pytest.mark.parametrize("kind", ["extra_file", "extra_directory", "candidate_symlink", "receipt_symlink", "package_symlink"])
def test_package_rejects_extra_paths_and_symlinks(package, tmp_path, kind):
    if kind == "extra_file":
        (package.directory / "extra.json").write_text("{}")
    elif kind == "extra_directory":
        (package.directory / "nested").mkdir()
    elif kind == "package_symlink":
        link = tmp_path / "package-link"
        link.symlink_to(package.directory, target_is_directory=True)
        package.directory = link
    else:
        name = publisher.CANDIDATE_NAME if kind == "candidate_symlink" else publisher.RECEIPT_NAME
        path = package.directory / name
        target = tmp_path / name
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    with pytest.raises(publisher.PublicationError, match="PACKAGE_PATHS_INVALID|PACKAGE_PATH_INVALID|FILE_NOT_REGULAR"):
        validate(package)


def test_prepare_requires_canonical_candidate_and_empty_package(source, approved):
    wrong = source.root / "other.json"
    wrong.write_bytes(source.raw)
    with pytest.raises(publisher.PublicationError, match="CANDIDATE_PATH_INVALID"):
        publisher.prepare_package(wrong, source.directory, source.context, STARTED, COMPLETED, root=source.root, now=NOW)
    source.directory.mkdir()
    (source.directory / "old").write_text("keep")
    with pytest.raises(publisher.PublicationError, match="PACKAGE_NOT_EMPTY"):
        publisher.prepare_package(source.candidate, source.directory, source.context, STARTED, COMPLETED, root=source.root, now=NOW)
    assert (source.directory / "old").read_text() == "keep"


def test_prepare_accepts_canonical_workflow_relative_path(source, approved, monkeypatch):
    monkeypatch.chdir(source.root)
    hashes = publisher.prepare_package(
        Path(publisher.DATA_PATH), source.directory, source.context, STARTED, COMPLETED, root=source.root, now=NOW,
    )
    assert hashes["candidate_sha256"] == publisher.sha256(source.raw)


@pytest.mark.parametrize("relative", ["data", "data/output"])
def test_prepare_rejects_symlinked_candidate_parent(source, approved, tmp_path, relative):
    original = source.root / relative
    moved = tmp_path / "moved-data"
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)
    with pytest.raises(publisher.PublicationError, match="FILE_NOT_REGULAR"):
        publisher.prepare_package(source.candidate, source.directory, source.context, STARTED, COMPLETED, root=source.root, now=NOW)


def test_config_symlink_is_rejected_even_when_bytes_match(package, tmp_path):
    config = package.root / publisher.CONFIG_PATHS[0]
    target = tmp_path / "same-config.csv"
    target.write_bytes(config.read_bytes())
    config.unlink()
    config.symlink_to(target)
    with pytest.raises(publisher.PublicationError, match="FILE_NOT_REGULAR"):
        validate(package)


def test_regular_bytes_rejects_oversized_candidate(source):
    source.candidate.write_bytes(b" " * (publisher.MAX_BYTES + 1))
    with pytest.raises(publisher.PublicationError, match="FILE_OVERSIZED"):
        publisher.regular_bytes(source.candidate)


@pytest.mark.parametrize("raw,code", [
    (b'{"key":1,"key":2}', "JSON_DUPLICATE_KEY"),
    (b'{"nested":{"key":1,"key":2}}', "JSON_DUPLICATE_KEY"),
    (b'{"n":NaN}', "JSON_NONFINITE"),
    (b'{"n":Infinity}', "JSON_NONFINITE"),
    (b'{"n":-Infinity}', "JSON_NONFINITE"),
    (b'not JSON', "JSON_INVALID"),
    (b'\xff', "JSON_INVALID"),
])
def test_json_parser_rejects_ambiguous_or_nonfinite_data(raw, code):
    with pytest.raises(publisher.PublicationError, match=code):
        publisher.parse_json(raw)


@pytest.mark.parametrize("path,value", [
    (("schema_version",), 2),
    (("policy", "execution_allowed"), True),
    (("policy", "portfolio_allocation_allowed"), 0),
    (("methodology", "volatility_penalty_weight"), 0),
    (("source_artifacts", "prices"), "local-private-prices.csv"),
    (("source_artifacts", "theme_taxonomy"), "other.csv"),
    (("as_of",), "2026-10-07"),
    (("generated_at",), "2026-10-06T10:30:00"),
    (("generated_at",), "2026-10-06T10:30:00+08:00"),
    (("generated_at",), "2026-10-06T09:59:59Z"),
    (("generated_at",), "2026-10-06T11:00:01Z"),
    (("expires_at",), "2026-10-05"),
    (("expires_at",), "2027-01-01"),
    (("summary", "ranked_theme_count"), True),
    (("summary", "top_theme_ids"), ["beta"]),
    (("data_quality", "coverage", "configured_symbol_count"), 999),
    (("data_quality", "coverage", "priced_symbol_count"), True),
    (("data_quality", "coverage", "price_coverage_ratio"), True),
    (("data_quality", "coverage", "price_coverage_ratio"), 0.5),
    (("data_quality", "missing_price_symbols"), ["OUTSIDE"]),
    (("data_quality", "missing_price_symbols"), ["CCC", "CCC"]),
    (("data_quality", "insufficient_history_symbols"), ["CCC"]),
    (("data_quality", "unranked_themes"), []),
    (("theme_ranks", 0, "theme_id"), "outside"),
    (("theme_ranks", 0, "theme_name"), "changed"),
    (("theme_ranks", 0, "source_policy"), "unrestricted"),
    (("theme_ranks", 0, "rank"), True),
    (("theme_ranks", 0, "component_count"), 1),
    (("theme_ranks", 0, "momentum_score"), "0.5"),
    (("theme_ranks", 0, "momentum_score"), True),
    (("theme_ranks", 0, "breadth_3m"), 1.01),
    (("theme_ranks", 0, "returns", "3m"), -1.01),
    (("theme_ranks", 0, "risk", "realized_vol_63d"), -0.1),
    (("theme_ranks", 0, "risk", "drawdown_126d"), 0.1),
    (("theme_ranks", 0, "top_symbols", 0, "symbol"), "CCC"),
    (("theme_ranks", 0, "top_symbols", 0, "return_3m"), False),
    (("theme_ranks", 0, "top_symbols"), []),
])
def test_candidate_rejects_changed_contract_numbers_dates_and_universe(source, path, value):
    candidate = copy.deepcopy(source.snapshot)
    change(candidate, path, value)
    with pytest.raises(publisher.PublicationError):
        publisher.validate_candidate(publisher.json_bytes(candidate), source.root, STARTED, COMPLETED, now=NOW)


@pytest.mark.parametrize("path", [(), ("summary",), ("policy",), ("data_quality",), ("theme_ranks", 0), ("theme_ranks", 0, "returns"), ("theme_ranks", 0, "top_symbols", 0)])
def test_candidate_rejects_extra_keys_at_each_contract_level(source, path):
    candidate = copy.deepcopy(source.snapshot)
    value = candidate
    for key in path:
        value = value[key]
    value["unexpected"] = "must not publish"
    with pytest.raises(publisher.PublicationError):
        publisher.validate_candidate(publisher.json_bytes(candidate), source.root, STARTED, COMPLETED, now=NOW)


def test_candidate_rejects_expired_and_future_completed_generation(source):
    for now, completed, code in [
        (NOW + dt.timedelta(days=85), COMPLETED, "CANDIDATE_EXPIRED"),
        (NOW, "2026-10-06T12:00:01Z", "GENERATION_TIME_INVALID"),
    ]:
        with pytest.raises(publisher.PublicationError, match=code):
            publisher.validate_candidate(source.raw, source.root, STARTED, completed, now=now)


def test_builder_insufficient_history_remains_valid_with_nullable_metrics(source):
    themes = publisher.load_theme_taxonomy(source.root / publisher.CONFIG_PATHS[0])
    exposures = publisher.load_symbol_theme_exposure(source.root / publisher.CONFIG_PATHS[1], known_theme_ids=themes)
    rows = [PriceRow(date=NOW.date(), symbol=symbol, close=100) for symbol in ("AAA", "BBB")]
    candidate = publisher.build_theme_momentum_snapshot(
        rows, themes=themes, exposures=exposures, as_of=NOW.date(), generated_at=NOW.replace(hour=10, minute=30),
    )
    candidate["source_artifacts"] = source.snapshot["source_artifacts"]
    validated = publisher.validate_candidate(publisher.json_bytes(candidate), source.root, STARTED, COMPLETED, now=NOW)
    assert validated["data_quality"]["insufficient_history_symbols"] == ["AAA", "BBB"]
    assert validated["theme_ranks"][0]["momentum_score"] is None


def test_builder_default_now_preserves_microseconds_within_same_second_window(source, monkeypatch):
    class FakeDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 6, 10, 30, 0, 123456, tzinfo=tz)

    monkeypatch.setattr(theme_momentum, "dt", SimpleNamespace(
        datetime=FakeDatetime, UTC=dt.UTC, date=dt.date, timedelta=dt.timedelta,
    ))
    themes = publisher.load_theme_taxonomy(source.root / publisher.CONFIG_PATHS[0])
    exposures = publisher.load_symbol_theme_exposure(source.root / publisher.CONFIG_PATHS[1], known_theme_ids=themes)
    candidate = theme_momentum.build_theme_momentum_snapshot(
        [PriceRow(date=NOW.date(), symbol="AAA", close=100)],
        themes=themes, exposures=exposures, as_of=NOW.date(),
    )
    candidate["source_artifacts"] = source.snapshot["source_artifacts"]
    assert candidate["generated_at"] == "2026-10-06T10:30:00.123456Z"
    validated = publisher.validate_candidate(
        publisher.json_bytes(candidate), source.root,
        "2026-10-06T10:30:00.123455Z", "2026-10-06T10:30:00.123457Z", now=NOW,
    )
    assert validated["generated_at"] == candidate["generated_at"]
    print("Real builder default-now generated_at=" + candidate["generated_at"])
    print("Exact same-second start=.123455Z < generated=.123456Z < completion=.123457Z: validated")


@pytest.mark.parametrize("duplicate", ["theme", "symbol"])
def test_candidate_rejects_duplicate_ranked_themes_and_symbols(source, duplicate):
    candidate = copy.deepcopy(source.snapshot)
    rank = candidate["theme_ranks"][0]
    if duplicate == "theme":
        candidate["theme_ranks"].append(copy.deepcopy(rank))
    else:
        rank["top_symbols"][1] = copy.deepcopy(rank["top_symbols"][0])
    with pytest.raises(publisher.PublicationError, match="THEME_SCOPE_INVALID|SYMBOL_SCOPE_INVALID"):
        publisher.validate_candidate(publisher.json_bytes(candidate), source.root, STARTED, COMPLETED, now=NOW)


def test_candidate_rejects_nonfinite_number_created_by_json_exponent(source):
    candidate = copy.deepcopy(source.snapshot)
    candidate["theme_ranks"][0]["momentum_score"] = "overflow-number"
    raw = publisher.json_bytes(candidate).replace(b'"overflow-number"', b"1e309")
    with pytest.raises(publisher.PublicationError, match="NUMBER_INVALID"):
        publisher.validate_candidate(raw, source.root, STARTED, COMPLETED, now=NOW)


class FakeGitHub:
    """The one-repository Git Data / draft PR protocol, entirely in memory."""

    def __init__(self, raw):
        self.raw = raw
        self.calls = []
        self.refs = {}
        self.prs = []
        self.base_reads = 0
        self.move_base_on_read = None
        self.fail_write = None
        self.fail_code = "API_HTTP_403"
        self.uncertain_write = None
        self.uncertain_code = "API_TRANSPORT_FAILED"
        self.scope = {"total_count": 1, "repositories": [{"full_name": publisher.REPOSITORY, "id": int(publisher.REPOSITORY_ID)}]}
        self.trees = {BASE_TREE: [
            {"path": publisher.DATA_PATH, "mode": "100644", "type": "blob", "sha": "e" * 40},
            {"path": "README.md", "mode": "100644", "type": "blob", "sha": "1" * 40},
        ]}
        self.commits = {BASE: {"sha": BASE, "tree": {"sha": BASE_TREE}, "parents": []}}

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(payload)))
        assert method in {"GET", "POST"}, "No PATCH, DELETE, merge, or force update is allowed"
        assert "/merge" not in path and "/dispatches" not in path
        if method == "POST":
            assert path in {publisher.endpoint("git/" + name) for name in ("blobs", "trees", "commits", "refs")} | {publisher.endpoint("pulls")}
            if path.endswith("/refs"):
                assert payload["ref"].startswith("refs/heads/automation/theme-")
                assert payload["ref"] != "refs/heads/main"
            if self.fail_write == path:
                raise publisher.PublicationError(self.fail_code)
        if path == "/installation/repositories?per_page=100":
            assert method == "GET" and kwargs["single_page"] is True
            return copy.deepcopy(self.scope)
        prefix = publisher.endpoint("")
        assert path.startswith(prefix)
        route = path[len(prefix):]
        if method == "GET":
            if route == "git/ref/heads/main":
                self.base_reads += 1
                sha = HEAD if self.move_base_on_read and self.base_reads >= self.move_base_on_read else BASE
                return {"object": {"sha": sha}}
            if route.startswith("git/ref/heads/"):
                branch = route.removeprefix("git/ref/heads/")
                return {"object": {"sha": self.refs[branch]}} if branch in self.refs else None
            if route.startswith("git/commits/"):
                return copy.deepcopy(self.commits[route.removeprefix("git/commits/")])
            if route.startswith("git/trees/"):
                sha = route.removeprefix("git/trees/").removesuffix("?recursive=1")
                return {"truncated": False, "tree": copy.deepcopy(self.trees[sha])}
            if route.startswith("pulls?"):
                assert "state=all" in route and kwargs["single_page"] is True
                return copy.deepcopy(self.prs)
        if method == "POST" and route == "git/blobs":
            assert payload["encoding"] == "base64"
            assert base64.b64decode(payload["content"], validate=True) == self.raw
            return {"sha": publisher.git_blob_sha(self.raw)}
        if method == "POST" and route == "git/trees":
            assert payload["base_tree"] == BASE_TREE
            assert payload["tree"] == [{"path": publisher.DATA_PATH, "mode": "100644", "type": "blob", "sha": publisher.git_blob_sha(self.raw)}]
            self.trees[NEW_TREE] = copy.deepcopy(
                [entry for entry in self.trees[BASE_TREE] if entry["path"] != publisher.DATA_PATH] + payload["tree"]
            )
            return {"sha": NEW_TREE}
        if method == "POST" and route == "git/commits":
            assert payload["parents"] == [BASE] and payload["tree"] == NEW_TREE
            self.commits[HEAD] = {"sha": HEAD, "tree": {"sha": NEW_TREE}, "parents": [{"sha": BASE}], "message": payload["message"]}
            return {"sha": HEAD}
        if method == "POST" and route == "git/refs":
            branch = payload["ref"].removeprefix("refs/heads/")
            assert branch not in self.refs, "Never repeat a create-ref write"
            self.refs[branch] = payload["sha"]
            response = {"ref": payload["ref"], "object": {"sha": payload["sha"]}}
        elif method == "POST" and route == "pulls":
            assert not self.prs, "Never duplicate a PR creation"
            assert payload["draft"] is True and payload["base"] == "main"
            repo = {"full_name": publisher.REPOSITORY, "id": int(publisher.REPOSITORY_ID)}
            response = {
                "number": 42, "html_url": f"https://github.com/{publisher.REPOSITORY}/pull/42",
                "state": "open", "draft": True, "merged_at": None, "body": payload["body"],
                "user": {"login": APP_SLUG + "[bot]", "type": "Bot"},
                "head": {"ref": payload["head"], "sha": self.refs[payload["head"]], "repo": repo},
                "base": {"ref": "main", "repo": repo},
            }
            self.prs.append(response)
        else:
            raise AssertionError(f"Unexpected fake request: {method} {path}")
        if self.uncertain_write == path:
            raise publisher.PublicationError(self.uncertain_code)
        return copy.deepcopy(response)


def publish(package, api, result=None):
    return publisher.publish_candidate(
        api, package.raw, package.receipt, package.context, ARTIFACT_ID, ARTIFACT_DIGEST,
        APP_SLUG, INSTALLATION_ID, result if result is not None else publisher.initial_result(),
        root=package.root, now=NOW,
    )


def test_full_dormant_prepare_validate_and_draft_pr_flow(package):
    raw, receipt = validate(package)
    api = FakeGitHub(raw)
    result = publish(package, api)
    assert result["status"] == "draft_pr_created"
    assert result["published"] is False and result["review_required"] is True
    assert result["branch_created"] is True and result["pr_created"] is True
    assert result["pr_url"] == f"https://github.com/{publisher.REPOSITORY}/pull/42"
    body = api.prs[0]["body"]
    for binding in (BASE, "12345", ARTIFACT_ID, ARTIFACT_DIGEST, publisher.sha256(raw), publisher.sha256(publisher.json_bytes(receipt))):
        assert binding in body
    assert "published=false" in body
    assert [path for method, path, _ in api.calls if method == "POST"] == [
        publisher.endpoint("git/" + name) for name in ("blobs", "trees", "commits", "refs")
    ] + [publisher.endpoint("pulls")]


def test_repeat_identical_package_reuses_draft_pr_without_any_write(package):
    api = FakeGitHub(package.raw)
    first = publish(package, api)
    count = len(api.calls)
    second = publish(package, api)
    assert second["status"] == "draft_pr_reused"
    assert second["pr_url"] == first["pr_url"] and second["branch"] == first["branch"]
    assert second["published"] is False
    assert all(method == "GET" for method, _, _ in api.calls[count:])


def test_candidate_already_on_main_is_noop_and_does_not_claim_publication(package):
    api = FakeGitHub(package.raw)
    api.trees[BASE_TREE][0]["sha"] = publisher.git_blob_sha(package.raw)
    result = publish(package, api)
    assert result["status"] == "already_on_base" and result["main_contains_candidate"] is True
    assert result["published"] is False
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize("when", [1, 2, 3])
def test_moved_base_stops_before_branch_or_pr_write(package, when):
    api = FakeGitHub(package.raw)
    api.move_base_on_read = when
    result = publisher.initial_result()
    with pytest.raises(publisher.PublicationError, match="BASE_MOVED"):
        publish(package, api, result)
    assert not api.prs and result["published"] is False
    if when <= 2:
        assert not api.refs


def test_base_moving_during_pr_creation_retains_pr_evidence_without_claiming_success(package):
    api = FakeGitHub(package.raw)
    api.move_base_on_read = 4
    result = publisher.initial_result()
    with pytest.raises(publisher.PublicationError, match="BASE_MOVED"):
        publish(package, api, result)
    assert len(api.prs) == 1 and result["pr_created"] is True
    assert result["pr_url"] == api.prs[0]["html_url"]
    assert result["published"] is False


@pytest.mark.parametrize("binding", ["raw", "receipt", "context", "config"])
def test_direct_publish_revalidates_local_bindings_before_contacting_github(package, binding):
    api = FakeGitHub(package.raw)
    if binding == "raw":
        package.raw += b"\n"
    elif binding == "receipt":
        package.receipt["source_sha"] = HEAD
    elif binding == "context":
        package.context["run_attempt"] = "99"
    else:
        path = package.root / publisher.CONFIG_PATHS[0]
        path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(publisher.PublicationError):
        publish(package, api)
    assert not api.calls


def test_publish_rejects_mismatching_explicit_receipt_digest_before_api_calls(package):
    api = FakeGitHub(package.raw)
    with pytest.raises(publisher.PublicationError, match="PACKAGE_HASH_MISMATCH"):
        publisher.publish_candidate(
            api, package.raw, package.receipt, package.context, ARTIFACT_ID, ARTIFACT_DIGEST,
            APP_SLUG, INSTALLATION_ID, publisher.initial_result(), root=package.root, now=NOW,
            receipt_sha256="0" * 64,
        )
    assert not api.calls


@pytest.mark.parametrize("edit", ["parent", "message", "other_file", "candidate_file", "extra_file"])
def test_existing_branch_human_edits_are_never_overwritten(package, edit):
    api = FakeGitHub(package.raw)
    publish(package, api)
    if edit == "parent":
        api.commits[HEAD]["parents"] = [{"sha": "9" * 40}]
    elif edit == "message":
        api.commits[HEAD]["message"] = "Human rewrite with identical file bytes"
    elif edit == "extra_file":
        api.trees[NEW_TREE].append({"path": "human.txt", "type": "blob", "mode": "100644", "sha": "9" * 40})
    else:
        path = "README.md" if edit == "other_file" else publisher.DATA_PATH
        next(entry for entry in api.trees[NEW_TREE] if entry["path"] == path)["sha"] = "9" * 40
    count = len(api.calls)
    with pytest.raises(publisher.PublicationError, match="BRANCH_MODIFIED"):
        publish(package, api)
    assert all(method == "GET" for method, _, _ in api.calls[count:])


def test_same_tree_manual_head_replacement_is_not_reused(package):
    api = FakeGitHub(package.raw)
    first = publish(package, api)
    replacement = "9" * 40
    api.commits[replacement] = {**copy.deepcopy(api.commits[HEAD]), "sha": replacement}
    api.refs[first["branch"]] = replacement
    api.prs[0]["head"]["sha"] = replacement
    count = len(api.calls)
    with pytest.raises(publisher.PublicationError, match="PR_IDENTITY_MISMATCH"):
        publish(package, api)
    assert all(method == "GET" for method, _, _ in api.calls[count:])


@pytest.mark.parametrize("path,value,code", [
    (("state",), "closed", "PR_HUMAN_STATE_CHANGED"),
    (("draft",), False, "PR_HUMAN_STATE_CHANGED"),
    (("merged_at",), "2026-10-06T12:00:00Z", "PR_HUMAN_STATE_CHANGED"),
    (("user", "login"), "human", "PR_IDENTITY_MISMATCH"),
    (("user", "type"), "User", "PR_IDENTITY_MISMATCH"),
    (("head", "sha"), "9" * 40, "PR_IDENTITY_MISMATCH"),
    (("head", "repo", "full_name"), "attacker/fork", "PR_IDENTITY_MISMATCH"),
    (("base", "ref"), "other", "PR_IDENTITY_MISMATCH"),
    (("body",), "human replacement", "PR_IDENTITY_MISMATCH"),
    (("html_url",), "https://attacker.invalid/pull/42", "PR_IDENTITY_MISMATCH"),
])
def test_existing_pr_human_state_and_identity_changes_are_respected(package, path, value, code):
    api = FakeGitHub(package.raw)
    publish(package, api)
    change(api.prs[0], path, value)
    count = len(api.calls)
    with pytest.raises(publisher.PublicationError, match=code):
        publish(package, api)
    assert all(method == "GET" for method, _, _ in api.calls[count:])


def test_multiple_prs_fail_closed(package):
    api = FakeGitHub(package.raw)
    publish(package, api)
    api.prs.append(copy.deepcopy(api.prs[0]))
    with pytest.raises(publisher.PublicationError, match="PR_IDENTITY_AMBIGUOUS"):
        publish(package, api)


@pytest.mark.parametrize("route", ["git/refs", "pulls"])
def test_uncertain_create_response_is_reconciled_without_duplicate_post(package, route):
    api = FakeGitHub(package.raw)
    api.uncertain_write = publisher.endpoint(route)
    result = publish(package, api)
    assert result["status"] == "draft_pr_created" and result["published"] is False
    assert len(api.refs) == len(api.prs) == 1
    assert sum(method == "POST" and path == publisher.endpoint(route) for method, path, _ in api.calls) == 1
    writes = [index for index, (method, path, _) in enumerate(api.calls) if method == "POST" and path == publisher.endpoint(route)]
    assert api.calls[writes[0] + 1][0] == "GET"


def test_create_ref_race_with_different_same_tree_head_is_not_adopted(package, monkeypatch):
    api = FakeGitHub(package.raw)
    request = api.request

    def race(method, path, payload=None, **kwargs):
        response = request(method, path, payload, **kwargs)
        if method == "POST" and path == publisher.endpoint("git/refs"):
            replacement = "9" * 40
            api.commits[replacement] = {**copy.deepcopy(api.commits[HEAD]), "sha": replacement}
            api.refs[payload["ref"].removeprefix("refs/heads/")] = replacement
            raise publisher.PublicationError("API_ALREADY_EXISTS")
        return response

    monkeypatch.setattr(api, "request", race)
    result = publisher.initial_result()
    with pytest.raises(publisher.PublicationError, match="BRANCH_MODIFIED"):
        publish(package, api, result)
    assert not api.prs and result["published"] is False
    assert sum(method == "POST" and path == publisher.endpoint("git/refs") for method, path, _ in api.calls) == 1


@pytest.mark.parametrize("route,code", [
    ("git/blobs", "API_HTTP_403"), ("git/trees", "API_HTTP_403"), ("git/commits", "API_HTTP_403"),
    ("git/refs", "API_HTTP_403"), ("pulls", "API_HTTP_403"),
])
def test_write_refusal_never_claims_publication_or_retries(package, route, code):
    api = FakeGitHub(package.raw)
    api.fail_write = publisher.endpoint(route)
    result = publisher.initial_result()
    with pytest.raises(publisher.PublicationError, match=code):
        publish(package, api, result)
    assert result["published"] is False and not api.prs
    assert sum(method == "POST" and path == publisher.endpoint(route) for method, path, _ in api.calls) == 1


@pytest.mark.parametrize("route", ["git/refs", "pulls"])
@pytest.mark.parametrize("code", ["API_HTTP_401", "API_HTTP_403", "API_HTTP_404", "API_HTTP_422", "API_HTTP_429"])
def test_definite_create_rejections_propagate_exact_cause_without_reconcile(package, route, code):
    api = FakeGitHub(package.raw)
    api.fail_write = publisher.endpoint(route)
    api.fail_code = code
    result = publisher.initial_result()
    with pytest.raises(publisher.PublicationError) as raised:
        publish(package, api, result)
    assert str(raised.value) == code
    assert result["published"] is False
    writes = [index for index, (method, path, _) in enumerate(api.calls) if method == "POST" and path == publisher.endpoint(route)]
    assert len(writes) == 1
    assert api.calls[writes[0] + 1:] == [], "Definite refusal must not trigger reconciliation or another API read"


@pytest.mark.parametrize("route", ["git/refs", "pulls"])
@pytest.mark.parametrize("code", ["API_TRANSPORT_FAILED", "API_HTTP_500", "API_HTTP_503", "API_ALREADY_EXISTS"])
@pytest.mark.parametrize("committed", [False, True])
def test_only_uncertain_or_classified_existing_create_has_one_read_reconcile(package, route, code, committed):
    api = FakeGitHub(package.raw)
    if committed:
        api.uncertain_write = publisher.endpoint(route)
        api.uncertain_code = code
    else:
        api.fail_write = publisher.endpoint(route)
        api.fail_code = code
    result = publisher.initial_result()
    if committed:
        assert publish(package, api, result)["status"] == "draft_pr_created"
    else:
        expected = "BRANCH_CREATION_UNCONFIRMED" if route == "git/refs" else "PR_CREATION_UNCONFIRMED"
        with pytest.raises(publisher.PublicationError, match=f"^{expected}$"):
            publish(package, api, result)
    assert result["published"] is False
    writes = [index for index, (method, path, _) in enumerate(api.calls) if method == "POST" and path == publisher.endpoint(route)]
    assert len(writes) == 1
    after = api.calls[writes[0] + 1:]
    assert after and after[0][0] == "GET"
    reconciliation_prefix = publisher.endpoint("git/ref/heads/automation/theme-") if route == "git/refs" else publisher.endpoint("pulls?")
    assert sum(method == "GET" and path.startswith(reconciliation_prefix) for method, path, _ in after) == 1
    if not committed:
        assert len(after) == 1


@pytest.mark.parametrize("field,value,code", [("state", "closed", "PR_HUMAN_STATE_CHANGED"), ("body", "wrong identity", "PR_IDENTITY_MISMATCH")])
def test_local_create_pr_response_validation_failure_is_not_reconciled(package, monkeypatch, field, value, code):
    api = FakeGitHub(package.raw)
    request = api.request

    def invalid_response(method, path, payload=None, **kwargs):
        response = request(method, path, payload, **kwargs)
        if method == "POST" and path == publisher.endpoint("pulls"):
            response[field] = value
        return response

    monkeypatch.setattr(api, "request", invalid_response)
    result = publisher.initial_result()
    with pytest.raises(publisher.PublicationError, match=f"^{code}$"):
        publish(package, api, result)
    assert result["published"] is False
    assert api.calls[-1][0:2] == ("POST", publisher.endpoint("pulls"))
    assert sum(method == "POST" and path == publisher.endpoint("pulls") for method, path, _ in api.calls) == 1


@pytest.mark.parametrize("scope", [
    {"total_count": 2, "repositories": []},
    {"total_count": True, "repositories": []},
    {"total_count": 1, "repositories": [{"full_name": "attacker/fork", "id": int(publisher.REPOSITORY_ID)}]},
    {"total_count": 1, "repositories": [{"full_name": publisher.REPOSITORY, "id": 1}]},
])
def test_wrong_app_scope_is_rejected_before_any_write(package, scope):
    api = FakeGitHub(package.raw)
    api.scope = scope
    with pytest.raises(publisher.PublicationError, match="APP_SCOPE_MISMATCH"):
        publish(package, api)
    assert all(method == "GET" for method, _, _ in api.calls)


def mock_http(monkeypatch, *, raw=b"{}", headers=None, error=None):
    requests = []

    class Response(io.BytesIO):
        def __init__(self):
            super().__init__(raw)
            self.headers = headers or {}

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert timeout == 20
            if error:
                raise error
            return Response()

    def build_opener(handler):
        assert isinstance(handler, publisher.NoRedirect)
        return Opener()

    monkeypatch.setattr(publisher.urllib.request, "build_opener", build_opener)
    return requests


def create_request_payload(route):
    branch = "automation/theme-12345-2-0123456789abcdef"
    if route == "git/refs":
        return {"ref": "refs/heads/" + branch, "sha": HEAD}
    return {"head": branch, "base": "main", "draft": True, "title": "Synthetic", "body": "Synthetic"}


@pytest.mark.parametrize("route,body", [
    ("git/refs", {"message": "Reference already exists"}),
    ("pulls", {"message": "Validation Failed", "errors": [{
        "resource": "PullRequest", "code": "custom",
        "message": "A pull request already exists for QuantStrategyLab:automation/theme-12345-2-0123456789abcdef.",
    }]}),
])
def test_github_422_classifies_only_exact_requested_existing_reference_or_pr(monkeypatch, route, body):
    error = urllib.error.HTTPError("https://api.github.com", 422, "Unprocessable Entity", {}, io.BytesIO(publisher.json_bytes(body)))
    requests = mock_http(monkeypatch, error=error)
    with pytest.raises(publisher.PublicationError) as raised:
        publisher.GitHub(TOKEN).request("POST", publisher.endpoint(route), create_request_payload(route))
    assert str(raised.value) == "API_ALREADY_EXISTS"
    assert len(requests) == 1


@pytest.mark.parametrize("route,raw", [
    ("git/refs", b'{"message":"Validation Failed"}'),
    ("git/refs", b'{"message":"Reference already exists for a different target"}'),
    ("git/refs", b'{"message":"Reference already exists","message":"other"}'),
    ("git/refs", b'not json'),
    ("pulls", b'{"message":"Reference already exists"}'),
    ("pulls", b'{"message":"Validation Failed","errors":[{"resource":"PullRequest","code":"custom","message":"A pull request already exists for QuantStrategyLab:other-branch."}]}'),
    ("pulls", b'{"message":"Validation Failed","errors":[{"resource":"Issue","code":"custom","message":"A pull request already exists for QuantStrategyLab:automation/theme-12345-2-0123456789abcdef."}]}'),
])
def test_github_422_unknown_or_wrong_target_response_remains_definite_rejection(monkeypatch, route, raw):
    error = urllib.error.HTTPError("https://api.github.com", 422, "Unprocessable Entity", {}, io.BytesIO(raw))
    requests = mock_http(monkeypatch, error=error)
    with pytest.raises(publisher.PublicationError, match="^API_HTTP_422$"):
        publisher.GitHub(TOKEN).request("POST", publisher.endpoint(route), create_request_payload(route))
    assert len(requests) == 1


def test_github_422_classifier_reads_a_bounded_body_and_rejects_oversized_response(monkeypatch):
    reads = []

    class BoundedBody(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024 + 1, "Error bodies must have a finite read bound"
            reads.append(size)
            return super().read(size)

    raw = b'{"message":"Reference already exists"}' + b" " * (1024 * 1024 + 1)
    error = urllib.error.HTTPError("https://api.github.com", 422, "Unprocessable Entity", {}, BoundedBody(raw))
    mock_http(monkeypatch, error=error)
    with pytest.raises(publisher.PublicationError, match="^API_HTTP_422$"):
        publisher.GitHub(TOKEN).request("POST", publisher.endpoint("git/refs"), create_request_payload("git/refs"))
    assert len(reads) == 1


@pytest.mark.parametrize("method,path,read_only", [
    ("PATCH", publisher.endpoint("git/refs/heads/main"), False),
    ("DELETE", publisher.endpoint("git/refs/heads/main"), False),
    ("POST", publisher.endpoint("pulls"), True),
    ("GET", "/repos/attacker/other/pulls", False),
    ("GET", "https://attacker.invalid/", False),
])
def test_github_client_rejects_mutation_methods_readonly_writes_and_other_targets(method, path, read_only):
    with pytest.raises(publisher.PublicationError, match="API_METHOD_FORBIDDEN|API_TARGET_FORBIDDEN"):
        publisher.GitHub(TOKEN, read_only=read_only).request(method, path)


@pytest.mark.parametrize("route,payload", [
    ("issues", {"title": "unrelated"}),
    ("git/refs", {"ref": "refs/heads/main", "sha": HEAD}),
    ("git/refs", {"ref": "refs/heads/other", "sha": HEAD}),
    ("pulls", {"head": "automation/theme-12345-2-0123456789abcdef", "base": "main", "draft": False}),
    ("pulls", {"head": "automation/theme-12345-2-0123456789abcdef", "base": "other", "draft": True}),
    ("git/trees", {"base_tree": BASE_TREE, "tree": [{"path": "README.md", "mode": "100644", "type": "blob", "sha": HEAD}]}),
    ("git/trees", {"base_tree": BASE_TREE, "tree": []}),
])
def test_github_post_allowlist_blocks_other_endpoints_main_refs_and_nondraft_prs(route, payload):
    with pytest.raises(publisher.PublicationError):
        publisher.GitHub(TOKEN).request("POST", publisher.endpoint(route), payload)


def test_github_client_uses_fixed_host_and_no_redirect_handler(monkeypatch):
    requests = mock_http(monkeypatch, raw=b'{"ok": true}')
    assert publisher.GitHub(TOKEN, read_only=True).request("GET", publisher.endpoint("git/ref/heads/main")) == {"ok": True}
    assert len(requests) == 1
    assert requests[0].full_url == "https://api.github.com" + publisher.endpoint("git/ref/heads/main")
    assert requests[0].get_header("Authorization") == "Bearer " + TOKEN
    assert requests[0].get_header("X-github-api-version") == "2022-11-28"
    assert publisher.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://attacker.invalid") is None


@pytest.mark.parametrize("error,code", [
    (urllib.error.HTTPError("https://api.github.com/" + TOKEN, 302, TOKEN, {}, None), "API_HTTP_302"),
    (urllib.error.HTTPError("https://api.github.com/" + TOKEN, 403, TOKEN, {}, None), "API_HTTP_403"),
    (urllib.error.URLError(TOKEN), "API_TRANSPORT_FAILED"),
    (TimeoutError(TOKEN), "API_TRANSPORT_FAILED"),
])
def test_github_failures_do_not_retry_redirect_or_expose_secrets(monkeypatch, error, code):
    requests = mock_http(monkeypatch, error=error)
    with pytest.raises(publisher.PublicationError) as raised:
        publisher.GitHub(TOKEN).request("GET", publisher.endpoint("pulls"))
    assert str(raised.value) == code
    assert TOKEN not in str(raised.value) and raised.value.__suppress_context__ is True
    assert len(requests) == 1


def test_unknown_error_text_is_never_a_public_reason_code():
    error = publisher.PublicationError(TOKEN)
    assert error.code == str(error) == "PUBLICATION_FAILED"


def test_github_missing_ref_404_is_optional_only_when_explicit(monkeypatch):
    requests = mock_http(monkeypatch, error=urllib.error.HTTPError("https://api.github.com", 404, "missing", {}, None))
    api = publisher.GitHub(TOKEN)
    assert api.request("GET", publisher.endpoint("git/ref/heads/example"), missing_ok=True) is None
    with pytest.raises(publisher.PublicationError, match="API_HTTP_404"):
        api.request("GET", publisher.endpoint("git/ref/heads/example"))
    assert len(requests) == 2


@pytest.mark.parametrize("raw,headers,code", [
    (b"{}", {"Link": "<https://api.github.com/page2>; rel=next"}, "API_PAGINATION_UNEXPECTED"),
    (b" " * (1024 * 1024 + 1), {}, "API_RESPONSE_OVERSIZED"),
    (b'{"duplicate":1,"duplicate":2}', {}, "JSON_DUPLICATE_KEY"),
])
def test_github_rejects_unbounded_or_ambiguous_responses(monkeypatch, raw, headers, code):
    mock_http(monkeypatch, raw=raw, headers=headers)
    with pytest.raises(publisher.PublicationError, match=code):
        publisher.GitHub(TOKEN).request("GET", publisher.endpoint("pulls"), single_page=True)


def test_validate_cli_uses_readonly_token_and_only_reads_main(package, monkeypatch, capsys):
    requests = mock_http(monkeypatch, raw=publisher.json_bytes({"object": {"sha": BASE}}))
    for key, value in {**trusted_env(), "GITHUB_TOKEN": TOKEN}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(publisher, "verify_checkout", lambda _: None)
    original_validate = publisher.validate_package
    monkeypatch.setattr(publisher, "validate_package", lambda *args: original_validate(*args, root=package.root, now=NOW))
    original_api = publisher.GitHub
    clients = []

    def client(token, *, read_only):
        assert token == TOKEN and read_only is True
        clients.append(original_api(token, read_only=read_only))
        return clients[-1]

    monkeypatch.setattr(publisher, "GitHub", client)
    arguments = ["validate", "--package-dir", str(package.directory), "--artifact-id", ARTIFACT_ID, "--artifact-digest", ARTIFACT_DIGEST]
    for key, value in package.hashes.items():
        arguments.extend(["--" + key.replace("_", "-"), value])
    assert publisher.main(arguments) == 0
    assert len(clients) == len(requests) == 1
    assert requests[0].get_method() == "GET"
    assert requests[0].full_url.endswith("/git/ref/heads/main")
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "package_validated" and result["published"] is False


@pytest.mark.parametrize("refuse", [False, True])
def test_publish_cli_records_verified_draft_or_failure_without_secrets(package, monkeypatch, capsys, tmp_path, refuse):
    api = FakeGitHub(package.raw)
    if refuse:
        api.fail_write = publisher.endpoint("pulls")
    for key, value in {
        **trusted_env(), "RESEARCH_PUBLISHER_TOKEN": TOKEN,
        "RESEARCH_PUBLISHER_APP_SLUG": APP_SLUG, "RESEARCH_PUBLISHER_INSTALLATION_ID": INSTALLATION_ID,
    }.items():
        monkeypatch.setenv(key, value)
    checkouts = []
    monkeypatch.setattr(publisher, "verify_checkout", lambda context: checkouts.append(context))
    original_validate = publisher.validate_package
    monkeypatch.setattr(publisher, "validate_package", lambda *args: original_validate(*args, root=package.root, now=NOW))
    original_publish = publisher.publish_candidate
    monkeypatch.setattr(publisher, "publish_candidate", lambda *args, **kwargs: original_publish(*args, root=package.root, now=NOW, **kwargs))
    monkeypatch.setattr(publisher, "GitHub", lambda token: api if token == TOKEN else pytest.fail("wrong token"))
    result_path = tmp_path / "result.json"
    arguments = ["publish", "--package-dir", str(package.directory), "--artifact-id", ARTIFACT_ID,
                 "--artifact-digest", ARTIFACT_DIGEST, "--result", str(result_path)]
    for key, value in package.hashes.items():
        arguments.extend(["--" + key.replace("_", "-"), value])
    assert publisher.main(arguments) == int(refuse)
    assert checkouts == [package.context]
    result = json.loads(result_path.read_bytes())
    assert result["published"] is False
    assert result["status"] == ("failed" if refuse else "draft_pr_created")
    assert result["reason_code"] == ("API_HTTP_403" if refuse else None)
    assert TOKEN not in result_path.read_text() + capsys.readouterr().out
