'''Human/audit communication envelope; internal agents use compact IR.'''

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from .contracts import Finding
from .organization import OrganizationGraph
from .timebase import authoritative_timestamp

MESSAGE_SCHEMA_VERSION = 'group-message/v0'
MESSAGE_PROFILE = 'human-audit-envelope'
ACTS = {
    'delegate', 'acknowledge', 'progress', 'report', 'request', 'respond',
    'clarify', 'escalate', 'approve', 'reject', 'cancel', 'refuse', 'fail',
    'notify',
}
DIRECTIONS = {'downward', 'upward', 'horizontal'}
ACT_DIRECTIONS = {
    'delegate': {'downward'},
    'acknowledge': {'upward'},
    'progress': {'upward'},
    'report': {'upward'},
    'escalate': {'upward'},
    'approve': {'downward'},
    'reject': {'downward'},
    'fail': {'upward'},
}
PRIORITIES = {'low', 'normal', 'high', 'critical'}
REPORT_STATUSES = {'completed', 'partial', 'blocked', 'failed'}
ACTOR_TYPES = {'human', 'executive', 'group', 'subsidiary', 'department'}
HUMAN_TO_EXECUTIVE_ACTS = {
    'delegate', 'request', 'clarify', 'approve', 'reject', 'cancel', 'notify',
}
EXECUTIVE_TO_HUMAN_ACTS = {
    'report', 'respond', 'clarify', 'escalate', 'request', 'notify', 'refuse',
}
PORTABLE_ID = re.compile(r'^[a-z0-9][a-z0-9._:-]{2,191}$')
LOCALE = re.compile(r'^[a-z]{2,3}(?:-[A-Z]{2})?$')
FORBIDDEN_REASONING_KEYS = {
    'chain_of_thought', 'chain-of-thought', 'reasoning_trace',
    'hidden_reasoning', 'private_reasoning',
}


def _required_string(
    data: dict[str, object], key: str, findings: list[Finding],
    location: str = '',
) -> str | None:
    value = data.get(key)
    target = f'{location}.{key}' if location else key
    if not isinstance(value, str) or not value.strip():
        findings.append(Finding('error', 'required_string', f'{target} must be non-empty.', target))
        return None
    return value


def _find_forbidden_reasoning(value: object, location: str = 'payload') -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_location = f'{location}.{key}'
            if str(key).lower() in FORBIDDEN_REASONING_KEYS:
                hits.append(child_location)
            hits.extend(_find_forbidden_reasoning(child, child_location))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(_find_forbidden_reasoning(child, f'{location}[{index}]'))
    return hits


def validate_message_envelope(data: dict[str, object]) -> list[Finding]:
    findings: list[Finding] = []
    if data.get('schema_version') != MESSAGE_SCHEMA_VERSION:
        findings.append(Finding(
            'error', 'message_schema_version',
            f'schema_version must be {MESSAGE_SCHEMA_VERSION!r}.',
            'schema_version',
        ))
    for key in ('message_id', 'conversation_id', 'task_id'):
        value = _required_string(data, key, findings)
        if value and not PORTABLE_ID.fullmatch(value):
            findings.append(Finding(
                'error', 'portable_id', f'{key} is not portable.', key,
            ))
    act = data.get('act')
    if act not in ACTS:
        findings.append(Finding('error', 'message_act', f'Unsupported act {act!r}.', 'act'))
    direction = data.get('direction')
    if direction not in DIRECTIONS:
        findings.append(Finding(
            'error', 'message_direction',
            f'Unsupported direction {direction!r}.', 'direction',
        ))
    allowed_directions = ACT_DIRECTIONS.get(str(act))
    if allowed_directions and direction not in allowed_directions:
        findings.append(Finding(
            'error', 'act_direction_mismatch',
            f'Act {act!r} requires direction {sorted(allowed_directions)}.',
            'direction',
        ))
    locale = data.get('locale')
    if not isinstance(locale, str) or not LOCALE.fullmatch(locale):
        findings.append(Finding(
            'error', 'locale', 'locale must look like zh-CN or en-US.', 'locale',
        ))
    _validate_timestamp(data.get('created_at'), findings)
    _validate_actor(data.get('sender'), 'sender', findings)
    _validate_actor(data.get('receiver'), 'receiver', findings)
    _validate_payload(data.get('payload'), str(act), findings)
    _validate_control(data.get('control'), findings)
    return findings


