'''Portable GroupPack and legacy CompanyPack root discovery.'''

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

GROUP_ROOT_ENV = 'PRIVATE_AI_GROUP_ROOT'
GROUP_ROOT_MARKER = '.private-ai-group-root.json'
GROUP_DEFAULT_MANIFEST = 'group.manifest.json'
COMPANY_ROOT_ENV = 'PRIVATE_AI_COMPANY_ROOT'
COMPANY_ROOT_MARKER = '.private-ai-company-root.json'
COMPANY_DEFAULT_MANIFEST = 'company.manifest.json'

# Backward-compatible aliases for the phase-1 CompanyPack experiment.
ROOT_ENV = COMPANY_ROOT_ENV
ROOT_MARKER = COMPANY_ROOT_MARKER
DEFAULT_MANIFEST = COMPANY_DEFAULT_MANIFEST
REPARSE_POINT_ATTRIBUTE = 0x400

ROOT_FORMATS = {
    GROUP_ROOT_MARKER: ('private-ai-group-root', GROUP_DEFAULT_MANIFEST, 'group'),
    COMPANY_ROOT_MARKER: ('private-ai-company-root', COMPANY_DEFAULT_MANIFEST, 'company'),
}


class RootDiscoveryError(RuntimeError):
    '''Raised when a valid portable company root cannot be located.'''


class PortablePathError(ValueError):
    '''Raised when a manifest path violates the portable path contract.'''


@dataclass(frozen=True)
class RootResolution:
    path: Path
    source: str
    marker: dict[str, object]
    marker_name: str
    pack_kind: str

    def to_dict(self) -> dict[str, object]:
        return {
            'path': str(self.path),
            'source': self.source,
            'marker': self.marker,
            'marker_name': self.marker_name,
            'pack_kind': self.pack_kind,
        }


def load_json_object(path: Path) -> dict[str, object]:
    try:
        raw = path.read_text(encoding='utf-8')
    except OSError as exc:
        raise RootDiscoveryError(f'Cannot read {path}: {exc}') from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RootDiscoveryError(f'Invalid JSON in {path}: {exc}') from exc
    if not isinstance(value, dict):
        raise RootDiscoveryError(f'Expected a JSON object in {path}.')
    return value


def is_reparse_point(path: Path) -> bool:
    try:
        stat_result = path.stat()
    except OSError:
        return False
    attributes = getattr(stat_result, 'st_file_attributes', 0)
    return bool(attributes & REPARSE_POINT_ATTRIBUTE) or path.is_symlink()


def validate_portable_relative_path(value: object, field: str = 'path') -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortablePathError(f'{field} must be a non-empty string.')
    candidate = value.strip()
    if chr(92) in candidate:
        raise PortablePathError(f'{field} must use forward slashes: {candidate!r}')
    if ':' in candidate:
        raise PortablePathError(f'{field} must not contain a drive or URI: {candidate!r}')
    portable = PurePosixPath(candidate)
    if portable.is_absolute():
        raise PortablePathError(f'{field} must be relative: {candidate!r}')
    if any(part in {'', '.', '..'} for part in portable.parts):
        raise PortablePathError(f'{field} contains an unsafe segment: {candidate!r}')
    return portable.as_posix()


def resolve_portable_path(root: Path, relative: object, field: str = 'path') -> Path:
    normalized = validate_portable_relative_path(relative, field)
    root_resolved = root.resolve()
    candidate = (root_resolved / Path(*PurePosixPath(normalized).parts)).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise PortablePathError(f'{field} escapes the portable pack root: {normalized!r}') from exc
    return candidate


def _find_marker(candidate: Path) -> str:
    matches = [name for name in ROOT_FORMATS if (candidate / name).is_file()]
    if not matches:
        names = ' or '.join(ROOT_FORMATS)
        raise RootDiscoveryError(f'Missing root marker ({names}) in {candidate}')
    if len(matches) > 1:
        raise RootDiscoveryError(
            f'Ambiguous portable root contains multiple root markers: {matches}'
        )
    return matches[0]


def _validate_root(candidate: Path, source: str, marker_name: str | None = None) -> RootResolution:
    resolved = candidate.expanduser().resolve()
    if not resolved.is_dir():
        raise RootDiscoveryError(f'Portable root is not a directory: {resolved}')
    marker_name = marker_name or _find_marker(resolved)
    expected_format, default_manifest, pack_kind = ROOT_FORMATS[marker_name]
    marker_path = resolved / marker_name
    marker = load_json_object(marker_path)
    if marker.get('format') != expected_format:
        raise RootDiscoveryError(f'Unsupported root marker format in {marker_path}.')
    if marker.get('format_version') != '0.1':
        raise RootDiscoveryError(f'Unsupported root marker version in {marker_path}.')
    manifest_name = validate_portable_relative_path(
        marker.get('manifest', default_manifest), 'marker.manifest'
    )
    manifest_path = resolve_portable_path(resolved, manifest_name, 'marker.manifest')
    if not manifest_path.is_file():
        raise RootDiscoveryError(f'Missing {pack_kind} manifest: {manifest_path}')
    return RootResolution(
        path=resolved,
        source=source,
        marker=marker,
        marker_name=marker_name,
        pack_kind=pack_kind,
    )


def discover_root(
    start: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> RootResolution:
    env = os.environ if environ is None else environ
    configured_roots = [
        (GROUP_ROOT_ENV, GROUP_ROOT_MARKER),
        (COMPANY_ROOT_ENV, COMPANY_ROOT_MARKER),
    ]
    for env_name, marker_name in configured_roots:
        configured = env.get(env_name, '').strip()
        if configured:
            return _validate_root(
                Path(configured), f'environment:{env_name}', marker_name
            )

    current = Path.cwd() if start is None else Path(start)
    if current.is_file():
        current = current.parent
    current = current.expanduser().resolve()
    for candidate in (current, *current.parents):
        matches = [name for name in ROOT_FORMATS if (candidate / name).is_file()]
        if matches:
            return _validate_root(candidate, 'parent-search')
    raise RootDiscoveryError(
        f'No portable root marker found from {current} upward. '
        f'Set {GROUP_ROOT_ENV}, {COMPANY_ROOT_ENV}, or pass --root.'
    )


def resolve_explicit_root(path: Path | str) -> RootResolution:
    return _validate_root(Path(path), 'explicit')
