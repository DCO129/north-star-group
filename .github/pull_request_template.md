## Scope
<!-- What changes and why. -->

## Tests
<!-- What was added or updated under tests/. -->

## Public/private-boundary impact
<!-- Does this change the default-deny allow-list (config/public-export-policy.json)
     or the exported surface? Confirm the boundary still holds. -->

## External effects / model calls
<!-- Explicitly state whether this performs any real model call, network call,
     payment, or other external effect. The public quickstart mode must remain
     model-free and effect-free. -->

## Checklist
- [ ] `python scripts/validate_docs.py --staging-root ./_staging` passes
- [ ] `python scripts/validate_public_alpha.py --staging-root ./_staging` reports PUBLIC_ALPHA_VALIDATE_OK with zero ZERO_* hits
- [ ] `python tests/smoke_public_alpha.py` passes
- [ ] No secrets, private filesystem paths, or generated private evidence submitted
