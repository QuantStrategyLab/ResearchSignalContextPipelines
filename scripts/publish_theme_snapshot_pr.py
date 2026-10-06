#!/usr/bin/env python3
"""Prepare one same-run theme JSON for a normal draft PR; publication is disabled."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from research_signal_context_pipelines.theme_momentum import (  # noqa: E402
    DEFAULT_TOP_SYMBOLS,
    build_theme_momentum_snapshot,
    validate_theme_momentum_snapshot,
)
from research_signal_context_pipelines.theme_universe import (  # noqa: E402
    load_symbol_theme_exposure,
    load_theme_taxonomy,
)

REPOSITORY = "QuantStrategyLab/ResearchSignalContextPipelines"
REPOSITORY_ID = "1252248184"
WORKFLOW_REF = f"{REPOSITORY}/.github/workflows/theme_momentum_snapshot.yml@refs/heads/main"
DATA_PATH = "data/output/theme_momentum_snapshot.json"
CANDIDATE_NAME = "theme_momentum_snapshot.json"
RECEIPT_NAME = "publication-package.json"
CONFIG_PATHS = ("config/theme_taxonomy.csv", "config/symbol_theme_exposure.csv")
MAX_BYTES = 128 * 1024
# Source-reviewed rights/content approval is required before changing this gate.
# A workflow input, environment variable, App grant or earlier one-off import is not approval.
PUBLICATION_CONTENT_APPROVED = False
PUBLIC_PRICE_SOURCE = "yahoo_chart_download"
SHA = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
POSITIVE = re.compile(r"[1-9][0-9]*\Z")


ERROR_CODES = frozenset("""
    API_ALREADY_EXISTS API_METHOD_FORBIDDEN API_PAGINATION_UNEXPECTED API_RESPONSE_OVERSIZED API_TARGET_FORBIDDEN
    API_TRANSPORT_FAILED API_WRITE_FORBIDDEN APP_IDENTITY_MISMATCH APP_SCOPE_MISMATCH
    ARGUMENTS_REQUIRED ARTIFACT_BINDING_INVALID BASE_INVALID BASE_MOVED
    BASE_PATH_INVALID BRANCH_CREATION_UNCONFIRMED BRANCH_MODIFIED CANDIDATE_EXPIRED
    CANDIDATE_PATH_INVALID CHECKOUT_SHA_MISMATCH CONFIG_HASH_MISMATCH CONTENT_APPROVAL_REQUIRED
    COUNT_INVALID COUNT_MISMATCH COVERAGE_MISMATCH DATE_INVALID
    FILE_NOT_REGULAR FILE_OVERSIZED GENERATION_TIME_INVALID JSON_DUPLICATE_KEY
    JSON_INVALID JSON_NONFINITE NUMBER_INVALID NUMBER_RANGE
    PACKAGE_CANDIDATE_MISMATCH PACKAGE_CONTEXT_MISMATCH PACKAGE_HASH_INVALID PACKAGE_HASH_MISMATCH
    PACKAGE_NOT_EMPTY PACKAGE_PATHS_INVALID PACKAGE_PATH_INVALID PACKAGE_SCHEMA_INVALID
    PRICE_SOURCE_NOT_ADMITTED PR_CREATION_UNCONFIRMED PR_HUMAN_STATE_CHANGED PR_IDENTITY_AMBIGUOUS
    PR_IDENTITY_MISMATCH PR_WRITE_FORBIDDEN PUBLICATION_FAILED RECEIPT_NONCANONICAL
    REF_WRITE_FORBIDDEN REMOTE_BLOB_MISMATCH REMOTE_COMMIT_INVALID REMOTE_REF_INVALID
    REMOTE_TREE_INVALID RESEARCH_CONTRACT_MISMATCH RESULT_REQUIRED RUN_INVALID
    SCHEMA_KEYS SCOPE_INVALID SOURCE_SHA_INVALID SUMMARY_MISMATCH
    SYMBOL_SCOPE_INVALID THEME_SCHEMA_INVALID THEME_SCOPE_INVALID TIME_INVALID
    TOKEN_UNAVAILABLE TREE_WRITE_FORBIDDEN UNTRUSTED_CONTEXT VALIDATION_OR_API_FAILED
