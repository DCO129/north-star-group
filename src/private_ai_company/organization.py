'''GroupPack organization contracts, loading, inheritance, and routing.'''

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .contracts import Finding, has_errors, load_json
from .root import PortablePathError, resolve_portable_path, validate_portable_relative_path

GROUP_SCHEMA_VERSION = 'group-pack/v0'
EXECUTIVE_SCHEMA_VERSION = 'executive-pack/v0'
SUBSIDIARY_SCHEMA_VERSION = 'subsidiary-pack/v0'
DEPARTMENT_SCHEMA_VERSION = 'department-pack/v0'
IDENTIFIER_PATTERN = re.compile(r'^[a-z0-9][a-z0-9._-]{2,127}$')
GROUP_PATH_KEYS = (
    'governance', 'shared', 'subsidiaries', 'tasks', 'checkpoints',
    'evaluations', 'adapters', 'migrations', 'provenance', 'audit',
)
SUBSIDIARY_PATH_KEYS = ('knowledge', 'memory', 'policies', 'departments')
DEPARTMENT_PATH_KEYS = ('skills', 'workflows', 'evaluations')
DEPARTMENT_STATUSES = {'active', 'dormant'}
PERMISSION_LEVELS = {'L0', 'L1', 'L2', 'L3', 'L4'}
EXECUTIVE_REQUIRED_CAPABILITIES = {
    'human-intake', 'task-decomposition', 'organization-routing',
    'result-aggregation', 'verification', 'human-reporting',
}


def _require_object(
    value: object, location: str, findings: list[Finding]
) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    findings.append(Finding('error', 'expected_object', 'Expected an object.', location))
    return {}


def _validate_identifier(
    value: object, location: str, findings: list[Finding]
) -> str | None:
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        findings.append(Finding(
            'error', 'invalid_identifier',
            f'{location} must be a portable lowercase identifier.', location,
        ))
        return None
    return value


def _validate_name(data: dict[str, object], findings: list[Finding]) -> None:
    if not isinstance(data.get('name'), str) or not str(data.get('name', '')).strip():
        findings.append(Finding('error', 'name', 'name must be non-empty.', 'name'))


def _validate_paths(
    data: dict[str, object], required: Iterable[str], findings: list[Finding]
) -> None:
    paths = _require_object(data.get('paths'), 'paths', findings)
    normalized_paths: set[str] = set()
    for key in required:
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
            findings.append(Finding(
                'error', 'duplicate_path', f'Duplicate path {normalized!r}.', location,
            ))
        normalized_paths.add(normalized)


def _validate_model_requirements(
    data: dict[str, object], findings: list[Finding]
) -> None:
    requirements = _require_object(
        data.get('model_requirements'), 'model_requirements', findings
    )
    required = _require_object(
        requirements.get('required'), 'model_requirements.required', findings
    )
    if required.get('text') is not True:
        findings.append(Finding(
            'error', 'text_required',
            'Text generation is the minimum required capability.',
            'model_requirements.required.text',
        ))
    optional = requirements.get('optional', [])
    if not isinstance(optional, list) or not all(isinstance(item, str) for item in optional):
        findings.append(Finding(
            'error', 'optional_requirements',
            'model_requirements.optional must be an array of strings.',
            'model_requirements.optional',
        ))


def _validate_registry(
    value: object, collection: str, id_key: str, findings: list[Finding]
) -> None:
    if not isinstance(value, list):
        findings.append(Finding(
            'error', 'expected_array', f'{collection} must be an array.', collection,
        ))
        return
    seen_ids: set[str] = set()
    seen_manifests: set[str] = set()
    for index, entry in enumerate(value):
        location = f'{collection}[{index}]'
        if not isinstance(entry, dict):
            findings.append(Finding('error', 'expected_object', 'Expected an object.', location))
            continue
        item_id = _validate_identifier(entry.get(id_key), f'{location}.{id_key}', findings)
        try:
            manifest = validate_portable_relative_path(
                entry.get('manifest'), f'{location}.manifest'
            )
        except PortablePathError as exc:
            findings.append(Finding('error', 'unsafe_path', str(exc), f'{location}.manifest'))
            manifest = None
        if item_id in seen_ids:
            findings.append(Finding(
                'error', f'duplicate_{id_key}', f'Duplicate {id_key} {item_id!r}.', location,
            ))
        if manifest in seen_manifests:
            findings.append(Finding(
                'error', 'duplicate_manifest', f'Duplicate manifest {manifest!r}.', location,
            ))
        if item_id:
            seen_ids.add(item_id)
        if manifest:
            seen_manifests.add(manifest)


