from __future__ import annotations

from pathlib import Path
import copy
import io
import re
import os
import subprocess
import sys
from datetime import datetime
import textwrap
import urllib.request

import json
import pytest


IDENTITY_REPOSITORY = "QuantStrategyLab/ResearchSignalContextPipelines"
IDENTITY_WORKFLOW = "theme_momentum_snapshot.yml"


# Identity-only tests execute the reviewed inline Python with mocked HTTP only.


IDENTITY_ACTION = "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1"
IDENTITY_URL = "https://api.github.com/installation/repositories?per_page=100"


def identity_workflow():
    return Path(__file__).parents[1] / ".github" / "workflows" / IDENTITY_WORKFLOW


def workflow_job(job_id):
    jobs = identity_workflow().read_text().split("\njobs:\n", 1)[1]
    return next(
        "  " + block for block in re.split(r"(?m)^  (?=[a-z][a-z-]+:\n)", jobs)
        if block.startswith(job_id + ":\n")
    )


def workflow_steps(job_id):
    return ["      - " + block for block in re.split(r"(?m)^      - ", workflow_job(job_id))[1:]]


def identity_steps():
    return workflow_steps("build-theme-momentum")


def identity_step(step_id):
    return next(block for block in identity_steps() if f"        id: {step_id}\n" in block)


def identity_python(step_id):
    block = identity_step(step_id).split("        run: |\n", 1)[1]
    script = textwrap.dedent(block)
    assert script.startswith("python - <<'PY'\n")
    return script.removeprefix("python - <<'PY'\n").rsplit("\nPY", 1)[0]


def identity_env(monkeypatch, tmp_path):
    values = {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": IDENTITY_REPOSITORY,
        "GITHUB_REPOSITORY_ID": "123456",
        "GITHUB_WORKFLOW_REF": f"{IDENTITY_REPOSITORY}/.github/workflows/{IDENTITY_WORKFLOW}@refs/heads/main",
        "RESEARCH_PUBLISHER_APP_ID": "5214420",
        "PUBLISH_DRAFT_PR": "false",
        "RESEARCH_PUBLISHER_INSTALLATION_ID": "168603414",
        "RESEARCH_PUBLISHER_TOKEN": "synthetic-test-token-never-real",
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def run_identity_python(step_id):
    exec(compile(identity_python(step_id), f"<mocked-{step_id}>", "exec"), {"__name__": "__main__"})


def identity_payload():
    return {"total_count": 1, "repositories": [{"id": 123456, "full_name": IDENTITY_REPOSITORY}]}


def mock_identity_http(monkeypatch, payload=None, *, body=None, status=200, headers=None, error=None):
    calls = []
    raw = body if body is not None else json.dumps(payload or identity_payload()).encode()

    class Response(io.BytesIO):
        def __init__(self):
            super().__init__(raw)
            self.status = status
            self.headers = headers or {}

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert request.full_url == IDENTITY_URL
            assert request.get_method() == "GET"
            assert request.data is None
            assert request.get_header("Authorization") == "Bearer synthetic-test-token-never-real"
            assert timeout == 20
            if error:
                raise error
            return Response()

    def build_opener(handler):
        assert isinstance(handler, urllib.request.HTTPRedirectHandler)
        assert handler.redirect_request(None, None, 302, "", {}, "https://untrusted.example/") is None
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    return calls


def test_identity_only_is_opt_in_and_isolates_all_normal_steps():
    text = identity_workflow().read_text()
    assert "      identity_only:\n" in text
    assert "        default: false\n        type: boolean" in text
    steps = identity_steps()
    identity = [step for step in steps if "        id: publisher_identity_" in step]
    assert len(identity) == 4
    for step in steps:
        if step not in identity:
            guard = next(line for line in step.splitlines() if line.strip().startswith("if:"))
            assert "!inputs.identity_only" in guard
            if "id: snapshot_upload" not in step:
                assert "||" not in guard
    for step in identity:
        assert "github.event_name == 'workflow_dispatch' && inputs.identity_only" in step
    assert "always()" in identity_step("publisher_identity_summary")
    mint = identity_step("publisher_identity_token")
    assert IDENTITY_ACTION in mint
    assert f"          repositories: {IDENTITY_REPOSITORY.split('/')[1]}\n" in mint
    assert "          owner: QuantStrategyLab\n" in mint
    assert "          permission-contents: write\n" in mint
    assert "          permission-pull-requests: write\n" in mint
    assert mint.count("          permission-") == 2
    assert "skip-token-revoke" not in mint
    assert "CROSS_REPO" not in "".join(identity)
    assert "persist-credentials" not in "".join(identity)
    assert "steps.publisher_identity_token.outputs.token" in identity_step("publisher_identity_verify")


def test_identity_preflight_succeeds_without_network(monkeypatch, tmp_path):
    values = identity_env(monkeypatch, tmp_path)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: pytest.fail("preflight must not use HTTP"))
    run_identity_python("publisher_identity_preflight")
    assert Path(values["GITHUB_OUTPUT"]).read_text() == "app_id_matches=true\n"


