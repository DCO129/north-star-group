# Public / Private Boundary

This package follows a **default-deny** export model: *nothing* is public unless it
is explicitly on the allow-list. The boundary is enforced by code
(`export_public_alpha.py` + `validate_public_alpha.py`) and is machine-readable in
`config/public-export-policy.json`.

## Allow-list (explicitly public)

- `src/private_ai_company/**/*.py`
- `requirements-*.txt`
- `README.md`, `architecture/**`, `examples/**`
- `scripts/export_public_alpha.py`, `scripts/validate_public_alpha.py`
- `config/public-export-policy.json`
- `docs/public/**`

## Deny rules (never public)

- **Secret patterns**: API keys, JWTs, private keys, bearer tokens, assigned
  credential assignments.
- **Private path substrings**: internal artifact directories, pilot evidence stores,
  the runtime root directory, the knowledge store, screen captures, and launcher/runtime state.
- **Machine-path patterns**: user-profile paths, CodexLab paths, WSL/Ubuntu mounts.
- **Prohibited file types**: binaries, certs, keys, logs, caches.
- **Prohibited filenames**: `.env` (only `.env.example` with empty values is staged).
- **Prohibited directory components**: caches, venvs, `.git`, temp/debug dirs.

## Scrubbing

Machine-path patterns found in allowed content are replaced with the placeholder
`<PROJECT_ROOT>` so the public package contains no absolute personal/machine paths.

## Validation (fail-closed)

`validate_public_alpha.py` scans the staged tree and fails closed on *any* finding:
secret hit, private-path hit, machine-path hit, prohibited file, symlink/junction
escape, path traversal, or manifest hash mismatch. It prints explicit `FAIL:` lines
and non-zero exit codes.

## Exclusion proof

- `EXCLUSION_RECEIPT.json` — what was excluded (expected: `excluded_count: 0` for a
  clean allow-list match).
- `SCAN_RECEIPT.json` — secret/private-path/machine-path hit counts (expected: all 0).
