'''Read-only host, GroupPack, legacy CompanyPack, and model diagnostics.'''

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .contracts import (
    COMPANY_SCHEMA_VERSION, Finding, has_errors, load_json, validate_capability_manifest,
    validate_company_layout, validate_company_manifest,
)
from .organization import (
    GROUP_SCHEMA_VERSION, load_organization_graph, validate_organization_layout,
)
from .root import (
    PortablePathError, RootDiscoveryError, discover_root, is_reparse_point,
    resolve_explicit_root, resolve_portable_path,
)


@dataclass(frozen=True)
class DoctorReport:
    root: str | None
    checks: list[Finding]

    @property
    def healthy(self) -> bool:
        return not has_errors(self.checks)

    def to_dict(self) -> dict[str, object]:
        counts = {severity: sum(1 for item in self.checks if item.severity == severity) for severity in ('pass', 'warning', 'error')}
        return {'healthy': self.healthy, 'root': self.root, 'summary': counts, 'checks': [item.to_dict() for item in self.checks]}


def _check_secret_refs(data: dict[str, object], env: Mapping[str, str]) -> list[Finding]:
    findings: list[Finding] = []
    refs = data.get('secret_refs', [])
    if not isinstance(refs, list):
        return findings
    for ref in refs:
        if isinstance(ref, str) and ref.startswith('env:'):
            name = ref.removeprefix('env:')
            severity = 'pass' if env.get(name) else 'warning'
            message = f'Environment secret reference {name} is available.' if severity == 'pass' else f'Environment secret reference {name} is not configured.'
            findings.append(Finding(severity, 'secret_reference', message, ref))
    return findings


def run_doctor(
    explicit_root: Path | str | None = None,
    start: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> DoctorReport:
    env = os.environ if environ is None else environ
    checks: list[Finding] = []
    try:
        resolution = resolve_explicit_root(explicit_root) if explicit_root else discover_root(start, env)
    except RootDiscoveryError as exc:
        return DoctorReport(None, [Finding('error', 'root_discovery', str(exc), 'root')])
    root = resolution.path
    checks.append(Finding('pass', 'root_discovery', f'Root resolved by {resolution.source}.', str(root)))
    if is_reparse_point(root):
        checks.append(Finding('error', 'root_reparse_point', 'Portable root must not be a symlink or reparse point.', str(root)))
    else:
        checks.append(Finding('pass', 'root_boundary', 'Portable root is a physical directory.', str(root)))
    if platform.system() == 'Windows':
        checks.append(Finding('pass', 'host_platform', 'Native Windows host detected.', platform.platform()))
    else:
        checks.append(Finding('warning', 'host_platform', 'Phase 1 targets native Windows.', platform.platform()))
    if sys.version_info >= (3, 11):
        checks.append(Finding('pass', 'python_version', f'Python {platform.python_version()} is supported.', sys.executable))
    else:
        checks.append(Finding('error', 'python_version', 'Python 3.11 or newer is required.', sys.executable))
    free = shutil.disk_usage(root).free
    severity = 'pass' if free >= 1024 ** 3 else 'warning'
    checks.append(Finding(severity, 'disk_space', f'{free} free bytes are available.', str(root)))
    git = shutil.which('git')
    checks.append(Finding('pass' if git else 'warning', 'git_available', f'Git found at {git}.' if git else 'Git is not available.', git or 'PATH'))

    default_manifest = 'group.manifest.json' if resolution.pack_kind == 'group' else 'company.manifest.json'
    manifest_name = resolution.marker.get('manifest', default_manifest)
    manifest_path = resolve_portable_path(root, manifest_name, 'marker.manifest')
    try:
        manifest = load_json(manifest_path)
    except ValueError as exc:
        checks.append(Finding('error', 'manifest_load', str(exc), str(manifest_path)))
        return DoctorReport(str(root), checks)
    schema_version = manifest.get('schema_version')
    expected_schema = (
        GROUP_SCHEMA_VERSION
        if resolution.pack_kind == 'group'
        else COMPANY_SCHEMA_VERSION
    )
    if schema_version != expected_schema:
        checks.append(Finding(
            'error', 'root_manifest_mismatch',
            f'{resolution.pack_kind} root marker requires schema {expected_schema!r}, '
            f'not {schema_version!r}.', str(manifest_path),
        ))
    elif schema_version == GROUP_SCHEMA_VERSION:
        graph, manifest_findings = load_organization_graph(root, manifest)
        checks.extend(manifest_findings)
        if not has_errors(manifest_findings):
            checks.append(Finding(
                'pass', 'manifest_contract',
                'GroupPack organization contract passed.', str(manifest_path),
            ))
            checks.extend(validate_organization_layout(graph))
    elif schema_version == COMPANY_SCHEMA_VERSION:
        manifest_findings = validate_company_manifest(manifest)
        checks.extend(manifest_findings)
        if not has_errors(manifest_findings):
            checks.append(Finding(
                'pass', 'manifest_contract',
                'Legacy CompanyPack manifest contract passed.', str(manifest_path),
            ))
            checks.extend(validate_company_layout(root, manifest))

    paths = manifest.get('paths', {})
    adapters = paths.get('adapters') if isinstance(paths, dict) else None
    if isinstance(adapters, str):
        try:
            adapter_root = resolve_portable_path(root, adapters, 'paths.adapters')
        except PortablePathError as exc:
            checks.append(Finding(
                'error', 'adapter_path', str(exc), 'paths.adapters',
            ))
            return DoctorReport(str(root), checks)
        files = sorted(adapter_root.glob('models/*.capabilities.json')) if adapter_root.is_dir() else []
        if not files:
            checks.append(Finding('warning', 'capability_manifests', 'No model capability manifests were found.', str(adapter_root)))
        for capability_path in files:
            try:
                capability = load_json(capability_path)
            except ValueError as exc:
                checks.append(Finding('error', 'capability_load', str(exc), str(capability_path)))
                continue
            capability_findings = validate_capability_manifest(capability)
            checks.extend(capability_findings)
            if not has_errors(capability_findings):
                checks.append(Finding('pass', 'capability_contract', 'Model capability contract passed.', str(capability_path)))
                checks.extend(_check_secret_refs(capability, env))
    return DoctorReport(str(root), checks)