@pytest.mark.parametrize(("key", "value"), [
    ("GITHUB_EVENT_NAME", "schedule"),
    ("GITHUB_REF", "refs/heads/unreviewed"),
    ("GITHUB_REPOSITORY", "other/other"),
    ("GITHUB_REPOSITORY_ID", "not-an-id"),
    ("GITHUB_WORKFLOW_REF", "other/other/.github/workflows/test.yml@refs/heads/main"),
    ("RESEARCH_PUBLISHER_APP_ID", "123"),
    ("PUBLISH_DRAFT_PR", "true"),
])
def test_identity_preflight_fails_closed(monkeypatch, tmp_path, capsys, key, value):
    identity_env(monkeypatch, tmp_path)
    monkeypatch.setenv(key, value)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: pytest.fail("preflight must not use HTTP"))
    with pytest.raises(SystemExit, match="1"):
        run_identity_python("publisher_identity_preflight")
    assert "PUBLISHER_IDENTITY_PREFLIGHT_FAILED" in capsys.readouterr().out


def test_identity_checks_single_repository_without_writing(monkeypatch, tmp_path, capsys):
    values = identity_env(monkeypatch, tmp_path)
    calls = mock_identity_http(monkeypatch)
    run_identity_python("publisher_identity_verify")
    assert len(calls) == 1
    output = Path(values["GITHUB_OUTPUT"]).read_text()
    assert "installation_matches=true\n" in output
    assert "single_repo_scope_matches=true\n" in output
    assert "identity_verified=true\n" in output
    assert "synthetic-test-token" not in capsys.readouterr().out + output


@pytest.mark.parametrize("mutation", ["extra_repo", "wrong_name", "wrong_id", "bool_id", "bool_count", "wrong_count", "no_repo"])
def test_identity_rejects_wrong_repository_scope(monkeypatch, tmp_path, capsys, mutation):
    values = identity_env(monkeypatch, tmp_path)
    payload = copy.deepcopy(identity_payload())
    if mutation == "extra_repo":
        payload["repositories"].append({"id": 44, "full_name": "other/other"})
    elif mutation == "wrong_name":
        payload["repositories"][0]["full_name"] = "other/other"
    elif mutation == "wrong_id":
        payload["repositories"][0]["id"] = 44
    elif mutation == "bool_id":
        payload["repositories"][0]["id"] = True
    elif mutation == "bool_count":
        payload["total_count"] = True
    elif mutation == "wrong_count":
        payload["total_count"] = 2
    else:
        payload["repositories"] = []
    mock_identity_http(monkeypatch, payload)
    with pytest.raises(SystemExit, match="1"):
        run_identity_python("publisher_identity_verify")
    assert "identity_verified=false" in Path(values["GITHUB_OUTPUT"]).read_text()
    assert "synthetic-test-token" not in capsys.readouterr().out


