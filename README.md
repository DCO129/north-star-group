# North-Star-Group — Public Alpha Candidate (v0.1-alpha)

> **Status: PUBLIC-ALPHA RELEASE CANDIDATE / LOCAL / NOT YET PUBLISHED.** This is a
> *portable export candidate* produced by a deterministic, default-deny exporter.
> It is intended for a future GitHub discussion, not for deployment, and is not yet
> published to any hosting provider. Nothing here connects to private
> infrastructure, reads private data, resolves private secrets, or uses a private
> virtual environment.

A curated, machine-readable subset of the runtime, rebuilt from scratch into a
separate staging tree. Only files on an explicit allow-list are copied;
everything else is excluded by default.

## Layout

```
pyproject.toml            # public packaging metadata + dependency definition
src/private_ai_company/   # Public-safe runtime source (allow-listed modules)
scripts/                  # export_public_alpha.py, validate_public_alpha.py,
                         # validate_docs.py, verify_g1_5_publication_candidate.py,
                         # run_clean_room.ps1
config/                   # public-export-policy.json (the export trust anchor)
examples/quickstart-group/  # Synthetic minimal Group example
tests/smoke_public_alpha.py # Standalone public smoke test (no private dependency)
tests/test_public_alpha_ci_contract.py  # Portable export/boundary CI contract test
tests/test_g1_5_publication_candidate.py # Publication-candidate verifier tests
bootstrap.ps1             # Portable host/group doctor launcher
docs/public/              # This documentation set
requirements-api.txt      # Declared public API dependencies
.env.example              # Variable NAMES only — no values, never exported
LICENSE                   # Apache License 2.0 (complete, official text)
CONTRIBUTING.md           # Authoritative contribution guide
SECURITY.md               # Security policy and private vulnerability reporting
CODE_OF_CONDUCT.md        # Contributor code of conduct
ROADMAP.md                # Public-alpha roadmap (proposal-only)
CHANGELOG.md              # Curated changelog
.github/                  # Issue/PR templates and read-only CI workflow
knowledge/.gitkeep        # Empty placeholder — private knowledge is NOT included
data/.gitkeep             # Empty placeholder — private data is NOT included
EXPORT_MANIFEST.json      # Deterministic hash manifest of the staged tree
FILE_INVENTORY.json       # Per-file inventory
EXCLUSION_RECEIPT.json    # Proof of what was excluded (expected: empty)
SCAN_RECEIPT.json         # Proof of zero secrets / zero private paths
EXEMPTION_RECEIPT.json    # Proof of verified-safe constant exemptions
```

## License & governance

- **License**: Apache-2.0. See [LICENSE](LICENSE) and the owner-ratified decision in
  [docs/public/LICENSE_DECISION.md](docs/public/LICENSE_DECISION.md).
- **Contributing**: [CONTRIBUTING.md](CONTRIBUTING.md).
- **Security**: report vulnerabilities privately per [SECURITY.md](SECURITY.md); do
  not open public issues for them.
- **Conduct**: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- **Architecture**: [docs/public/ARCHITECTURE.md](docs/public/ARCHITECTURE.md).
- **Governance & roadmap**: [ROADMAP.md](ROADMAP.md) and
  [docs/public/ROADMAP_GOVERNANCE.md](docs/public/ROADMAP_GOVERNANCE.md).

This package performs **no real model call** and **no external publication** in
deterministic quickstart mode.

## Install (outsider clone)

The package installs with only its declared public dependencies — no private
repository on `PYTHONPATH`, no private virtual environment, no secret values.

```powershell
python -m venv .venv
.\.venv\Scripts\pip install .
```

`pyproject.toml` declares `fastapi`, `uvicorn`, and `tzdata`. The novel
production workflow (which needs a third-party agent framework) is an optional
extra and is intentionally **not** required for the CLI/API quickstart.

## CLI quickstart (deterministic, model-free)

All CLI commands run against the synthetic example group and use local
template executors — no model call, no secret, no network.

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

## Local API quickstart (127.0.0.1 only)

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
and serves OpenAPI at `/docs`. It performs zero real-model calls and zero
external publication/payment/account effects.

## Verification

```bash
# 1) Static documentation validator (rejects missing files / private paths / private-only assets)
python scripts/validate_docs.py --staging-root .

# 2) Standalone public smoke test (imports the package, runs CLI + local API health)
python tests/smoke_public_alpha.py

# 3) Fail-closed validation of the staged tree
python scripts/validate_public_alpha.py --staging-root <PROJECT_ROOT>/release-staging/north-star-group-v0.1-alpha
# Expect: PUBLIC_ALPHA_VALIDATE_OK and zero ZERO_* hits.
```

## Clean-room acceptance (native Windows)

`scripts/run_clean_room.ps1` proves the candidate works like an outsider clone:

- copies staging into a fresh directory **outside** the source tree,
- clears inherited `PYTHONPATH` and sets `PYTHONNOUSERSITE=1`,
- creates a fresh virtual environment and installs **only** from the clean copy,
- prints `sys.path` and every loaded project module path,
- proves every loaded project module lives inside the clean room,
- runs the exact CLI quickstart and the local API on `127.0.0.1`,
- records all commands, exit codes, hashes, module origins, and HTTP results,
- leaves only a `DONE.marker` on success (or `FAILED.marker` on failure).

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_clean_room.ps1 -StagingRoot .
```

## Notes

- No secrets, no private knowledge, no personal/machine paths are included.
- The exporter never deletes or modifies the private source; it only rebuilds staging.
- Two consecutive exports of an unchanged source produce byte-identical output.
- Secret-like stable error-code constants are covered by exact, machine-readable
  safe exemptions (bound to path + symbol + value) — not by an allow-all rule.
