# Security (pointer)

The authoritative security policy is the repository-root
[SECURITY.md](../../SECURITY.md). This `docs/public/` copy is a short pointer only.
In short: the package is produced by a deterministic, default-deny exporter and
a fail-closed validator; it contains no secret material, private data, or machine
paths. Report vulnerabilities **privately** per the root policy — do not open
public issues for them. The generated receipts (`EXPORT_MANIFEST.json`,
`SCAN_RECEIPT.json`, etc.) are evidence, not hand-edited source.