@pytest.mark.parametrize("kwargs", [
    {"headers": {"Link": '<https://api.github.com/installation/repositories?page=2>; rel="next"'}},
    {"status": 302},
    {"body": b"invalid JSON synthetic-test-token-never-real"},
    {"body": b"x" * (1024 * 1024 + 1)},
    {"error": OSError("sensitive-response synthetic-test-token-never-real")},
])
def test_identity_failures_do_not_leak_responses(monkeypatch, tmp_path, capsys, kwargs):
    values = identity_env(monkeypatch, tmp_path)
    mock_identity_http(monkeypatch, **kwargs)
    with pytest.raises(SystemExit, match="1"):
        run_identity_python("publisher_identity_verify")
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == "PUBLISHER_IDENTITY_VERIFICATION_FAILED\n"
    assert "identity_verified=false" in Path(values["GITHUB_OUTPUT"]).read_text()


@pytest.mark.parametrize(("key", "value"), [("RESEARCH_PUBLISHER_INSTALLATION_ID", "1"), ("RESEARCH_PUBLISHER_TOKEN", "")])
def test_identity_rejects_bad_installation_or_missing_token_before_http(monkeypatch, tmp_path, key, value):
    identity_env(monkeypatch, tmp_path)
    monkeypatch.setenv(key, value)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: pytest.fail("HTTP must not run"))
    with pytest.raises(SystemExit, match="1"):
        run_identity_python("publisher_identity_verify")


def test_identity_summary_defaults_to_unpublished_and_unverified(monkeypatch, tmp_path, capsys):
    values = identity_env(monkeypatch, tmp_path)
    for key in ["APP_ID_MATCHES", "INSTALLATION_MATCHES", "SINGLE_REPO_SCOPE_MATCHES", "IDENTITY_VERIFIED"]:
        monkeypatch.delenv(key, raising=False)
    run_identity_python("publisher_identity_summary")
    summary = json.loads(capsys.readouterr().out)
    assert summary == {"app_id_matches": False, "installation_matches": False, "single_repo_scope_matches": False, "identity_verified": False, "published": False}
    assert "synthetic-test-token" not in Path(values["GITHUB_STEP_SUMMARY"]).read_text()


def publication_step(step_id):
    return next(
        block for block in workflow_steps("publish-theme-draft-pr")
        if f"        id: {step_id}\n" in block
    )


def run_inline_python(block):
    script = textwrap.dedent(block.split("        run: |\n", 1)[1])
    assert script.startswith("python - <<'PY'\n")
    source = script.removeprefix("python - <<'PY'\n").rsplit("\nPY", 1)[0]
    exec(compile(source, "<mocked-workflow-step>", "exec"), {"__name__": "__main__"})


def test_workflow_never_commits_outputs_or_grants_default_write_permissions():
    text = identity_workflow().read_text()
    assert "permissions:\n  contents: read\n" in text
    assert not re.search(r"(?m)^\s+contents: write$", text)
    for removed in ("git push", "git commit", "git add", "GH013", "continue-on-error", "[skip ci]"):
        assert removed not in text
    assert 'description: "Deprecated, ignored:' in text
    assert "      commit_outputs:\n" in text
    assert "      publish_draft_pr:\n" in text
    publication_input = text.split("      publish_draft_pr:\n", 1)[1].split("      as_of:\n", 1)[0]
    assert "        default: false\n        type: boolean\n" in publication_input
    schedule = text.split("  schedule:\n", 1)[1].split("\npermissions:\n", 1)[0]
    assert "publish_draft_pr" not in schedule
    assert "artifact-only" in schedule
    for step in identity_steps():
        if "commit_outputs" in step:
            assert "id: generation_summary" in step


def test_all_workflow_action_dependencies_are_immutable():
    actions = re.findall(r"(?m)^\s+uses: (\S+)", identity_workflow().read_text())
    assert actions
    for action in actions:
        assert re.fullmatch(r"actions/[a-z-]+@[a-f0-9]{40}", action)
    assert "skip-token-revoke" not in identity_workflow().read_text()