def _validate_timestamp(value: object, findings: list[Finding]) -> None:
    if not isinstance(value, str):
        findings.append(Finding(
            'error', 'created_at', 'created_at must be an ISO timestamp.', 'created_at',
        ))
        return
    try:
        from datetime import datetime
        parsed = datetime.fromisoformat(value)
    except ValueError:
        findings.append(Finding(
            'error', 'created_at', 'created_at must be an ISO timestamp.', 'created_at',
        ))
        return
    if parsed.utcoffset() != timedelta(hours=8):
        findings.append(Finding(
            'error', 'authoritative_time',
            'Internal group messages must use Asia/Shanghai offset +08:00.',
            'created_at',
        ))


def _validate_actor(
    value: object, location: str, findings: list[Finding]
) -> None:
    if not isinstance(value, dict):
        findings.append(Finding(
            'error', 'actor', f'{location} must be an object.', location,
        ))
        return
    actor_type = value.get('type')
    if actor_type not in ACTOR_TYPES:
        findings.append(Finding(
            'error', 'actor_type',
            f'{location}.type must be one of {sorted(ACTOR_TYPES)}.',
            f'{location}.type',
        ))
    actor_id = _required_string(value, 'id', findings, location)
    if actor_id and not PORTABLE_ID.fullmatch(actor_id):
        findings.append(Finding(
            'error', 'portable_id', f'{location}.id is not portable.',
            f'{location}.id',
        ))


def _validate_payload(
    value: object, act: str, findings: list[Finding]
) -> None:
    if not isinstance(value, dict):
        findings.append(Finding(
            'error', 'payload', 'payload must be an object.', 'payload',
        ))
        return
    summary = value.get('summary')
    if not isinstance(summary, str) or not summary.strip():
        findings.append(Finding(
            'error', 'summary',
            'payload.summary must be a non-empty natural-language summary.',
            'payload.summary',
        ))
    elif len(summary) > 8000:
        findings.append(Finding(
            'error', 'summary_too_large',
            'payload.summary must not exceed 8000 characters.',
            'payload.summary',
        ))
    structured = value.get('data')
    if not isinstance(structured, dict):
        findings.append(Finding(
            'error', 'structured_data',
            'payload.data must be an object.', 'payload.data',
        ))
        structured = {}
    for key in ('artifacts', 'evidence'):
        references = value.get(key, [])
        if not isinstance(references, list) or not all(
            isinstance(item, dict) and isinstance(item.get('ref'), str)
            for item in references
        ):
            findings.append(Finding(
                'error', 'reference_list',
                f'payload.{key} must be an array of objects with ref.',
                f'payload.{key}',
            ))
    for location in _find_forbidden_reasoning(value):
        findings.append(Finding(
            'error', 'private_reasoning_disclosure',
            'Transmit conclusions, evidence, and concise rationale; '
            'do not transmit private chain-of-thought.', location,
        ))
    if act == 'report':
        if structured.get('status') not in REPORT_STATUSES:
            findings.append(Finding(
                'error', 'report_status',
                f'Report status must be one of {sorted(REPORT_STATUSES)}.',
                'payload.data.status',
            ))
        outcome = structured.get('outcome')
        if not isinstance(outcome, str) or not outcome.strip():
            findings.append(Finding(
                'error', 'report_outcome',
                'Reports require a non-empty payload.data.outcome.',
                'payload.data.outcome',
            ))
        if not isinstance(structured.get('metrics', {}), dict):
            findings.append(Finding(
                'error', 'report_metrics',
                'payload.data.metrics must be an object.',
                'payload.data.metrics',
            ))
        unresolved = structured.get('unresolved', [])
        if not isinstance(unresolved, list) or not all(
            isinstance(item, str) for item in unresolved
        ):
            findings.append(Finding(
                'error', 'report_unresolved',
                'payload.data.unresolved must be an array of strings.',
                'payload.data.unresolved',
            ))
        confidence = structured.get('confidence')
        if confidence is not None and (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
        ):
            findings.append(Finding(
                'error', 'report_confidence',
                'payload.data.confidence must be between 0 and 1.',
                'payload.data.confidence',
            ))


