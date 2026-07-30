# Roadmap (Public Alpha)

> **Status: LOCAL RELEASE CANDIDATE — NOT YET PUBLISHED.** This roadmap is a
> *non-binding* draft for discussion. No item here is committed until the owner
> (Zero) and the Codex governance path explicitly accept it.

## Current state

- Deterministic, default-deny public export pipeline (`scripts/export_public_alpha.py`).
- Fail-closed public-boundary validator (`scripts/validate_public_alpha.py`).
- Native clean-room acceptance (`scripts/run_clean_room.ps1`) — model-free,
  effect-free quickstart.
- License: **Apache-2.0** (owner-ratified on 2026-07-30).

## Community proposals (proposal-only)

Architecture and roadmap changes are welcome as **proposals**
(`.github/ISSUE_TEMPLATE/architecture_proposal.yml`). A proposal does **not**
automatically replace this roadmap. It becomes effective only after explicit
owner/Codex governance acceptance.

Suggested areas for future discussion (none committed):

- First tagged release `v0.1-alpha` cut from the certified staging tree.
- Hosting decision (e.g., a public GitHub organization/repository).
- Additional portable public tests and broader platform CI matrices.
- Documentation depth for the public-safe runtime surface.

## Governance principles

- **Boundary first**: no publish until the default-deny boundary is certified.
- **Reproducible**: every public artifact is produced by deterministic tooling.
- **Fail-closed**: validation errors block, never warn-and-pass.
- **Separation**: private knowledge/data remain outside the public package.

## Decision rights

Publication target, license, and release cadence are **owner** decisions. This
document is informational and does not authorize any external action.
