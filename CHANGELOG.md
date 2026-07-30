# Changelog

All notable changes to the public-alpha candidate are documented here. This
project adheres to a lightweight, manually-curated changelog (no automated
release tooling is part of the public package).

## [0.1.0a1] — 2026-07-30 (LOCAL RELEASE CANDIDATE)

Preparation of the first public-alpha repository release candidate. This entry
records what changed in the candidate assembly; it does **not** represent a
published release.

### Added
- Complete Apache License 2.0 text at the repository root (`LICENSE`).
- Root governance surface: `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`,
  `ROADMAP.md`, and this `CHANGELOG.md`.
- GitHub issue templates (bug report, feature request, architecture proposal,
  and issue config) and a pull-request template.
- Lightweight, read-only GitHub Actions CI
  (`.github/workflows/public-alpha-ci.yml`) running only portable public checks.
- Portable CI-contract test (`tests/test_public_alpha_ci_contract.py`).
- Publication-candidate verifier (`scripts/verify_g1_5_publication_candidate.py`)
  and its tests.

### Changed
- `pyproject.toml` license metadata updated to Apache-2.0.
- `README.md` / `docs/public/README.quickstart.md` status updated to
  "PUBLIC-ALPHA RELEASE CANDIDATE / LOCAL / NOT YET PUBLISHED".
- `docs/public/LICENSE_DECISION.md` recorded the owner-ratified Apache-2.0
  decision.
- `docs/public/ROADMAP_GOVERNANCE.md` stripped stale G1-1-era sequencing and
  documented the proposal-only community rule.

### Unchanged
- Product runtime modules (`src/private_ai_company/*.py`) were not modified.
- Default-deny export policy preserved; the allow-list was extended only for the
  exact new governance/CI/portable-test files.
- Prior clean-room acceptance evidence (G1-4B-R2) was not altered.
