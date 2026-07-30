'''Unit tests for the G1-5 publication-candidate verifier gates.

These are pure, portable, and require no network or private repository. They
verify the fail-closed contract: a fully-valid bundle passes, and flipping any
single required gate fails closed.
'''
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent / "scripts") not in sys.path:
    sys.path.insert(0, str(_HERE.parent / "scripts"))

import verify_g1_5_publication_candidate as v  # noqa: E402


def valid_bundle():
    return {
        "license_apache": True,
        "license_file_complete": True,
        "no_stale_license_text": True,
        "governance_files_present": True,
        "readme_status_ok": True,
        "workflow_static_valid": True,
        "exporter_default_deny": True,
        "runner_byte_equal": True,
        "double_export_deterministic": True,
        "boundary_zero_violations": True,
        "smoke_compile_import_pass": True,
        "no_product_source_changed": True,
        "prior_evidence_unchanged": True,
        "real_model_calls_zero": True,
        "external_effects_false": True,
    }


class G1_5GateTest(unittest.TestCase):
    def test_all_true_is_pass(self):
        gates, verdict = v.compute_gates(valid_bundle())
        self.assertTrue(verdict)
        self.assertEqual(sum(1 for x in gates.values() if x), len(gates))

    def test_each_single_flip_fails(self):
        base = valid_bundle()
        for key in base:
            b = dict(base)
            b[key] = False
            gates, verdict = v.compute_gates(b)
            self.assertFalse(verdict, "flipping %s should fail closed" % key)
            self.assertFalse(gates[key])

    def test_evaluate_writes_fail_cert(self):
        b = valid_bundle()
        b["license_file_complete"] = False
        with tempfile.TemporaryDirectory() as td:
            rc = v.evaluate(b, Path(td))
            self.assertEqual(rc, 2)
            cert = (Path(td) / "g1-5-certificate.json").read_text(encoding="utf-8")
            self.assertIn("FAIL", cert)
            self.assertIn('"current_task_requires_codex": true', cert)

    def test_license_completeness(self):
        full = ("END OF TERMS AND CONDITIONS\n"
                + "\n".join(v.LICENSE_SECTION_HEADERS)
                + "\n" + ("x" * 1200))  # pad past the 1000-char anti-truncation floor
        self.assertTrue(v._license_complete(full))
        self.assertFalse(v._license_complete("short"))
        self.assertFalse(v._license_complete(
            "\n".join(v.LICENSE_SECTION_HEADERS)))  # missing END OF TERMS + too short

    def test_workflow_static_valid(self):
        good = (
            "permissions:\n  contents: read\n"
            "on:\n  push:\n  pull_request:\n  workflow_dispatch:\n"
            "jobs:\n  j:\n    runs-on: windows-latest\n"
            "    steps:\n      - run: python scripts/validate_public_alpha.py --staging-root .\n"
        )
        self.assertTrue(v._workflow_static_valid(good))
        # prohibited: git push
        self.assertFalse(v._workflow_static_valid(good + "    - run: git push origin\n"))
        # prohibited: model call
        self.assertFalse(v._workflow_static_valid(good + "    - run: call openai\n"))
        # prohibited: secrets
        self.assertFalse(v._workflow_static_valid(good + "  token: ${{ secrets.GH }}\n"))
        # missing read-only perms
        self.assertFalse(v._workflow_static_valid(
            "on:\n  push:\n  pull_request:\n  workflow_dispatch:\n"
            "jobs:\n  j:\n    runs-on: windows-latest\n"))

    def test_exporter_default_deny(self):
        self.assertTrue(v._exporter_default_deny({"mode": "default-deny", "allow": {"files": ["a/b.py"]}}))
        self.assertFalse(v._exporter_default_deny({"mode": "allowlist", "allow": {"files": []}}))
        self.assertFalse(v._exporter_default_deny({"mode": "default-deny", "allow": {"files": ["*"]}}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