def test_content_admission_is_immediately_after_checkout_and_before_collection():
    steps = identity_steps()
    checkout = steps.index(identity_step("generation_checkout"))
    admission = steps.index(identity_step("publisher_admission"))
    assert admission == checkout + 1
    assert "python scripts/publish_theme_snapshot_pr.py admission" in steps[admission]
    assert "!inputs.identity_only && inputs.publish_draft_pr" in steps[admission]
    assert "github.sha" in steps[checkout]
    assert "persist-credentials: false" in steps[checkout]
    for step in steps[admission + 1:]:
        if any(label in step for label in ("setup-python", "name: Install package", "id: generation\n", "id: publisher_package\n")):
            assert "always()" not in step
            assert "continue-on-error" not in step
    assert steps.index(identity_step("generation")) > admission
    preflight = identity_step("publisher_identity_preflight")
    assert "PUBLISH_DRAFT_PR: ${{ inputs.publish_draft_pr }}" in preflight
    assert "success()" in identity_step("publisher_identity_token")
    assert "success()" in identity_step("publisher_identity_verify")


def test_same_run_package_has_only_candidate_and_receipt_and_bound_outputs():
    build = workflow_job("build-theme-momentum")
    package = identity_step("publisher_package")
    upload = identity_step("publisher_package_upload")
    assert '--candidate data/output/theme_momentum_snapshot.json' in package
    assert 'GENERATION_STARTED_AT: ${{ steps.generation.outputs.started_at }}' in package
    assert 'GENERATION_COMPLETED_AT: ${{ steps.generation.outputs.completed_at }}' in package
    assert 'PACKAGE_DIR: ${{ runner.temp }}/theme-publication-package' in package
    assert 'if: ${{ !inputs.identity_only && inputs.publish_draft_pr }}' in package
    assert 'if: ${{ !inputs.identity_only && inputs.publish_draft_pr }}' in upload
    paths = upload.split('          path: |\n', 1)[1].split('          if-no-files-found:', 1)[0]
    assert paths.splitlines() == [
        '            ${{ runner.temp }}/theme-publication-package/theme_momentum_snapshot.json',
        '            ${{ runner.temp }}/theme-publication-package/publication-package.json',
    ]
    assert '${{ github.run_id }}-${{ github.run_attempt }}' in upload
    for output, source in (
        ('artifact_id', 'steps.publisher_package_upload.outputs.artifact-id'),
        ('artifact_digest', 'steps.publisher_package_upload.outputs.artifact-digest'),
        ('candidate_sha256', 'steps.publisher_package.outputs.candidate_sha256'),
        ('receipt_sha256', 'steps.publisher_package.outputs.receipt_sha256'),
    ):
        assert f'      {output}: ${{{{ {source} }}}}' in build
    assert '!inputs.publish_draft_pr' in identity_step('snapshot_upload')


def test_isolated_publication_requires_trusted_manual_main_request():
    job = workflow_job("publish-theme-draft-pr")
    guard = job.split("    if: >-\n", 1)[1].split("    runs-on:", 1)[0]
    for required in (
        "github.event_name == 'workflow_dispatch'",
        f"github.repository == '{IDENTITY_REPOSITORY}'",
        "github.ref == 'refs/heads/main'",
        f"github.workflow_ref == '{IDENTITY_REPOSITORY}/.github/workflows/{IDENTITY_WORKFLOW}@refs/heads/main'",
        "inputs.publish_draft_pr && !inputs.identity_only",
    ):
        assert required in guard
    assert "||" not in guard
    assert "needs: build-theme-momentum" in job
    assert "permissions:\n      contents: read" in job
    assert "actions: read" not in job
    checkout = publication_step("publisher_checkout")
    assert "ref: ${{ github.sha }}" in checkout
    assert "persist-credentials: false" in checkout
    download = publication_step("publisher_download")
    assert "artifact-ids: ${{ needs.build-theme-momentum.outputs.artifact_id }}" in download
    assert "path: ${{ runner.temp }}/theme-publication-package" in download
    assert "digest-mismatch: error" in download
    for disallowed in ("github-token:", "repository:", "run-id:", "name:"):
        assert disallowed not in download.split("        with:\n", 1)[1]


