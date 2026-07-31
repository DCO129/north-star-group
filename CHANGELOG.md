# Changelog

All notable changes to the public-alpha package are documented here. This project
adheres to a lightweight, manually-curated changelog (no automated release tooling is
part of the public package).

## [0.1.0a1] — 2026-07-30 (Published)

First public-alpha repository release. Published to the public GitHub repository under
Apache-2.0 on 2026-07-30. Source, reproducible staging, and the published remote tree
were reconciled as identical (144 files, zero diff) in the follow-up G1-6-R1 pass.

### Added
- Complete Apache License 2.0 text at the repository root (`LICENSE`).
- Root governance surface: `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`,
  `ROADMAP.md`, and this `CHANGELOG.md`.
- GitHub issue templates (bug report, feature request, architecture proposal, and issue
  config) and a pull-request template.
- Lightweight, read-only GitHub Actions CI
  (`.github/workflows/public-alpha-ci.yml`) running only portable public checks.
- Portable CI-contract test (`tests/test_public_alpha_ci_contract.py`).
- Publication-candidate verifier (`scripts/verify_g1_5_publication_candidate.py`) and
  its tests.

### Changed
- `pyproject.toml` license metadata updated to Apache-2.0.
- `docs/public/LICENSE_DECISION.md` recorded the owner-ratified Apache-2.0 decision.
- `docs/public/ROADMAP_GOVERNANCE.md` stripped stale G1-1-era sequencing and documented
  the proposal-only community rule.
- README and `docs/public/*` notes were corrected from the earlier pre-publication
  candidate text to reflect the published public alpha.

### Unchanged
- Product runtime modules (`src/private_ai_company/*.py`) were not modified.
- Default-deny export policy preserved; the allow-list was extended only for the exact
  new governance/CI/portable-test files.
- Prior clean-room acceptance evidence was not altered.

## [Unreleased] — 2026-07-31 (Documentation only)

Documentation-only update. No product code, tests, scripts, workflow, config, or license
changed; no new Git tag or release was created.

### Added
- Bilingual (Chinese / English) public narrative across `README.md`, `ROADMAP.md`,
  `CONTRIBUTING.md`, and `docs/public/*`.
- Honest-limitations statement, a "why open source" section, and a public/private
  boundary summary in the README.
- A public roadmap with six phases and consistent community-governance rules across
  `ROADMAP.md`, `CONTRIBUTING.md`, and `docs/public/ROADMAP_GOVERNANCE.md`.

### Changed
- Removed the earlier pre-publication candidate framing — the status notes that
  implied the package was only a local candidate and not yet released, the line about
  a future discussion venue, and the line about repository creation — from every
  public document.
- This `CHANGELOG.md` corrected: the 2026-07-30 entry is now recorded as a published
  release, not a candidate.

### Unchanged
- Runtime, CLI, API, export/validation tooling, tests, CI, and license are unchanged by
  this documentation pass.
