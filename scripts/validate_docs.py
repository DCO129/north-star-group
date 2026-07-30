'''Static documentation validator for the public alpha candidate.

Rejects documentation that:
  * references a project file that is missing from the staged tree,
  * references a command whose target script/module is missing,
  * leaks a private path (reuses the export boundary deny lists),
  * depends on the private source tree (private-only assets/venv/repo name).

Fail-closed: any finding prints ``FAIL:`` and exits non-zero.
'''
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Assets that only exist in the private source tree. A public doc must never
# tell an outsider to use them.
PRIVATE_ONLY_ASSETS = (
    "certify_model_department_executor.py",
    "test_public_alpha_export.py",
    "test_public_alpha_export",
    "start-control-center.ps1",
    "start-novel-studio.ps1",
    "start-novel-studio.cmd",
    ".venv-novel",
    "private-ai-company-runtime",
    "run_n1_",
    "validate_n1_",
    "run_promptfoo_evaluations.py",
    "demo_durable_dag.py",
)

KNOWN_DIR_PREFIXES = (
    "src/", "scripts/", "config/", "docs/", "examples/", "tests/",
    "architecture/", "knowledge/", "data/",
)
KNOWN_BARE_FILES = {
    "pyproject.toml", "README.md", "bootstrap.ps1", ".env.example",
    "README.quickstart.md", "requirements-api.txt", "requirements-novel.txt",
    "requirements-research.txt", "LICENSE_DECISION.md", "SECURITY.md",
    "CONTRIBUTING.md", "ARCHITECTURE.md", "PUBLIC_PRIVATE_BOUNDARY.md",
    "ROADMAP_GOVERNANCE.md", "EXPORT_MANIFEST.json", "FILE_INVENTORY.json",
    "SCAN_RECEIPT.json", "EXCLUSION_RECEIPT.json", "EXEMPTION_RECEIPT.json",
    "LICENSE", "CODE_OF_CONDUCT.md", "ROADMAP.md", "CHANGELOG.md",
}

# Path tokens are only extracted when they carry a known directory prefix
# followed by a path segment that stops at whitespace/punctuation. This
# avoids capturing README layout-comment tails (e.g. "scripts/foo.py, # note").
PATH_RE = re.compile(r"(?:src|scripts|config|docs|examples|tests|architecture|knowledge|data)[\\/][^\s`\"',;:#)+]+")
FILENAME_RE = re.compile(r"[A-Za-z0-9_.\-]+\.(?:py|ps1|toml|json|md|txt|cmd)")


def fail(msg):
    print(f"FAIL: {msg}")
    return False


def load_policy(staging_root: Path):
    path = staging_root / "config" / "public-export-policy.json"
    if not path.is_file():
        # No policy present: still validate presence/private checks with empty lists.
        return {"deny": {"private_path_substrings": [], "machine_path_patterns": []}}
    return json.loads(path.read_text(encoding="utf-8"))


def _path_tokens_from_text(s: str):
    out = set()
    out.update(PATH_RE.findall(s))
    out.update(FILENAME_RE.findall(s))
    return out


def extract_path_tokens(text: str):
    '''Extract path-like tokens from fenced code blocks and inline code.

    Fenced blocks are processed first, then *removed* from the text so the
    inline-backtick regex cannot re-capture an entire triple-backtick block as
    one (false) token.
    '''
    tokens = set()
    # 1) Fenced code blocks (handles ```lang and bare ``` variants).
    fences = re.findall(r"```.*?```", text, re.DOTALL)
    for fence in fences:
        inner = re.sub(r"^```[^\n]*\n?", "", fence, flags=re.DOTALL)
        inner = inner.rstrip("`").strip()
        tokens.update(_path_tokens_from_text(inner))
    # 2) Blank fences so inline regex only sees true inline spans.
    text_no_fences = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    # 3) Inline backtick spans (true single-line code).
    for m in re.finditer(r"`([^`\n]+)`", text_no_fences):
        span = m.group(1)
        tokens.add(span)
        tokens.update(_path_tokens_from_text(span))
    # 4) Markdown links: [label](target)
    for m in re.finditer(r"\]\(([^)\s]+)\)", text_no_fences):
        tokens.add(m.group(1))
    # 5) "python -m <module>" — only the public package is a project module to
    #    verify; stdlib/tooling modules (venv, pytest, pip) are not.
    for m in re.finditer(r"python\s+-m\s+([A-Za-z0-9_.]+)", text):
        mod = m.group(1)
        if mod.startswith("private_ai_company"):
            tokens.add("MODULE:" + mod)
    return tokens


def is_project_path(token: str) -> bool:
    if token.startswith("MODULE:"):
        return True
    if PATH_RE.search(token):
        return True
    name = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if name in KNOWN_BARE_FILES:
        return True
    if FILENAME_RE.search(token) and ("/" in token or "\\" in token):
        return True
    return False


def validate(staging_root: Path):
    ok = True
    policy = load_policy(staging_root)
    private_subs = policy["deny"].get("private_path_substrings", [])
    machine_res = [re.compile(p) for p in policy["deny"].get("machine_path_patterns", [])]

    doc_files = [staging_root / "README.md"]
    pub = staging_root / "docs" / "public"
    if pub.is_dir():
        doc_files += sorted(pub.glob("*.md"))

    missing_docs = [d for d in doc_files if not d.is_file()]
    for d in missing_docs:
        ok = fail(f"document missing: {d}")
    if missing_docs:
        return False

    for doc in doc_files:
        text = doc.read_text(encoding="utf-8")

        # 1) private-path leak (reuse export boundary deny lists)
        low = text.lower()
        for sub in private_subs:
            if sub.lower() in low:
                ok = fail(f"{doc.name}: contains private path substring '{sub}'")
        for rex in machine_res:
            if rex.search(text):
                ok = fail(f"{doc.name}: matches machine path pattern '{rex.pattern}'")

        # 2) private-only assets / private source tree
        for asset in PRIVATE_ONLY_ASSETS:
            if asset in text:
                ok = fail(f"{doc.name}: references private-only asset '{asset}'")

        # 3) referenced files / commands must resolve
        for token in extract_path_tokens(text):
            if not token:
                continue
            # Glob patterns are documentation wildcards, not real references.
            if "*" in token or "**" in token:
                continue
            if not is_project_path(token):
                continue
            if token.startswith("MODULE:"):
                mod = token[len("MODULE:"):].replace(".", "/")
                candidate_py = staging_root / "src" / (mod + ".py")
                candidate_pkg = staging_root / "src" / mod / "__init__.py"
                if not (candidate_py.is_file() or candidate_pkg.is_file()):
                    ok = fail(f"{doc.name}: module '{token[len('MODULE:'):]}' "
                              f"not present at {candidate_py}")
                continue
            norm = token
            while norm.startswith("./") or norm.startswith(".\\"):
                norm = norm[2:]
            cand_repo = staging_root / norm
            cand_doc = doc.parent / norm
            if cand_repo.exists() or cand_doc.exists():
                continue
            ok = fail(f"{doc.name}: referenced path '{token}' is missing "
                      f"from staging")

    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--staging-root", default=None)
    args = ap.parse_args()
    root = Path(args.staging_root) if args.staging_root else Path(__file__).resolve().parent.parent
    passed = validate(root)
    if passed:
        print("DOCS_VALIDATE_OK")
        return 0
    print("DOCS_VALIDATE_FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
