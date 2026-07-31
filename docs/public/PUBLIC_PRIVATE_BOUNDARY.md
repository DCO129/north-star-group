# Public / Private Boundary / 公开与私有边界

[中文](#中文--chinese) | [English](#english)

本包采用**默认拒绝**导出模型：除非显式在白名单上，否则**没有任何内容**是公开的。边界由代码
（`export_public_alpha.py` + `validate_public_alpha.py`）强制，并在
`config/public-export-policy.json` 中以机器可读形式定义。

This package follows a **default-deny** export model: *nothing* is public unless it is
explicitly on the allow-list. The boundary is enforced by code
(`export_public_alpha.py` + `validate_public_alpha.py`) and is machine-readable in
`config/public-export-policy.json`.

---

## 中文 / Chinese

### 可公开 / What is public

- 运行时框架 / Runtime framework
- 组织 / 子公司 / 部门结构 / Organization, subsidiary, and department structures
- 任务路由与任务图 / Task routing and task graphs
- 状态 / 恢复 / State and recovery
- 自审与验收机制 / Self-review and acceptance mechanisms
- 模型 / provider 接口 / Model and provider interfaces
- 公开知识协议 / connector / Public knowledge protocols and connectors
- 安全 / 导出验证器 / Security and export validators
- 合成示例与公开测试 / Synthetic examples and public tests
- 部门模板与社区贡献 / Department templates and community contributions

### 不公开 / What is not public

- 真实用户知识 / Real user knowledge
- 付费蒸馏知识数据集 / Paid distillation knowledge datasets
- 私有作品 / 小说 / Private works and novels
- 对话与任务历史 / Conversations and task history
- 凭据、Secret、账号 / Credentials, secrets, and accounts
- 真实运营数据 / Real operational data
- 截图、机器路径与私有证据 / Screen captures, machine paths, and private evidence
- 任何无发布权的内容 / Anything without publication rights

不暗示私有数据未来会自动开源。/ No suggestion is made that private data will be open-sourced in the future.

### 允许清单（显式公开）/ Allow-list (explicitly public)

以下清单与 `config/public-export-policy.json` 的 `allow.files` **逐项一致**，由机器 gate 校验（missing/extra = 0/0）。不要手动扩宽、缩窄或用不等价概括替代。

```
src/private_ai_company/**/*.py
pyproject.toml
requirements-api.txt
requirements-novel.txt
requirements-research.txt
README.md
bootstrap.ps1
examples/quickstart-group/**/*
architecture/**/*
scripts/export_public_alpha.py
scripts/validate_public_alpha.py
scripts/validate_docs.py
scripts/run_clean_room.ps1
scripts/verify_g1_5_publication_candidate.py
tests/smoke_public_alpha.py
tests/test_public_alpha_ci_contract.py
tests/test_g1_5_publication_candidate.py
config/public-export-policy.json
LICENSE
CONTRIBUTING.md
SECURITY.md
CODE_OF_CONDUCT.md
ROADMAP.md
CHANGELOG.md
.github/**/*
docs/public/**/*
```

### 拒绝规则（永不公开）/ Deny rules (never public)

- **密钥模式**：API key、JWT、私钥、bearer token、已分配的凭据。
- **私有路径子串**：内部产物目录、试点证据库、运行时根目录、知识库、截图与启动器/运行时状态。
- **机器路径模式**：用户目录、CodexLab 路径、WSL/Ubuntu 挂载。
- **禁止文件类型**：二进制、证书、密钥、日志、缓存。
- **禁止文件名**：`.env`（仅 `.env.example` 带空值被暂存）。
- **禁止目录组件**：缓存、venv、`.git`、临时/调试目录。

### 擦洗 / Scrubbing

允许内容中发现的机器路径模式会被替换为占位符 `<PROJECT_ROOT>`，使公开包不含任何绝对个人/机器路径。

### 验证（失败即止）/ Validation (fail-closed)

`validate_public_alpha.py` 扫描暂存树，对任何发现都失败即止：密钥命中、私有路径命中、机器路径命中、
禁止文件、符号链接/ junctions 逃逸、路径遍历，或 manifest 哈希不匹配。它打印明确的 `FAIL:` 行与非零退出码。

### 排除证明 / Exclusion proof

- `EXCLUSION_RECEIPT.json` — 被排除的内容（干净白名单匹配时预期 `excluded_count: 0`）。
- `SCAN_RECEIPT.json` — 密钥/私有路径/机器路径命中计数（预期全为 0）。

---

## English

### What is public

- Runtime framework
- Organization, subsidiary, and department structures
- Task routing and task graphs
- State and recovery
- Self-review and acceptance mechanisms
- Model and provider interfaces
- Public knowledge protocols and connectors
- Security and export validators
- Synthetic examples and public tests
- Department templates and community contributions

### What is not public

- Real user knowledge
- Paid distillation knowledge datasets
- Private works and novels
- Conversations and task history
- Credentials, secrets, and accounts
- Real operational data
- Screen captures, machine paths, and private evidence
- Anything without publication rights

No suggestion is made that private data will be open-sourced in the future.

### Allow-list (explicitly public)

The list below is kept **line-for-line consistent** with `allow.files` in
`config/public-export-policy.json`, enforced by a machine gate (missing/extra =
0/0). Do not widen, narrow, or replace it with a non-equivalent summary.

```
src/private_ai_company/**/*.py
pyproject.toml
requirements-api.txt
requirements-novel.txt
requirements-research.txt
README.md
bootstrap.ps1
examples/quickstart-group/**/*
architecture/**/*
scripts/export_public_alpha.py
scripts/validate_public_alpha.py
scripts/validate_docs.py
scripts/run_clean_room.ps1
scripts/verify_g1_5_publication_candidate.py
tests/smoke_public_alpha.py
tests/test_public_alpha_ci_contract.py
tests/test_g1_5_publication_candidate.py
config/public-export-policy.json
LICENSE
CONTRIBUTING.md
SECURITY.md
CODE_OF_CONDUCT.md
ROADMAP.md
CHANGELOG.md
.github/**/*
docs/public/**/*
```

### Deny rules (never public)

- **Secret patterns**: API keys, JWTs, private keys, bearer tokens, assigned credential assignments.
- **Private path substrings**: internal artifact directories, pilot evidence stores, the
  runtime root directory, the knowledge store, screen captures, and launcher/runtime state.
- **Machine-path patterns**: user-profile paths, CodexLab paths, WSL/Ubuntu mounts.
- **Prohibited file types**: binaries, certs, keys, logs, caches.
- **Prohibited filenames**: `.env` (only `.env.example` with empty values is staged).
- **Prohibited directory components**: caches, venvs, `.git`, temp/debug dirs.

### Scrubbing

Machine-path patterns found in allowed content are replaced with the placeholder
`<PROJECT_ROOT>` so the public package contains no absolute personal/machine paths.

### Validation (fail-closed)

`validate_public_alpha.py` scans the staged tree and fails closed on *any* finding: secret
hit, private-path hit, machine-path hit, prohibited file, symlink/junction escape, path
traversal, or manifest hash mismatch. It prints explicit `FAIL:` lines and non-zero exit
codes.

### Exclusion proof

- `EXCLUSION_RECEIPT.json` — what was excluded (expected: `excluded_count: 0` for a clean
  allow-list match).
- `SCAN_RECEIPT.json` — secret/private-path/machine-path hit counts (expected: all 0).
