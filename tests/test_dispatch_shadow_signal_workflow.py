from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import sys
import textwrap

import pytest


def test_dispatch_shadow_signal_pins_bridge_ref_via_variable() -> None:
    workflow = Path(".github/workflows/dispatch_shadow_signal.yml").read_text(encoding="utf-8")

    assert "BRIDGE_REF: ${{ vars.CODEX_AUDIT_BRIDGE_REF || 'main' }}" in workflow
    assert "default: \"codex\"" in workflow
    assert "BRIDGE_PROVIDER: ${{ github.event.inputs.provider || 'codex' }}" in workflow
    assert '"ref": os.environ["BRIDGE_REF"]' in workflow
    assert "QuantStrategyLab/AIAuditBridge" in workflow


@pytest.mark.parametrize("manifest", [{"schema_version": "1"}, {"schema_version": 2}])
def test_untrusted_old_signal_does_not_block_new_context(tmp_path: Path, manifest: dict) -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/dispatch_shadow_signal.yml").read_text(encoding="utf-8")
    old_signal = tmp_path / "data/output/latest_signal.json"
    old_signal.parent.mkdir(parents=True)
    old_signal.write_bytes((root / "examples/latest_signal.example.json").read_bytes())
    old_manifest = old_signal.with_suffix(".manifest.json")
    old_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    retained = (old_signal.read_bytes(), old_manifest.read_bytes())

    def workflow_command(script: str) -> str:
        step = next(part for part in workflow.split("\n      - name:") if f"scripts/{script}" in part)
        run = step.split("        run: ", 1)[1].rstrip()
        command = textwrap.dedent(run[2:]) if run.startswith("|\n") else run
        return command.replace(f"python scripts/{script}", f"{shlex.quote(sys.executable)} {shlex.quote(str(root / 'scripts' / script))}")

    diagnose = workflow_command("validate_latest_signal.py")
    generate = workflow_command("build_context_bundle.py")
    # Exercise the real generation entrypoint with local synthetic prices only.
    generate += f" --prices {shlex.quote(str(root / 'examples/price_history.example.csv'))} --symbols QQQ --no-theme-context"
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", f"{diagnose}\n{generate}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "::warning::Existing signal artifact is untrusted or unavailable" in result.stdout
    assert "diagnostic only, not acceptance of a new artifact" in result.stdout
    assert result.stderr == ""
    context = json.loads((tmp_path / "data/output/context_bundle/latest_context_bundle.json").read_text(encoding="utf-8"))
    assert "QQQ" in context["price_context"]
    assert (old_signal.read_bytes(), old_manifest.read_bytes()) == retained
