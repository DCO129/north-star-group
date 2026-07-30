'''Deterministic default-deny public exporter for North-Star-Group v0.1-alpha.

Rebuilds the public staging tree from scratch using an explicit allow-list from
the export policy. Everything not explicitly allowed is excluded. The exporter
never deletes or modifies private source data; it only (re)builds the separate
staging directory. Output is byte-identical across runs when source is unchanged.

The deny rules live in config/public-export-policy.json (the trust anchor). That
file is the only staged artifact whose literal content may contain rule strings
such as private-path or machine-path tokens; it is therefore excluded from
content scanning (path scanning still applies) so the policy does not flag itself.
'''

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

# The export policy is the only file allowed to contain rule-definition strings.
TRUSTED_CONFIG_REL = "config/public-export-policy.json"


def load_policy(policy_path=None):
    if policy_path and Path(policy_path).is_file():
        with open(policy_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    default_path = Path(__file__).resolve().parent.parent / "config" / "public-export-policy.json"
    if default_path.is_file():
        with open(default_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    raise FileNotFoundError("public export policy not found; pass --policy")


def _compiled(patterns):
    return [re.compile(p) for p in (patterns or [])]


def scan_content(text, policy, rel_path=""):
    '''Return a structured scan result for a single file's text.

    Keys:
      secret_real        : genuine secret hits that MUST fail closed (list of reasons)
      secret_exempted    : secret-pattern hits reconciled by a verified safe exemption
      exempted_signatures: list of (path, symbol, expected_value) exemptions applied
      private_path       : private knowledge/data path substring hits
      machine_path       : machine-specific absolute path hits

    Safe exemptions are bound to (exact relative path + exact symbol name +
    exact expected non-credential value). A secret-pattern match is exempted
    only when it is fully contained within the exact assignment span of a
    matching exemption for this file. Any symbol/value/path change, or any
    additional secret-like assignment, is NOT exempted and fails closed. The
    rest of an exempted file is still scanned in full; nothing is whole-file
    exempted.
    '''
    deny = policy.get("deny", {})
    secret_res = _compiled(deny.get("secret_patterns", []))
    private_subs = deny.get("private_path_substrings", [])
    machine_res = _compiled(deny.get("machine_path_patterns", []))

    result = {
        "secret_real": [],
        "secret_exempted": [],
        "exempted_signatures": [],
        "private_path": [],
        "machine_path": [],
    }

    # 1. Collect exact safe-exemption assignment spans for this relative path.
    exempt_spans = []  # list of (start, end) byte offsets
    for ex in policy.get("secret_exemptions", []):
        if ex.get("path") != rel_path:
            continue
        sym = ex.get("symbol", "")
        val = ex.get("expected_value", "")
        assign_re = re.compile(
            r"(?m)^\s*" + re.escape(sym) + r"\s*[:=]\s*['\"]" + re.escape(val) + r"['\"]"
        )
        for m in assign_re.finditer(text):
            exempt_spans.append((m.start(), m.end()))
            result["exempted_signatures"].append((ex.get("path"), sym, val))

    # 2. Secret detection with exemption reconciliation.
    for rex in secret_res:
        for m in rex.finditer(text):
            s, e = m.start(), m.end()
            covered = any(s >= es and e <= ee for (es, ee) in exempt_spans)
            if covered:
                result["secret_exempted"].append("secret_exempted:%s" % m.group(0)[:48])
            else:
                result["secret_real"].append("secret_pattern")

    # 3. Private path substrings.
    lowered = text.lower()
    for sub in private_subs:
        if sub.lower() in lowered:
            result["private_path"].append("private_path_substring")

    # 4. Machine paths.
    for rex in machine_res:
        if rex.search(text):
            result["machine_path"].append("machine_path")

    return result


def path_denied(rel_path, policy):
    deny = policy.get("deny", {})
    path_obj = Path(rel_path)
    # Only directory components are checked against prohibited dir names (a file
    # named .gitignore / .gitkeep must not be caught by the '.git' rule).
    dir_parts = path_obj.parts[:-1]
    if path_obj.name in set(deny.get("prohibited_filenames", [])):
        return ["prohibited_filename:%s" % path_obj.name]
    for component in dir_parts:
        if component in set(deny.get("prohibited_dir_components", [])):
            return ["prohibited_dir:%s" % component]
    ext = path_obj.suffix.lower()
    if ext in set(deny.get("prohibited_extensions", [])):
        return ["prohibited_extension:%s" % ext]
    return []


def resolve_allow_files(source_root, policy):
    source_root = Path(source_root)
    found = []
    seen = set()
    for pattern in policy.get("allow", {}).get("files", []):
        for match in sorted(source_root.glob(pattern)):
            if not match.is_file():
                continue
            rel = match.resolve().relative_to(source_root.resolve()).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            found.append((match, rel))
    found.sort(key=lambda item: item[1])
    return found


def _scrub(text, policy):
    scrub = policy.get("scrub", {})
    replacement = scrub.get("replacement", "<PROJECT_ROOT>")
    count = 0
    for rex in _compiled(scrub.get("machine_path_patterns", [])):
        text, n = rex.subn(replacement, text)
        count += n
    return text, count


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_export(source_root, staging_root, policy_path=None):
    policy = load_policy(policy_path)
    source_root = Path(source_root).resolve()
    staging_root = Path(staging_root).resolve()

    if staging_root == source_root or str(staging_root).startswith(str(source_root) + os.sep):
        raise SystemExit("Refusing: staging root must not be inside the source root.")

    # Rebuild staging from scratch (separate directory; private source untouched).
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_root.mkdir(parents=True, exist_ok=True)

    mtime_epoch = int(policy.get("staging", {}).get("mtime_epoch", 0))
    normalize_mtime = bool(policy.get("staging", {}).get("normalize_mtime", True))

    manifest_files = []
    exclusion_records = []
    exemption_records = []
    scan = {
        "scanned": 0,
        "source_secret_suspect_hits": 0,
        "safe_exemption_count": 0,
        "staged_secret_hits": 0,
        "private_path_hits": 0,
        "machine_path_hits": 0,
        "scrubbed": 0,
        "excluded": 0,
    }

    for abs_path, rel_path in resolve_allow_files(source_root, policy):
        path_reasons = path_denied(rel_path, policy)
        try:
            raw = abs_path.read_bytes()
            text = raw.decode("utf-8")
        except (UnicodeDecodeError, OSError):
            exclusion_records.append({"rel_path": rel_path, "reasons": ["undecodable_binary"]})
            scan["excluded"] += 1
            continue

        # The trust anchor (policy file) may contain rule-definition strings;
        # skip content scanning for it, but still apply path-based deny.
        if rel_path != TRUSTED_CONFIG_REL:
            content = scan_content(text, policy, rel_path)
        else:
            content = {
                "secret_real": [], "secret_exempted": [],
                "exempted_signatures": [], "private_path": [], "machine_path": [],
            }

        secret_real = content["secret_real"]
        secret_exempted = content["secret_exempted"]
        private_reasons = content["private_path"]
        machine_reasons = content["machine_path"]

        # Source suspects = every secret-pattern detection (real + exempted).
        scan["source_secret_suspect_hits"] += len(secret_real) + len(secret_exempted)
        scan["safe_exemption_count"] += len(secret_exempted)
        for sig in content["exempted_signatures"]:
            if sig not in exemption_records:
                exemption_records.append(sig)

        blocking = bool(path_reasons) or bool(secret_real) or bool(private_reasons) or bool(machine_reasons)
        if blocking:
            exclusion_records.append({
                "rel_path": rel_path,
                "reasons": (path_reasons
                            + ["secret_real"] * len(secret_real)
                            + private_reasons
                            + machine_reasons),
            })
            scan["excluded"] += 1
            scan["staged_secret_hits"] += len(secret_real)
            scan["private_path_hits"] += len(private_reasons)
            scan["machine_path_hits"] += len(machine_reasons)
            continue

        scrubbed, n = _scrub(text, policy)
        scan["scrubbed"] += n
        out_bytes = scrubbed.encode("utf-8")
        dest = staging_root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(out_bytes)
        if normalize_mtime:
            os.utime(dest, (mtime_epoch, mtime_epoch))
        manifest_files.append({
            "rel_path": rel_path,
            "sha256": _sha256_bytes(out_bytes),
            "length": len(out_bytes),
        })
        scan["scanned"] += 1

    write_public_artifacts(staging_root, policy, mtime_epoch, normalize_mtime, manifest_files, scan)
    write_receipts(staging_root, policy, manifest_files, exclusion_records, exemption_records,
                   scan, mtime_epoch, normalize_mtime)

    manifest = build_manifest(policy, manifest_files)
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False).encode("utf-8")
    (staging_root / "EXPORT_MANIFEST.json").write_bytes(manifest_bytes)
    if normalize_mtime:
        os.utime(staging_root / "EXPORT_MANIFEST.json", (mtime_epoch, mtime_epoch))
    return manifest


def write_public_artifacts(staging_root, policy, mtime_epoch, normalize_mtime,
                           manifest_files, scan):
    env_example = (
        "# Public example environment configuration. Names only - no values.\n"
        "# Copy to .env and supply real values out-of-band; .env is never exported.\n"
        "PUBLIC_ALPHA_STAGING_ROOT=\n"
        "PUBLIC_ALPHA_SOURCE_ROOT=\n"
        "PUBLIC_ALPHA_POLICY=\n"
        "MODEL_PROVIDER_API_KEY=\n"
        "MODEL_PROVIDER_BASE_URL=\n"
        "DEEPSEEK_API_KEY=\n"
    )
    _write_staged(staging_root / ".env.example", env_example, staging_root, manifest_files,
                  scan, mtime_epoch, normalize_mtime)

    for empty_dir in ("knowledge", "data"):
        keep = staging_root / empty_dir / ".gitkeep"
        keep.parent.mkdir(parents=True, exist_ok=True)
        _write_staged(keep, "", staging_root, manifest_files, scan, mtime_epoch, normalize_mtime)

    gitignore = (
        "# North-Star-Group v0.1-alpha public candidate\n"
        ".env\n"
        "*.log\n"
        "__pycache__/\n"
        ".pytest_cache/\n"
        ".ruff_cache/\n"
        ".venv/\n"
        "node_modules/\n"
        ".git/\n"
        "*.pyc\n"
        "secrets/\n"
        "private/\n"
    )
    _write_staged(staging_root / ".gitignore", gitignore, staging_root, manifest_files, scan,
                  mtime_epoch, normalize_mtime)


def _write_staged(dest, content, staging_root, manifest_files, scan, mtime_epoch, normalize_mtime):
    data = content.encode("utf-8") if isinstance(content, str) else content
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    if normalize_mtime:
        os.utime(dest, (mtime_epoch, mtime_epoch))
    rel_path = dest.resolve().relative_to(staging_root.resolve()).as_posix()
    manifest_files.append({
        "rel_path": rel_path,
        "sha256": _sha256_bytes(data),
        "length": len(data),
    })
    scan["scanned"] += 1


def build_manifest(policy, manifest_files):
    listed = [f for f in manifest_files if f["rel_path"] != "EXPORT_MANIFEST.json"]
    listed.sort(key=lambda f: f["rel_path"])
    tree_hash_input = "\n".join("%s|%s|%s" % (f["rel_path"], f["sha256"], f["length"])
                                 for f in listed).encode("utf-8")
    return {
        "schema_version": "public-export-manifest/v1",
        "generated_by": "export_public_alpha.py",
        "policy_mode": policy.get("mode", "default-deny"),
        "tree_hash": _sha256_bytes(tree_hash_input),
        "file_count": len(listed),
        "files": listed,
    }


def write_receipts(staging_root, policy, manifest_files, exclusion_records,
                   exemption_records, scan, mtime_epoch, normalize_mtime):
    unresolved = scan["source_secret_suspect_hits"] - scan["safe_exemption_count"]
    verdict = ("clean" if (unresolved == 0 and scan["staged_secret_hits"] == 0
                           and scan["private_path_hits"] == 0
                           and scan["machine_path_hits"] == 0) else "dirty")
    receipt_specs = [
        ("EXCLUSION_RECEIPT.json", {
            "schema_version": "public-exclusion-receipt/v1",
            "excluded_count": len(exclusion_records),
            "excluded": exclusion_records,
        }),
        ("SCAN_RECEIPT.json", {
            "schema_version": "public-scan-receipt/v2",
            "scanned_files": scan["scanned"],
            "source_secret_suspect_hits": scan["source_secret_suspect_hits"],
            "safe_exemption_count": scan["safe_exemption_count"],
            "unresolved_source_suspects": unresolved,
            "staged_secret_hits": scan["staged_secret_hits"],
            "private_path_hits": scan["private_path_hits"],
            "machine_path_hits": scan["machine_path_hits"],
            "scrubbed_count": scan["scrubbed"],
            "excluded_count": scan["excluded"],
            "exemptions": [
                {"path": p, "symbol": s, "expected_value": v, "status": "verified_safe"}
                for (p, s, v) in exemption_records
            ],
            "verdict": verdict,
        }),
        ("EXEMPTION_RECEIPT.json", {
            "schema_version": "public-exemption-receipt/v1",
            "safe_exemption_count": scan["safe_exemption_count"],
            "verified_exemptions": [
                {"path": p, "symbol": s, "expected_value": v, "status": "verified_safe"}
                for (p, s, v) in exemption_records
            ],
        }),
    ]
    for name, payload in receipt_specs:
        data = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8")
        dest = staging_root / name
        dest.write_bytes(data)
        if normalize_mtime:
            os.utime(dest, (mtime_epoch, mtime_epoch))
        manifest_files.append({
            "rel_path": name,
            "sha256": _sha256_bytes(data),
            "length": len(data),
        })

    listed = [f for f in manifest_files if f["rel_path"] != "EXPORT_MANIFEST.json"]
    inventory = {
        "schema_version": "public-file-inventory/v1",
        "file_count": len(listed),
        "files": listed,
    }
    inv_data = json.dumps(inventory, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8")
    (staging_root / "FILE_INVENTORY.json").write_bytes(inv_data)
    if normalize_mtime:
        os.utime(staging_root / "FILE_INVENTORY.json", (mtime_epoch, mtime_epoch))
    manifest_files.append({
        "rel_path": "FILE_INVENTORY.json",
        "sha256": _sha256_bytes(inv_data),
        "length": len(inv_data),
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description="Deterministic default-deny public exporter.")
    parser.add_argument("--source-root", default=os.environ.get("PUBLIC_ALPHA_SOURCE_ROOT"))
    parser.add_argument("--staging-root", default=os.environ.get("PUBLIC_ALPHA_STAGING_ROOT"))
    parser.add_argument("--policy", default=os.environ.get("PUBLIC_ALPHA_POLICY"))
    args = parser.parse_args(argv)

    if not args.source_root or not args.staging_root:
        parser.error("--source-root and --staging-root are required.")

    here = Path(__file__).resolve().parents[1]
    policy_path = args.policy or str(here / "config" / "public-export-policy.json")

    manifest = run_export(args.source_root, args.staging_root, policy_path)
    print("EXPORT_DONE files=%d tree_hash=%s" % (manifest["file_count"], manifest["tree_hash"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