def _validate_executive_reference(
    value: object, findings: list[Finding]
) -> None:
    executive = _require_object(value, 'executive', findings)
    _validate_identifier(
        executive.get('executive_id'), 'executive.executive_id', findings
    )
    try:
        validate_portable_relative_path(
            executive.get('manifest'), 'executive.manifest'
        )
    except PortablePathError as exc:
        findings.append(Finding(
            'error', 'unsafe_path', str(exc), 'executive.manifest'
        ))


def validate_group_manifest(data: dict[str, object]) -> list[Finding]:
    findings: list[Finding] = []
    if data.get('schema_version') != GROUP_SCHEMA_VERSION:
        findings.append(Finding(
            'error', 'group_schema_version',
            f'schema_version must be {GROUP_SCHEMA_VERSION!r}.', 'schema_version',
        ))
    _validate_identifier(data.get('group_id'), 'group_id', findings)
    _validate_name(data, findings)
    runtime = _require_object(data.get('runtime'), 'runtime', findings)
    if not isinstance(runtime.get('minimum_version'), str):
        findings.append(Finding(
            'error', 'runtime_version',
            'runtime.minimum_version must be a string.', 'runtime.minimum_version',
        ))
    _validate_paths(data, GROUP_PATH_KEYS, findings)
    _validate_executive_reference(data.get('executive'), findings)
    _validate_registry(data.get('subsidiaries'), 'subsidiaries', 'subsidiary_id', findings)
    governance = _require_object(data.get('governance'), 'governance', findings)
    if governance.get('decision_authority') != 'group':
        findings.append(Finding(
            'error', 'decision_authority',
            'governance.decision_authority must be group.',
            'governance.decision_authority',
        ))
    if governance.get('operational_authority') != 'executive':
        findings.append(Finding(
            'error', 'operational_authority',
            'governance.operational_authority must be executive.',
            'governance.operational_authority',
        ))
    if governance.get('human_gateway') != 'executive_only':
        findings.append(Finding(
            'error', 'human_gateway',
            'governance.human_gateway must be executive_only.',
            'governance.human_gateway',
        ))
    security = _require_object(data.get('security'), 'security', findings)
    if security.get('secrets') != 'external_only':
        findings.append(Finding(
            'error', 'secrets_policy',
            'security.secrets must be external_only.', 'security.secrets',
        ))
    if security.get('allow_absolute_paths') is not False:
        findings.append(Finding(
            'error', 'absolute_paths_policy',
            'security.allow_absolute_paths must be false.',
            'security.allow_absolute_paths',
        ))
    continuity = _require_object(data.get('task_continuity'), 'task_continuity', findings)
    if continuity.get('provider_threads') != 'aliases_only':
        findings.append(Finding(
            'error', 'provider_threads', 'Provider thread IDs may only be aliases.',
            'task_continuity.provider_threads',
        ))
    indexes = _require_object(data.get('indexes'), 'indexes', findings)
    if indexes.get('rebuildable') is not True:
        findings.append(Finding(
            'error', 'rebuildable_indexes', 'indexes.rebuildable must be true.',
            'indexes.rebuildable',
        ))
    _validate_model_requirements(data, findings)
    return findings


