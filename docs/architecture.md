# Architecture

## Current Architecture Understanding

QuantStrategyLab already separates strategy math, snapshot generation, runtime
execution, and broker adapters. This repository adds a research-only signal context
pipeline without changing that production boundary.

This repository is deliberately narrower than `AIAuditBridge`. It owns
research inputs, validation, saved artifacts, and replay harnesses. It does not
own model provider routing, API keys, cross-repository GitHub App write
orchestration, live notifications, or execution behavior.

A limited same-repository exception is the source-only, production-disabled
publisher for `data/output/theme_momentum_snapshot.json`. It can only transport
that exact file through a run-bound draft PR using the dedicated single-repository
App; it never writes `main`, merges, bypasses protection, or handles AI signal
pairs or other files. Content/source rights require explicit approval and a
reviewed source-policy change before a manual-main publication request can pass.
The schedule remains artifact-only. See the [English workflow boundary](../README.md#theme-snapshot-workflow-and-draft-pr-boundary)
and [中文说明](../README.zh-CN.md#主题快照-workflow-与草稿-pr-边界).

## Main Design Pressure

LLM output is not naturally deterministic or backtestable. The repository must
therefore preserve every generated artifact and keep AI output away from live
order routing.

## Recommended Low-Risk Shape

- `ResearchSignalContextPipelines` stores context examples, schema, validation,
  replay tooling, and shadow artifacts.
- `AIAuditBridge` owns provider routing and API keys.
- GitHub Issues are the first operator notification layer for monthly shadow
  signal runs.
- The scheduled workflow builds the market context bundle before dispatching the
  bridge and embeds that bundle into the issue, because the bridge reads the
  source repository ref plus issue content.
- `QuantStrategyPlugins` may later read promoted artifacts as sidecar context.
- Platform repositories remain unchanged.

## Lifecycle

The current lifecycle is accumulation-first:

1. Build a point-in-time context bundle.
2. Ask `AIAuditBridge` to review it and produce a shadow-only artifact when
   evidence is sufficient.
3. Save both `latest_signal.json` and dated `signal_history/YYYY-MM-DD.json`.
4. Replay only saved artifacts against later prices.
5. Consider a deterministic plugin only after enough walk-forward evidence
   exists.

## Not Recommended

- Giving AI broker credentials.
- Parsing free text into orders.
- Letting AI change strategy thresholds, max leverage, universe membership, or
  execution mode.
- Re-generating old AI judgments during replay instead of replaying stored
  artifacts.
- Sending runtime Telegram or broker-facing notifications directly from this
  research repository before a deterministic plugin contract exists.
- Duplicating `AIAuditBridge` provider fallback or cross-repository write
  logic inside this repository.

## Validation Strategy

The current minimum checks are schema validation and deterministic overlay
replay. Replay consumes stored signal artifacts from `signal_history` and maps
them through a fixed risk-reducing policy. It must never ask a model to recreate
old judgments.

The first overlay harness intentionally measures only:

- final equity
- total return
- maximum drawdown
- average exposure
- exposure turnover

This is enough to identify whether the stored AI context would have reduced
risk or created unacceptable opportunity cost before any runtime integration.

The replay harness can read either compact `date,symbol,close` CSV files or the
existing QuantStrategyLab `symbol,as_of,close` price-history files. Large source
files should stay in their owning strategy repositories or object storage; this
repository only stores small extracted replay inputs when needed for research.

## Strict Live Signal-Pair Structure Gate

Legacy examples and replay retain their existing schema reader. A separate
strict publication check applies to the canonical pair
`data/output/latest_signal.json` and `data/output/latest_signal.manifest.json`.
It requires an integer-v2 manifest while retaining the consumer's supported
signal schemas 1 and 2; it never upgrades or rewrites retained data.

The strict check reads each input once, binds the exact signal bytes to the
manifest hash, rejects undeclared fields, and checks matching dates, mode,
timezone-aware generation, bounded finite confidence, and no-execution policy.
Its success label is `PAIR_STRUCTURE_VALID`. Actual context bytes, model use,
and producer-run provenance remain `UNVERIFIED`: a formatted input digest or a
repository commit ancestor does not prove a real model run used that context.
Authentic context/run linkage and source rights remain separate publication
review conditions; no downloader or synthetic provenance default is introduced.

The existing required `test` job retains `contents: read`. For PRs it checks
the merge-base-to-head change; for main pushes it checks before-to-head. If
either canonical live path changes, both must change and exist as regular head
tree blobs. Deletion, rename away from a canonical path, single-file updates,
invalid comparison commits, or workspace substitution fail closed. Validation
uses committed head bytes and verifies producer ancestry as repository lineage
only. Unrelated changes still run ordinary tests without relabeling the retained
legacy pair trusted or blocking all work because that old pair is stale.

Commands are `validate_latest_signal.py --require-manifest` for an
explicit file-pair check, and `--changed-pair --event pull_request|push
--base-sha <sha> --head-sha <sha>` for the local Git-only CI gate. Schema-only
examples and the existing `--allow-missing` diagnostic remain available, but
cannot bypass the strict gate. No clock-relative freshness is asserted here;
the downstream report evaluates its own fixed cutoff.

Recurring publisher identity and permission scope are separate decisions from
this structural validation. Public derived-snapshot publication must follow its
explicitly approved scope and applicable source-data rights; a one-time import
approval does not establish a recurring publication service or a general exception
to the generated-artifact contribution policy.
Repository MIT is not proof of market-data redistribution rights. This patch
does not change permissions or CONTRIBUTING and does not import any real data.

中文摘要：保留旧样例与回放读取；仅当正式 signal/manifest 路径发生变更时，要求成对提交并
验证原始字节、字段、时间和政策。成功只表示 PAIR_STRUCTURE_VALID，不代表实际 context、
模型或生产运行已核实。无关 PR 不被旧数据阻塞；单侧修改、删除、改名、非法 SHA 或工作树
替换均拒绝。持续发布身份和派生数据公开权利另行决策，本补丁不导入数据或扩大权限。

## Risk Notes

The artifact is research evidence, not a trading instruction. Missing evidence,
expired artifacts, low confidence, or schema failures should default to no-op in
any downstream consumer. Current promoted signal artifacts must use the
`1-3 years` horizon and provide enough theme or symbol coverage for Advisor to
distinguish a genuine missing long-horizon signal from an ingestion gap.

## Cross-Sector Theme Taxonomy

The long-horizon context must not be limited to the current hot AI trade.  The
stable research universe now uses a static, versioned theme taxonomy stored in:

```text
config/theme_taxonomy.csv
config/symbol_theme_exposure.csv
```

The taxonomy intentionally covers multiple durable sectors:

- AI compute, HBM/memory, foundry and AI server infrastructure
- data-center power, utilities, grid transition and nuclear optionality
- cybersecurity
- defense and aerospace
- energy security and hydrocarbons
- financial and market infrastructure
- healthcare policy
- consumer platforms, industrial automation, EV/auto, and crypto infrastructure

Theme membership is static research context.  A symbol is not added to a theme
just because it is hot this month.  Monthly AI output may express `theme_bias`
and optional `symbol_bias`; both can use structured values with bias,
confidence, linked themes, rationale, and risk flags. Downstream consumers must
keep that output shadow-only and replay saved artifacts point-in-time.

This is the anti-overfit boundary:

1. Define universe and theme exposure before looking at future returns.
2. Save every AI theme judgment as an artifact.
3. Replay only saved artifacts; never regenerate old model judgments.
4. Treat theme and symbol bias as context, not as execution or allocation.

## Horizon Boundary

This repository should not directly produce short-term recommendations. Short-term (`1-10 trading days`) catalyst handling belongs to `PoliticalEventTrackingResearch` plus deterministic Advisor rules. `theme_momentum_snapshot.json` is explicitly a medium-horizon (`2-12 weeks`) theme context artifact, while `latest_signal.json` and `signal_history/*.json` remain long-horizon (`1-3 years`) AI shadow context. `QuantAdvisorResearch` is the final composition layer for short/medium/long recommendation buckets.

## Theme Momentum Snapshot

A cross-sector theme ranking is produced separately from the static taxonomy.
The snapshot uses fixed windows rather than tuning to recent winners:

- 12-1 month momentum: 252 trading-day lookback, skipping the latest 21 trading days
- 6-1 month momentum: 126 trading-day lookback, skipping the latest 21 trading days
- 3 month momentum: 63 trading-day recent trend
- breadth: share of priced theme members with positive 3 month returns
- risk penalty: 63 day realized volatility and 126 day drawdown

Output path convention:

```text
data/output/theme_momentum_snapshot.json
```

The artifact is point-in-time medium-horizon research context.  It ranks themes and highlights
strong members inside a theme, but it does not encode short-term recommendations,
orders, target weights, or execution policy.  Future replay must consume saved
snapshots rather than recomputing old theme ranks with revised constituents or revised weights.

## Repository Name Decision

`ResearchSignalContextPipelines` is the canonical name. The short/medium/long
final recommendation buckets live in `QuantAdvisorResearch`; this repository
provides reusable research context artifacts, including medium-horizon theme
momentum and long-horizon AI shadow context.