def _validate_control(value: object, findings: list[Finding]) -> None:
    if not isinstance(value, dict):
        findings.append(Finding(
            'error', 'control', 'control must be an object.', 'control',
        ))
        return
    if value.get('priority') not in PRIORITIES:
        findings.append(Finding(
            'error', 'priority',
            f'control.priority must be one of {sorted(PRIORITIES)}.',
            'control.priority',
        ))
    if not isinstance(value.get('expects_reply'), bool):
        findings.append(Finding(
            'error', 'expects_reply',
            'control.expects_reply must be boolean.',
            'control.expects_reply',
        ))
    _required_string(value, 'idempotency_key', findings, 'control')


def _actor_identity(
    graph: OrganizationGraph, actor: dict[str, object]
) -> tuple[str, str] | None:
    actor_type = actor.get('type')
    actor_id = actor.get('id')
    if actor_type == 'human' and isinstance(actor_id, str):
        return 'human', actor_id
    if (
        actor_type == 'executive'
        and actor_id == graph.executive.get('executive_id')
    ):
        return 'executive', str(actor_id)
    if (
        actor_type == 'group'
        and isinstance(actor_id, str)
        and actor_id == graph.group.get('group_id')
    ):
        return 'group', str(actor_id)
    if (
        actor_type == 'subsidiary'
        and isinstance(actor_id, str)
        and actor_id in graph.subsidiaries
    ):
        return 'subsidiary', str(actor_id)
    if (
        actor_type == 'department'
        and isinstance(actor_id, str)
        and actor_id in graph.departments
    ):
        return 'department', str(actor_id)
    return None


def validate_organization_message(
    graph: OrganizationGraph, data: dict[str, object]
) -> list[Finding]:
    findings = validate_message_envelope(data)
    sender = data.get('sender')
    receiver = data.get('receiver')
    if not isinstance(sender, dict) or not isinstance(receiver, dict):
        return findings
    sender_identity = _actor_identity(graph, sender)
    receiver_identity = _actor_identity(graph, receiver)
    if sender_identity is None:
        findings.append(Finding(
            'error', 'unknown_sender',
            'sender is not registered in the organization graph.', 'sender',
        ))
    if receiver_identity is None:
        findings.append(Finding(
            'error', 'unknown_receiver',
            'receiver is not registered in the organization graph.', 'receiver',
        ))
    if sender_identity is None or receiver_identity is None:
        return findings
    if sender_identity == receiver_identity:
        findings.append(Finding(
            'error', 'self_message',
            'Messages must have different sender and receiver actors.',
            'receiver',
        ))
        return findings

    direction = data.get('direction')
    sender_type = sender_identity[0]
    receiver_type = receiver_identity[0]
    if {sender_type, receiver_type} != {'human', 'executive'}:
        findings.append(Finding(
            'error', 'human_gateway_boundary',
            'Natural-language group messages are restricted to the exclusive '
            'human-CEO boundary; internal execution traffic must use compact IR.',
            'sender',
        ))
        return findings

    act = data.get('act')
    if sender_type == 'human':
        if direction != 'downward':
            findings.append(Finding(
                'error', 'human_gateway_direction',
                'Human commands must flow downward to the CEO.', 'direction',
            ))
        if act not in HUMAN_TO_EXECUTIVE_ACTS:
            findings.append(Finding(
                'error', 'human_gateway_act',
                f'Human-to-CEO act must be one of '
                f'{sorted(HUMAN_TO_EXECUTIVE_ACTS)}.', 'act',
            ))
    else:
        if direction != 'upward':
            findings.append(Finding(
                'error', 'human_gateway_direction',
                'CEO reports and questions must flow upward to the human.',
                'direction',
            ))
        if act not in EXECUTIVE_TO_HUMAN_ACTS:
            findings.append(Finding(
                'error', 'human_gateway_act',
                f'CEO-to-human act must be one of '
                f'{sorted(EXECUTIVE_TO_HUMAN_ACTS)}.', 'act',
            ))
    return findings