def validate_executive_manifest(data: dict[str, object]) -> list[Finding]:
    findings: list[Finding] = []
    if data.get('schema_version') != EXECUTIVE_SCHEMA_VERSION:
        findings.append(Finding(
            'error', 'executive_schema_version',
            f'schema_version must be {EXECUTIVE_SCHEMA_VERSION!r}.',
            'schema_version',
        ))
    _validate_identifier(data.get('executive_id'), 'executive_id', findings)
    _validate_identifier(data.get('group_id'), 'group_id', findings)
    if data.get('role') != 'ceo':
        findings.append(Finding(
            'error', 'executive_role', 'Executive role must be ceo.', 'role',
        ))
    if data.get('status') != 'active':
        findings.append(Finding(
            'error', 'executive_status',
            'The registered group executive must be active.', 'status',
        ))
    if data.get('human_gateway') != 'exclusive':
        findings.append(Finding(
            'error', 'executive_human_gateway',
            'CEO human_gateway must be exclusive.', 'human_gateway',
        ))
    capabilities = data.get('capabilities')
    if not isinstance(capabilities, list) or not all(
        isinstance(item, str) and IDENTIFIER_PATTERN.fullmatch(item)
        for item in capabilities
    ):
        findings.append(Finding(
            'error', 'executive_capabilities',
            'Executive capabilities must be portable lowercase identifiers.',
            'capabilities',
        ))
    else:
        missing = EXECUTIVE_REQUIRED_CAPABILITIES - set(capabilities)
        if missing:
            findings.append(Finding(
                'error', 'executive_capabilities',
                f'CEO is missing required capabilities: {sorted(missing)}.',
                'capabilities',
            ))
        if len(set(capabilities)) != len(capabilities):
            findings.append(Finding(
                'error', 'duplicate_capability',
                'Executive capabilities must be unique.', 'capabilities',
            ))
    _validate_model_requirements(data, findings)
    return findings


def validate_subsidiary_manifest(data: dict[str, object]) -> list[Finding]:
    findings: list[Finding] = []
    if data.get('schema_version') != SUBSIDIARY_SCHEMA_VERSION:
        findings.append(Finding(
            'error', 'subsidiary_schema_version',
            f'schema_version must be {SUBSIDIARY_SCHEMA_VERSION!r}.',
            'schema_version',
        ))
    _validate_identifier(data.get('subsidiary_id'), 'subsidiary_id', findings)
    _validate_identifier(data.get('group_id'), 'group_id', findings)
    _validate_name(data, findings)
    _validate_paths(data, SUBSIDIARY_PATH_KEYS, findings)
    _validate_registry(data.get('departments'), 'departments', 'department_id', findings)
    _validate_model_requirements(data, findings)
    return findings


