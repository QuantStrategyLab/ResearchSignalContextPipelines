#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections.abc import Mapping
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_API_URL = "https://api.github.com"
DEFAULT_LABEL = "long-horizon-shadow"
COMMIT_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40}")


def github_request(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> Any:
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "research-signal-context-pipelines",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset("utf-8")
        raw = response.read().decode(charset)
        return json.loads(raw) if raw else None


def ensure_label(api_url: str, repo: str, token: str, label: str) -> None:
    encoded = urllib.parse.quote(label, safe="")
    label_url = f"{api_url}/repos/{repo}/labels/{encoded}"
    try:
        github_request("GET", label_url, token)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        github_request(
            "POST",
            f"{api_url}/repos/{repo}/labels",
            token,
            {
                "name": label,
                "color": "5319E7",
                "description": "Long-horizon AI shadow signal request and review",
            },
        )


def find_existing_issue(api_url: str, repo: str, token: str, label: str, title: str) -> dict[str, Any] | None:
    issues = github_request(
        "GET",
        f"{api_url}/repos/{repo}/issues?state=open&labels={urllib.parse.quote(label)}&per_page=100",
        token,
    )
    return next((issue for issue in issues if issue.get("title") == title), None)


def build_issue_title(as_of_date: str) -> str:
    return f"Long-horizon AI shadow signal: {as_of_date}"


def load_context_bundle(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_immutable_provenance(context_path: Path, producer_commit_sha: str) -> dict[str, str]:
    if not COMMIT_SHA_PATTERN.fullmatch(producer_commit_sha):
        raise ValueError("producer commit must be an immutable 40-hex commit")
    return {
        "producer_commit_sha": producer_commit_sha,
        "input_digest": f"sha256:{hashlib.sha256(context_path.read_bytes()).hexdigest()}",
    }


def resolve_as_of_date(raw_as_of_date: str | None, context_bundle: Mapping[str, Any] | None) -> str:
    if raw_as_of_date:
        return raw_as_of_date
    if context_bundle and str(context_bundle.get("as_of") or "").strip():
        return str(context_bundle["as_of"]).strip()
    return dt.date.today().isoformat()


def context_markdown(context_bundle: Mapping[str, Any] | None) -> str:
    if not context_bundle:
        return "\n".join(
            [
                "## Context",
                "",
                "No generated context bundle was attached to this request.",
                "If evidence is insufficient, report findings and leave artifacts unchanged.",
            ]
        )
    context_json = json.dumps(context_bundle, ensure_ascii=True, indent=2, sort_keys=True)
    return "\n".join(
        [
            "## Context Bundle",
            "",
            "Use this point-in-time context bundle as the primary evidence for the shadow signal review.",
            "",
            "```json",
            context_json,
            "```",
        ]
    )


def build_issue_body(
    *,
    as_of_date: str,
    source_ref: str,
    provider: str,
    bridge_repository: str,
    context_bundle: Mapping[str, Any] | None = None,
    immutable_provenance: Mapping[str, str] | None = None,
) -> str:
    sections = [
        [
            "## Long-Horizon Shadow Signal Request",
            "",
            f"- As of date: `{as_of_date}`",
            f"- Source ref: `{source_ref}`",
            f"- Bridge repository: `{bridge_repository}`",
            f"- Provider: `{provider}`",
            "- Mode: `shadow`",
            "- Required output: `data/output/latest_signal.json`",
            "- Historical copy: `data/output/signal_history/YYYY-MM-DD.json` when evidence is sufficient",
            "",
            "## Operator Notification",
            "",
            "This issue is the operator-facing notification for the shadow signal run.",
            "AIAuditBridge should post its review result here and may open a focused PR",
            "only for schema-valid shadow artifacts.",
            "",
            "## Boundaries",
            "",
            "- AI output must not place orders or change live strategy rules.",
            "- Any downstream use must remain advisory until a deterministic policy consumes the artifact.",
            "- If evidence is insufficient, report findings and leave artifacts unchanged.",
            "",
        ],
        [context_markdown(context_bundle)],
        [
            "",
            "## Immutable Provenance Contract",
            "",
            "Any new signal must update `latest_signal.json` and its sibling manifest in the same PR.",
            "The manifest must use this v2 contract; v1 is legacy_untrusted and cannot authorize downstream use.",
            "",
            "```json",
            json.dumps(
                {
                    "manifest_type": "research_signal_context",
                    "schema_version": 2,
                    "artifact": {
                        "path": "data/output/latest_signal.json",
                        "sha256": "sha256 of exact latest_signal.json bytes",
                    },
                    "as_of": "copy exactly from signal",
                    "generated_at": "copy exactly from signal",
                    "expires_at": "copy exactly from signal",
                    "mode": "copy exactly from signal",
                    "producer": {
                        "repository": "QuantStrategyLab/ResearchSignalContextPipelines",
                        "commit_sha": (immutable_provenance or {}).get("producer_commit_sha", "required 40-hex commit"),
                    },
                    "input_digest": (immutable_provenance or {}).get("input_digest", "required sha256 digest"),
                    "policy": {"execution_allowed": False},
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            ),
            "```",
            "",
            "Do not add a publisher or publication self-commit field. Do not upgrade existing v1 artifacts by hand.",
        ],
        [
            "",
            "Do not infer historical AI signals that were not generated point-in-time.",
        ],
    ]
    return "\n".join(line for section in sections for line in section)


def upsert_issue(
    *,
    api_url: str,
    repo: str,
    token: str,
    title: str,
    body: str,
    label: str,
) -> tuple[str, int, str]:
    ensure_label(api_url, repo, token, label)
    payload = {"title": title, "body": body, "labels": [label]}
    existing = find_existing_issue(api_url, repo, token, label, title)
    if existing:
        updated = github_request("PATCH", f"{api_url}/repos/{repo}/issues/{existing['number']}", token, payload)
        return "updated", int(updated["number"]), str(updated["html_url"])
    created = github_request("POST", f"{api_url}/repos/{repo}/issues", token, payload)
    return "created", int(created["number"]), str(created["html_url"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or update a long-horizon shadow signal issue.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--source-ref", default="main")
    parser.add_argument("--provider", default="codex")
    parser.add_argument("--bridge-repository", default="QuantStrategyLab/PersonalAIService")
    parser.add_argument("--as-of-date")
    parser.add_argument("--context-file", help="Optional JSON context bundle to embed in the issue body")
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    return parser.parse_args()


def write_outputs(*, action: str, issue_number: int, issue_url: str, title: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            print(f"issue_action={action}", file=handle)
            print(f"issue_number={issue_number}", file=handle)
            print(f"issue_url={issue_url}", file=handle)
            print(f"issue_title={title}", file=handle)
    print(f"issue_action={action}")
    print(f"issue_number={issue_number}")
    print(f"issue_url={issue_url}")
    print(f"issue_title={title}")


def main() -> int:
    args = parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 1
    if not args.context_file:
        print("immutable provenance requires --context-file", file=sys.stderr)
        return 1

    context_bundle = load_context_bundle(args.context_file)
    as_of_date = resolve_as_of_date(args.as_of_date, context_bundle)
    title = build_issue_title(as_of_date)
    try:
        immutable_provenance = build_immutable_provenance(
            Path(args.context_file),
            os.environ.get("GITHUB_SHA", args.source_ref),
        )
    except (OSError, ValueError) as exc:
        print(f"immutable provenance unavailable: {exc}", file=sys.stderr)
        return 1
    body = build_issue_body(
        as_of_date=as_of_date,
        source_ref=args.source_ref,
        provider=args.provider,
        bridge_repository=args.bridge_repository,
        context_bundle=context_bundle,
        immutable_provenance=immutable_provenance,
    )
    try:
        action, issue_number, issue_url = upsert_issue(
            api_url=args.api_url.rstrip("/"),
            repo=args.repo,
            token=token,
            title=title,
            body=body,
            label=args.label,
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"GitHub API request failed: {exc.code} {detail}", file=sys.stderr)
        return 1

    write_outputs(action=action, issue_number=issue_number, issue_url=issue_url, title=title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
