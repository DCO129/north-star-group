"""Isolated external evaluation runner and local acceptance artifact builder."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def build_acceptance_artifact(raw: dict, *, runner_version: str) -> dict:
    result_set = raw.get('results', {})
    rows = result_set.get('results', []) if isinstance(result_set, dict) else []
    cases = []
    for row in rows:
        variables = row.get('vars', {}) if isinstance(row, dict) else {}
        response = row.get('response', {}) if isinstance(row, dict) else {}
        try:
            observed = json.loads(str(response.get('output') or '{}'))
        except json.JSONDecodeError:
            observed = {}
        expected_reason = str(variables.get('expected_reason') or '')
        expected_verdict = str(variables.get('expected_verdict') or '')
        blocked_reasons = [
            str(item) for item in observed.get('blocked_reasons', [])
            if isinstance(item, str)
        ]
        locally_valid = (
            observed.get('schema_version') == 'business-acceptance/v0'
            and observed.get('verdict') == expected_verdict
            and (not expected_reason or expected_reason in blocked_reasons)
        )
        cases.append({
            'case_id': str(variables.get('case_id') or 'unknown'),
            'passed': bool(row.get('success')) and locally_valid,
            'expected_verdict': expected_verdict,
            'expected_blocked_reason': expected_reason or None,
            'observed_verdict': observed.get('verdict'),
            'observed_blocked_reasons': blocked_reasons,
        })
    stats = result_set.get('stats', {}) if isinstance(result_set, dict) else {}
    passed = sum(1 for case in cases if case['passed'])
    total = len(cases)
    accepted = total > 0 and passed == total and int(stats.get('errors', 0) or 0) == 0
    return {
        'schema_version': 'external-evaluation-acceptance/v0',
        'generated_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        'runner': {
            'name': 'promptfoo', 'version': runner_version,
            'authority': 'external-runner-only',
        },
        'suite_id': 'audit-attack-gate-v0',
        'status': 'passed' if accepted else 'failed',
        'summary': {
            'total': total, 'passed': passed, 'failed': total - passed,
            'duration_ms': int(stats.get('durationMs', 0) or 0),
            'token_usage_total': int(stats.get('tokenUsage', {}).get('total', 0) or 0),
        },
        'cases': cases,
        'local_acceptance': {
            'accepted': accepted,
            'rule': 'all sanitized cases must match local business-acceptance/v0',
            'raw_prompt_or_output_persisted': False,
        },
    }


def run_promptfoo_suite(runtime_root: Path) -> Path:
    runtime_root = runtime_root.resolve()
    source = runtime_root / 'evaluations' / 'promptfoo'
    package = json.loads((source / 'node_modules' / 'promptfoo' / 'package.json').read_text(encoding='utf-8'))
    entrypoint = source / 'node_modules' / 'promptfoo' / package['bin']['promptfoo']
    output = runtime_root / 'examples' / 'minimal-group' / 'evaluations' / 'promptfoo-audit.acceptance.json'
    with tempfile.TemporaryDirectory(prefix='private-ai-company-eval-') as directory:
        isolated = Path(directory)
        for name in ('promptfooconfig.json', 'provider.py', 'cases.json'):
            shutil.copy2(source / name, isolated / name)
        raw_path = isolated / 'raw.json'
        env = os.environ.copy()
        env.update({
            'PRIVATE_AI_RUNTIME_ROOT': str(runtime_root),
            'PROMPTFOO_DISABLE_TELEMETRY': '1',
            'PROMPTFOO_DISABLE_UPDATE': '1',
            'NO_COLOR': '1',
        })
        completed = subprocess.run(
            [
                'node', str(entrypoint), 'eval', '--config', 'promptfooconfig.json',
                '--no-cache', '--output', str(raw_path),
            ],
            cwd=isolated, env=env, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120, check=False,
        )
        if not raw_path.is_file():
            raise RuntimeError(
                f'Promptfoo did not produce JSON output (exit {completed.returncode}): '
                f'{completed.stderr[-800:]}'
            )
        raw = json.loads(raw_path.read_text(encoding='utf-8'))
        artifact = build_acceptance_artifact(raw, runner_version=str(package['version']))
        artifact['runner']['exit_code'] = completed.returncode
        if completed.returncode != 0:
            artifact['status'] = 'failed'
            artifact['local_acceptance']['accepted'] = False
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
        )
    return output