def validate_department_manifest(data: dict[str, object]) -> list[Finding]:
    findings: list[Finding] = []
    if data.get('schema_version') != DEPARTMENT_SCHEMA_VERSION:
        findings.append(Finding(
            'error', 'department_schema_version',
            f'schema_version must be {DEPARTMENT_SCHEMA_VERSION!r}.',
            'schema_version',
        ))
    _validate_identifier(data.get('department_id'), 'department_id', findings)
    _validate_identifier(data.get('subsidiary_id'), 'subsidiary_id', findings)
    _validate_identifier(data.get('group_id'), 'group_id', findings)
    _validate_name(data, findings)
    if not isinstance(data.get('mission'), str):
        findings.append(Finding('error', 'mission', 'mission must be a string.', 'mission'))
    if data.get('status', 'active') not in DEPARTMENT_STATUSES:
        findings.append(Finding(
            'error', 'department_status',
            f'status must be one of {sorted(DEPARTMENT_STATUSES)}.', 'status',
        ))
    capabilities = data.get('capabilities')
    if not isinstance(capabilities, list) or not capabilities:
        findings.append(Finding(
            'error', 'capabilities',
            'capabilities must be a non-empty array.', 'capabilities',
        ))
    elif not all(
        isinstance(item, str) and IDENTIFIER_PATTERN.fullmatch(item)
        for item in capabilities
    ):
        findings.append(Finding(
            'error', 'capabilities',
            'Each capability must be a portable lowercase identifier.',
            'capabilities',
        ))
    elif len(set(capabilities)) != len(capabilities):
        findings.append(Finding(
            'error', 'duplicate_capability',
            'Department capabilities must be unique.', 'capabilities',
        ))
    _validate_paths(data, DEPARTMENT_PATH_KEYS, findings)
    execution_policy = data.get('execution_policy')
    if execution_policy is not None and not isinstance(execution_policy, dict):
        findings.append(Finding(
            'error', 'execution_policy',
            'execution_policy must be an object when supplied.', 'execution_policy',
        ))
    elif isinstance(execution_policy, dict):
        action = execution_policy.get('action')
        if not isinstance(action, str) or not IDENTIFIER_PATTERN.fullmatch(action):
            findings.append(Finding(
                'error', 'execution_policy_action',
                'execution_policy.action must be a portable identifier.',
                'execution_policy.action',
            ))
        if execution_policy.get('level') not in PERMISSION_LEVELS:
            findings.append(Finding(
                'error', 'execution_policy_level',
                f'execution_policy.level must be one of {sorted(PERMISSION_LEVELS)}.',
                'execution_policy.level',
            ))
        resource = execution_policy.get('resource')
        if not isinstance(resource, str) or not resource.startswith('group://'):
            findings.append(Finding(
                'error', 'execution_policy_resource',
                'execution_policy.resource must use a group:// URI.',
                'execution_policy.resource',
            ))
        for key in ('reversible', 'external_effect'):
            if not isinstance(execution_policy.get(key), bool):
                findings.append(Finding(
                    'error', 'execution_policy_flag',
                    f'execution_policy.{key} must be boolean.',
                    f'execution_policy.{key}',
                ))
        budget_category = execution_policy.get('budget_category')
        if budget_category is not None and (
            not isinstance(budget_category, str)
            or not IDENTIFIER_PATTERN.fullmatch(budget_category)
        ):
            findings.append(Finding(
                'error', 'execution_policy_budget_category',
                'execution_policy.budget_category must be a portable identifier.',
                'execution_policy.budget_category',
            ))
    _validate_model_requirements(data, findings)
    return findings


@dataclass(frozen=True)
class OrganizationGraph:
    root: Path
    group: dict[str, object]
    executive: dict[str, object]
    subsidiaries: dict[str, dict[str, object]]
    departments: dict[str, dict[str, object]]

    def lineage(self, department_id: str) -> tuple[str, str, str]:
        department = self.departments[department_id]
        return (
            str(self.group['group_id']),
            str(department['subsidiary_id']),
            department_id,
        )

    def effective_model_requirements(self, department_id: str) -> dict[str, object]:
        department = self.departments[department_id]
        subsidiary = self.subsidiaries[str(department['subsidiary_id'])]
        required: dict[str, object] = {}
        optional: list[str] = []
        for manifest in (self.group, subsidiary, department):
            requirements = manifest.get('model_requirements', {})
            if not isinstance(requirements, dict):
                continue
            level_required = requirements.get('required', {})
            if isinstance(level_required, dict):
                required.update(level_required)
            level_optional = requirements.get('optional', [])
            if isinstance(level_optional, list):
                optional.extend(
                    item for item in level_optional
                    if isinstance(item, str) and item not in optional
                )
        return {'required': required, 'optional': optional}

    def route(self, capability: str) -> list[dict[str, object]]:
        normalized = capability.strip().lower()
        routes = []
        for department_id, department in sorted(self.departments.items()):
            if department.get('status', 'active') != 'active':
                continue
            capabilities = department.get('capabilities', [])
            if normalized not in capabilities:
                continue
            group_id, subsidiary_id, _ = self.lineage(department_id)
            routes.append({
                'group_id': group_id,
                'subsidiary_id': subsidiary_id,
                'department_id': department_id,
                'name': department.get('name'),
                'capability': normalized,
                'model_requirements': self.effective_model_requirements(department_id),
            })
        return routes

    def to_dict(self) -> dict[str, object]:
        subsidiaries = []
        for subsidiary_id, subsidiary in sorted(self.subsidiaries.items()):
            departments = []
            for department_id, department in sorted(self.departments.items()):
                if department.get('subsidiary_id') == subsidiary_id:
                    departments.append({
                        'department_id': department_id,
                        'name': department.get('name'),
                        'status': department.get('status', 'active'),
                        'capabilities': department.get('capabilities', []),
                    })
            subsidiaries.append({
                'subsidiary_id': subsidiary_id,
                'name': subsidiary.get('name'),
                'departments': departments,
            })
        return {
            'group_id': self.group.get('group_id'),
            'name': self.group.get('name'),
            'executive': {
                'executive_id': self.executive.get('executive_id'),
                'role': self.executive.get('role'),
                'human_gateway': self.executive.get('human_gateway'),
            },
            'subsidiaries': subsidiaries,
        }