""".split())


class PublicationError(RuntimeError):
    """Only fixed, non-sensitive error codes reach logs or receipts."""

    def __init__(self, code: str):
        self.code = code if code in ERROR_CODES or re.fullmatch(r"API_HTTP_[1-5][0-9]{2}", code) else "PUBLICATION_FAILED"
        super().__init__(self.code)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise PublicationError(code)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def git_blob_sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def parse_json(raw: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "JSON_DUPLICATE_KEY")
            result[key] = value
        return result

    def invalid(_):
        raise PublicationError("JSON_NONFINITE")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except PublicationError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise PublicationError("JSON_INVALID") from None


def regular_bytes(path: Path, *, limit: int = MAX_BYTES) -> bytes:
    require(path.exists() and not path.is_symlink() and stat.S_ISREG(path.stat().st_mode), "FILE_NOT_REGULAR")
    require(path.stat().st_size <= limit, "FILE_OVERSIZED")
    raw = path.read_bytes()
    require(len(raw) <= limit, "FILE_OVERSIZED")
    return raw


def keys(value: object, expected: str) -> dict:
    require(type(value) is dict and set(value) == set(expected.split()), "SCHEMA_KEYS")
    return value


def same_typed(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(same_typed(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(same_typed(a, b) for a, b in zip(actual, expected))
    return actual == expected


def date(value: object) -> dt.date:
    try:
        require(type(value) is str, "DATE_INVALID")
        parsed = dt.date.fromisoformat(value)
        require(parsed.isoformat() == value, "DATE_INVALID")
        return parsed
    except (ValueError, TypeError):
        raise PublicationError("DATE_INVALID") from None


def instant(value: object) -> dt.datetime:
    try:
        require(type(value) is str, "TIME_INVALID")
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None and parsed.utcoffset() == dt.timedelta(0), "TIME_INVALID")
        return parsed
    except (ValueError, TypeError):
        raise PublicationError("TIME_INVALID") from None


def number(value: object, low: float | None = None, high: float | None = None, *, nullable: bool = True) -> None:
    if value is None and nullable:
        return
    require(type(value) in (int, float) and math.isfinite(value), "NUMBER_INVALID")
    require((low is None or value >= low) and (high is None or value <= high), "NUMBER_RANGE")


def count(value: object, expected: int | None = None) -> None:
    require(type(value) is int and value >= 0, "COUNT_INVALID")
    require(expected is None or value == expected, "COUNT_MISMATCH")


def string_set(value: object, allowed: set[str]) -> set[str]:
    require(type(value) is list and all(type(x) is str for x in value), "SCOPE_INVALID")
    require(value == sorted(set(value)) and set(value) <= allowed, "SCOPE_INVALID")
    return set(value)


def context_from_env(env: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ if env is None else env
    require(env.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "UNTRUSTED_CONTEXT")
    require(env.get("GITHUB_REF") == "refs/heads/main", "UNTRUSTED_CONTEXT")
    require(env.get("GITHUB_REPOSITORY") == REPOSITORY, "UNTRUSTED_CONTEXT")
    require(env.get("GITHUB_REPOSITORY_ID") == REPOSITORY_ID, "UNTRUSTED_CONTEXT")
    require(env.get("GITHUB_WORKFLOW_REF") == WORKFLOW_REF, "UNTRUSTED_CONTEXT")
    source_sha = env.get("GITHUB_SHA", "")
    require(bool(SHA.fullmatch(source_sha)), "SOURCE_SHA_INVALID")
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        require(bool(POSITIVE.fullmatch(env.get(name, ""))), "RUN_INVALID")
    return {
        "repository": REPOSITORY, "repository_id": REPOSITORY_ID,
        "source_sha": source_sha, "expected_base_sha": source_sha,
        "workflow_ref": WORKFLOW_REF,
        "run_id": env["GITHUB_RUN_ID"], "run_attempt": env["GITHUB_RUN_ATTEMPT"],
    }


def admission(context: dict[str, str]) -> None:
    keys(context, "repository repository_id source_sha expected_base_sha workflow_ref run_id run_attempt")
    require(all(type(value) is str for value in context.values()), "UNTRUSTED_CONTEXT")
    require(context["repository_id"] == REPOSITORY_ID, "UNTRUSTED_CONTEXT")
    require(bool(SHA.fullmatch(context["source_sha"])) and context["source_sha"] == context["expected_base_sha"], "SOURCE_SHA_INVALID")
    require(bool(POSITIVE.fullmatch(context["run_id"])) and bool(POSITIVE.fullmatch(context["run_attempt"])), "RUN_INVALID")
    # This is deliberately not overridable by CLI inputs or environment variables.
    require(PUBLICATION_CONTENT_APPROVED is True, "CONTENT_APPROVAL_REQUIRED")
    require(context["repository"] == REPOSITORY and context["workflow_ref"] == WORKFLOW_REF, "UNTRUSTED_CONTEXT")


def verify_checkout(context: dict[str, str], root: Path = ROOT) -> None:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False)
    require(result.returncode == 0 and result.stdout.strip() == context["source_sha"], "CHECKOUT_SHA_MISMATCH")


def config_hashes(root: Path) -> dict[str, str]:
    return {path: sha256(regular_bytes(root / path)) for path in CONFIG_PATHS}


def validate_candidate(raw: bytes, root: Path, started_at: str, completed_at: str, *, now: dt.datetime | None = None) -> dict:
    require(len(raw) <= MAX_BYTES, "FILE_OVERSIZED")
    candidate = keys(parse_json(raw), "schema_version as_of generated_at expires_at model_version scoring_version mode artifact_type horizon horizon_window horizon_window_label taxonomy_version methodology summary theme_ranks data_quality policy source_artifacts")
    try:
        validate_theme_momentum_snapshot(candidate)
    except (ValueError, TypeError, KeyError):
        raise PublicationError("THEME_SCHEMA_INVALID") from None
    as_of = date(candidate["as_of"])
    generated = instant(candidate["generated_at"])
    started, completed = instant(started_at), instant(completed_at)
    now = now or dt.datetime.now(dt.UTC)
    require(started <= generated <= completed <= now and as_of <= generated.date(), "GENERATION_TIME_INVALID")
    require(now.date() <= date(candidate["expires_at"]), "CANDIDATE_EXPIRED")
    themes = load_theme_taxonomy(root / CONFIG_PATHS[0])
    exposures = load_symbol_theme_exposure(root / CONFIG_PATHS[1], known_theme_ids=themes)
    template = build_theme_momentum_snapshot([], themes=themes, exposures=exposures, as_of=as_of, generated_at=generated)
    for field in ("schema_version", "expires_at", "model_version", "scoring_version", "mode", "artifact_type", "horizon", "horizon_window", "horizon_window_label", "taxonomy_version", "methodology", "policy"):
        require(same_typed(candidate[field], template[field]), "RESEARCH_CONTRACT_MISMATCH")
    require(same_typed(candidate["source_artifacts"], {
        "prices": PUBLIC_PRICE_SOURCE, "theme_taxonomy": CONFIG_PATHS[0], "theme_exposures": CONFIG_PATHS[1],
    }), "PRICE_SOURCE_NOT_ADMITTED")
    quality = keys(candidate["data_quality"], "coverage missing_price_symbols insufficient_history_symbols unranked_themes")
    universe = set(exposures)
    missing = string_set(quality["missing_price_symbols"], universe)
    priced = universe - missing
    insufficient = string_set(quality["insufficient_history_symbols"], priced)
    unranked = string_set(quality["unranked_themes"], set(themes))
    coverage = keys(quality["coverage"], "configured_symbol_count priced_symbol_count price_coverage_ratio insufficient_history_symbol_count")
    count(coverage["configured_symbol_count"], len(universe))
    count(coverage["priced_symbol_count"], len(priced))
    count(coverage["insufficient_history_symbol_count"], len(insufficient))
    number(coverage["price_coverage_ratio"], 0, 1, nullable=False)
    require(coverage["price_coverage_ratio"] == round(len(priced) / len(universe), 6), "COVERAGE_MISMATCH")
    ranks = candidate["theme_ranks"]
    require(type(ranks) is list and len(ranks) <= len(themes), "THEME_SCOPE_INVALID")
    seen = set()
    for index, rank in enumerate(ranks, 1):
        keys(rank, "theme_id theme_name sector horizon rank momentum_score breadth_3m component_count priced_symbol_count returns risk top_symbols source_policy")
        theme_id = rank["theme_id"]
        require(type(theme_id) is str and theme_id in themes and theme_id not in seen, "THEME_SCOPE_INVALID")
        seen.add(theme_id)
        theme = themes[theme_id]
        for field in ("theme_name", "sector", "horizon", "source_policy"):
            require(type(rank[field]) is str and rank[field] == getattr(theme, field), "THEME_SCOPE_INVALID")
        members = {symbol for symbol, exposure in exposures.items() if theme_id in exposure.theme_ids}
        priced_members = members & priced
        require(bool(priced_members), "THEME_SCOPE_INVALID")
        count(rank["rank"], index)
        count(rank["component_count"], len(members))
        count(rank["priced_symbol_count"], len(priced_members))
        number(rank["momentum_score"])
        number(rank["breadth_3m"], 0, 1)
        for value in keys(rank["returns"], "3m 6_1m 12_1m").values():
            number(value, -1)
        risk = keys(rank["risk"], "realized_vol_63d drawdown_126d")
        number(risk["realized_vol_63d"], 0)
        number(risk["drawdown_126d"], -1, 0)
        top = rank["top_symbols"]
        require(type(top) is list and len(top) == min(DEFAULT_TOP_SYMBOLS, len(priced_members)), "SYMBOL_SCOPE_INVALID")
        symbols = set()
        for symbol in top:
            keys(symbol, "symbol momentum_score return_3m return_6_1m return_12_1m")
            name = symbol["symbol"]
            require(type(name) is str and name in priced_members and name not in symbols, "SYMBOL_SCOPE_INVALID")
            symbols.add(name)
            number(symbol["momentum_score"])
            for field in ("return_3m", "return_6_1m", "return_12_1m"):
                number(symbol[field], -1)
    expected_ranked = {theme_id for theme_id in themes if any(theme_id in exposures[symbol].theme_ids for symbol in priced)}
    require(seen == expected_ranked and unranked == set(themes) - seen, "THEME_SCOPE_INVALID")
    summary = keys(candidate["summary"], "ranked_theme_count priced_symbol_count top_theme_ids")
    count(summary["ranked_theme_count"], len(ranks))
    count(summary["priced_symbol_count"], len(priced))
    require(summary["top_theme_ids"] == [rank["theme_id"] for rank in ranks[:5]], "SUMMARY_MISMATCH")
    return candidate


def prepare_package(candidate: Path, directory: Path, context: dict, started_at: str, completed_at: str, *, root: Path = ROOT, now=None) -> dict:
    admission(context)
    candidate = candidate.absolute()
    expected = root.absolute() / DATA_PATH
    require(candidate == expected, "CANDIDATE_PATH_INVALID")
    require(not any(path.is_symlink() for path in (candidate, candidate.parent, candidate.parent.parent)), "FILE_NOT_REGULAR")
    raw = regular_bytes(candidate)
    validate_candidate(raw, root, started_at, completed_at, now=now)
    require(not directory.is_symlink(), "PACKAGE_PATH_INVALID")
    directory.mkdir(parents=True, exist_ok=True)
    require(not any(directory.iterdir()), "PACKAGE_NOT_EMPTY")
    receipt = {
        "schema_version": 1, "kind": "rscp_theme_publication", **context,
        "generation_started_at": started_at, "generation_completed_at": completed_at,
        "candidate": {"path": DATA_PATH, "sha256": sha256(raw), "size_bytes": len(raw)},
        "config_sha256": config_hashes(root),
    }
    receipt_raw = json_bytes(receipt)
    (directory / CANDIDATE_NAME).write_bytes(raw)
    (directory / RECEIPT_NAME).write_bytes(receipt_raw)
    return {"candidate_sha256": sha256(raw), "receipt_sha256": sha256(receipt_raw)}


def validate_package(directory: Path, context: dict, candidate_sha256: str, receipt_sha256: str, artifact_id: str, artifact_digest: str, *, root: Path = ROOT, now=None) -> tuple[bytes, dict]:
    admission(context)
    require(bool(SHA256.fullmatch(candidate_sha256)) and bool(SHA256.fullmatch(receipt_sha256)), "PACKAGE_HASH_INVALID")
    require(bool(POSITIVE.fullmatch(artifact_id)) and bool(SHA256.fullmatch(artifact_digest)), "ARTIFACT_BINDING_INVALID")
    require(directory.is_dir() and not directory.is_symlink(), "PACKAGE_PATH_INVALID")
    require({path.name for path in directory.iterdir()} == {CANDIDATE_NAME, RECEIPT_NAME}, "PACKAGE_PATHS_INVALID")
    raw, receipt_raw = regular_bytes(directory / CANDIDATE_NAME), regular_bytes(directory / RECEIPT_NAME)
    require(sha256(raw) == candidate_sha256 and sha256(receipt_raw) == receipt_sha256, "PACKAGE_HASH_MISMATCH")
    receipt = parse_json(receipt_raw)
    require(receipt_raw == json_bytes(receipt), "RECEIPT_NONCANONICAL")
    validate_receipt(raw, receipt, context, root=root, now=now)
    return raw, receipt


def validate_receipt(raw: bytes, receipt: dict, context: dict, *, root: Path = ROOT, now=None) -> None:
    keys(receipt, "schema_version kind repository repository_id source_sha expected_base_sha workflow_ref run_id run_attempt generation_started_at generation_completed_at candidate config_sha256")
    require(type(receipt["schema_version"]) is int and receipt["schema_version"] == 1 and receipt["kind"] == "rscp_theme_publication", "PACKAGE_SCHEMA_INVALID")
    require(all(type(receipt.get(key)) is str and receipt[key] == value for key, value in context.items()), "PACKAGE_CONTEXT_MISMATCH")
    require(same_typed(receipt["candidate"], {"path": DATA_PATH, "sha256": sha256(raw), "size_bytes": len(raw)}), "PACKAGE_CANDIDATE_MISMATCH")
    require(same_typed(receipt["config_sha256"], config_hashes(root)), "CONFIG_HASH_MISMATCH")
    validate_candidate(raw, root, receipt["generation_started_at"], receipt["generation_completed_at"], now=now)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def already_exists_response(error: urllib.error.HTTPError, method: str, path: str, payload: object) -> bool:
    """Recognize only bounded, exact conflicts for this attempted ref/PR."""
    if error.code != 422 or method != "POST" or type(payload) is not dict:
        return False
    if path not in {endpoint("git/refs"), endpoint("pulls")}:
        return False
    try:
        raw = error.read(16384 + 1)
        if len(raw) > 16384:
            return False
        value = parse_json(raw)
        if type(value) is not dict:
            return False
        if path == endpoint("git/refs"):
            return value.get("message") == "Reference already exists"
        errors = value.get("errors")
        expected = f"A pull request already exists for QuantStrategyLab:{payload.get('head')}."
        return (
            value.get("message") == "Validation Failed"
            and type(errors) is list and len(errors) == 1 and type(errors[0]) is dict
            and errors[0].get("resource") == "PullRequest"
            and errors[0].get("code") == "custom"
            and errors[0].get("message") == expected
        )
    except Exception:
        return False
    finally:
        error.close()


def may_reconcile(error: PublicationError) -> bool:
    return error.code in {"API_TRANSPORT_FAILED", "API_ALREADY_EXISTS"} or bool(re.fullmatch(r"API_HTTP_5[0-9]{2}", error.code))


class GitHub:
    """Fixed GitHub host; no credential persistence, redirects, raw errors or retries."""

    def __init__(self, token: str, *, read_only: bool = False):
        require(bool(token), "TOKEN_UNAVAILABLE")
        self.token, self.read_only = token, read_only

    def request(self, method: str, path: str, payload=None, *, missing_ok=False, single_page=False):
        require(method in {"GET", "POST"} and (not self.read_only or method == "GET"), "API_METHOD_FORBIDDEN")
        require(path.startswith(f"/repos/{REPOSITORY}/") or path == "/installation/repositories?per_page=100", "API_TARGET_FORBIDDEN")
        if method == "POST":
            allowed = {endpoint(name) for name in ("git/blobs", "git/trees", "git/commits", "git/refs", "pulls")}
            require(path in allowed and type(payload) is dict, "API_WRITE_FORBIDDEN")
            if path == endpoint("git/refs"):
                require(type(payload.get("ref")) is str and bool(re.fullmatch(r"refs/heads/automation/theme-[1-9][0-9]*-[1-9][0-9]*-[0-9a-f]{16}", payload["ref"])), "REF_WRITE_FORBIDDEN")
            elif path == endpoint("git/trees"):
                entries = payload.get("tree")
                require(type(entries) is list and len(entries) == 1, "TREE_WRITE_FORBIDDEN")
                entry = keys(entries[0], "path mode type sha")
                require(entry["path"] == DATA_PATH and entry["mode"] == "100644" and entry["type"] == "blob", "TREE_WRITE_FORBIDDEN")
            elif path == endpoint("pulls"):
                require(payload.get("draft") is True and payload.get("base") == "main" and bool(re.fullmatch(r"automation/theme-[1-9][0-9]*-[1-9][0-9]*-[0-9a-f]{16}", str(payload.get("head", "")))), "PR_WRITE_FORBIDDEN")
        request = urllib.request.Request(
            "https://api.github.com" + path,
            data=json.dumps(payload, allow_nan=False).encode() if payload is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"},
            method=method,
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
                require(not single_page or not response.headers.get("Link"), "API_PAGINATION_UNEXPECTED")
                raw = response.read(1024 * 1024 + 1)
                require(len(raw) <= 1024 * 1024, "API_RESPONSE_OVERSIZED")
                return parse_json(raw)
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            if already_exists_response(error, method, path, payload):
                raise PublicationError("API_ALREADY_EXISTS") from None
            raise PublicationError(f"API_HTTP_{error.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise PublicationError("API_TRANSPORT_FAILED") from None


def endpoint(suffix: str) -> str:
    return f"/repos/{REPOSITORY}/{suffix}"


def assert_base(api: GitHub, context: dict) -> None:
    ref = api.request("GET", endpoint("git/ref/heads/main"))
    require(type(ref) is dict and ref.get("object", {}).get("sha") == context["expected_base_sha"], "BASE_MOVED")


def verify_app_scope(api: GitHub, installation_id: str, app_slug: str) -> None:
    require(installation_id == "168603414" and bool(re.fullmatch(r"[a-z0-9-]+", app_slug)), "APP_IDENTITY_MISMATCH")
    scope = api.request("GET", "/installation/repositories?per_page=100", single_page=True)
    require(type(scope) is dict and type(scope.get("total_count")) is int and scope["total_count"] == 1, "APP_SCOPE_MISMATCH")
    repos = scope.get("repositories")
    require(type(repos) is list and len(repos) == 1 and type(repos[0]) is dict, "APP_SCOPE_MISMATCH")
    require(repos[0].get("full_name") == REPOSITORY and type(repos[0].get("id")) is int and repos[0]["id"] == int(REPOSITORY_ID), "APP_SCOPE_MISMATCH")


def leaf_tree(api: GitHub, tree_sha: str) -> dict:
    require(bool(SHA.fullmatch(tree_sha)), "REMOTE_TREE_INVALID")
    response = api.request("GET", endpoint(f"git/trees/{tree_sha}?recursive=1"))
    require(response.get("truncated") is False and type(response.get("tree")) is list, "REMOTE_TREE_INVALID")
    leaves = [entry for entry in response["tree"] if entry.get("type") != "tree"]
    require(len({entry.get("path") for entry in leaves}) == len(leaves), "REMOTE_TREE_INVALID")
    return {entry["path"]: (entry.get("mode"), entry.get("type"), entry.get("sha")) for entry in leaves}


def assert_candidate_commit(api: GitHub, head: str, base: str, expected_tree: dict, message: str) -> None:
    commit = api.request("GET", endpoint(f"git/commits/{head}"))
    require(commit.get("sha") == head and [parent.get("sha") for parent in commit.get("parents", [])] == [base], "BRANCH_MODIFIED")
    require(commit.get("message") == message, "BRANCH_MODIFIED")
    require(leaf_tree(api, commit.get("tree", {}).get("sha", "")) == expected_tree, "BRANCH_MODIFIED")


def find_pr(api: GitHub, branch: str, head: str, marker: str, app_slug: str):
    query = urllib.parse.urlencode({"state": "all", "head": f"QuantStrategyLab:{branch}", "base": "main", "per_page": "100"})
    results = api.request("GET", endpoint("pulls?" + query), single_page=True)
    require(type(results) is list and len(results) <= 1, "PR_IDENTITY_AMBIGUOUS")
    if not results:
        return None
    return validate_pr(results[0], branch, head, marker, app_slug)


def validate_pr(pr: dict, branch: str, head: str, marker: str, app_slug: str) -> dict:
    require(pr.get("state") == "open" and pr.get("draft") is True and not pr.get("merged_at"), "PR_HUMAN_STATE_CHANGED")
    require(pr.get("head", {}).get("ref") == branch and pr["head"].get("sha") == head and pr.get("base", {}).get("ref") == "main", "PR_IDENTITY_MISMATCH")
    for side in ("head", "base"):
        require(pr[side].get("repo", {}).get("full_name") == REPOSITORY and pr[side]["repo"].get("id") == int(REPOSITORY_ID), "PR_IDENTITY_MISMATCH")
    require(pr.get("user", {}).get("login") == app_slug + "[bot]" and pr["user"].get("type") == "Bot", "PR_IDENTITY_MISMATCH")
    require(type(pr.get("body")) is str and pr["body"].count(marker) == 1, "PR_IDENTITY_MISMATCH")
    require(pr["body"].count(f"<!-- rscp-theme-head:{head} -->") == 1, "PR_IDENTITY_MISMATCH")
    require(type(pr.get("number")) is int and pr["number"] > 0, "PR_IDENTITY_MISMATCH")
    require(pr.get("html_url") == f"https://github.com/{REPOSITORY}/pull/{pr['number']}", "PR_IDENTITY_MISMATCH")
    return pr


def publish_candidate(api: GitHub, raw: bytes, receipt: dict, context: dict, artifact_id: str, artifact_digest: str, app_slug: str, installation_id: str, result: dict, *, root: Path = ROOT, now=None, receipt_sha256: str | None = None) -> dict:
    admission(context)
    validate_receipt(raw, receipt, context, root=root, now=now)
    require(bool(POSITIVE.fullmatch(artifact_id)) and bool(SHA256.fullmatch(artifact_digest)), "ARTIFACT_BINDING_INVALID")
    canonical_receipt_sha256 = sha256(json_bytes(receipt))
    require(receipt_sha256 is None or receipt_sha256 == canonical_receipt_sha256, "PACKAGE_HASH_MISMATCH")
    receipt_sha256 = canonical_receipt_sha256 if receipt_sha256 is None else receipt_sha256
    result.update(candidate_sha256=sha256(raw), receipt_sha256=receipt_sha256, artifact_id=artifact_id, artifact_digest=artifact_digest, **context)
    verify_app_scope(api, installation_id, app_slug)
    assert_base(api, context)
    base = context["expected_base_sha"]
    base_commit = api.request("GET", endpoint(f"git/commits/{base}"))
    require(base_commit.get("sha") == base, "BASE_INVALID")
    base_tree_sha = base_commit.get("tree", {}).get("sha", "")
    expected_tree = leaf_tree(api, base_tree_sha)
    require(DATA_PATH in expected_tree and expected_tree[DATA_PATH][:2] == ("100644", "blob"), "BASE_PATH_INVALID")
    blob = git_blob_sha(raw)
    if expected_tree[DATA_PATH][2] == blob:
        result.update(status="already_on_base", main_contains_candidate=True)
        return result
    expected_tree[DATA_PATH] = ("100644", "blob", blob)
    package_key = sha256(json_bytes({**context, "candidate_sha256": sha256(raw)}))
    branch = f"automation/theme-{context['run_id']}-{context['run_attempt']}-{package_key[:16]}"
    marker = f"<!-- rscp-theme-publication:{package_key} -->"
    commit_message = f"Update reviewed research theme snapshot\n\nResearch-Package: {package_key}"
    expected_new_head = None
    result.update(branch=branch, package_key=package_key, candidate_sha256=sha256(raw))
    ref_path = endpoint("git/ref/heads/" + branch)
    ref = api.request("GET", ref_path, missing_ok=True)
    if ref is None:
        created_blob = api.request("POST", endpoint("git/blobs"), {"encoding": "base64", "content": base64.b64encode(raw).decode("ascii")})
        require(created_blob.get("sha") == blob, "REMOTE_BLOB_MISMATCH")
        tree = api.request("POST", endpoint("git/trees"), {"base_tree": base_tree_sha, "tree": [{"path": DATA_PATH, "mode": "100644", "type": "blob", "sha": blob}]})
        require(bool(SHA.fullmatch(tree.get("sha", ""))), "REMOTE_TREE_INVALID")
        commit = api.request("POST", endpoint("git/commits"), {"message": commit_message, "tree": tree["sha"], "parents": [base]})
        head = commit.get("sha", "")
        require(bool(SHA.fullmatch(head)), "REMOTE_COMMIT_INVALID")
        expected_new_head = head
        assert_candidate_commit(api, head, base, expected_tree, commit_message)
        assert_base(api, context)
        result["branch_created"] = None
        try:
            api.request("POST", endpoint("git/refs"), {"ref": "refs/heads/" + branch, "sha": head})
        except PublicationError as error:
            # Only unknown outcomes / exact already-exists conflicts permit one read.
            if not may_reconcile(error):
                if re.fullmatch(r"API_HTTP_4[0-9]{2}", error.code):
                    result["branch_created"] = False
                raise
        ref = api.request("GET", ref_path, missing_ok=True)
        if ref is None:
            result["branch_created"] = False
            raise PublicationError("BRANCH_CREATION_UNCONFIRMED")
    head = ref.get("object", {}).get("sha", "")
    require(bool(SHA.fullmatch(head)), "REMOTE_REF_INVALID")
    require(expected_new_head is None or head == expected_new_head, "BRANCH_MODIFIED")
    assert_candidate_commit(api, head, base, expected_tree, commit_message)
    result.update(branch_created=True, head_sha=head)
    assert_base(api, context)
    pr = find_pr(api, branch, head, marker, app_slug)
    reused = pr is not None
    if pr is None:
        body = "\n".join([
            marker, f"<!-- rscp-theme-head:{head} -->",
            "Research-only theme context; human review and required CI remain mandatory.",
            f"Source/base: {base}", f"Run: https://github.com/{REPOSITORY}/actions/runs/{context['run_id']}",
            f"Run attempt: {context['run_attempt']}", f"Candidate raw SHA256: {sha256(raw)}",
            f"Receipt raw SHA256: {receipt_sha256}",
            f"Same-run artifact ID: {artifact_id}; archive SHA256: {artifact_digest}",
            "State: draft PR prepared; published=false until separately reviewed and merged.",
        ])
        result["pr_created"] = None
        try:
            response = api.request("POST", endpoint("pulls"), {"title": "Update reviewed research theme snapshot", "head": branch, "base": "main", "body": body, "draft": True})
        except PublicationError as error:
            if not may_reconcile(error):
                if re.fullmatch(r"API_HTTP_4[0-9]{2}", error.code):
                    result["pr_created"] = False
                raise
            pr = find_pr(api, branch, head, marker, app_slug)
        else:
            # A local response-validation failure is not an unknown write outcome.
            pr = validate_pr(response, branch, head, marker, app_slug)
        require(pr is not None, "PR_CREATION_UNCONFIRMED")
    result.update(status="draft_pr_reused" if reused else "draft_pr_created", pr_created=True, pr_url=pr["html_url"], review_required=True)
    assert_base(api, context)
    return result


def initial_result() -> dict:
    return {"status": "not_run", "reason_code": None, "published": False, "branch_created": False, "pr_created": False, "review_required": True}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("admission", "prepare", "validate", "publish"))
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--package-dir", type=Path)
    parser.add_argument("--started-at")
    parser.add_argument("--completed-at")
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--receipt-sha256")
    parser.add_argument("--artifact-id")
    parser.add_argument("--artifact-digest")
    parser.add_argument("--result", type=Path)
    args = parser.parse_args(argv)
    result = initial_result()
    try:
        context = context_from_env()
        admission(context)
        verify_checkout(context)
        if args.mode == "admission":
            result["status"] = "admitted"
        elif args.mode == "prepare":
            require(all((args.candidate, args.package_dir, args.started_at, args.completed_at)), "ARGUMENTS_REQUIRED")
            outputs = prepare_package(args.candidate, args.package_dir, context, args.started_at, args.completed_at)
            with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
                for key, value in outputs.items():
                    output.write(f"{key}={value}\n")
            result.update(status="package_prepared", **outputs)
        else:
            require(all((args.package_dir, args.candidate_sha256, args.receipt_sha256, args.artifact_id, args.artifact_digest)), "ARGUMENTS_REQUIRED")
            raw, receipt = validate_package(args.package_dir, context, args.candidate_sha256, args.receipt_sha256, args.artifact_id, args.artifact_digest)
            if args.mode == "validate":
                assert_base(GitHub(os.environ.get("GITHUB_TOKEN", ""), read_only=True), context)
                result["status"] = "package_validated"
            else:
                require(args.result is not None, "RESULT_REQUIRED")
                publish_candidate(GitHub(os.environ.get("RESEARCH_PUBLISHER_TOKEN", "")), raw, receipt, context, args.artifact_id, args.artifact_digest, os.environ.get("RESEARCH_PUBLISHER_APP_SLUG", ""), os.environ.get("RESEARCH_PUBLISHER_INSTALLATION_ID", ""), result, receipt_sha256=args.receipt_sha256)
    except Exception as error:
        result.update(status="failed", reason_code=error.code if isinstance(error, PublicationError) else "VALIDATION_OR_API_FAILED")
    if args.result is not None:
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_bytes(json_bytes(result))
    print(json.dumps(result, sort_keys=True))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
