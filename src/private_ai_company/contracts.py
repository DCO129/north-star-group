'''CompanyPack and model-capability contract validation.'''

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .root import PortablePathError, resolve_portable_path, validate_portable_relative_path

COMPANY_SCHEMA_VERSION = 'company-pack/v0'
CAPABILITY_SCHEMA_VERSION = 'model-capability/v0'
COMPANY_ID_PATTERN = re.compile(r'^[a-z0-9][a-z0-9._-]{2,127}$')
REQUIRED_PATH_KEYS = (
    'organization', 'policies', 'knowledge', 'memory', 'skills', 'workflows',
    'tasks', 'checkpoints', 'evaluations', 'adapters', 'migrations', 'provenance',
)
TOOL_LEVELS = {'none', 'single', 'parallel'}
STRUCTURED_OUTPUT_LEVELS = {'none', 'json', 'json_schema', 'tested_subset'}
CERTIFICATION_STATUSES = {
    'untested', 'passed', 'passed_with_degradation', 'quarantined', 'failed',
}
SECRET_PREFIXES = ('env:', 'credential:', 'vault:')


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    location: str = ''

    def to_dict(self) -> dict[str, str]:
        return {
            'severity': self.severity,
            'code': self.code,
            'message': self.message,
            'location': self.location,
        }


def load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except OSError as exc:
        raise ValueError(f'Cannot read {path}: {exc}') from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f'Invalid JSON in {path}: {exc}') from exc
    if not isinstance(value, dict):
        raise ValueError(f'Expected a JSON object in {path}.')
    return value


def _require_object(
    value: object, location: str, findings: list[Finding]
) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    findings.append(Finding('error', 'expected_object', 'Expected an object.', location))
    return {}


def validate_company_manifest(data: dict[str, object]) -> list[Finding]:
    findings: list[Finding] = []
    if data.get('schema_version') != COMPANY_SCHEMA_VERSION:
        findings.append(Finding('error', 'company_schema_version', f'schema_version must be {COMPANY_SCHEMA_VERSION!r}.', 'schema_version'))
    company_id = data.get('company_id')
    if not isinstance(company_id, str) or not COMPANY_ID_PATTERN.fullmatch(company_id):
        findings.append(Finding('error', 'company_id', 'company_id must be a portable lowercase identifier.', 'company_id'))
    if not isinstance(data.get('name'), str) or not str(data.get('name', '')).strip():
        findings.append(Finding('error', 'company_name', 'name must be non-empty.', 'name'))
    runtime = _require_object(data.get('runtime'), 'runtime', findings)
    if not isinstance(runtime.get('minimum_version'), str):
        findings.append(Finding('error', 'runtime_version', 'runtime.minimum_version must be a string.', 'runtime.minimum_version'))

    paths = _require_object(data.get('paths'), 'paths', findings)
    normalized_paths: set[str] = set()
    for key in REQUIRED_PATH_KEYS:
        location = f'paths.{key}'
        if key not in paths:
            findings.append(Finding('error', 'missing_path', f'Missing {location}.', location))
            continue
        try:
            normalized = validate_portable_relative_path(paths[key], location)
        except PortablePathError as exc:
            findings.append(Finding('error', 'unsafe_path', str(exc), location))
            continue
        if normalized in normalized_paths:
            findings.append(Finding('error', 'duplicate_path', f'Duplicate path {normalized!r}.', location))
        normalized_paths.add(normalized)

    security = _require_object(data.get('security'), 'security', findings)
    if security.get('secrets') != 'external_only':
        findings.append(Finding('error', 'secrets_policy', 'security.secrets must be external_only.', 'security.secrets'))
    if security.get('allow_absolute_paths') is not False:
        findings.append(Finding('error', 'absolute_paths_policy', 'security.allow_absolute_paths must be false.', 'security.allow_absolute_paths'))
    continuity = _require_object(data.get('task_continuity'), 'task_continuity', findings)
    if continuity.get('provider_threads') != 'aliases_only':
        findings.append(Finding('error', 'provider_threads', 'Provider thread IDs may only be aliases.', 'task_continuity.provider_threads'))
    indexes = _require_object(data.get('indexes'), 'indexes', findings)
    if indexes.get('rebuildable') is not True:
        findings.append(Finding('error', 'rebuildable_indexes', 'indexes.rebuildable must be true.', 'indexes.rebuildable'))
    requirements = _require_object(data.get('model_requirements'), 'model_requirements', findings)
    required = _require_object(requirements.get('required'), 'model_requirements.required', findings)
    if required.get('text') is not True:
        findings.append(Finding('error', 'text_required', 'Text generation is the minimum required capability.', 'model_requirements.required.text'))
    return findings