def _prefixed(findings: Iterable[Finding], prefix: str) -> list[Finding]:
    return [
        Finding(item.severity, item.code, item.message, f'{prefix}:{item.location}')
        for item in findings
    ]


def _load_manifest(
    root: Path, relative: object, location: str, findings: list[Finding]
) -> tuple[Path | None, dict[str, object] | None]:
    try:
        path = resolve_portable_path(root, relative, location)
    except PortablePathError as exc:
        findings.append(Finding('error', 'unsafe_path', str(exc), location))
        return None, None
    try:
        return path, load_json(path)
    except ValueError as exc:
        findings.append(Finding('error', 'manifest_load', str(exc), str(path)))
        return path, None


def _require_containment(
    path: Path, parent: Path | None, code: str, message: str,
    findings: list[Finding],
) -> None:
    if parent is None:
        return
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        findings.append(Finding('error', code, message, str(path)))


def load_organization_graph(
    root: Path, group_manifest: dict[str, object]
) -> tuple[OrganizationGraph, list[Finding]]:
    root = root.resolve()
    findings = validate_group_manifest(group_manifest)
    executive: dict[str, object] = {}
    subsidiaries: dict[str, dict[str, object]] = {}
    departments: dict[str, dict[str, object]] = {}
    registered_subsidiary_paths: set[Path] = set()
    registered_department_paths: set[Path] = set()
    group_id = group_manifest.get('group_id')
    group_paths = group_manifest.get('paths', {})
    group_governance = (
        group_paths.get('governance') if isinstance(group_paths, dict) else None
    )
    try:
        governance_root = (
            resolve_portable_path(root, group_governance, 'paths.governance')
            if isinstance(group_governance, str) else None
        )
    except PortablePathError:
        governance_root = None
    registered_executive_path: Path | None = None
    executive_reference = group_manifest.get('executive')
    if isinstance(executive_reference, dict):
        executive_path, executive_manifest = _load_manifest(
            root, executive_reference.get('manifest'),
            'executive.manifest', findings,
        )
        if executive_path:
            registered_executive_path = executive_path.resolve()
            _require_containment(
                executive_path, governance_root, 'executive_location',
                'Executive manifest must be inside paths.governance.',
                findings,
            )
        if executive_manifest is not None:
            findings.extend(_prefixed(
                validate_executive_manifest(executive_manifest),
                str(executive_path),
            ))
            expected_executive_id = executive_reference.get('executive_id')
            actual_executive_id = executive_manifest.get('executive_id')
            if actual_executive_id != expected_executive_id:
                findings.append(Finding(
                    'error', 'executive_registry_mismatch',
                    f'Registry ID {expected_executive_id!r} does not match '
                    f'manifest ID {actual_executive_id!r}.',
                    str(executive_path),
                ))
            if executive_manifest.get('group_id') != group_id:
                findings.append(Finding(
                    'error', 'executive_group_mismatch',
                    f'Executive must belong to group {group_id!r}.',
                    str(executive_path),
                ))
            executive = executive_manifest
    group_subsidiaries = (
        group_paths.get('subsidiaries') if isinstance(group_paths, dict) else None
    )
    try:
        subsidiaries_root = (
            resolve_portable_path(root, group_subsidiaries, 'paths.subsidiaries')
            if isinstance(group_subsidiaries, str) else None
        )
    except PortablePathError:
        subsidiaries_root = None

    registry = group_manifest.get('subsidiaries', [])
    if isinstance(registry, list):
        for index, entry in enumerate(registry):
            if not isinstance(entry, dict):
                continue
            path, subsidiary = _load_manifest(
                root, entry.get('manifest'),
                f'subsidiaries[{index}].manifest', findings,
            )
            if path:
                registered_subsidiary_paths.add(path)
                _require_containment(
                    path, subsidiaries_root, 'subsidiary_location',
                    'Subsidiary manifest must be inside paths.subsidiaries.',
                    findings,
                )
            if subsidiary is None:
                continue
            findings.extend(_prefixed(
                validate_subsidiary_manifest(subsidiary), str(path)
            ))
            expected_id = entry.get('subsidiary_id')
            actual_id = subsidiary.get('subsidiary_id')
            if actual_id != expected_id:
                findings.append(Finding(
                    'error', 'subsidiary_registry_mismatch',
                    f'Registry ID {expected_id!r} does not match manifest ID {actual_id!r}.',
                    str(path),
                ))
            if subsidiary.get('group_id') != group_id:
                findings.append(Finding(
                    'error', 'subsidiary_parent_mismatch',
                    f'Subsidiary must belong to group {group_id!r}.', str(path),
                ))
            if not isinstance(actual_id, str):
                continue
            if actual_id in subsidiaries:
                findings.append(Finding(
                    'error', 'duplicate_subsidiary_id',
                    f'Duplicate subsidiary_id {actual_id!r}.', str(path),
                ))
                continue
            subsidiaries[actual_id] = subsidiary

            department_registry = subsidiary.get('departments', [])
            subsidiary_paths = subsidiary.get('paths', {})
            departments_relative = (
                subsidiary_paths.get('departments')
                if isinstance(subsidiary_paths, dict) else None
            )
            try:
                departments_root = (
                    resolve_portable_path(
                        root, departments_relative,
                        f'{path}:paths.departments',
                    )
                    if isinstance(departments_relative, str) else None
                )
            except PortablePathError:
                departments_root = None
            if not isinstance(department_registry, list):
                continue
            for dept_index, dept_entry in enumerate(department_registry):
                if not isinstance(dept_entry, dict):
                    continue
                dept_path, department = _load_manifest(
                    root, dept_entry.get('manifest'),
                    f'{path}:departments[{dept_index}].manifest', findings,
                )
                if dept_path:
                    registered_department_paths.add(dept_path)
                    _require_containment(
                        dept_path, departments_root, 'department_location',
                        'Department manifest must be inside the subsidiary departments path.',
                        findings,
                    )
                if department is None:
                    continue
                findings.extend(_prefixed(
                    validate_department_manifest(department), str(dept_path)
                ))
                expected_department_id = dept_entry.get('department_id')
                actual_department_id = department.get('department_id')
                if actual_department_id != expected_department_id:
                    findings.append(Finding(
                        'error', 'department_registry_mismatch',
                        f'Registry ID {expected_department_id!r} does not match manifest ID {actual_department_id!r}.',
                        str(dept_path),
                    ))
                if department.get('group_id') != group_id:
                    findings.append(Finding(
                        'error', 'department_group_mismatch',
                        f'Department must belong to group {group_id!r}.',
                        str(dept_path),
                    ))
                if department.get('subsidiary_id') != actual_id:
                    findings.append(Finding(
                        'error', 'department_parent_mismatch',
                        f'Department must belong to subsidiary {actual_id!r}.',
                        str(dept_path),
                    ))
                if not isinstance(actual_department_id, str):
                    continue
                if actual_department_id in departments:
                    findings.append(Finding(
                        'error', 'duplicate_department_id',
                        f'Duplicate department_id {actual_department_id!r}.',
                        str(dept_path),
                    ))
                    continue
                departments[actual_department_id] = department

    paths = group_manifest.get('paths', {})
    subsidiaries_path = paths.get('subsidiaries') if isinstance(paths, dict) else None
    if isinstance(subsidiaries_path, str):
        try:
            subsidiaries_root = resolve_portable_path(
                root, subsidiaries_path, 'paths.subsidiaries'
            )
        except PortablePathError:
            subsidiaries_root = None
        if subsidiaries_root and subsidiaries_root.is_dir():
            for candidate in subsidiaries_root.rglob('company.manifest.json'):
                if candidate.resolve() not in registered_subsidiary_paths:
                    findings.append(Finding(
                        'error', 'unregistered_subsidiary',
                        'Subsidiary manifest is not registered by the group.',
                        str(candidate),
                    ))
            for candidate in subsidiaries_root.rglob('department.manifest.json'):
                if candidate.resolve() not in registered_department_paths:
                    findings.append(Finding(
                        'error', 'unregistered_department',
                        'Department manifest is not registered by a subsidiary.',
                        str(candidate),
                    ))

    if governance_root and governance_root.is_dir():
        for candidate in governance_root.rglob('*.json'):
            if candidate.resolve() == registered_executive_path:
                continue
            try:
                candidate_manifest = load_json(candidate)
            except ValueError:
                continue
            if candidate_manifest.get('schema_version') == EXECUTIVE_SCHEMA_VERSION:
                findings.append(Finding(
                    'error', 'unregistered_executive',
                    'Executive manifest is not the single executive registered by the group.',
                    str(candidate),
                ))

    graph = OrganizationGraph(
        root, group_manifest, executive, subsidiaries, departments
    )
    if not has_errors(findings):
        findings.append(Finding(
            'pass', 'organization_tree',
            f'Validated one group, one CEO, {len(subsidiaries)} subsidiaries, '
            f'and {len(departments)} departments.', str(root),
        ))
    return graph, findings