def test_package_and_main_validation_precedes_isolated_single_repo_mint():
    steps = workflow_steps("publish-theme-draft-pr")
    validate = publication_step("publisher_validate")
    mint = publication_step("draft_publisher_token")
    publish = publication_step("draft_publish")
    assert steps.index(validate) < steps.index(mint) < steps.index(publish)
    assert "GITHUB_TOKEN: ${{ github.token }}" in validate
    assert "scripts/publish_theme_snapshot_pr.py validate" in validate
    assert IDENTITY_ACTION in mint
    assert 'app-id: "5214420"' in mint
    assert "          repositories: ResearchSignalContextPipelines\n" in mint
    assert "          owner: QuantStrategyLab\n" in mint
    assert "          permission-contents: write\n" in mint
    assert "          permission-pull-requests: write\n" in mint
    assert mint.count("          permission-") == 2
    assert "scripts/publish_theme_snapshot_pr.py publish" in publish
    assert "GITHUB_TOKEN:" not in publish
    assert "RESEARCH_PUBLISHER_TOKEN: ${{ steps.draft_publisher_token.outputs.token }}" in publish
    assert "RESEARCH_PUBLISHER_INSTALLATION_ID: ${{ steps.draft_publisher_token.outputs.installation-id }}" in publish
    assert "RESEARCH_PUBLISHER_APP_SLUG: ${{ steps.draft_publisher_token.outputs.app-slug }}" in publish
    for step in steps:
        assert "pip install" not in step
        assert "setup-python" not in step
        if step != publish:
            assert "steps.draft_publisher_token.outputs.token" not in step
        if step != mint:
            assert "secrets.RESEARCH_PUBLISHER_APP_PRIVATE_KEY" not in step
    for step in (validate, mint, publish):
        assert "always()" not in step
        assert "continue-on-error" not in step
    assert "always()" in publication_step("draft_publisher_summary")
    assert "if: ${{ always() }}" in steps[-1]
    assert "publication-result.json" in steps[-1]


@pytest.mark.parametrize(("requested", "generation", "admission", "package", "upload", "snapshot", "expected"), [
    ("", "success", "skipped", "skipped", "skipped", "success", "artifact_only"),
    ("false", "success", "skipped", "skipped", "skipped", "success", "artifact_only"),
    ("true", "skipped", "failure", "skipped", "skipped", "skipped", "admission_rejected"),
    ("false", "failure", "skipped", "skipped", "skipped", "skipped", "generation_failed"),
    ("true", "success", "success", "failure", "skipped", "skipped", "package_rejected"),
    ("true", "success", "success", "success", "failure", "skipped", "artifact_upload_failed"),
    ("false", "success", "skipped", "skipped", "skipped", "failure", "artifact_upload_failed"),
    ("true", "success", "success", "success", "success", "skipped", "package_ready"),
])
def test_generation_summary_distinguishes_artifact_mode_and_failures(
    monkeypatch, tmp_path, capsys, requested, generation, admission, package, upload, snapshot, expected,
):
    for name, value in {
        "PUBLICATION_REQUESTED": requested,
        "LEGACY_COMMIT_OUTPUTS": "true",
        "GENERATION_OUTCOME": generation,
        "ADMISSION_OUTCOME": admission,
        "PACKAGE_OUTCOME": package,
        "PACKAGE_UPLOAD_OUTCOME": upload,
        "SNAPSHOT_UPLOAD_OUTCOME": snapshot,
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }.items():
        monkeypatch.setenv(name, value)
    run_inline_python(identity_step("generation_summary"))
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "generated": generation == "success",
        "publication_requested": requested == "true",
        "legacy_commit_outputs_ignored": True,
        "status": expected,
        "published": False,
    }


