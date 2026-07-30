'''Fail-closed publication-candidate verifier for North-Star-Group G1-5.

Assembles a structured bundle of required gates from the public staging tree,
prior clean-room evidence, and a double-export evidence file, then derives a
machine certificate using ``all(required_gates.values())``. Missing fields or any
false gate fails closed (exit 2, explicit FAIL). The certificate is written to
the G1-5 evidence root, never into the public staging tree.

This verifier is part of the publication tooling. It deliberately contains no
hardcoded absolute machine paths; every path is supplied via CLI arguments so it
survives export scanning and can be re-run by an independent reviewer.
'''
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Sibling export/validation tooling (shipped in scripts/).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import export_public_alpha  # noqa: E402
import validate_public_alpha  # noqa: E402

# ---------------------------------------------------------------------------
# Frozen constants (G1-5 source freeze; see execution order §2.5 and §12).
# These pin the accepted R2 runner baseline and the unchanged prior evidence so
# the certificate cannot be earned by regenerating or mutating them.
# ---------------------------------------------------------------------------
RUNNER_SHA_FROZEN = "4669c4467aa95b15d1344cfa96d79ab9f971a0b0a2aa23b76a61d496de299924"
SRC_TREE_SHA_FROZEN = "e7584eb4f7072efff24e75b8be8d821370bc5f11e20dfef2bcfca137dede1bca"

R2_EVIDENCE_FROZEN = {
    "clean_room_evidence.json": "a888910f300a80cb079214e218f9471920da36510fcae4e4880589a08ce9fdb2",
    "launcher-start.json": "7534a158231dcc59d0212059b3f4ac0237b2c00aa19707f0ef9013fa25540453",
    "launcher-finish.json": "eb661f50a23356534b614188e8fec230d01adcfa78d51b0c8753a87394fa84f7",
    "g1-4b-r2-certificate.json": "8dcae9d732e98cdf035742ca830d34e660ac4f6852d60ba798027f9f111573d9",
}

LICENSE_SECTION_HEADERS = [
    "1. Definitions",
    "2. Grant of Copyright License",
    "3. Grant of Patent License",
    "4. Redistribution",
    "5. Submission of Contributions",
    "6. Trademarks",
    "7. Disclaimer of Warranty",
    "8. Limitation of Liability",
    "9. Accepting Warranty or Additional Liability",
]

# Static CI gate: prohibited command/regex tokens (no model, no publish, no
# secrets, no absolute-machine-path deploy, no heavy external actions).
PROHIBITED_WORKFLOW_TOKENS = [
    r"git\s+push",
    r"git\s+remote\s+add",
    r"gh\s+release",
    r"twine\s+upload",
    r"npm\s+publish",
    r"deploy\s*:",
    r"\$\{\{\s*secrets",
    r"\bopenai\b",
    r"\bdeepseek\b",
    r"\banthropic\b",
    r"\bgemini\b",
    r"curl\s+",
]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return ""


def src_tree_hash(source_root: Path) -> str:
    pkg = source_root / "src" / "private_ai_company"
    if not pkg.is_dir():
        return ""
    files = sorted(pkg.rglob("*.py"))
    lines = []
    for f in files:
        rel = f.relative_to(source_root).as_posix()
        lines.append("%s|%s|%d" % (rel, sha256_file(f), f.stat().st_size))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Gate derivation (pure functions over a bundle of structured facts).
# ---------------------------------------------------------------------------
def compute_gates(bundle: dict) -> tuple:
    gate_keys = [
        "license_apache",
        "license_file_complete",
        "no_stale_license_text",
        "governance_files_present",
        "readme_status_ok",
        "workflow_static_valid",
        "exporter_default_deny",
        "runner_byte_equal",
        "double_export_deterministic",
        "boundary_zero_violations",
        "smoke_compile_import_pass",
        "no_product_source_changed",
        "prior_evidence_unchanged",
        "real_model_calls_zero",
        "external_effects_false",
    ]
    gates = {k: bool(bundle.get(k, False)) for k in gate_keys}
    verdict = all(gates.values())
    return gates, verdict


