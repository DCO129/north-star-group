'''Portable CI-contract test for the public-alpha export/boundary pipeline.

This test is safe to run inside a GitHub clone (no absolute machine paths, no
private-repository dependencies). It exercises the deterministic default-deny
exporter and the fail-closed validator against a throwaway staging directory and
asserts that the public-boundary contract holds with zero violations.

It is the portable replacement for the private-only
``tests/test_public_alpha_export.py`` (which references a hardcoded private
working tree and is intentionally NOT exported to the public candidate).
'''
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Make the sibling scripts/ directory importable regardless of CWD.
_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import export_public_alpha  # noqa: E402
import validate_public_alpha  # noqa: E402


class PublicAlphaCiContractTest(unittest.TestCase):
    def setUp(self):
        self.source_root = _HERE.parent
        self.staging_root = Path(tempfile.mkdtemp(prefix="nsg-ci-"))

    def tearDown(self):
        shutil.rmtree(self.staging_root, ignore_errors=True)

    def test_export_then_validate_zero_violations(self):
        # 1) Deterministic default-deny export into a throwaway staging dir.
        manifest = export_public_alpha.run_export(
            str(self.source_root), str(self.staging_root)
        )
        self.assertGreater(manifest["file_count"], 0)
        self.assertTrue((self.staging_root / "EXPORT_MANIFEST.json").is_file())
        self.assertTrue((self.staging_root / "LICENSE").is_file())

        # 2) Fail-closed boundary validation must pass with zero hits.
        passed, failures, counts = validate_public_alpha.validate(self.staging_root)
        self.assertTrue(passed, "boundary validation failed: %r" % failures)
        self.assertEqual(counts["secret"], 0)
        self.assertEqual(counts["private_path"], 0)
        self.assertEqual(counts["machine_path"], 0)
        self.assertEqual(counts["prohibited_file"], 0)
        self.assertEqual(counts["symlink_escape"], 0)
        self.assertEqual(counts["traversal"], 0)
        self.assertEqual(counts["missing_module"], 0)
        self.assertEqual(counts["hash_mismatch"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
