# North Star Group · 北极星集团

> **北极星集团是一个本地优先、模型无关、面向长期运行的 AI 组织框架。**
> **North Star Group is a local-first, model-agnostic framework for persistent AI organizations.**

> **构建一个能够运行的 AI 组织，而不是再造一个聊天机器人。**
> **Build an AI organization, not another chatbot.**

[中文](#中文) | [English](#english)

---

This repository is the **public early-alpha** of North Star Group. It is already
published under Apache-2.0. The documentation below is bilingual so that external
contributors can understand and join the project. The install, CLI, and API command
blocks are shared and appear **once** in the [Quickstart](#quickstart--快速开始)
section; both language narratives reference them.

本仓库是北极星集团的**公开早期 Alpha**。已基于 Apache-2.0 公开发布。以下文档为中英双语，
方便外部贡献者理解与参与。安装、CLI 与 API 命令块为共享内容，**仅保留一份**，见
[快速开始](#quickstart--快速开始) 一节；两种语言叙事均引用之。

## Quickstart / 快速开始

The package installs with only its declared public dependencies — no private
repository on `PYTHONPATH`, no private virtual environment, no secret values.

本包仅依赖其声明的公开依赖安装——`PYTHONPATH` 上无私有仓库、无私有虚拟环境、无密钥值。

```powershell
python -m venv .venv
.\.venv\Scripts\pip install .
```

`pyproject.toml` declares `fastapi`, `uvicorn`, and `tzdata`. The novel production
workflow (which needs a third-party agent framework) is an optional extra and is
intentionally **not** required for the CLI/API quickstart.

`pyproject.toml` 声明了 `fastapi`、`uvicorn` 与 `tzdata`。小说生产工作流（需第三方
agent 框架）为可选扩展，CLI/API 快速开始**无需**它。

```powershell
# Read-only diagnostics and structure
python -m private_ai_company doctor --root examples/quickstart-group
python -m private_ai_company validate-group --root examples/quickstart-group
python -m private_ai_company organization-tree --root examples/quickstart-group
python -m private_ai_company route-department research --root examples/quickstart-group --json

# One bounded local task (deterministic local executors, cost 0.00 CNY)
python -m private_ai_company run-task --root examples/quickstart-group `
    --task-id task-local-001 `
    --instruction 'Prepare the baseline.' `
    --capability research --capability task-orchestration `
    --criterion 'Evidence and artifacts are registered.'
```

The `run-task` command writes only inside the selected GroupPack
(`tasks/events/`, `checkpoints/`, `provenance/content/`). It performs no
downloads, installations, API calls, or external effects.

`run-task` 命令只写入所选 GroupPack 内部（`tasks/events/`、`checkpoints/`、
`provenance/content/`）。它不进行任何下载、安装、API 调用或外部效应。

```powershell
# Terminal 1 — local CEO FastAPI gateway, local host only
python -m private_ai_company serve-api --root examples/quickstart-group --host 127.0.0.1 --port 8790
```

```bash
# Terminal 2 — verify the local endpoints
curl -s http://127.0.0.1:8790/health
curl -s http://127.0.0.1:8790/api/status
curl -s http://127.0.0.1:8790/docs        # OpenAPI UI
```

The API binds only to `127.0.0.1`, returns a 200 on `/health` and `/api/status`,
and serves OpenAPI at `/docs`. It performs zero real-model calls and zero external
publication/payment/account effects.

API 仅绑定 `127.0.0.1`，在 `/health` 与 `/api/status` 返回 200，并在 `/docs` 提供
OpenAPI。它进行零真实模型调用、零外部发布/支付/账号效应。

```bash
# 1) Static documentation validator (rejects missing files / private paths / private-only assets)
python scripts/validate_docs.py --staging-root .

# 2) Standalone public smoke test (imports the package, runs CLI + local API health)
python tests/smoke_public_alpha.py

# 3) Fail-closed validation of the staged tree
python scripts/validate_public_alpha.py --staging-root <PROJECT_ROOT>/release-staging/north-star-group-v0.1-alpha
# Expect: PUBLIC_ALPHA_VALIDATE_OK and zero ZERO_* hits.
```

```powershell
# Native Windows clean-room acceptance (proves outsider-clone behavior)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_clean_room.ps1 -StagingRoot .
```

---

## 中文

### 1. 项目身份与定位

**北极星集团（North Star Group）** 是一个本地优先、模型无关、面向长期运行的 **AI 组织框架**。
它的目标是帮助个人和小团队搭建属于自己的 AI 公司或长期运行的 AI 组织——有持久状态、可追溯证据、
可替换的模型与平台、清晰的人/机职责边界。

我们刻意把它定位为**框架**，而不是：

- 一个聊天机器人外壳；
- 一个单一的小说生成器；
- 一个已经成熟的全自治公司；
- 一个声称已支持所有模型的平台。

小说子公司是当前最成熟的**第一个真实纵向验证**，不是项目的终点。

### 2. 它是什么

北极星集团把"运行一个组织"拆成可组合、可验证、可重启的部件：组织与子公司结构、部门路由、
任务图、权限/预算/工具边界、执行追踪、状态脊柱与知识连续性、自审与验收机制。公开面主要是
Python 运行时、CLI、本地 API、配置与验证工具。

### 3. 它解决什么问题

个人和小团队想运行自己的 AI 组织，而不只是叠加聊天机器人。他们需要：持久状态、知识连续性、
部门路由、权限与预算边界、证据与溯源、以及本地优先与模型无关以避免被锁定。北极星集团把这些
能力做成一套可复用、可审计、可重启的基础，而不是一次性的脚本集合。

### 4. 基本运行方式

所有命令均为本地、确定性、无真实模型调用。详见文首
[快速开始](#quickstart--快速开始)：`doctor` / `validate-group` / `organization-tree` /
`route-department` 为只读诊断；`run-task` 在示例 GroupPack 内执行一个有边界的本地任务；
`serve-api` 在 `127.0.0.1` 提供本地 CEO 网关。

### 5. 诚实说明

我们尽量如实描述这个项目：

- **项目由个人发起。**
- 开发过程中**大量使用了多个 AI 编程工具**，显著加快了原型与实现速度。
- AI 辅助开发也可能带来**过度设计、重复抽象、不必要复杂度、封闭式自证测试，以及未经独立验证的假设**。
- 当前是 **early public alpha**，**不是**成熟的全自治生产公司。
- 开源的重要原因之一，是**诚实地请求有经验的工程师、研究者、安全审查者与 Agent 系统开发者进行独立审查与帮助**。

> **我们知道自己要去哪里，但不假装自己已经知道最好的路。**
> **We know where we want to go, but we do not pretend that we already know the best path.**

我们不会把社区当作免费外包，也不会把维护责任转嫁给贡献者。我们希望通过尊重、协作、独立审查与共同学习改进项目。

### 6. 当前已实现能力

以下能力已在公开运行时中存在并被验证（以公开测试与导出验证为准）：

- **本地优先的 CEO 编排 API**（`ceo_api.py`）：FastAPI 网关，仅绑定 `127.0.0.1`。
- **部门路由**（`route-department`）：按能力把请求派发到对应部门。
- **Provider 抽象**（`providers.py` / `platform_adapter.py`）：架构层的模型/平台接入点；本地模板执行器已验证，更多 provider 待接入。
- **权限、预算与工具边界**（`permissions.py` / `budget.py` / `tools.py`）：策略门、人民币预算预检、工具调度在派发前即生效。
- **执行追踪**（`trace.py`）：统一 task/provider/tool trace，默认排除凭据与正文型敏感数据。
- **任务图与 DAG**（`dag.py` / `governed_dag.py` / `task_graph.py`）：依赖、并发、checkpoint、resume、retry、补偿与明确终态。
- **小说生产管线**（`novel_mvp.py` / `novel_operations.py` / `novel_studio.py`）：规划、知识上下文、生成、内审、不合格草稿阻断、五章滚动缓冲、provenance、生产批次、重启恢复与 owner 控制。
- **自审与验收**（`acceptance.py` / `external_evaluations.py`）：质量门与失败草稿阻断。
- **restart-safe 状态脊柱**（`state_spine.py` / `storage.py` / `company_events.py`）：运行事件（EventStore）、当前状态（Snapshot / State DB）、稳定决策经验（Canonical Memory）、生产制品（Artifact Store）与原始对话证据（Archive）五类事实存储。
- **知识适配器接口**（`novel_knowledge.py`）：公开安全的版本化知识注入契约。
- **确定性默认拒绝导出与失败即止验证**（`export_public_alpha.py` / `validate_public_alpha.py` / `validate_docs.py`）。
- **原生 clean-room 验收**（`run_clean_room.ps1`）：模型无关、效应无关的本地快速开始。
- **Apache-2.0 许可证、GitHub 治理面与 CI**：已公开发布，CI 运行中。

### 7. 已知限制

- 当前是 **early public alpha**，尚不适合生产环境。
- 目前以**人工触发/半自动**为主，不是完全自治。
- **小说子公司是当前最成熟的纵向切片**，其他部门并未达到同等完整度。
- 公开面主要是 Python 运行时、CLI、API、配置与验证机制；**精致的公开 UI 尚未完成**。
- "**model-agnostic**" 是架构原则，**不代表每个 provider 都已接入并验证**。
- **跨平台、长期运行、高并发、分布式与多用户**场景尚未全面证明。
- 尚无**独立专业安全审计**。
- 公开知识框架/connector 尚未完成；**私有知识与付费蒸馏数据不会进入仓库**。
- AI 辅助代码可能存在**重复与过度设计**；后续会进行 Ponytail 风格的安全精简。
- 文档与贡献者 onboarding 仍不成熟。

### 8. 为什么开源

1. 获得**独立的架构与安全审查**。
2. 寻找**更简单、更可靠的实现**。
3. 避免被**单一模型、供应商或平台**锁定。
4. 建立可复用的**个人/小团队 AI 组织基础**。
5. 允许社区贡献**部门、provider、工具、测试、UI、安全与跨平台能力**。
6. 让**工具、文件、网络与权限行为**保持透明。
7. 验证架构能否**从小说子公司推广到更多组织与业务**。

一份清晰的缺陷报告、反例、架构异议或安全问题，**本身就是有效贡献**。社区贡献者**不是无偿外包人员**；维护者仍负责审查、整合、反馈与署名。

### 9. 希望社区在哪些方面帮助

- 架构与安全的独立审查。
- 更简单、更可靠的重构与去重（Ponytail 风格精简）。
- 更多 provider / 平台 Adapter。
- 测试、文档与 contributor onboarding。
- 公开 UI / 控制中心。
- 跨平台验证。
- 新的部门模板与 skill 包。

### 10. 当前进度

| 阶段 / Phase | 状态 / Status |
| --- | --- |
| 1. 组织运行时基础 / Organizational Runtime Foundations | Completed Alpha |
| 2. 持久状态与知识连续性 / Durable State and Knowledge Continuity | First vertical validated |
| 3. 首个可运行子公司 / First Operational Subsidiary | Novel Alpha closure completed |
| 4. 可复现公开 Alpha / Reproducible Public Alpha | Completed |
| 5. 精简、文档与外部审查 / Simplification, Documentation, and External Review | Current |
| 6. 通用 AI 组织与多部门运行 / General AI Organizations and Multi-Department Operations | Planned |

### 11. 公开路线图摘要

完整路线图见 [ROADMAP.md](ROADMAP.md)。要点：

- **Phase 1（Completed Alpha）、Phase 2（First vertical validated）、Phase 3（Novel Alpha closure completed）、Phase 4（Completed）**：分别对应组织运行时基础、持久状态与知识连续性、小说子公司闭环、可复现公开 Alpha（含默认拒绝导出、隐私扫描、确定性 manifests、clean-room、CLI/API、Apache-2.0 与 GitHub 治理面）。四个阶段状态含义不同，不笼统宣称"1–4 全部完成"。
- **Phase 5 进行中**：双语文档、诚实限制说明、公开路线图、清除发布前陈旧文本、Ponytail 安全精简、外部审查、跨平台验证、contributor onboarding。
- **Phase 6 规划中**：通用部门合同、更多部门类型、公开知识 connector、更多 provider、更完善的控制中心/UI、部门间协作、更长周期任务、社区部门包与 skill 包。

### 12. 公开 / 私有边界

可公开：运行时框架、组织/子公司/部门结构、任务路由与任务图、状态/恢复、自审与验收机制、
模型/provider 接口、公开知识协议/connector、安全/导出验证器、合成示例与公开测试、部门模板与社区贡献。

不公开：真实用户知识、付费蒸馏知识数据集、私有作品/小说、对话与任务历史、凭据与 Secret、真实运营数据、
截图、机器路径与私有证据、任何无发布权内容。

详见 [docs/public/PUBLIC_PRIVATE_BOUNDARY.md](docs/public/PUBLIC_PRIVATE_BOUNDARY.md)。

### 13. 路线图治理

- 社区提案可以挑战架构与路线图顺序。
- 评价维度：证据、用户价值、架构一致性、安全、维护成本、可逆性、零号的优先级。
- 社区意见是**决策证据**，不是自动执行权。
- 任意时刻只保留**一个有效可执行路线图**。
- 提案被接受后，必须先明确更新 canonical roadmap，再进入执行。

详见 [docs/public/ROADMAP_GOVERNANCE.md](docs/public/ROADMAP_GOVERNANCE.md) 与
[ROADMAP.md](ROADMAP.md)。

### 14. Quickstart

安装、CLI 与 API 命令见文首 [快速开始](#quickstart--快速开始) 一节（共享代码块，仅一份）。

### 15. License / Security / Contributing

- **License**：Apache-2.0。见 [LICENSE](LICENSE) 与
  [docs/public/LICENSE_DECISION.md](docs/public/LICENSE_DECISION.md)。
- **Contributing**：[CONTRIBUTING.md](CONTRIBUTING.md)。
- **Security**：漏洞请按 [SECURITY.md](SECURITY.md) 私下报告，不要开公开 issue。
- **Conduct**：[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
- **Architecture**：[docs/public/ARCHITECTURE.md](docs/public/ARCHITECTURE.md)。
- **Governance & roadmap**：[ROADMAP.md](ROADMAP.md) 与
  [docs/public/ROADMAP_GOVERNANCE.md](docs/public/ROADMAP_GOVERNANCE.md)。

本包在确定性 quickstart 模式下**不进行任何真实模型调用、不进行任何外部发布**。

---

## English

### 1. Project identity & positioning

**North Star Group** is a **local-first, model-agnostic framework for persistent AI
organizations**. Its goal is to help individuals and small teams build their own AI
company or long-running AI organization — with durable state, traceable evidence,
swappable models and platforms, and clear human/machine responsibility boundaries.

We deliberately position it as a **framework**, not:

- a chatbot shell;
- a single novel generator;
- an already-mature fully-autonomous company;
- a platform that claims to support every model.

The novel subsidiary is the most mature **first real vertical validation** — not the
project's final boundary.

### 2. What it is

North Star Group decomposes "running an organization" into composable, verifiable, and
restart-safe parts: organization and subsidiary structures, department routing, task
graphs, permission/budget/tool boundaries, execution tracing, a state spine with
knowledge continuity, and self-review/acceptance mechanisms. The public surface is
mainly a Python runtime, a CLI, a local API, configuration, and validation tooling.

### 3. What problem it solves

Individuals and small teams want to run their own AI organization, not just stack
chatbots. They need durable state, knowledge continuity, department routing, permission
and budget boundaries, evidence and provenance, and local-first / model-agnostic design
to avoid lock-in. North Star Group turns these into a reusable, auditable, restart-safe
foundation instead of a one-off script collection.

### 4. Basic operation

All commands are local, deterministic, and perform no real model call. See the
[Quickstart](#quickstart--快速开始) section at the top: `doctor` / `validate-group` /
`organization-tree` / `route-department` are read-only diagnostics; `run-task` executes
one bounded local task inside the example GroupPack; `serve-api` exposes a local CEO
gateway on `127.0.0.1`.

### 5. Honest statement

We try to describe this project honestly:

- **The project was initiated by an individual.**
- Development made **heavy use of multiple AI coding tools**, which significantly
  accelerated prototyping and implementation.
- AI-assisted development can also introduce **over-engineering, redundant abstraction,
  unnecessary complexity, closed self-validating tests, and unverified assumptions**.
- This is an **early public alpha**, **not** a mature fully-autonomous production company.
- A key reason for open-sourcing is to **honestly ask experienced engineers, researchers,
  security reviewers, and agent-system developers for independent review and help**.

> **We know where we want to go, but we do not pretend that we already know the best path.**

We do not treat the community as unpaid outsourcing or shift maintainer responsibility onto
contributors. We want to improve the project through respectful collaboration, independent
review, and mutual learning.

### 6. Currently implemented capabilities

The following capabilities exist and are verified in the public runtime (per public tests
and export validation):

- **Local-first CEO orchestration API** (`ceo_api.py`): a FastAPI gateway bound only to `127.0.0.1`.
- **Department routing** (`route-department`): dispatches by capability to the right department.
- **Provider abstraction** (`providers.py` / `platform_adapter.py`): an architectural model/platform entry point; local template executors are verified, more providers are pending.
- **Permission, budget, and tool boundaries** (`permissions.py` / `budget.py` / `tools.py`): policy gates, CNY budget pre-checks, and tool dispatch active before dispatch.
- **Execution tracing** (`trace.py`): unified task/provider/tool trace, excluding credentials and body-type sensitive data by default.
- **Task graphs and DAG** (`dag.py` / `governed_dag.py` / `task_graph.py`): dependencies, concurrency, checkpoint, resume, retry, compensation, and explicit terminal states.
- **Novel production pipeline** (`novel_mvp.py` / `novel_operations.py` / `novel_studio.py`): planning, knowledge context, generation, internal review, failed-draft blocking, a five-chapter rolling buffer, provenance, production batches, restart recovery, and owner controls.
- **Self-review and acceptance** (`acceptance.py` / `external_evaluations.py`): quality gates and failed-draft blocking.
- **Restart-safe state spine** (`state_spine.py` / `storage.py` / `company_events.py`): five fact stores — run events (EventStore), current state (Snapshot / State DB), stable decision experience (Canonical Memory), production artifacts (Artifact Store), and raw conversation evidence (Archive). Canonical Memory is the stable decision-experience layer / read-only retrieval boundary, not a writable store owned by the state spine.
- **Knowledge adapter interface** (`novel_knowledge.py`): a public-safe, versioned knowledge-injection contract.
- **Deterministic default-deny export and fail-closed validation** (`export_public_alpha.py` / `validate_public_alpha.py` / `validate_docs.py`).
- **Native clean-room acceptance** (`run_clean_room.ps1`): model-free, effect-free local quickstart.
- **Apache-2.0 license, GitHub governance surface, and CI**: already published, CI running.

### 7. Known limitations

- This is an **early public alpha** and is not yet suitable for production.
- It is currently **human-triggered / semi-automatic**, not fully autonomous.
- The **novel subsidiary is the most mature vertical slice**; other departments have not reached the same completeness.
- The public surface is mainly the Python runtime, CLI, API, configuration, and validation mechanisms; a **polished public UI is not yet done**.
- "**model-agnostic**" is an architectural principle and **does not mean every provider is wired and verified**.
- **Cross-platform, long-running, high-concurrency, distributed, and multi-user** scenarios are not yet fully proven.
- There is **no independent professional security audit** yet.
- The public knowledge framework/connector is not finished; **private knowledge and paid distillation data will not enter the repository**.
- AI-assisted code may contain **duplication and over-engineering**; a later Ponytail-style safe reduction will address this.
- Documentation and contributor onboarding are still immature.

### 8. Why open source

1. Obtain **independent architecture and security review**.
2. Find **simpler, more reliable implementations**.
3. Avoid lock-in to a **single model, vendor, or platform**.
4. Build a reusable **personal/small-team AI organization foundation**.
5. Let the community contribute **departments, providers, tools, tests, UI, security, and cross-platform capabilities**.
6. Keep **tools, files, network, and permission behavior** transparent.
7. Validate whether the architecture can **generalize from the novel subsidiary to more organizations and businesses**.

A clear defect report, counterexample, architecture objection, or security issue **is itself
a valid contribution**. Community contributors are **not unpaid outsourcers**; maintainers
remain responsible for review, integration, feedback, and attribution.

### 9. How the community can help

- Independent architecture and security review.
- Simpler, more reliable refactors and de-duplication (Ponytail-style reduction).
- More provider / platform adapters.
- Tests, documentation, and contributor onboarding.
- Public UI / control center.
- Cross-platform verification.
- New department templates and skill packages.

### 10. Where We Are Today

| Phase | Status |
| --- | --- |
| 1. Organizational Runtime Foundations | Completed Alpha |
| 2. Durable State and Knowledge Continuity | First vertical validated |
| 3. First Operational Subsidiary | Novel Alpha closure completed |
| 4. Reproducible Public Alpha | Completed |
| 5. Simplification, Documentation, and External Review | Current |
| 6. General AI Organizations and Multi-Department Operations | Planned |

### 11. Public roadmap summary

The full roadmap is in [ROADMAP.md](ROADMAP.md). Highlights:

- **Phase 1 (Completed Alpha), Phase 2 (First vertical validated), Phase 3 (Novel Alpha
  closure completed), and Phase 4 (Completed)**: respectively organizational runtime
  foundations, durable state and knowledge continuity, the novel-subsidiary closure, and a
  reproducible public Alpha (default-deny export, privacy scan, deterministic manifests,
  clean-room, CLI/API, Apache-2.0, and the GitHub governance surface). Their status meanings
  differ; we do not collapse them into a single "all done" claim.
- **Phase 5 is current**: bilingual docs, honest-limitations notes, the public roadmap,
  removal of pre-publication stale text, Ponytail safe reduction, external review,
  cross-platform verification, and contributor onboarding.
- **Phase 6 is planned**: generic department contracts, more department types, a public
  knowledge connector, more providers, a more complete control center/UI, inter-department
  collaboration, longer-horizon tasks, and community department/skill packages.

### 12. Public / private boundary

Public: the runtime framework, organization/subsidiary/department structures, task routing
and task graphs, state/recovery, self-review and acceptance mechanisms, model/provider
interfaces, public knowledge protocols/connectors, security/export validators, synthetic
examples and public tests, department templates and community contributions.

Not public: real user knowledge, paid distillation knowledge datasets, private
works/novels, conversations and task history, credentials and secrets, real operational
data, screen captures, machine paths and private evidence, and anything without publication
rights.

See [docs/public/PUBLIC_PRIVATE_BOUNDARY.md](docs/public/PUBLIC_PRIVATE_BOUNDARY.md).

### 13. Roadmap governance

- Community proposals may challenge architecture and roadmap ordering.
- Evaluation dimensions: evidence, user value, architectural consistency, security,
  maintenance cost, reversibility, and Zero's priorities.
- Community opinions are **decision evidence**, not automatic execution rights.
- At any time only **one valid executable roadmap** is kept.
- After a proposal is accepted, the canonical roadmap must be explicitly updated before
  execution begins.

See [docs/public/ROADMAP_GOVERNANCE.md](docs/public/ROADMAP_GOVERNANCE.md) and
[ROADMAP.md](ROADMAP.md).

### 14. Quickstart

Install, CLI, and API commands are in the [Quickstart](#quickstart--快速开始) section at
the top (shared code block, single copy).

### 15. License / Security / Contributing

- **License**: Apache-2.0. See [LICENSE](LICENSE) and
  [docs/public/LICENSE_DECISION.md](docs/public/LICENSE_DECISION.md).
- **Contributing**: [CONTRIBUTING.md](CONTRIBUTING.md).
- **Security**: report vulnerabilities privately per [SECURITY.md](SECURITY.md); do not
  open public issues for them.
- **Conduct**: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- **Architecture**: [docs/public/ARCHITECTURE.md](docs/public/ARCHITECTURE.md).
- **Governance & roadmap**: [ROADMAP.md](ROADMAP.md) and
  [docs/public/ROADMAP_GOVERNANCE.md](docs/public/ROADMAP_GOVERNANCE.md).

This package performs **no real model call** and **no external publication** in
deterministic quickstart mode.

---

## Notes

- No secrets, no private knowledge, no personal/machine paths are included.
- The exporter never deletes or modifies the private source; it only rebuilds staging.
- Two consecutive exports of an unchanged source produce byte-identical output.
- Secret-like stable error-code constants are covered by exact, machine-readable safe
  exemptions (bound to path + symbol + value) — not by an allow-all rule.