# ---------------------------------------------------------------------------
# Bundle assembly (reads real artifacts; never fabricates a PASS).
# ---------------------------------------------------------------------------
def _license_complete(license_text: str) -> bool:
    if len(license_text) < 1000:
        return False
    if "END OF TERMS AND CONDITIONS" not in license_text:
        return False
    return all(h in license_text for h in LICENSE_SECTION_HEADERS)


def _read_json(path: Path):
    try:
        return json.loads(read_text(path))
    except (ValueError, OSError):
        return {}


def _workflow_static_valid(wf_text: str) -> bool:
    if not wf_text:
        return False
    low = wf_text.lower()
    # Read-only permissions must be present.
    if "contents: read" not in low and "read-all" not in low:
        return False
    # Must run on a Windows runner (portable Windows/Python 3.11 job).
    if "runs-on:" not in low or "windows" not in low:
        return False
    # Required triggers.
    if "push:" not in low or "pull_request:" not in low or "workflow_dispatch:" not in low:
        return False
    # No prohibited heavy/external/model/secrets commands.
    for tok in PROHIBITED_WORKFLOW_TOKENS:
        if re.search(tok, wf_text, re.IGNORECASE):
            return False
    return True


def _exporter_default_deny(policy: dict) -> bool:
    if policy.get("mode") != "default-deny":
        return False
    allow = policy.get("allow", {}).get("files", [])
    # Directory-wide trust (a bare "*" or "**") would defeat default-deny.
    for entry in allow:
        if entry in ("*", "**"):
            return False
    return True


