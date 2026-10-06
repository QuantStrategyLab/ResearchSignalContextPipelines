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

## Useful docs

- [`docs/architecture.md`](docs/architecture.md)

## Community and security

- See [CONTRIBUTING.md](CONTRIBUTING.md) for pull request scope, local verification, and documentation expectations.
- Follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for maintainer and contributor conduct.
- Report credential, automation, broker, exchange, or cloud-resource vulnerabilities through [SECURITY.md](SECURITY.md); do not open public issues for secrets or live-execution risk.

## License

See [LICENSE](LICENSE).
