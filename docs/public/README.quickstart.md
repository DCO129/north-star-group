# North-Star-Group — v0.1-Alpha Public Quickstart / 公开快速开始

[中文](#中文-4) | [English](#english-4)

本包是北极星集团的**公开 Alpha**（Apache-2.0，已发布）。以下快速开始使用确定性的本地执行器，
**无真实模型调用、无外部效应**。

This is the **public Alpha** of North Star Group (Apache-2.0, already published). The
quickstart below uses deterministic local executors — **no real model call, no external
effect**.

---

## 中文 / Chinese

### 这是什么 / What this package is

北极星集团运行时的一个精选、机器可读子集，从零重建为独立的暂存树。只有显式白名单上的文件被复制，
其余默认排除。

### 安装（外部克隆）/ Install (outsider clone)

```powershell
python -m venv .venv
.\.venv\Scripts\pip install .
```

声明过的公开依赖：`fastapi`、`uvicorn`、`tzdata`。`PYTHONPATH` 上无私有仓库，无私有虚拟环境。

### CLI 快速开始（确定性，无模型）/ CLI quickstart (deterministic, model-free)

```powershell
python -m private_ai_company doctor --root examples/quickstart-group
python -m private_ai_company validate-group --root examples/quickstart-group
python -m private_ai_company organization-tree --root examples/quickstart-group
python -m private_ai_company route-department research --root examples/quickstart-group --json
python -m private_ai_company run-task --root examples/quickstart-group `
    --task-id task-local-001 `
    --instruction 'Prepare the baseline.' `
    --capability research --capability task-orchestration `
    --criterion 'Evidence and artifacts are registered.'
```

所有命令使用本地模板执行器。`run-task` 只写入所选 GroupPack，不进行任何下载、安装、API 调用或
外部效应（成本 0.00 CNY）。

### 本地 API 快速开始（仅 127.0.0.1）/ Local API quickstart (127.0.0.1 only)

```powershell
python -m private_ai_company serve-api --root examples/quickstart-group --host 127.0.0.1 --port 8790
```

```bash
curl -s http://127.0.0.1:8790/health
curl -s http://127.0.0.1:8790/api/status
curl -s http://127.0.0.1:8790/docs
```

网关仅绑定 `127.0.0.1`，在 `/health` 与 `/api/status` 返回 `200`，并在 `/docs` 提供 OpenAPI。
零真实模型调用、零外部效应。

### 验证 / Verification

```bash
python scripts/validate_docs.py --staging-root .
python tests/smoke_public_alpha.py
python scripts/validate_public_alpha.py --staging-root <PROJECT_ROOT>/release-staging/north-star-group-v0.1-alpha
```

期望 `DOCS_VALIDATE_OK`、smoke 测试通过，以及 `PUBLIC_ALPHA_VALIDATE_OK` 且全 `ZERO_*` 为零。

### Clean-room 验收（原生 Windows）/ Clean-room acceptance (native Windows)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_clean_room.ps1 -StagingRoot .
```

这会把暂存复制到源树之外、构建全新虚拟环境、仅从干净副本安装、证明每个加载的模块都在 clean-room
内、运行 CLI 与本地 API，并在成功时写入 `DONE.marker`。

### 备注 / Notes

- 不含密钥、私有知识、个人/机器路径。
- 稳定的错误码常量由精确的、机器可读的安全豁免覆盖（路径 + 符号 + 值），而非全允许规则。
- 对未改动的源连续两次导出产生逐字节一致的输出。

---

## English

### What this package is

A curated, machine-readable subset of the North Star Group runtime, rebuilt from scratch
into a separate staging tree. Only files on an explicit allow-list are copied; everything
else is excluded by default.

### Install (outsider clone)

```powershell
python -m venv .venv
.\.venv\Scripts\pip install .
```

Declared public dependencies: `fastapi`, `uvicorn`, `tzdata`. No private repository on
`PYTHONPATH`, no private virtual environment.

### CLI quickstart (deterministic, model-free)

```powershell
python -m private_ai_company doctor --root examples/quickstart-group
python -m private_ai_company validate-group --root examples/quickstart-group
python -m private_ai_company organization-tree --root examples/quickstart-group
python -m private_ai_company route-department research --root examples/quickstart-group --json
python -m private_ai_company run-task --root examples/quickstart-group `
    --task-id task-local-001 `
    --instruction 'Prepare the baseline.' `
    --capability research --capability task-orchestration `
    --criterion 'Evidence and artifacts are registered.'
```

All commands use local template executors. `run-task` writes only inside the selected
GroupPack and performs no downloads, installations, API calls, or external effects (cost
0.00 CNY).

### Local API quickstart (127.0.0.1 only)

```powershell
python -m private_ai_company serve-api --root examples/quickstart-group --host 127.0.0.1 --port 8790
```

```bash
curl -s http://127.0.0.1:8790/health
curl -s http://127.0.0.1:8790/api/status
curl -s http://127.0.0.1:8790/docs
```

The gateway binds only to `127.0.0.1`, returns `200` on `/health` and `/api/status`, and
serves OpenAPI at `/docs`. Zero real-model calls, zero external effects.

### Verification

```bash
python scripts/validate_docs.py --staging-root .
python tests/smoke_public_alpha.py
python scripts/validate_public_alpha.py --staging-root <PROJECT_ROOT>/release-staging/north-star-group-v0.1-alpha
```

Expect `DOCS_VALIDATE_OK`, the smoke test to pass, and `PUBLIC_ALPHA_VALIDATE_OK` with
zero `ZERO_*` hits.

### Clean-room acceptance (native Windows)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_clean_room.ps1 -StagingRoot .
```

This copies staging outside the source tree, builds a fresh virtual environment, installs
only from the clean copy, proves every loaded module is inside the clean room, runs the
CLI + local API, and writes a `DONE.marker` on success.

### Notes

- No secrets, no private knowledge, no personal/machine paths are included.
- Stable error-code constants are covered by exact, machine-readable safe exemptions
  (path + symbol + value) — not by an allow-all rule.
- Two consecutive exports of an unchanged source produce byte-identical output.