@pytest.mark.parametrize(("outcome", "expected"), [
    ("CHECKOUT_OUTCOME", "checkout_failed"),
    ("DOWNLOAD_OUTCOME", "download_failed"),
    ("VALIDATE_OUTCOME", "validation_rejected"),
    ("MINT_OUTCOME", "mint_failed"),
])
def test_publisher_summary_keeps_failure_receipt_without_a_token(monkeypatch, tmp_path, capsys, outcome, expected):
    for key in ("CHECKOUT_OUTCOME", "DOWNLOAD_OUTCOME", "VALIDATE_OUTCOME", "MINT_OUTCOME"):
        monkeypatch.setenv(key, "failure" if key == outcome else "success")
    result_path = tmp_path / "result" / "publication-result.json"
    monkeypatch.setenv("RESULT_PATH", str(result_path))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    monkeypatch.delenv("RESEARCH_PUBLISHER_TOKEN", raising=False)
    run_inline_python(publication_step("draft_publisher_summary"))
    result = json.loads(capsys.readouterr().out)
    assert result == {"published": False, "review_required": True, "status": expected}
    assert json.loads(result_path.read_text()) == result


def test_publisher_summary_never_logs_untrusted_extra_fields(monkeypatch, tmp_path, capsys):
    result_path = tmp_path / "publication-result.json"
    result_path.write_text(json.dumps({
        "status": "draft_pr_created",
        "published": True,
        "review_required": False,
        "branch_created": True,
        "pr_created": True,
        "pr_url": f"https://github.com/{IDENTITY_REPOSITORY}/pull/12",
        "head_sha": "a" * 40,
        "candidate_sha256": "b" * 64,
        "reason_code": "secret-token-value",
        "raw_response": "secret-token-value",
    }))
    for key in ("CHECKOUT_OUTCOME", "DOWNLOAD_OUTCOME", "VALIDATE_OUTCOME", "MINT_OUTCOME", "PUBLISH_OUTCOME"):
        monkeypatch.setenv(key, "success")
    monkeypatch.setenv("RESULT_PATH", str(result_path))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    run_inline_python(publication_step("draft_publisher_summary"))
    logged = capsys.readouterr().out
    assert "secret-token-value" not in logged + result_path.read_text() + (tmp_path / "summary").read_text()
    result = json.loads(logged)
    assert result["status"] == "draft_pr_created"
    assert result["published"] is False
    assert result["review_required"] is True
    assert result["pr_created"] is True
    assert result["pr_url"] == f"https://github.com/{IDENTITY_REPOSITORY}/pull/12"
    assert result["head_sha"] == "a" * 40


@pytest.mark.parametrize(("as_of", "prices", "strict", "extra"), [
    ("", "", "false", []),
    ("2026-09-30", "prices path.csv", "true", ["--as-of", "2026-09-30", "--prices", "prices path.csv", "--strict-downloads"]),
])
def test_generation_preserves_arguments_and_records_real_interval(tmp_path, as_of, prices, strict, extra):
    # Replace only the Python executable with a local stand-in; never collect prices.
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    python = binary_dir / "python"
    python.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
        "print('{}')\n"
    )
    python.chmod(0o700)
    block = identity_step("generation")
    shell = textwrap.dedent(block.split("        run: |\n", 1)[1])
    # Keep all test output in the temporary directory, including the tee file.
    shell = shell.replace("/tmp/theme_momentum_summary.json", str(tmp_path / "generated-summary.json"))
    env = dict(os.environ)
    env.update({
        "PATH": f"{binary_dir}:{env['PATH']}",
        "CAPTURE": str(tmp_path / "arguments"),
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "INPUT_AS_OF": as_of,
        "PRICES_PATH": prices,
        "STRICT_DOWNLOADS": strict,
    })
    before = datetime.now().astimezone()
    run = subprocess.run(["bash", "-c", shell], cwd=tmp_path, env=env, capture_output=True, text=True)
    after = datetime.now().astimezone()
    assert run.returncode == 0, run.stderr
    assert json.loads((tmp_path / "arguments").read_text()) == [
        "scripts/build_theme_momentum_snapshot.py", "--output", "data/output/theme_momentum_snapshot.json", *extra,
    ]
    outputs = dict(line.split("=", 1) for line in (tmp_path / "outputs").read_text().splitlines())
    start = datetime.fromisoformat(outputs["started_at"])
    finish = datetime.fromisoformat(outputs["completed_at"])
    assert before <= start <= finish <= after
    assert start.tzinfo is not None and finish.tzinfo is not None


