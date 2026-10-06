# ResearchSignalContextPipelines

[English README](README.md)

> 投资有风险。本项目不构成投资建议，仅用于学习、研究和工程审阅。

## 这个仓库是什么

ResearchSignalContextPipelines 是 QuantStrategyLab 的研究信号上下文流水线。生成中期主题上下文和长期 AI shadow context 产物。

它产出研究、审计或编排类 artifact，不应自行提交券商订单，也不应直接修改 live allocation。

## QSL 架构角色

- **层级**：`研究/证据`。
- **职责**：中长期 research signal context 流水线。
- **事实源/归属**：theme context artifacts 和 AI shadow context outputs。
- **消费对象**：公开/研究输入和下游 advisory/pipeline consumers。
- **禁止事项**：下单或修改 live runtime allocations。

## 输出边界

- 生成报告应作为证据或审阅材料，不是自动交易指令。
- 保留来源可追溯性和 artifact 时间戳。
- 输出用于下游策略或平台改动前，需要人工 review。
- 凭据、私人数据和外部服务 token 不能提交到 Git，也不能写入日志。

## 仓库结构

- `src/`：库代码和运行时代码。
- `tests/`：单元测试、契约测试和回归测试。
- `docs/`：运行手册、设计说明、证据和集成契约。
- `.github/workflows/`：CI、定时任务、发布或部署 workflow。
- `scripts/`：运维脚本和本地辅助工具。
- `config/`：运行或流水线配置。

## 快速开始

```bash
python -m pip install -e .
python -m pytest -q
```

## 主题快照 workflow 与草稿 PR 边界

`.github/workflows/theme_momentum_snapshot.yml` 默认生成
`data/output/theme_momentum_snapshot.json` 并保留为 Actions artifact。
每周定时任务仅生成 artifact。旧 `commit_outputs` 输入已**弃用且忽略**，
不会向 `main` 推送，也不代表 PR 发布授权。

源码包含受限且**尚未启用生产发布**的草稿 publisher：

- `identity_only=true` 保留现有的手动 main 身份诊断，不采集、生成或发布数据。
  若同时设置 `publish_draft_pr=true`，在任何 token mint 或生成前拒绝。
- `publish_draft_pr` 是默认 `false` 的手动布尔输入。设为 `true` 仍不构成内容授权：
  `scripts/publish_theme_snapshot_pr.py` 中的 `PUBLICATION_CONTENT_APPROVED = False`
  会在 checkout 后立即阻止发布请求，早于安装、采集、生成和生产 token mint。
- 未来真实运行需要明确批准来源/provider 及派生数据的公开范围，经源码 review
  修改内容政策，并显式开启手动 main 发布开关。公开可读取、仓库 MIT 许可证和 App
  安装均不证明再分发权利。本实现不授权定时发布。

普通生成保留 `as_of`、可选 `prices_path` 和 `strict_downloads` 的原有行为。
发布政策另行约束允许的输入；用户提供 CSV 不代表获得公开许可。未来数据 PR
唯一允许的路径为 `data/output/theme_momentum_snapshot.json`，不包括原始价格、
AI signal/manifest pair、history、配置或任何其他文件。

build job 记录真实生成起止时间，准备精确 JSON 与 `publication-package.json`
回执，通过同 run 的 job outputs 传递 artifact ID/digest 和两个原始文件 SHA-256。
隔离 publisher checkout 当前 run 的固定 SHA，关闭凭据持久化，仅按该 run 的
artifact ID 下载到 runner 临时目录；在生产 mint 前验证 package、源码/配置/run
绑定、内容政策及 `main` 是否仍是预期基线。不会安装或执行 artifact 中的代码。

`GITHUB_TOKEN` 仅有 `contents: read`，没有 Actions read 权限或跨 run 检索。
专用 App token 仅申请本仓 Contents 与 Pull requests write，并由官方 action
默认 post step 撤销。诊断 token 只用于 identity-only 分支，生产 token 只传给
受限 publish helper。所有 workflow action 依赖均固定为完整 commit SHA。

未来写入使用绑定 run 的不可变分支、单 parent/单文件 commit 和草稿 PR。
完全匹配的现有草稿可复用；`main` 前进、分支被修改、PR 已关闭/合并/转为非草稿时
拒绝继续。没有 force-push、直接写 `main`、自动 merge、绕过保护、旧 App 回退，
也不会把远端失败静默当作成功。脱敏回执分别记录 artifact 生成、package 就绪、
草稿创建或失败；`published=false` 表明仍需人工审阅和合并。

本地源码/mock 验证，不调用 provider、不 mint、不 dispatch、不创建数据 PR：

```bash
python -m pip install -e '.[test]'
python -m pytest -q tests/test_theme_momentum_snapshot_workflow.py tests/test_publish_theme_snapshot_pr.py
python -m pytest -q
git diff --check
```

## 延伸文档

- [`docs/architecture.md`](docs/architecture.md)

## 社区和安全

- 贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，确认 PR 范围、本地校验和文档要求。
- 讨论、issue 和 review 请遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
- 涉及密钥、自动化、券商/交易所或云资源的漏洞请按 [SECURITY.md](SECURITY.md) 私密报告；不要为 secret 或实盘风险开公开 issue。

## 许可证

详见 [LICENSE](LICENSE)。
