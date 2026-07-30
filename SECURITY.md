# Security Policy

## Default-deny publication boundary

This package is produced by a deterministic, default-deny exporter
(`scripts/export_public_alpha.py`) and verified by a fail-closed validator
(`scripts/validate_public_alpha.py`). By design:

- No secret material is included.
- No private knowledge or operational data is included.
- No personal or machine filesystem paths are included (machine paths are
  scrubbed to `<PROJECT_ROOT>`).

## Reporting a vulnerability

**Do not open a public issue for security vulnerabilities.** If you discover a
vulnerability in the export/validation tooling or the public-boundary
enforcement (for example, a way the boundary could leak private data), report it
through a **private** channel to the maintainers instead, until a fix is
available. Public issue templates are for non-security bugs, feature requests,
and architecture proposals only.

## Boundary assurance

Each release candidate is accompanied by machine-generated evidence:

- `EXCLUSION_RECEIPT.json` — what was excluded (expected: `excluded_count: 0`).
- `SCAN_RECEIPT.json` — secret / private-path / machine-path hit counts
  (expected: all 0).
- `EXPORT_MANIFEST.json` — a deterministic hash manifest for reproducibility.

These receipts are **evidence**, not hand-edited source. Regenerate them with
the exporter; never patch them manually.

## Out of scope

This policy does **not** cover the private runtime, its infrastructure, or any
deployed service. Those are governed separately and are not part of this public
candidate.
