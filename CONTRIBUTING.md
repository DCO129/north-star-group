# Contributing / 贡献指南

[中文](#中文-1) | [English](#english-1)

本仓库是北极星集团的公开早期 Alpha（Apache-2.0，已发布）。以下规则是中英双语的权威贡献指南。

This repository is the public early Alpha of North Star Group (Apache-2.0, already
published). The rules below are the authoritative, bilingual contribution guide.

---

## 中文 / Chinese

### 可接受的贡献类型

我们欢迎各类贡献，并且明确：

- **缺陷报告、反例、架构异议、安全问题，本身就是有效贡献**——不一定需要先写代码。
- 文档、示例、测试、去重重构、provider/平台 Adapter、公开 UI、跨平台验证、部门模板与 skill 包。
- 社区贡献者**不是无偿外包人员**；维护者仍负责审查、整合、反馈与署名。

### 公开 / 私有边界

只提交公开安全的代码与文档。请勿提交：

- 密钥、凭据或 `.env` 文件（`.env.example` 仅含变量名）。
- 私有知识库内容或运营/试点数据。
- 绝对个人路径或机器路径（不可避免时用 `<PROJECT_ROOT>` 占位）。
- 生成的私有证据、暂存回执或机器特定日志。

### 开发流程

1. 在私有源仓库上做修改。
2. 任何新文件必须先进入导出白名单（`config/public-export-policy.json`）才能进入公开导出。
3. 本地运行轻量、可移植的验证：
   ```bash
   python scripts/export_public_alpha.py --source-root . --staging-root ./_staging
   python scripts/validate_public_alpha.py --staging-root ./_staging
   python scripts/validate_docs.py --staging-root ./_staging
   python tests/smoke_public_alpha.py
   python tests/test_public_alpha_ci_contract.py
   ```
4. 失败即止验证器必须报告 `PUBLIC_ALPHA_VALIDATE_OK` 且全 `ZERO_*` 为零；smoke 与 CI-contract 测试必须通过。

### Pull Request 要求

每个 PR 必须声明：

- **范围**：改了什么、为什么。
- **测试**：在 `tests/` 下新增或更新了什么。
- **公开/私有边界影响**：是否新增或移除了白名单面，默认拒绝边界是否仍然成立。
- **外部效应 / 模型调用**：明确说明是否进行任何真实模型调用、网络调用、支付或其他外部效应。公开 quickstart 模式必须保持无模型、无效应。

### 治理：提案，而非自动采纳

- 架构与路线图变更以**提案**形式进入（见 `.github/ISSUE_TEMPLATE/architecture_proposal.yml`）。
- 提案不自动替换 canonical roadmap；仅在所有者/Codex 治理明确接受后生效。
- 生成的暂存回执（`EXPORT_MANIFEST.json`、`FILE_INVENTORY.json`、`*_RECEIPT.json`）是**证据**，不是手改源；用导出器重新生成。

### 代码风格

- 尽量保持模块可导入、副作用可控。
- 任何导出/验证行为变更都要新增或更新测试。
- 保持公开包依赖精简；小说生产工作流为可选扩展，CLI/API 快速开始不需要它。

---

## English

### Acceptable contribution types

We welcome all kinds of contributions, and we state clearly:

- **A defect report, counterexample, architecture objection, or security issue is itself a valid contribution** — you do not have to write code first.
- Documentation, examples, tests, de-duplication refactors, provider/platform adapters, public UI, cross-platform verification, and department templates/skill packages.
- Community contributors are **not unpaid outsourcers**; maintainers remain responsible for review, integration, feedback, and attribution.

### Public / private boundary

Submit only public-safe code and documentation. Do **not** submit:

- Secrets, credentials, or `.env` files (`.env.example` carries names only).
- Private knowledge-store contents or operational/pilot data.
- Absolute personal or machine filesystem paths (use `<PROJECT_ROOT>` if a placeholder is unavoidable).
- Generated private evidence, staging receipts, or machine-specific logs.

### Development flow

1. Make changes against the private source repository.
2. Every new file must be on the export allow-list
   (`config/public-export-policy.json`) before it can appear in a public export.
3. Run the lightweight, portable validation locally:
   ```bash
   python scripts/export_public_alpha.py --source-root . --staging-root ./_staging
   python scripts/validate_public_alpha.py --staging-root ./_staging
   python scripts/validate_docs.py --staging-root ./_staging
   python tests/smoke_public_alpha.py
   python tests/test_public_alpha_ci_contract.py
   ```
4. The fail-closed validator must report `PUBLIC_ALPHA_VALIDATE_OK` with zero
   `ZERO_*` hits, and the smoke/CI-contract tests must pass.

### Pull-request requirements

Every pull request must declare:

- **Scope** — what changes and why.
- **Tests** — what was added or updated under `tests/`.
- **Public/private-boundary impact** — whether the change adds or removes any
  allow-listed surface, and whether the default-deny boundary still holds.
- **External effects / model calls** — explicitly state whether the change performs
  any real model call, network call, payment, or other external effect. The public
  quickstart mode must remain model-free and effect-free.

### Governance: proposals, not automatic adoption

- Architecture and roadmap changes enter as **proposals** (see
  `.github/ISSUE_TEMPLATE/architecture_proposal.yml`).
- No proposal automatically replaces the canonical roadmap. It becomes effective only
  after explicit owner/Codex governance acceptance.
- Generated staging receipts (`EXPORT_MANIFEST.json`, `FILE_INVENTORY.json`,
  `*_RECEIPT.json`) are **evidence**, not hand-edited source. Regenerate them with the
  exporter.

### Code style

- Keep modules importable and side-effect-free where possible.
- Add or update tests for any export/validate behavior change.
- Keep the public package dependency-light; the novel production workflow is an optional
  extra and is not required for the CLI/API quickstart.
