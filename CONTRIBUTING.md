# Contributing

Thanks for your interest in the North-Star-Group public-alpha runtime.

This repository is a **local release candidate** produced by a deterministic,
default-deny exporter. It is **not yet published** to any hosting provider. The
guidelines below are the authoritative contribution rules for this package.

## Scope of what belongs here

Only public-safe code and documentation live in this repository. Do **not**
submit:

- Secrets, credentials, or `.env` files (`.env.example` carries names only).
- Private knowledge-store contents or operational/pilot data.
- Absolute personal or machine filesystem paths (use `<PROJECT_ROOT>` if a
  placeholder is unavoidable).
- Generated private evidence, staging receipts, or machine-specific logs.

## Development flow

1. Make changes against the private source repository.
2. Ensure every new file is on the export allow-list
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

## Pull-request requirements

Every pull request must declare:

- **Scope** — what changes and why.
- **Tests** — what was added or updated under `tests/`.
- **Public/private-boundary impact** — whether the change adds or removes any
  allow-listed surface, and whether the default-deny boundary still holds.
- **External effects / model calls** — explicitly state whether the change
  performs any real model call, network call, payment, or other external effect.
  The public quickstart mode must remain model-free and effect-free.

## Governance: proposals, not automatic adoption

- Architecture and roadmap changes enter as **proposals** (see
  `.github/ISSUE_TEMPLATE/architecture_proposal.yml`).
- No proposal automatically replaces the canonical roadmap. A proposal becomes
  effective only after explicit owner/Codex governance acceptance.
- Generated staging receipts (`EXPORT_MANIFEST.json`, `FILE_INVENTORY.json`,
  `*_RECEIPT.json`) are **evidence**, not hand-edited source. Do not edit them
  by hand; regenerate them with the exporter.

## Code style

- Keep modules importable and side-effect-free where possible.
- Add or update tests for any export/validate behavior change.
- Keep the public package dependency-light; the novel production workflow is an
  optional extra and is not required for the CLI/API quickstart.