def test_publisher_summary_preserves_unknown_write_state_and_transport_binding(monkeypatch, tmp_path, capsys):
    result_path = tmp_path / "publication-result.json"
    result_path.write_text(json.dumps({
        "status": "failed", "reason_code": "API_HTTP_503",
        "published": False, "review_required": True,
        "branch_created": True, "pr_created": None,
        "branch": "automation/theme-12-1-" + "f" * 16,
        "package_key": "f" * 64,
    }))
    for key in ("CHECKOUT_OUTCOME", "DOWNLOAD_OUTCOME", "VALIDATE_OUTCOME", "MINT_OUTCOME"):
        monkeypatch.setenv(key, "success")
    bindings = {"artifact_id": "1234", "artifact_digest": "b" * 64, "candidate_sha256": "c" * 64, "receipt_sha256": "d" * 64}
    for key, value in bindings.items():
        monkeypatch.setenv(key.upper(), value)
    monkeypatch.setenv("PUBLISH_OUTCOME", "failure")
    monkeypatch.setenv("RESULT_PATH", str(result_path))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    run_inline_python(publication_step("draft_publisher_summary"))
    result = json.loads(capsys.readouterr().out)
    assert result["reason_code"] == "API_HTTP_503"
    assert result["branch_created"] is True
    assert result["pr_created"] is None
    assert result["published"] is False
    assert result["package_key"] == "f" * 64
    for key, value in bindings.items():
        assert result[key] == value
    assert json.loads(result_path.read_text()) == result


@pytest.mark.parametrize("identity_only", [False, True])
@pytest.mark.parametrize("publish_draft_pr", [False, True])
@pytest.mark.parametrize("generation_outcome", ["success", "failure", "skipped"])
@pytest.mark.parametrize("package_upload_outcome", ["success", "failure", "skipped"])
def test_snapshot_fallback_retains_only_a_successfully_generated_candidate(
    identity_only, publish_draft_pr, generation_outcome, package_upload_outcome,
):
    step = identity_step("snapshot_upload")
    guard = next(line.strip() for line in step.splitlines() if line.strip().startswith("if:"))
    expression = guard.removeprefix("if: ${{ ").removesuffix(" }}")
    assert "always()" in expression
    assert "continue-on-error" not in step
    # Evaluate the checked-in guard with only synthetic outcome/input values.
    expression = expression.replace("always()", "True")
    expression = expression.replace("inputs.identity_only", "identity_only")
    expression = expression.replace("inputs.publish_draft_pr", "publish_draft_pr")
    expression = expression.replace("steps.generation.outcome", "generation_outcome")
    expression = expression.replace("steps.publisher_package_upload.outcome", "package_upload_outcome")
    expression = expression.replace("!identity_only", "not identity_only")
    expression = expression.replace("!publish_draft_pr", "not publish_draft_pr")
    expression = expression.replace("&&", "and").replace("||", "or")
    actual = eval(expression, {"__builtins__": {}}, {
        "identity_only": identity_only,
        "publish_draft_pr": publish_draft_pr,
        "generation_outcome": generation_outcome,
        "package_upload_outcome": package_upload_outcome,
    })
    expected = (
        not identity_only and generation_outcome == "success"
        and (not publish_draft_pr or package_upload_outcome != "success")
    )
    assert actual is expected


