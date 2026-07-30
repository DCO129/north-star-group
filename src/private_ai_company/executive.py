'''CEO task lifecycle and durable orchestration state.'''

from __future__ import annotations

import copy
import re

from .contracts import Finding
from .timebase import authoritative_timestamp

EXECUTIVE_TASK_SCHEMA_VERSION = 'executive-task/v0'
EXECUTIVE_STATES = (
    'human_command',
    'ceo_intake',
    'clarify_or_accept',
    'decompose',
    'route',
    'execute',
    'return',
    'verify',
    'aggregate',
    'human_report',
    'blocked',
    'failed',
)
ALLOWED_TRANSITIONS = {
    'human_command': {'ceo_intake'},
    'ceo_intake': {'clarify_or_accept'},
    'clarify_or_accept': {'ceo_intake', 'decompose'},
    'decompose': {'route'},
    'route': {'execute', 'blocked', 'failed'},
    'execute': {'return', 'blocked', 'failed'},
    'return': {'verify'},
    'verify': {'execute', 'aggregate', 'blocked', 'failed'},
    'aggregate': {'human_report'},
    'human_report': set(),
    'blocked': set(),
    'failed': set(),
}
PORTABLE_ID = re.compile(r'^[a-z0-9][a-z0-9._:-]{2,191}$')


def new_executive_task(
    *,
    task_id: str,
    command_id: str,
    executive_id: str,
    original_instruction: str,
    acceptance_criteria: list[str],
    idempotency_key: str,
) -> dict[str, object]:
    now = authoritative_timestamp()
    task = {
        'schema_version': EXECUTIVE_TASK_SCHEMA_VERSION,
        'task_id': task_id,
        'command_id': command_id,
        'executive_id': executive_id,
        'state': 'human_command',
        'original_instruction': original_instruction,
        'acceptance_criteria': acceptance_criteria,
        'idempotency_key': idempotency_key,
        'plan': [],
        'assignments': [],
        'evidence_refs': [],
        'artifact_refs': [],
        'unresolved': [],
        'final_report': None,
        'created_at': now,
        'updated_at': now,
    }
    findings = validate_executive_task(task)
    errors = [item for item in findings if item.severity == 'error']
    if errors:
        raise ValueError('; '.join(item.message for item in errors))
    return task


def validate_executive_task(task: object) -> list[Finding]:
    if not isinstance(task, dict):
        return [Finding(
            'error', 'executive_task_shape',
            'Executive task state must be an object.', 'task',
        )]
    findings: list[Finding] = []
    if task.get('schema_version') != EXECUTIVE_TASK_SCHEMA_VERSION:
        findings.append(Finding(
            'error', 'executive_task_schema',
            f'schema_version must be {EXECUTIVE_TASK_SCHEMA_VERSION!r}.',
            'schema_version',
        ))
    for key in ('task_id', 'command_id', 'executive_id', 'idempotency_key'):
        value = task.get(key)
        if not isinstance(value, str) or not PORTABLE_ID.fullmatch(value):
            findings.append(Finding(
                'error', 'executive_task_id',
                f'{key} must be a portable identifier.', key,
            ))
    instruction = task.get('original_instruction')
    if not isinstance(instruction, str) or not instruction.strip():
        findings.append(Finding(
            'error', 'executive_instruction',
            'original_instruction must preserve the human command.',
            'original_instruction',
        ))
    criteria = task.get('acceptance_criteria')
    if not isinstance(criteria, list) or not criteria or not all(
        isinstance(item, str) and item.strip() for item in criteria
    ):
        findings.append(Finding(
            'error', 'executive_acceptance_criteria',
            'acceptance_criteria must be a non-empty string array.',
            'acceptance_criteria',
        ))
    state = task.get('state')
    if state not in EXECUTIVE_STATES:
        findings.append(Finding(
            'error', 'executive_task_state',
            f'state must be one of {list(EXECUTIVE_STATES)}.', 'state',
        ))
    for key in (
        'plan', 'assignments', 'evidence_refs', 'artifact_refs', 'unresolved',
    ):
        if not isinstance(task.get(key), list):
            findings.append(Finding(
                'error', 'executive_task_collection',
                f'{key} must be an array.', key,
            ))
    if state in {'human_report', 'blocked', 'failed'} and not isinstance(
        task.get('final_report'), dict,
    ):
        findings.append(Finding(
            'error', 'executive_final_report',
            'Terminal task states require a structured final_report.',
            'final_report',
        ))
    return findings


def transition_executive_task(
    task: dict[str, object],
    next_state: str,
    **updates: object,
) -> dict[str, object]:
    findings = validate_executive_task(task)
    errors = [item for item in findings if item.severity == 'error']
    if errors:
        raise ValueError('Cannot transition an invalid executive task.')
    current_state = str(task['state'])
    if next_state not in ALLOWED_TRANSITIONS[current_state]:
        raise ValueError(
            f'Invalid CEO task transition {current_state!r} -> {next_state!r}.'
        )
    allowed_updates = {
        'plan', 'assignments', 'evidence_refs', 'artifact_refs', 'unresolved',
        'final_report',
    }
    unknown = set(updates) - allowed_updates
    if unknown:
        raise ValueError(f'Unsupported executive task updates: {sorted(unknown)}.')
    result = copy.deepcopy(task)
    result.update(updates)
    result['state'] = next_state
    result['updated_at'] = authoritative_timestamp()
    next_findings = validate_executive_task(result)
    next_errors = [item for item in next_findings if item.severity == 'error']
    if next_errors:
        raise ValueError('; '.join(item.message for item in next_errors))
    return result