def new_message(
    *,
    conversation_id: str,
    task_id: str,
    sender: dict[str, str],
    receiver: dict[str, str],
    act: str,
    direction: str,
    summary: str,
    data: dict[str, object] | None = None,
    artifacts: list[dict[str, object]] | None = None,
    evidence: list[dict[str, object]] | None = None,
    locale: str = 'zh-CN',
    priority: str = 'normal',
    expects_reply: bool = False,
    parent_message_id: str | None = None,
) -> dict[str, object]:
    message_id = f'msg:{uuid.uuid4().hex}'
    control: dict[str, object] = {
        'priority': priority,
        'expects_reply': expects_reply,
        'idempotency_key': f'idem:{uuid.uuid4().hex}',
    }
    if parent_message_id:
        control['parent_message_id'] = parent_message_id
    return {
        'schema_version': MESSAGE_SCHEMA_VERSION,
        'message_id': message_id,
        'conversation_id': conversation_id,
        'task_id': task_id,
        'sender': sender,
        'receiver': receiver,
        'act': act,
        'direction': direction,
        'locale': locale,
        'created_at': authoritative_timestamp(),
        'payload': {
            'summary': summary,
            'data': data or {},
            'artifacts': artifacts or [],
            'evidence': evidence or [],
        },
        'control': control,
    }


def new_report(
    *,
    conversation_id: str,
    task_id: str,
    sender: dict[str, str],
    receiver: dict[str, str],
    summary: str,
    status: str,
    outcome: str,
    metrics: dict[str, object] | None = None,
    unresolved: list[str] | None = None,
    artifacts: list[dict[str, object]] | None = None,
    evidence: list[dict[str, object]] | None = None,
    confidence: float | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        'status': status,
        'outcome': outcome,
        'metrics': metrics or {},
        'unresolved': unresolved or [],
    }
    if confidence is not None:
        data['confidence'] = confidence
    return new_message(
        conversation_id=conversation_id,
        task_id=task_id,
        sender=sender,
        receiver=receiver,
        act='report',
        direction='upward',
        summary=summary,
        data=data,
        artifacts=artifacts,
        evidence=evidence,
        expects_reply=False,
    )


def new_human_command(
    *,
    conversation_id: str,
    task_id: str,
    human_id: str,
    executive_id: str,
    instruction: str,
    acceptance_criteria: list[str] | None = None,
    priority: str = 'normal',
) -> dict[str, object]:
    return new_message(
        conversation_id=conversation_id,
        task_id=task_id,
        sender={'type': 'human', 'id': human_id},
        receiver={'type': 'executive', 'id': executive_id},
        act='delegate',
        direction='downward',
        summary=instruction,
        data={'acceptance_criteria': acceptance_criteria or []},
        priority=priority,
        expects_reply=True,
    )


def new_human_report(
    *,
    conversation_id: str,
    task_id: str,
    executive_id: str,
    human_id: str,
    summary: str,
    status: str,
    outcome: str,
    metrics: dict[str, object] | None = None,
    unresolved: list[str] | None = None,
    artifacts: list[dict[str, object]] | None = None,
    evidence: list[dict[str, object]] | None = None,
    confidence: float | None = None,
) -> dict[str, object]:
    return new_report(
        conversation_id=conversation_id,
        task_id=task_id,
        sender={'type': 'executive', 'id': executive_id},
        receiver={'type': 'human', 'id': human_id},
        summary=summary,
        status=status,
        outcome=outcome,
        metrics=metrics,
        unresolved=unresolved,
        artifacts=artifacts,
        evidence=evidence,
        confidence=confidence,
    )