def run_smoke_in_temp_venv(staging_root: Path, python_exe: str) -> tuple:
    '''Install the staged package into a throwaway venv and run the public
    smoke test. Mirrors the outsider-clone expectation without touching the
    host environment. Returns (passed, detail).

    The install runs against a throwaway COPY of the staging tree so the
    canonical publication-candidate directory is never polluted by build
    artifacts, ``__pycache__`` directories, or ``.egg-info`` metadata.
    '''
    staging_root = Path(staging_root)
    try:
        import shutil
        with tempfile.TemporaryDirectory() as td:
            venv = Path(td) / "venv"
            r = subprocess.run([python_exe, "-m", "venv", str(venv)],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                return False, "venv_create_failed"
            vpy = (venv / "Scripts" / "python.exe") if os.name == "nt" \
                else (venv / "bin" / "python")
            subprocess.run([str(vpy), "-m", "pip", "install", "--upgrade", "pip"],
                           capture_output=True, text=True, timeout=300)
            # Work on a copy so the canonical staging stays byte-clean.
            work = Path(td) / "staging-copy"
            shutil.copytree(staging_root, work)
            install = subprocess.run([str(vpy), "-m", "pip", "install", str(work)],
                                     capture_output=True, text=True, timeout=900)
            if install.returncode != 0:
                return False, "pip_install_failed:" + install.stderr[-400:]
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            # Drop any variable exceeding the Windows environment-value limit
            # (32767 chars). Hosts such as the WorkBuddy runtime inject config
            # vars far larger than this (e.g. ACC_PRODUCT_CONFIG_V3 ~400KB);
            # the outsider-clone smoke test never needs them, and restoring them
            # via unittest.mock.patch.dict teardown raises ValueError.
            for _k in [k for k, _v in env.items() if len(_v) > 32767]:
                del env[_k]
            smoke = subprocess.run([str(vpy), "tests/smoke_public_alpha.py"],
                                   cwd=str(work), env=env,
                                   capture_output=True, text=True, timeout=300)
            return (smoke.returncode == 0), (smoke.stdout + smoke.stderr)[-600:]
    except Exception as exc:  # noqa: BLE001
        return False, "smoke_exception:%s" % exc


def build_bundle(staging_root, source_root, prior_r2_evidence_dir,
                 double_export_evidence_path, python_exe):
    staging_root = Path(staging_root)
    source_root = Path(source_root)
    bundle = {}

    # g1 license_apache: owner-ratified Apache-2.0 decision on record.
    lic_decision = read_text(staging_root / "docs" / "public" / "LICENSE_DECISION.md")
    pyproject_text = read_text(staging_root / "pyproject.toml")
    bundle["license_apache"] = (
        ("Apache-2.0" in lic_decision and "approved" in lic_decision.lower())
        and ("Apache-2.0" in pyproject_text)
    )

    # g2 license_file_complete: full Apache 2.0 text present and non-empty.
    bundle["license_file_complete"] = _license_complete(
        read_text(staging_root / "LICENSE")
    )

    # g3 no_stale_license_text: no Proprietary claim, no "does NOT choose".
    low_pyproject = pyproject_text.lower()
    bundle["no_stale_license_text"] = (
        "proprietary" not in low_pyproject
        and "does not choose a license" not in lic_decision.lower()
        and "not auto-selected" not in lic_decision.lower()
    )

    # g4 governance_files_present.
    gov = [
        "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/ISSUE_TEMPLATE/architecture_proposal.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/pull_request_template.md",
        ".github/workflows/public-alpha-ci.yml",
    ]
    bundle["governance_files_present"] = all(
        (staging_root / g).is_file() for g in gov
    )

    # g5 readme_status_ok: local release candidate, not falsely published.
    readme = read_text(staging_root / "README.md").lower()
    bundle["readme_status_ok"] = (
        "release candidate" in readme
        and "not yet published" in readme
        and "github.com" not in readme
    )

    # g6 workflow_static_valid.
    bundle["workflow_static_valid"] = _workflow_static_valid(
        read_text(staging_root / ".github" / "workflows" / "public-alpha-ci.yml")
    )

    # g7 exporter_default_deny.
    bundle["exporter_default_deny"] = _exporter_default_deny(
        _read_json(staging_root / "config" / "public-export-policy.json")
    )

    # g8 runner_byte_equal: source == staged == frozen R2 runner baseline.
    src_runner = source_root / "scripts" / "run_clean_room.ps1"
    staged_runner = staging_root / "scripts" / "run_clean_room.ps1"
    src_sha = sha256_file(src_runner) if src_runner.is_file() else ""
    staged_sha = sha256_file(staged_runner) if staged_runner.is_file() else ""
    bundle["runner_byte_equal"] = (
        bool(src_sha) and src_sha == staged_sha
        and src_sha == RUNNER_SHA_FROZEN
    )

    # g9 double_export_deterministic.
    de = _read_json(Path(double_export_evidence_path)) if double_export_evidence_path else {}
    bundle["double_export_deterministic"] = bool(
        de.get("deterministic") is True
        or (de.get("tree_hash_a") and de.get("tree_hash_a") == de.get("tree_hash_b"))
    )

    # g10 boundary_zero_violations: fail-closed validator reports clean.
    try:
        passed, _failures, counts = validate_public_alpha.validate(staging_root)
        bundle["boundary_zero_violations"] = bool(
            passed
            and counts["secret"] == 0
            and counts["private_path"] == 0
            and counts["machine_path"] == 0
            and counts["prohibited_file"] == 0
            and counts["symlink_escape"] == 0
            and counts["traversal"] == 0
            and counts["missing_module"] == 0
            and counts["hash_mismatch"] == 0
        )
    except Exception:  # noqa: BLE001
        bundle["boundary_zero_violations"] = False

    # g11 smoke_compile_import_pass: compileall + temp-venv smoke test.
    # compileall runs against a throwaway copy so the canonical staging tree is
    # never left with ``__pycache__`` directories (keeps the candidate clean
    # and the verifier idempotent).
    import shutil as _shutil
    with tempfile.TemporaryDirectory() as _td:
        _cc = Path(_td) / "compile-check"
        _shutil.copytree(staging_root, _cc)
        compile_r = subprocess.run(
            [python_exe, "-m", "compileall", "-q",
             str(_cc / "src"), str(_cc / "scripts")],
            capture_output=True,
        )
    smoke_ok, _detail = run_smoke_in_temp_venv(staging_root, python_exe)
    bundle["smoke_compile_import_pass"] = (compile_r.returncode == 0) and smoke_ok

    # g12 no_product_source_changed: src tree hash matches freeze.
    bundle["no_product_source_changed"] = (
        src_tree_hash(source_root) == SRC_TREE_SHA_FROZEN
    )

    # g13 prior_evidence_unchanged: G1-4B-R2 evidence hashes match freeze.
    r2_dir = Path(prior_r2_evidence_dir) if prior_r2_evidence_dir else None
    if r2_dir and r2_dir.is_dir():
        prior_ok = True
        for name, frozen in R2_EVIDENCE_FROZEN.items():
            p = r2_dir / name
            if not p.is_file() or sha256_file(p) != frozen:
                prior_ok = False
                break
        bundle["prior_evidence_unchanged"] = prior_ok
    else:
        bundle["prior_evidence_unchanged"] = False

    # g14/g15 real_model_calls & external_effects from prior clean-room evidence.
    r2_ev = _read_json(r2_dir / "clean_room_evidence.json") if (r2_dir and (r2_dir / "clean_room_evidence.json").is_file()) else {}
    bundle["real_model_calls_zero"] = (r2_ev.get("real_model_calls") == 0)
    bundle["external_effects_false"] = (r2_ev.get("external_effects") is False)

    return bundle


# ---------------------------------------------------------------------------
# Evaluation: derive verdict, write certificate, return exit code.
# ---------------------------------------------------------------------------
def evaluate(bundle: dict, evidence_root: Path) -> int:
    gates, verdict = compute_gates(bundle)
    evidence_root = Path(evidence_root)
    evidence_root.mkdir(parents=True, exist_ok=True)
    certificate = {
        "schema_version": "g1-5-publication-candidate-certificate/v1",
        "generated_by": "verify_g1_5_publication_candidate.py",
        "verdict": "PASS" if verdict else "FAIL",
        "finish_exit_code": 0 if verdict else 2,
        "current_task_requires_codex": (not verdict),
        "gates": gates,
        "gate_count": len(gates),
        "gate_true_count": sum(1 for v in gates.values() if v),
    }
    cert_json = json.dumps(certificate, indent=2, sort_keys=True, ensure_ascii=False)
    (evidence_root / "g1-5-certificate.json").write_text(cert_json, encoding="utf-8")
    (evidence_root / "g1-5-certificate.md").write_text(
        "# G1-5 Publication-Candidate Certificate\n\n"
        "```\n" + json.dumps(certificate, indent=2, ensure_ascii=False) + "\n```\n",
        encoding="utf-8",
    )
    return 0 if verdict else 2


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fail-closed G1-5 publication-candidate verifier.")
    ap.add_argument("--staging-root", required=True)
    ap.add_argument("--source-root", required=True)
    ap.add_argument("--evidence-root", required=True)
    ap.add_argument("--prior-r2-evidence", default=None)
    ap.add_argument("--double-export-evidence", default=None)
    ap.add_argument("--python-exe", default=sys.executable)
    args = ap.parse_args(argv)

    bundle = build_bundle(args.staging_root, args.source_root,
                          args.prior_r2_evidence, args.double_export_evidence,
                          args.python_exe)
    gates, verdict = compute_gates(bundle)
    for k in sorted(gates):
        print("%s=%s" % (k, "true" if gates[k] else "false"))
    print("VERDICT=%s" % ("PASS" if verdict else "FAIL"))
    return evaluate(bundle, args.evidence_root)


if __name__ == "__main__":
    sys.exit(main())
