# North-Star-Group — v0.1-Alpha Public Candidate (Quickstart)

> **Status: PUBLIC-ALPHA RELEASE CANDIDATE / LOCAL / NOT YET PUBLISHED.** A
> *portable export candidate* produced by a deterministic, default-deny exporter.
> Nothing here connects to private infrastructure, reads private data, resolves
> private secrets, or uses a private virtual environment.

## What this package is

A curated, machine-readable subset of the runtime, rebuilt from scratch into a
separate staging tree. Only files on an explicit allow-list are copied;
everything else is excluded by default.

```
src/private_ai_company/      # Public-safe runtime source (allow-listed modules)
scripts/                     # export_public_alpha.py, validate_public_alpha.py,
                              # validate_docs.py, run_clean_room.ps1
config/                      # public-export-policy.json (the export trust anchor)
examples/quickstart-group/   # Synthetic minimal Group example
tests/smoke_public_alpha.py  # Standalone public smoke test
pyproject.toml               # Public packaging metadata + dependency definition
bootstrap.ps1                # Portable host/group doctor launcher
docs/public/                 # This documentation set
.env.example                 # Variable NAMES only — no values, never exported
```

## Install (outsider clone)

```powershell
python -m venv .venv
.\.venv\Scripts\pip install .
```

Declared public dependencies: `fastapi`, `uvicorn`, `tzdata`. No private
repository on `PYTHONPATH`, no private virtual environment.

## CLI quickstart (deterministic, model-free)

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

All commands use local template executors. `run-task` writes only inside the
selected GroupPack and performs no downloads, installations, API calls, or
external effects (cost 0.00 CNY).

## Local API quickstart (127.0.0.1 only)

```powershell
python -m private_ai_company serve-api --root examples/quickstart-group --host 127.0.0.1 --port 8790
```

```bash
curl -s http://127.0.0.1:8790/health
curl -s http://127.0.0.1:8790/api/status
curl -s http://127.0.0.1:8790/docs
```

The gateway binds only to `127.0.0.1`, returns `200` on `/health` and
`/api/status`, and serves OpenAPI at `/docs`. Zero real-model calls, zero
external effects.

## Verification

```bash
python scripts/validate_docs.py --staging-root .
python tests/smoke_public_alpha.py
python scripts/validate_public_alpha.py --staging-root <PROJECT_ROOT>/release-staging/north-star-group-v0.1-alpha
```

Expect `DOCS_VALIDATE_OK`, the smoke test to pass, and
`PUBLIC_ALPHA_VALIDATE_OK` with zero `ZERO_*` hits.

## Clean-room acceptance (native Windows)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_clean_room.ps1 -StagingRoot .
```

This copies staging outside the source tree, builds a fresh virtual environment,
installs only from the clean copy, proves every loaded module is inside the clean
room, runs the CLI + local API, and writes a `DONE.marker` on success.

## Notes

- No secrets, no private knowledge, no personal/machine paths are included.
- Stable error-code constants are covered by exact, machine-readable safe
  exemptions (path + symbol + value) — not by an allow-all rule.
- Two consecutive exports of an unchanged source produce byte-identical output.
