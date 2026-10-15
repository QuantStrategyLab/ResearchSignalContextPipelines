from __future__ import annotations

import json
import sys

from scripts import post_shadow_signal_request as shadow_issue


def test_build_issue_body_marks_notification_and_shadow_boundary() -> None:
    body = shadow_issue.build_issue_body(
        as_of_date="2026-05-29",
        source_ref="main",
        provider="auto",
        bridge_repository="QuantStrategyLab/PersonalAIService",
        context_bundle={"as_of": "2026-05-29", "price_context": {"QQQ": {"trend": "above_200d"}}},
    )

    assert "operator-facing notification" in body
    assert "Mode: `shadow`" in body
    assert "must not place orders" in body
    assert "Do not infer historical AI signals" in body
    assert "Context Bundle" in body
    assert '"QQQ"' in body


def test_resolve_as_of_date_prefers_context_bundle() -> None:
    assert shadow_issue.resolve_as_of_date(None, {"as_of": "2026-05-29"}) == "2026-05-29"
    assert shadow_issue.resolve_as_of_date("2026-05-28", {"as_of": "2026-05-29"}) == "2026-05-28"


def test_parse_args_defaults_provider_to_codex(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["post_shadow_signal_request.py", "--repo", "QuantStrategyLab/ResearchSignalContextPipelines"])
    args = shadow_issue.parse_args()

    assert args.provider == "codex"


def test_upsert_issue_updates_existing_issue(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_github_request(method, url, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET" and url.endswith("/labels/long-horizon-shadow"):
            return {"name": "long-horizon-shadow"}
        if method == "GET" and "/issues?state=open" in url:
            return [
                {
                    "number": 7,
                    "title": "Long-horizon AI shadow signal: 2026-05-29",
                    "html_url": "https://github.test/issues/7",
                }
            ]
        if method == "PATCH":
            return {"number": 7, "html_url": "https://github.test/issues/7"}
        raise AssertionError(f"unexpected request: {method} {url} {json.dumps(payload)}")

    monkeypatch.setattr(shadow_issue, "github_request", fake_github_request)

    action, issue_number, issue_url = shadow_issue.upsert_issue(
        api_url="https://api.github.test",
        repo="QuantStrategyLab/ResearchSignalContextPipelines",
        token="token",
        title="Long-horizon AI shadow signal: 2026-05-29",
        body="body",
        label="long-horizon-shadow",
    )

    assert action == "updated"
    assert issue_number == 7
    assert issue_url == "https://github.test/issues/7"
    assert calls[-1][0] == "PATCH"


def test_upsert_issue_creates_missing_label_and_issue(monkeypatch) -> None:
    import urllib.error

    calls: list[tuple[str, str, dict | None]] = []

    def fake_github_request(method, url, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET" and url.endswith("/labels/long-horizon-shadow"):
            raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)
        if method == "POST" and url.endswith("/labels"):
            return {"name": payload["name"]}
        if method == "GET" and "/issues?state=open" in url:
            return []
        if method == "POST" and url.endswith("/issues"):
            return {"number": 8, "html_url": "https://github.test/issues/8"}
        raise AssertionError(f"unexpected request: {method} {url} {json.dumps(payload)}")

    monkeypatch.setattr(shadow_issue, "github_request", fake_github_request)

    action, issue_number, issue_url = shadow_issue.upsert_issue(
        api_url="https://api.github.test",
        repo="QuantStrategyLab/ResearchSignalContextPipelines",
        token="token",
        title="Long-horizon AI shadow signal: 2026-05-29",
        body="body",
        label="long-horizon-shadow",
    )

    assert action == "created"
    assert issue_number == 8
    assert issue_url == "https://github.test/issues/8"
    assert [call[0] for call in calls] == ["GET", "POST", "GET", "POST"]
