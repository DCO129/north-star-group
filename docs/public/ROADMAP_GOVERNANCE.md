# Roadmap & Governance / 路线图与治理

[中文](#中文--chinese) | [English](#english)

本文件是前瞻、非约束性的公开治理说明。所有者（零号）应在任何决策生效前予以批准。北极星集团
已作为公开 Alpha 发布（Apache-2.0）。

This is a forward-looking, non-binding public governance note. The owner (Zero) should
ratify any decision before it takes effect. North Star Group is already published as a
public Alpha (Apache-2.0).

---

## 中文 / Chinese

### 治理原则 / Governance principles

- **边界优先**：默认拒绝边界认证前不发布。/ No publish until the default-deny boundary is certified.
- **可复现**：每个公开产物由确定性工具产出。/ Every public artifact is produced by deterministic tooling.
- **失败即止**：验证错误阻断，从不警告后放行。/ Validation errors block, never warn-and-pass.
- **隔离**：私有知识/数据留在公开包之外。/ Private knowledge/data remain outside the public package.

### 社区提案仅为提案 / Community proposals are proposal-only

架构与路线图变更以**提案**形式进入（见 `architecture_proposal` issue 模板）。提案**不**自动
替换 canonical roadmap；仅在所有者/Codex 治理明确接受后才生效。仓库根的公开 `ROADMAP.md` 是被
接受的方向的活摘要；社区建议在被接受前只是提案。

### 治理一致规则 / Consistent governance rules

- 社区提案可以挑战架构与路线图顺序。
- 评价维度：证据、用户价值、架构一致性、安全、维护成本、可逆性、零号的优先级。
- 社区意见是**决策证据**，不是自动执行权。
- 任意时刻只保留**一个有效可执行路线图**。
- 提案被接受后，必须先明确更新 canonical roadmap，再进入执行。

### 决策权 / Decision rights

发布目标、许可证与发布节奏是**所有者（零号）**决策。本文档为信息性，不授权任何外部动作。

### 许可证 / License

所有者批准的许可证决定是 Apache-2.0（见 `LICENSE_DECISION.md`）。

---

## English

### Governance principles

- **Boundary first**: No publish until the default-deny boundary is certified.
- **Reproducible**: Every public artifact is produced by deterministic tooling.
- **Fail-closed**: Validation errors block, never warn-and-pass.
- **Separation**: Private knowledge/data remain outside the public package.

### Community proposals are proposal-only

Architecture and roadmap changes enter as **proposals** (see the `architecture_proposal`
issue template). A proposal does **not** automatically replace the canonical roadmap. It
becomes effective only after explicit owner/Codex governance acceptance. The public
`ROADMAP.md` at the repository root is the living summary of accepted direction; community
suggestions remain proposals until accepted.

### Consistent governance rules

- Community proposals may challenge architecture and roadmap ordering.
- Evaluation dimensions: evidence, user value, architectural consistency, security,
  maintenance cost, reversibility, and Zero's priorities.
- Community opinions are **decision evidence**, not automatic execution rights.
- At any time only **one valid executable roadmap** is kept.
- After a proposal is accepted, the canonical roadmap must be explicitly updated before
  execution begins.

### Decision rights

Publication target, license, and release cadence are **owner (Zero)** decisions. This
document is informational and does not authorize any external action.

### License

The owner-ratified license decision is Apache-2.0 (see `LICENSE_DECISION.md`).