@pytest.mark.parametrize(("package_outcome", "expected"), [
    ("failure", "package_rejected"),
    ("success", "artifact_upload_failed"),
])
def test_fallback_artifact_does_not_hide_publication_package_failure(monkeypatch, tmp_path, capsys, package_outcome, expected):
    for key, value in {
        "PUBLICATION_REQUESTED": "true",
        "GENERATION_OUTCOME": "success",
        "ADMISSION_OUTCOME": "success",
        "PACKAGE_OUTCOME": package_outcome,
        "PACKAGE_UPLOAD_OUTCOME": "failure",
        "SNAPSHOT_UPLOAD_OUTCOME": "success",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }.items():
        monkeypatch.setenv(key, value)
    run_inline_python(identity_step("generation_summary"))
    result = json.loads(capsys.readouterr().out)
    assert result["generated"] is True
    assert result["publication_requested"] is True
    assert result["published"] is False
    assert result["status"] == expected


@pytest.mark.parametrize("reason_code", [
    "API_WRITE_FORBIDDEN", "REF_WRITE_FORBIDDEN", "TREE_WRITE_FORBIDDEN",
    "PR_WRITE_FORBIDDEN", "RECEIPT_NONCANONICAL", "PUBLICATION_FAILED",
])
def test_summary_preserves_fixed_write_guard_reason_codes(monkeypatch, tmp_path, capsys, reason_code):
    result_path = tmp_path / "publication-result.json"
    result_path.write_text(json.dumps({"status": "failed", "reason_code": reason_code}))
    for key in ("CHECKOUT_OUTCOME", "DOWNLOAD_OUTCOME", "VALIDATE_OUTCOME", "MINT_OUTCOME"):
        monkeypatch.setenv(key, "success")
    monkeypatch.setenv("RESULT_PATH", str(result_path))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    run_inline_python(publication_step("draft_publisher_summary"))
    result = json.loads(capsys.readouterr().out)
    assert result["reason_code"] == reason_code
    assert result["published"] is False


@pytest.mark.parametrize("mutation", [None, "repository", "repository_id", "workflow_ref", "source_sha", "run_id", "run_attempt"])
def test_summary_producer_provenance_comes_only_from_fixed_runtime(monkeypatch, tmp_path, capsys, mutation):
    values = {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": IDENTITY_REPOSITORY,
        "GITHUB_REPOSITORY_ID": "1252248184",
        "GITHUB_WORKFLOW_REF": f"{IDENTITY_REPOSITORY}/.github/workflows/{IDENTITY_WORKFLOW}@refs/heads/main",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "1234",
        "GITHUB_RUN_ATTEMPT": "2",
    }
    expected = {
        "repository": values["GITHUB_REPOSITORY"],
        "repository_id": values["GITHUB_REPOSITORY_ID"],
        "workflow_ref": values["GITHUB_WORKFLOW_REF"],
        "source_sha": values["GITHUB_SHA"],
        "expected_base_sha": values["GITHUB_SHA"],
        "run_id": values["GITHUB_RUN_ID"],
        "run_attempt": values["GITHUB_RUN_ATTEMPT"],
    }
    if mutation is not None:
        env_name = "GITHUB_SHA" if mutation == "source_sha" else "GITHUB_" + mutation.upper()
        values[env_name] = "untrusted"
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    result_path = tmp_path / "publication-result.json"
    result_path.write_text(json.dumps({
        "status": "failed", "reason_code": "BASE_MOVED",
        **{key: "arbitrary-receipt-string" for key in expected},
    }))
    for key in ("CHECKOUT_OUTCOME", "DOWNLOAD_OUTCOME", "VALIDATE_OUTCOME", "MINT_OUTCOME"):
        monkeypatch.setenv(key, "success")
    monkeypatch.setenv("RESULT_PATH", str(result_path))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    run_inline_python(publication_step("draft_publisher_summary"))
    logged = capsys.readouterr().out
    assert "arbitrary-receipt-string" not in logged + result_path.read_text()
    result = json.loads(logged)
    if mutation in ("repository", "repository_id", "workflow_ref"):
        assert not set(expected) & set(result)
    else:
        for key, value in expected.items():
            if key == mutation or (mutation == "source_sha" and key == "expected_base_sha"):
                assert key not in result
            else:
                assert result[key] == value
    assert result["published"] is False