def validate_company_layout(root: Path, data: dict[str, object]) -> list[Finding]:
    findings: list[Finding] = []
    paths = data.get('paths')
    if not isinstance(paths, dict):
        return findings
    for key in REQUIRED_PATH_KEYS:
        if key not in paths:
            continue
        try:
            target = resolve_portable_path(root, paths[key], f'paths.{key}')
        except PortablePathError:
            continue
        if not target.is_dir():
            findings.append(Finding('warning', 'missing_directory', f'Declared directory does not exist: {target}', f'paths.{key}'))
    return findings


def validate_capability_manifest(data: dict[str, object]) -> list[Finding]:
    findings: list[Finding] = []
    if data.get('schema_version') != CAPABILITY_SCHEMA_VERSION:
        findings.append(Finding('error', 'capability_schema_version', f'schema_version must be {CAPABILITY_SCHEMA_VERSION!r}.', 'schema_version'))
    for key in ('provider', 'model', 'driver', 'endpoint_ref'):
        if not isinstance(data.get(key), str) or not str(data.get(key, '')).strip():
            findings.append(Finding('error', 'required_string', f'{key} must be non-empty.', key))
    secret_refs = data.get('secret_refs', [])
    if not isinstance(secret_refs, list):
        findings.append(Finding('error', 'secret_refs', 'secret_refs must be an array.', 'secret_refs'))
    else:
        for index, secret_ref in enumerate(secret_refs):
            if not isinstance(secret_ref, str) or not secret_ref.startswith(SECRET_PREFIXES):
                findings.append(Finding('error', 'unsafe_secret_ref', 'Use env:, credential:, or vault: references only.', f'secret_refs[{index}]'))
    capabilities = _require_object(data.get('capabilities'), 'capabilities', findings)
    if capabilities.get('text') is not True:
        findings.append(Finding('error', 'text_capability', 'capabilities.text must be true.', 'capabilities.text'))
    if capabilities.get('tools') not in TOOL_LEVELS:
        findings.append(Finding('error', 'tools_capability', f'Unsupported tools value; use {sorted(TOOL_LEVELS)}.', 'capabilities.tools'))
    if capabilities.get('structured_output') not in STRUCTURED_OUTPUT_LEVELS:
        findings.append(Finding('error', 'structured_output', 'Unsupported structured_output value.', 'capabilities.structured_output'))
    context_tokens = capabilities.get('context_tokens')
    if not isinstance(context_tokens, int) or isinstance(context_tokens, bool) or context_tokens < 1024:
        findings.append(Finding('error', 'context_tokens', 'context_tokens must be an integer of at least 1024.', 'capabilities.context_tokens'))
    certification = _require_object(data.get('certification'), 'certification', findings)
    if certification.get('status') not in CERTIFICATION_STATUSES:
        findings.append(Finding('error', 'certification_status', f'Unsupported status; use {sorted(CERTIFICATION_STATUSES)}.', 'certification.status'))
    return findings


def has_errors(findings: Iterable[Finding]) -> bool:
    return any(finding.severity == 'error' for finding in findings)
