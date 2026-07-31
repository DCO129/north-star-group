# Architecture (Public Overview) / 架构（公开概览）

[中文](#中文--chinese) | [English](#english)

本文描述北极星集团运行时的**公开安全面**，刻意省略私有运营拓扑、凭据与内部服务地址。

This document describes the **public-safe** surface of the North Star Group runtime. It
intentionally omits private operational topology, credentials, and internal service
addresses.

---

## 中文 / Chinese

北极星集团是一个**本地优先、模型无关、面向长期运行的 AI 组织框架**。公开运行时当前包含
编排 API、小说生产管线、导出/验证工具与策略信任锚。

### 当前公开组件 / Current public components

| 领域 / Area | 公开模块 / Public module(s) | 用途 / Purpose |
| --- | --- | --- |
| 编排 API / Orchestration API | `src/private_ai_company/ceo_api.py` | 公开安全的请求/响应面 / Public-safe request/response surface |
| 小说生产 / Novel production | `src/private_ai_company/novel_mvp.py` | 内容生成管线（公开逻辑）/ Content pipeline (public logic) |
| 运营 / Operations | `src/private_ai_company/novel_operations.py` | 运营辅助（公开逻辑）/ Operational helpers (public logic) |
| 导出工具 / Export tooling | `scripts/export_public_alpha.py` | 确定性默认拒绝导出器 / Deterministic default-deny exporter |
| 验证 / Validation | `scripts/validate_public_alpha.py` | 失败即止公开边界验证器 / Fail-closed public-boundary validator |
| 策略 / Policy | `config/public-export-policy.json` | 机器可读导出边界（信任锚）/ Machine-readable export boundary (trust anchor) |

### 导出管线 / Export pipeline

```
private source ──▶ 白名单解析 ──▶ 内容/路径扫描（拒绝规则）
                                      │
                                            排除 ──▶ EXCLUSION_RECEIPT.json
                                            通过 ──▶ 擦洗机器路径
                                                           │
                                                 暂存树（归一化 mtime）
                                                           │
                                                 EXPORT_MANIFEST.json（tree_hash）
                                                           │
                                            validate_public_alpha.py（失败即止）
```

### 确定性保证 / Determinism guarantees

- 每次运行都从零重建暂存树（无增量状态）。
- 所有暂存文件 mtime 归一化为固定 epoch。
- `EXPORT_MANIFEST.json` 是树的稳定、键排序哈希。
- 对未改动的源连续两次导出产生逐字节一致的输出。

### 已实现 vs 设计方向

- **已实现**：本地优先 CEO 编排 API（仅 `127.0.0.1`）、部门路由、provider 抽象（本地模板执行器已验证）、权限/预算/工具边界、执行追踪、任务图、小说生产管线、restart-safe 状态脊柱、默认拒绝导出与失败即止验证。
- **设计方向**：更多 provider/平台接入、公开知识 connector、更完善的控制中心/UI、跨平台与高并发验证——这些尚在路线图中，未全部落地。

### 范围之外（私有，永不导出）/ Out of scope (private, never exported)

- 私有知识库内容（`knowledge/` 在此为占位空目录）。
- 运营状态、日志、截图与运行时产物。
- 任何密钥材料或个人/机器文件系统路径。

---

## English

North Star Group is a **local-first, model-agnostic framework for persistent AI
organizations**. The public runtime currently includes the orchestration API, the novel
production pipeline, export/validation tooling, and the policy trust anchor.

### Current public components

| Area | Public module(s) | Purpose |
| --- | --- | --- |
| Orchestration API | `src/private_ai_company/ceo_api.py` | Public-safe request/response surface |
| Novel production | `src/private_ai_company/novel_mvp.py` | Content generation pipeline (public logic) |
| Operations | `src/private_ai_company/novel_operations.py` | Operational helpers (public logic) |
| Export tooling | `scripts/export_public_alpha.py` | Deterministic default-deny exporter |
| Validation | `scripts/validate_public_alpha.py` | Fail-closed public-boundary validator |
| Policy | `config/public-export-policy.json` | Machine-readable export boundary (trust anchor) |

### Export pipeline

```
private source ──▶ allow-list resolution ──▶ content/path scan (deny rules)
                                                      │
                                            excluded ──▶ EXCLUSION_RECEIPT.json
                                            passed  ──▶ scrub machine paths
                                                           │
                                                 staged tree (normalized mtime)
                                                           │
                                                 EXPORT_MANIFEST.json (tree_hash)
                                                           │
                                            validate_public_alpha.py (fail-closed)
```

### Determinism guarantees

- Staging is rebuilt from scratch on every run (no incremental state).
- All staged file mtimes are normalized to a fixed epoch.
- `EXPORT_MANIFEST.json` is a stable, key-sorted hash of the tree.
- Re-running the exporter on an unchanged source yields byte-identical output.

### Implemented vs design direction

- **Implemented**: local-first CEO orchestration API (bound to `127.0.0.1` only),
  department routing, provider abstraction (local template executors verified),
  permission/budget/tool boundaries, execution tracing, task graphs, the novel
  production pipeline, a restart-safe state spine, default-deny export, and fail-closed
  validation.
- **Design direction**: more provider/platform integrations, a public knowledge
  connector, a more complete control center/UI, and cross-platform / high-concurrency
  verification — these remain on the roadmap and are not all landed.

### Out of scope (private, never exported)

- Private knowledge base contents (`knowledge/` is an empty placeholder here).
- Operational state, logs, screen captures, and runtime artifacts.
- Any secret material or personal/machine filesystem paths.
