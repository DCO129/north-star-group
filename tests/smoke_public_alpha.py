'''Public-alpha smoke test — standalone, no private exporter dependency.

Proves the public candidate works like an outsider clone:

* the ``private_ai_company`` package imports from a clean install,
* the documented CLI quickstart runs and exits 0 on the synthetic group,
* the local CEO API answers health/status with zero real-model calls.

Run from the installed/clean-room copy::

    python -m pytest tests/smoke_public_alpha.py -q
    # or
    python tests/smoke_public_alpha.py
'''
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "quickstart-group"


class PublicCandidateSmokeTests(unittest.TestCase):
    def test_package_imports(self):
        import private_ai_company  # noqa: F401  (import success is the assertion)

    def test_example_present(self):
        self.assertTrue(EXAMPLE.is_dir(), "synthetic example missing from staging")
        self.assertTrue((EXAMPLE / "group.manifest.json").is_file())

    def _run_cli(self, *args):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        return subprocess.run(
            [sys.executable, "-m", "private_ai_company", *args],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )

    def test_cli_readonly_quickstart(self):
        for args in (
            ("validate-group", "--root", str(EXAMPLE)),
            ("organization-tree", "--root", str(EXAMPLE)),
            ("route-department", "research", "--root", str(EXAMPLE), "--json"),
        ):
            proc = self._run_cli(*args)
            self.assertEqual(
                proc.returncode, 0,
                f"CLI {args} failed (rc={proc.returncode}):\n"
                f"STDOUT:{proc.stdout}\nSTDERR:{proc.stderr}",
            )

    def test_cli_run_task_model_free(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "group"
            shutil.copytree(EXAMPLE, work)
            proc = self._run_cli(
                "run-task", "--root", str(work),
                "--task-id", "task-local-001",
                "--instruction", "Prepare the baseline.",
                "--capability", "research", "--capability", "task-orchestration",
                "--criterion", "Evidence and artifacts are registered.",
            )
            self.assertEqual(
                proc.returncode, 0,
                f"run-task failed (rc={proc.returncode}):\n"
                f"STDOUT:{proc.stdout}\nSTDERR:{proc.stderr}",
            )

    def test_local_api_health(self):
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except ImportError:
            self.skipTest("fastapi test client unavailable")
        from private_ai_company.ceo_api import create_runtime_app
        from unittest.mock import patch
        # Force model-free deterministic mode for the local API smoke, bounded
        # by a context manager so the parent environment is always restored,
        # even if app creation or a request raises. App creation and the
        # TestClient lifetime live inside this context.
        with patch.dict(os.environ, {"N1_3_DETERMINISTIC": "1"}):
            with tempfile.TemporaryDirectory() as td:
                work = Path(td) / "group"
                shutil.copytree(EXAMPLE, work)
                app = create_runtime_app(Path(work))
                with TestClient(app) as client:
                    self.assertEqual(client.get("/health").status_code, 200)
                    self.assertEqual(client.get("/api/status").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
