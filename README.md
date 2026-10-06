# ResearchSignalContextPipelines

[Chinese README](README.zh-CN.md)

> Investing involves risk. This project does not provide investment advice and is for education, research, and engineering review only.

## What this repository is

ResearchSignalContextPipelines is a QuantStrategyLab research signal context pipeline. It builds medium-horizon theme context and long-horizon AI shadow context artifacts.

It produces research, audit, or orchestration artifacts. It should not submit broker orders or mutate live allocations by itself.

## QSL architecture role

- **Layer**: `research`.
- **Responsibility**: medium/long-horizon research signal context pipeline.
- **Owns**: theme context artifacts and AI shadow context outputs.
- **Consumes**: public/research inputs and downstream advisory/pipeline consumers.
- **Must not**: submit orders or mutate live runtime allocations.

## Output boundary

- Treat generated reports as evidence or review material, not automatic trading instructions.
- Keep source traceability and artifact timestamps visible.
- Require human review before using outputs in downstream strategy or platform changes.
- Keep credentials, private data, and external service tokens out of Git and logs.

## Repository layout

- `src/`: library and runtime code.
- `tests/`: unit, contract, and regression tests.
- `docs/`: runbooks, design notes, evidence, and integration contracts.
- `.github/workflows/`: CI, scheduled jobs, release, or deployment workflows.
- `scripts/`: operator scripts and local helpers.
- `config/`: runtime or pipeline configuration.

## Quick start

```bash
python -m pip install -e .
python -m pytest -q
```

## Theme snapshot workflow and draft PR boundary

`.github/workflows/theme_momentum_snapshot.yml` normally builds
`data/output/theme_momentum_snapshot.json` and retains it as an Actions artifact.
The weekly schedule is artifact-only. The legacy `commit_outputs` input is
**deprecated and ignored**; it never pushes to `main` or authorizes a PR.

The source includes a restricted, **production-disabled** draft publisher:

- `identity_only=true` is the existing manual-main identity diagnostic. It does
  not collect, generate, or publish data. Combining it with `publish_draft_pr=true`
  fails before any token mint or generation.
- `publish_draft_pr` is a manual boolean, default `false`. Even `true` is not
  content authorization: `PUBLICATION_CONTENT_APPROVED = False` in
  `scripts/publish_theme_snapshot_pr.py` blocks publication requests immediately
  after checkout, before installation, collection, generation, or production mint.
- Enabling a future run requires explicit approval of the source/provider and
  derived-data publication scope, a reviewed source-policy change, and the
  explicit manual-main flag. Publicly readable input, repository MIT licensing,
  and an App installation do not establish redistribution rights. No scheduled
  publication is authorized by this implementation.

Normal generation preserves `as_of`, optional `prices_path`, and
`strict_downloads`. Publication policy separately constrains eligible inputs;
operator-supplied CSVs do not receive implied publication approval. The only
future data-PR path is `data/output/theme_momentum_snapshot.json`; raw prices,
AI signal/manifest pairs, history, configuration, and all other files are excluded.

The build job records the actual generation interval, prepares an exact JSON plus
`publication-package.json` receipt, and passes artifact ID/digest and both raw
SHA-256 hashes through same-run job outputs. The isolated publisher checks out
the fixed run SHA with credentials persistence disabled, downloads only that
current-run artifact ID into runner temporary storage, and validates the package,
source/config/run provenance, content policy, and unchanged expected `main` before
minting a production token. It does not install or execute artifact content.

`GITHUB_TOKEN` has only `contents: read`; there is no Actions read permission or
cross-run retrieval. The dedicated App token requests only Contents and Pull
requests write for this repository and is revoked by the official action's
normal post step. The diagnostic token remains isolated to the identity-only
branch; the production token is passed only to the restricted publish helper.
All workflow action dependencies are pinned to full commit SHAs.

Future writes use a run-bound immutable branch, a single-parent/single-file
commit, and a draft PR. Exact existing drafts may be reused; moved `main`, changed
branches, and closed/merged/non-draft PRs fail closed. There is no force-push,
`main` write, automatic merge, protection bypass, old App fallback, or silent
success after remote failure. Sanitized receipts distinguish artifact generation,
package readiness, draft creation, and failure. A created draft still reports
`published=false`: human review and merge are still required.

Local source/mock validation (no provider calls, App mint, dispatch, or data PR):

```bash
python -m pip install -e '.[test]'
python -m pytest -q tests/test_theme_momentum_snapshot_workflow.py tests/test_publish_theme_snapshot_pr.py
python -m pytest -q
git diff --check
```

## Useful docs

- [`docs/architecture.md`](docs/architecture.md)

## Community and security

- See [CONTRIBUTING.md](CONTRIBUTING.md) for pull request scope, local verification, and documentation expectations.
- Follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for maintainer and contributor conduct.
- Report credential, automation, broker, exchange, or cloud-resource vulnerabilities through [SECURITY.md](SECURITY.md); do not open public issues for secrets or live-execution risk.

## License

See [LICENSE](LICENSE).