def validate_organization_layout(graph: OrganizationGraph) -> list[Finding]:
    findings: list[Finding] = []
    manifests = [
        (graph.group, GROUP_PATH_KEYS, 'group'),
        *[
            (item, SUBSIDIARY_PATH_KEYS, f'subsidiary:{key}')
            for key, item in graph.subsidiaries.items()
        ],
        *[
            (item, DEPARTMENT_PATH_KEYS, f'department:{key}')
            for key, item in graph.departments.items()
        ],
    ]
    for manifest, required, owner in manifests:
        paths = manifest.get('paths', {})
        if not isinstance(paths, dict):
            continue
        for key in required:
            if key not in paths:
                continue
            try:
                target = resolve_portable_path(
                    graph.root, paths[key], f'{owner}.paths.{key}'
                )
            except PortablePathError:
                continue
            if not target.is_dir():
                findings.append(Finding(
                    'warning', 'missing_directory',
                    f'Declared directory does not exist: {target}',
                    f'{owner}.paths.{key}',
                ))
    return findings


def render_organization_tree(graph: OrganizationGraph) -> str:
    lines = [
        f"{graph.group.get('name')} [{graph.group.get('group_id')}] (Group)"
    ]
    subsidiary_items = sorted(graph.subsidiaries.items())
    for subsidiary_index, (subsidiary_id, subsidiary) in enumerate(subsidiary_items):
        subsidiary_last = subsidiary_index == len(subsidiary_items) - 1
        branch = '`--' if subsidiary_last else '|--'
        lines.append(
            f"{branch} {subsidiary.get('name')} "
            f"[{subsidiary_id}] (Subsidiary)"
        )
        department_items = [
            (department_id, department)
            for department_id, department in sorted(graph.departments.items())
            if department.get('subsidiary_id') == subsidiary_id
        ]
        for department_index, (department_id, department) in enumerate(department_items):
            department_last = department_index == len(department_items) - 1
            stem = '    ' if subsidiary_last else '|   '
            child = '`--' if department_last else '|--'
            lines.append(
                f"{stem}{child} {department.get('name')} "
                f"[{department_id}] (Department)"
            )
    return '\n'.join(lines)
