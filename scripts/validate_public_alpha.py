'''Fail-closed validator for the North-Star-Group v0.1-alpha public staging tree.

Scans the staged public candidate for: secret patterns, private knowledge/data
path substrings, machine-specific absolute paths, prohibited file types or
directories, symlink/junction escapes and path traversal, and staged-tree
hash-manifest mismatch. Any finding fails closed (non-zero exit, explicit FAIL
lines). The validator never modifies the staged tree.
'''

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

from export_public_alpha import load_policy, path_denied, scan_content, TRUSTED_CONFIG_REL

# Generated export artifacts are trusted (path-scanned, not content-scanned) so the
# validator does not flag its own receipts for the rule strings they record.
_TRUSTED_ARTIFACTS = (
    TRUSTED_CONFIG_REL,
    "EXPORT_MANIFEST.json",
    "FILE_INVENTORY.json",
    "EXCLUSION_RECEIPT.json",
    "SCAN_RECEIPT.json",
    "EXEMPTION_RECEIPT.json",
)

MARKERS = []


def _mark(name, value):
    MARKERS.append("%s=%s" % (name, value))


def _is_reparse_point(path: Path) -> bool:
    # Use lstat() — stat() follows the symlink on Windows and would report the
    # target's attributes (a normal directory), hiding the reparse point.
    try:
        st = path.lstat()
    except (OSError, AttributeError):
        try:
            return path.is_symlink()
        except OSError:
            return False
    try:
        return bool(st.st_file_attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except AttributeError:
        # Non-Windows: no st_file_attributes; fall back to the link flag.
        return path.is_symlink()


def _check_dependency_closure(staging_root):
    '''Fail-closed: every intra-package import must resolve to a staged module.

    The staged package is flat under src/private_ai_company/. We verify that each
    relative (from .X import) or absolute (from private_ai_company.X import /
    import private_ai_company.X) intra-package import target exists as a staged
    .py module, and that required modules (ceo_api, novel_operations,
    platform_adapter, production_batch) are present. Returns a list of missing
    targets (empty when the closure is complete).
    '''
    staging_root = Path(staging_root)
    pkg_dir = staging_root / "src" / "private_ai_company"
    if not pkg_dir.is_dir():
        return ["src/private_ai_company (package directory missing)"]

    present = {f.name[:-3] for f in pkg_dir.rglob("*.py")}

    required = ["ceo_api", "novel_operations", "platform_adapter", "production_batch"]
    missing = []
    for mod in required:
        if mod not in present:
            missing.append("private_ai_company.%s (required module absent)" % mod)

    rel_from_re = re.compile(r"from\s+\.([A-Za-z_]\w*)\s+import")
    abs_from_re = re.compile(r"from\s+private_ai_company\.([A-Za-z_]\w*)\s+import")
    abs_import_re = re.compile(r"import\s+private_ai_company\.([A-Za-z_]\w*)")
    rel_sibling_re = re.compile(
        r"from\s+\.\s+import\s+([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)"
    )
    targets = set()
    for f in pkg_dir.rglob("*.py"):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in rel_from_re.finditer(text):
            targets.add(m.group(1))
        for m in abs_from_re.finditer(text):
            targets.add(m.group(1))
        for m in abs_import_re.finditer(text):
            targets.add(m.group(1))
        for m in rel_sibling_re.finditer(text):
            for name in m.group(1).split(","):
                name = name.strip()
                if name:
                    targets.add(name)
    for t in targets:
        if t not in present:
            missing.append("private_ai_company.%s (imported, not staged)" % t)

    # De-duplicate preserving order.
    seen = set()
    out = []
    for m in missing:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def validate(staging_root, policy_path=None):
    policy = load_policy(policy_path)
    staging_root = Path(staging_root).resolve()
    if not staging_root.is_dir():
        return False, ["staging_root_missing:%s" % staging_root], {}

    deny = policy.get("deny", {})
    prohibited_ext = set(deny.get("prohibited_extensions", []))
    prohibited_dirs = set(deny.get("prohibited_dir_components", []))

    failures = []
    counts = {
        "secret": 0, "private_path": 0, "machine_path": 0,
        "prohibited_file": 0, "symlink_escape": 0, "traversal": 0,
        "hash_mismatch": 0, "manifest_missing": 0, "missing_module": 0,
    }
    manifest_entries = {}

    manifest_path = staging_root / "EXPORT_MANIFEST.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for entry in manifest.get("files", []):
                manifest_entries[entry["rel_path"]] = entry
        except (ValueError, KeyError, OSError):
            failures.append("manifest_unreadable")
            counts["manifest_missing"] += 1
    else:
        failures.append("manifest_missing")
        counts["manifest_missing"] += 1

    actual_files = []
    for root, dirs, files in os.walk(staging_root):
        root_path = Path(root)
        # Reparse points (symlink/junction) must fail closed, whether reached as a
        # directory entry (preventing recursion) or a file entry (below).
        for entry in list(dirs):
            entry_full = root_path / entry
            if _is_reparse_point(entry_full):
                failures.append("symlink_escape:%s" % entry_full)
                counts["symlink_escape"] += 1
                dirs.remove(entry)
        for name in files:
            full = root_path / name
            if _is_reparse_point(full):
                failures.append("symlink_escape:%s" % full)
                counts["symlink_escape"] += 1
                continue
            rel = full.resolve().relative_to(staging_root).as_posix()
            actual_files.append((full, rel))

            parts = PurePosixPath(rel).parts
            if ".." in parts:
                failures.append("path_traversal:%s" % rel)
                counts["traversal"] += 1
                continue
            bad_dir = next((c for c in parts if c in prohibited_dirs), None)
            if bad_dir:
                failures.append("prohibited_dir:%s" % rel)
                counts["prohibited_file"] += 1
                continue
            ext = full.suffix.lower()
            if ext in prohibited_ext:
                failures.append("prohibited_extension:%s" % rel)
                counts["prohibited_file"] += 1
                continue

            reasons = path_denied(rel, policy)
            try:
                raw = full.read_bytes()
                text = raw.decode("utf-8", errors="replace")
            except OSError:
                failures.append("unreadable:%s" % rel)
                continue
            # The trust-anchor policy file and generated export receipts/manifest
            # may contain rule-definition strings; skip content scanning for them
            # (path scanning already applied above).
            if rel not in _TRUSTED_ARTIFACTS:
                content = scan_content(text, policy, rel)
                # Only genuine secret hits block; exempted hits are safe.
                reasons += content["secret_real"]
                reasons += content["private_path"]
                reasons += content["machine_path"]
            if reasons:
                for reason in reasons:
                    if reason.startswith("secret"):
                        counts["secret"] += 1
                    elif reason.startswith("private"):
                        counts["private_path"] += 1
                    elif reason.startswith("machine"):
                        counts["machine_path"] += 1
                failures.append("%s:%s" % ("|".join(reasons), rel))
                continue

            # Manifest consistency (EXPORT_MANIFEST.json itself excluded).
            if rel == "EXPORT_MANIFEST.json":
                continue
            entry = manifest_entries.get(rel)
            if entry is None:
                failures.append("untracked_file:%s" % rel)
                counts["hash_mismatch"] += 1
                continue
            import hashlib
            actual_sha = hashlib.sha256(raw).hexdigest()
            if actual_sha != entry.get("sha256"):
                failures.append("hash_mismatch:%s" % rel)
                counts["hash_mismatch"] += 1

    # Every manifest entry must correspond to an existing staged file.
    actual_rel = {rel for _, rel in actual_files}
    for rel in manifest_entries:
        if rel != "EXPORT_MANIFEST.json" and rel not in actual_rel:
            failures.append("manifest_entry_missing_on_disk:%s" % rel)
            counts["hash_mismatch"] += 1

    # Public package dependency closure: every intra-package import must resolve
    # to an exported module. Fail-closed for missing modules (e.g. when a module
    # was excluded from staging). This also enforces presence of required modules.
    missing = _check_dependency_closure(staging_root)
    for m in missing:
        failures.append("missing_module:%s" % m)
        counts["missing_module"] += 1

    passed = len(failures) == 0
    _mark("ZERO_SECRET", counts["secret"])
    _mark("ZERO_PRIVATE_PATH", counts["private_path"])
    _mark("ZERO_MACHINE_PATH", counts["machine_path"])
    _mark("ZERO_PROHIBITED_FILE", counts["prohibited_file"])
    _mark("ZERO_SYMLINK_ESCAPE", counts["symlink_escape"])
    _mark("ZERO_TRAVERSAL", counts["traversal"])
    _mark("ZERO_MISSING_MODULE", counts["missing_module"])
    _mark("HASH_MANIFEST_OK", "yes" if counts["hash_mismatch"] == 0 else "no")
    if passed:
        _mark("PUBLIC_ALPHA_VALIDATE_OK", "yes")
    else:
        for failure in failures:
            sys.stderr.write("FAIL: %s\n" % failure)
    return passed, failures, counts


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fail-closed public staging validator.")
    parser.add_argument("--staging-root", default=os.environ.get("PUBLIC_ALPHA_STAGING_ROOT"))
    parser.add_argument("--policy", default=os.environ.get("PUBLIC_ALPHA_POLICY"))
    args = parser.parse_args(argv)
    if not args.staging_root:
        parser.error("--staging-root is required.")

    here = Path(__file__).resolve().parents[1]
    policy_path = args.policy or str(here / "config" / "public-export-policy.json")

    passed, failures, counts = validate(args.staging_root, policy_path)
    for marker in MARKERS:
        print(marker)
    if not passed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
