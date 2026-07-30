# Architecture (Public Overview)

This document describes the *public-safe* surface of the North-Star-Group runtime.
It intentionally omits private operational topology, credentials, and internal
service addresses.

## Components (public subset)

| Area | Public module(s) | Purpose |
|------|------------------|---------|
| Orchestration API | `src/private_ai_company/ceo_api.py` | Public-safe request/response surface |
| Novel production | `src/private_ai_company/novel_mvp.py` | Content generation pipeline (public logic) |
| Operations | `src/private_ai_company/novel_operations.py` | Operational helpers (public logic) |
| Export tooling | `scripts/export_public_alpha.py` | Deterministic default-deny exporter |
| Validation | `scripts/validate_public_alpha.py` | Fail-closed public-boundary validator |
| Policy | `config/public-export-policy.json` | Machine-readable export boundary (trust anchor) |

## Export pipeline

```
private source ──▶ allow-list resolution ──▶ content/path scan (deny rules)
                                                      │
                                            excluded ──▶ EXCLUSION_RECEIPT.json
                                            passed  ──▶ scrub machine paths
                                                           │
                                                 staged tree (normalized mtime)
                                                           │
                                                 EXPORT_MANIFEST.json (tree_hash)
                                                           │
                                            validate_public_alpha.py (fail-closed)
```

## Determinism guarantees

- Staging is rebuilt from scratch on every run (no incremental state).
- All staged file mtimes are normalized to a fixed epoch.
- `EXPORT_MANIFEST.json` is a stable, key-sorted hash of the tree.
- Re-running the exporter on an unchanged source yields byte-identical output.

## Out of scope (private, never exported)

- Private knowledge base contents (`knowledge/` is an empty placeholder here).
- Operational state, logs, screen captures, and runtime artifacts.
- Any secret material or personal/machine filesystem paths.
