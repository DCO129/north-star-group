# Roadmap (Public Alpha) / 公开路线图（公开 Alpha）

> 北极星集团是一个本地优先、模型无关、面向长期运行的 AI 组织框架。
> North Star Group is a local-first, model-agnostic framework for persistent AI organizations.

本路线图是**公开、非约束性**的。它不逐字公开内部 canonical 计划，也不包含任何承诺日期。
内部 ID（如 G1-6-R1）仅作为可选证据引用，不是外部读者理解路线图的前置条件。

This roadmap is **public and non-binding**. It does not publish the internal canonical
plan verbatim and contains no committed dates. Internal IDs (e.g. G1-6-R1) appear only as
optional evidence references, not as a prerequisite for external readers.

状态词 / Status vocabulary: `Completed Alpha` · `First vertical validated` ·
`Novel Alpha closure completed` · `Completed` · `Current` · `Planned` · `Exploratory`.

---

## Phase 1 — Organizational Runtime Foundations / 组织运行时基础

**状态 / Status: `Completed Alpha`**

- 组织、子公司、部门定义 / Organization, subsidiary, and department definitions
- CEO 语义规划 / CEO semantic planning
- 已验证任务图 / Validated task graphs
- 部门路由 / Department routing
- provider 抽象 / Provider abstraction
- 权限、预算和工具边界 / Permissions, budget, and tool boundaries
- 执行追踪 / Execution tracing

## Phase 2 — Durable State and Knowledge Continuity / 持久状态与知识连续性

**状态 / Status: `First vertical validated`**

- checkpoint / resume / 检查点 / 恢复
- export / restore / 导出 / 恢复
- artifacts / evidence / 制品 / 证据
- restart-safe state / 重启安全状态
- KnowledgeAdapter 与版本化知识注入 / KnowledgeAdapter and versioned knowledge injection

## Phase 3 — First Operational Subsidiary / 首个可运行子公司

**状态 / Status: `Novel Alpha closure completed`**

- 规划、知识上下文、生成、内审 / Planning, knowledge context, generation, internal review
- 不合格草稿阻断 / Failed-draft blocking
- 五章滚动缓冲 / Five-chapter rolling buffer
- provenance / 溯源
- production batches / 生产批次
- 重启恢复 / Restart recovery
- owner controls / 所有者控制
- 有边界的真模型试点 / Bounded real-model pilot

注意：这**不**表示所有小说生产场景已完成，也**不**表示可无人监管运行。
Note: this does **not** mean every novel-production scenario is finished, nor that it can
run unsupervised.

## Phase 4 — Reproducible Public Alpha / 可复现公开 Alpha

**状态 / Status: `Completed`**

- default-deny export / 默认拒绝导出
- 隐私扫描 / Privacy scan
- 确定性 manifests / Deterministic manifests
- clean-room install / Clean-room 安装
- CLI / API
- Apache-2.0
- GitHub governance surface / GitHub 治理面
- 公开发布与 CI / Public release and CI
- G1-6-R1 source / staging / remote reconciliation / G1-6-R1 源/暂存/远端对齐

## Phase 5 — Simplification, Documentation, and External Review / 精简、文档与外部审查

**状态 / Status: `Current`**

- 双语公开叙事 / Bilingual public narrative
- 诚实限制说明 / Honest-limitations notes
- 公开路线图 / Public roadmap
- 删除发布前陈旧文本 / Removal of pre-publication stale text
- Ponytail 安全精简 / Ponytail safe reduction
- 外部架构/安全审查 / External architecture/security review
- 跨平台验证 / Cross-platform verification
- contributor onboarding / 贡献者上手

## Phase 6 — General AI Organizations and Multi-Department Operations / 通用 AI 组织与多部门运行

**状态 / Status: `Planned`**

- 通用部门合同 / Generic department contracts
- 更多部门类型 / More department types
- 公开知识 connector / Public knowledge connector
- 更多 provider / More providers
- 更完善的控制中心/UI / More complete control center / UI
- 部门间协作 / Inter-department collaboration
- 更长周期任务 / Longer-horizon tasks
- 审批与恢复策略 / Approval and recovery policies
- 社区部门包与 skill 包 / Community department and skill packages

---

## Community proposal governance / 社区提案治理

以下规则在 `ROADMAP.md`、`CONTRIBUTING.md` 与
`docs/public/ROADMAP_GOVERNANCE.md` 中保持一致。

These rules are kept consistent across `ROADMAP.md`, `CONTRIBUTING.md`, and
`docs/public/ROADMAP_GOVERNANCE.md`.

- 社区提案可以挑战架构和路线图顺序。/ Community proposals may challenge architecture and roadmap ordering.
- 评价维度：证据、用户价值、架构一致性、安全、维护成本、可逆性、零号的优先级。/
  Evaluation dimensions: evidence, user value, architectural consistency, security,
  maintenance cost, reversibility, and Zero's priorities.
- 社区意见是**决策证据**，不是自动执行权。/ Community opinions are **decision evidence**, not automatic execution rights.
- 任意时刻只保留**一个有效可执行路线图**。/ At any time only **one valid executable roadmap** is kept.
- 提案被接受后，必须先明确更新 canonical roadmap，再进入执行。/ After a proposal is accepted, the canonical roadmap must be explicitly updated before execution begins.
- “canonical roadmap” 不是拒绝异议的挡箭牌；社区路线图也不与现有主线并行执行。/
  The "canonical roadmap" is not a shield to reject dissent, and the community roadmap does
  not run in parallel with the existing mainline.

## Governance principles / 治理原则

- **Boundary first**: 默认拒绝边界认证前不发布。/ No publish until the default-deny boundary is certified.
- **Reproducible**: 每个公开产物由确定性工具产出。/ Every public artifact is produced by deterministic tooling.
- **Fail-closed**: 验证错误阻断，从不警告后放行。/ Validation errors block, never warn-and-pass.
- **Separation**: 私有知识/数据留在公开包之外。/ Private knowledge/data remain outside the public package.

## Decision rights / 决策权

发布目标、许可证与发布节奏是**所有者（零号）**决策。本文档为信息性，不授权任何外部动作。

Publication target, license, and release cadence are **owner (Zero)** decisions. This
document is informational and does not authorize any external action.
