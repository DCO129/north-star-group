'''Token-minimized discrete intermediate representation for agent coordination.'''

from __future__ import annotations

import json
import re

from .contracts import Finding
from .organization import OrganizationGraph

IR_VERSION = 1
PACKET_LENGTH = 9
ACT_CODES = {
    'delegate': 1,
    'acknowledge': 2,
    'progress': 3,
    'report': 4,
    'request': 5,
    'respond': 6,
    'clarify': 7,
    'escalate': 8,
    'approve': 9,
    'reject': 10,
    'cancel': 11,
    'refuse': 12,
    'fail': 13,
    'notify': 14,
}
ACT_NAMES = {value: key for key, value in ACT_CODES.items()}
STATUS_CODES = {
    'none': 0,
    'queued': 1,
    'running': 2,
    'completed': 3,
    'partial': 4,
    'blocked': 5,
    'failed': 6,
}
FLAG_REPLY_REQUIRED = 1
FLAG_URGENT = 2
FLAG_HUMAN_REVIEW = 4
FLAG_SENSITIVE = 8
KNOWN_FLAGS = (
    FLAG_REPLY_REQUIRED | FLAG_URGENT | FLAG_HUMAN_REVIEW | FLAG_SENSITIVE
)
BODY_KEYS = {'q', 'o', 'm', 'u', 'e', 'a', 'c', 'd', 'x'}
ATOM = re.compile(r'^[A-Za-z0-9._:/@+-]{1,128}$')


def _is_ir_value(value: object, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    if isinstance(value, str):
        return bool(ATOM.fullmatch(value))
    if isinstance(value, list):
        return len(value) <= 256 and all(
            _is_ir_value(item, depth + 1) for item in value
        )
    if isinstance(value, dict):
        return len(value) <= 128 and all(
            isinstance(key, str)
            and ATOM.fullmatch(key)
            and _is_ir_value(item, depth + 1)
            for key, item in value.items()
        )
    return False


def validate_ir_packet(packet: object) -> list[Finding]:
    findings: list[Finding] = []
    if not isinstance(packet, list) or len(packet) != PACKET_LENGTH:
        return [Finding(
            'error', 'ir_shape',
            f'IR packet must be a {PACKET_LENGTH}-element array.', 'packet',
        )]
    version, dictionary_id, task_ref, sender_ref, receiver_ref, act, status, flags, body = packet
    if version != IR_VERSION:
        findings.append(Finding(
            'error', 'ir_version', f'IR version must be {IR_VERSION}.', '[0]',
        ))
    for index, value, label in (
        (1, dictionary_id, 'dictionary_id'),
        (2, task_ref, 'task_ref'),
        (3, sender_ref, 'sender_ref'),
        (4, receiver_ref, 'receiver_ref'),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            findings.append(Finding(
                'error', 'ir_reference',
                f'{label} must be a positive integer symbol reference.',
                f'[{index}]',
            ))
    if act not in ACT_CODES.values():
        findings.append(Finding(
            'error', 'ir_act', 'Unknown IR communicative act code.', '[5]',
        ))
    if status not in STATUS_CODES.values():
        findings.append(Finding(
            'error', 'ir_status', 'Unknown IR status code.', '[6]',
        ))
    if not isinstance(flags, int) or isinstance(flags, bool) or flags < 0:
        findings.append(Finding(
            'error', 'ir_flags', 'IR flags must be a non-negative integer.', '[7]',
        ))
    elif flags & ~KNOWN_FLAGS:
        findings.append(Finding(
            'error', 'ir_flags', 'IR flags contain unknown bits.', '[7]',
        ))
    if not isinstance(body, dict):
        findings.append(Finding(
            'error', 'ir_body', 'IR body must be an object.', '[8]',
        ))
        return findings
    unknown = set(body) - BODY_KEYS
    if unknown:
        findings.append(Finding(
            'error', 'ir_body_key',
            f'Unknown IR body keys: {sorted(unknown)}.', '[8]',
        ))
    if not _is_ir_value(body):
        findings.append(Finding(
            'error', 'ir_prose_or_value',
            'IR body accepts bounded scalar structures and compact ASCII atoms, '
            'not natural-language prose.', '[8]',
        ))
    confidence = body.get('c')
    if confidence is not None and (
        not isinstance(confidence, int)
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1000
    ):
        findings.append(Finding(
            'error', 'ir_confidence',
            'IR confidence must be an integer from 0 to 1000.', '[8].c',
        ))
    return findings


def _resolve_actor(
    graph: OrganizationGraph, actor: object
) -> tuple[str, str] | None:
    if not isinstance(actor, dict):
        return None
    actor_type = actor.get('type')
    actor_id = actor.get('id')
    if (
        actor_type == 'executive'
        and actor_id == graph.executive.get('executive_id')
    ):
        return 'executive', str(actor_id)
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


def validate_ir_route(
    graph: OrganizationGraph,
    packet: object,
    actor_symbols: dict[int, dict[str, str]],
) -> list[Finding]:
    findings = validate_ir_packet(packet)
    if any(item.severity == 'error' for item in findings):
        return findings
    if not isinstance(packet, list):
        return findings
    sender_ref = packet[3]
    receiver_ref = packet[4]
    if not isinstance(sender_ref, int) or not isinstance(receiver_ref, int):
        return findings
    sender = _resolve_actor(graph, actor_symbols.get(sender_ref))
    receiver = _resolve_actor(graph, actor_symbols.get(receiver_ref))
    if sender is None:
        findings.append(Finding(
            'error', 'ir_unknown_sender',
            'IR sender symbol must resolve to the CEO or a registered execution unit.',
            '[3]',
        ))
    if receiver is None:
        findings.append(Finding(
            'error', 'ir_unknown_receiver',
            'IR receiver symbol must resolve to the CEO or a registered execution unit.',
            '[4]',
        ))
    if sender is None or receiver is None:
        return findings
    if sender == receiver:
        findings.append(Finding(
            'error', 'ir_self_route', 'IR sender and receiver must differ.', '[4]',
        ))
        return findings

    act = ACT_NAMES[int(packet[5])]
    sender_type = sender[0]
    receiver_type = receiver[0]
    executive_to_unit = (
        sender_type == 'executive'
        and receiver_type in {'subsidiary', 'department'}
    )
    unit_to_executive = (
        sender_type in {'subsidiary', 'department'}
        and receiver_type == 'executive'
    )
    unit_to_unit = (
        sender_type in {'subsidiary', 'department'}
        and receiver_type in {'subsidiary', 'department'}
    )
    allowed = False
    if executive_to_unit:
        allowed = act in {
            'delegate', 'approve', 'reject', 'cancel', 'request', 'clarify',
            'notify',
        }
    elif unit_to_executive:
        allowed = act in {
            'acknowledge', 'progress', 'report', 'fail', 'escalate',
            'respond', 'clarify', 'request', 'notify', 'refuse',
        }
    elif unit_to_unit:
        allowed = act in {'request', 'respond', 'clarify', 'notify'}
    if not allowed:
        findings.append(Finding(
            'error', 'ir_command_graph',
            'Operational command traffic must be CEO-to-unit, execution returns '
            'must be unit-to-CEO, and peer traffic is coordination-only.',
            '[5]',
        ))
        return findings

    if act == 'delegate':
        body = packet[8]
        if not isinstance(body, dict):
            return findings
        extension = body.get('x')
        if 'q' not in body:
            findings.append(Finding(
                'error', 'ir_delegation_objective',
                'CEO delegation requires objective or acceptance references in body.q.',
                '[8].q',
            ))
        if not isinstance(extension, dict) or not isinstance(
            extension.get('ik'), str
        ):
            findings.append(Finding(
                'error', 'ir_idempotency_key',
                'CEO delegation requires a compact idempotency key in body.x.ik.',
                '[8].x.ik',
            ))
        if not isinstance(extension, dict) or not isinstance(
            extension.get('ac'), list
        ) or not extension.get('ac'):
            findings.append(Finding(
                'error', 'ir_acceptance_criteria',
                'CEO delegation requires acceptance references in body.x.ac.',
                '[8].x.ac',
            ))
    return findings


def delegation_body(
    *,
    objective_refs: list[int],
    acceptance_refs: list[int],
    idempotency_key: str,
    artifact_refs: list[int] | None = None,
) -> dict[str, object]:
    return {
        'q': objective_refs,
        'a': artifact_refs or [],
        'x': {'ac': acceptance_refs, 'ik': idempotency_key},
    }


def new_ir_packet(
    *,
    dictionary_id: int,
    task_ref: int,
    sender_ref: int,
    receiver_ref: int,
    act: str,
    status: str = 'none',
    flags: int = 0,
    body: dict[str, object] | None = None,
) -> list[object]:
    if act not in ACT_CODES:
        raise ValueError(f'Unknown IR act {act!r}.')
    if status not in STATUS_CODES:
        raise ValueError(f'Unknown IR status {status!r}.')
    packet: list[object] = [
        IR_VERSION,
        dictionary_id,
        task_ref,
        sender_ref,
        receiver_ref,
        ACT_CODES[act],
        STATUS_CODES[status],
        flags,
        body or {},
    ]
    findings = validate_ir_packet(packet)
    if findings:
        messages = '; '.join(item.message for item in findings)
        raise ValueError(messages)
    return packet


def encode_ir_text(packet: list[object]) -> str:
    findings = validate_ir_packet(packet)
    if findings:
        raise ValueError('; '.join(item.message for item in findings))
    return json.dumps(packet, ensure_ascii=True, separators=(',', ':'))


def decode_ir_text(value: str) -> list[object]:
    try:
        packet = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f'Invalid IR JSON: {exc}') from exc
    findings = validate_ir_packet(packet)
    if findings:
        raise ValueError('; '.join(item.message for item in findings))
    return packet


def report_body(
    *,
    outcome_refs: list[int] | None = None,
    metric_codes: dict[str, int | float] | None = None,
    unresolved_refs: list[int] | None = None,
    evidence_refs: list[int] | None = None,
    artifact_refs: list[int] | None = None,
    confidence_milli: int | None = None,
    delta: dict[str, object] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {}
    if outcome_refs:
        body['o'] = outcome_refs
    if metric_codes:
        body['m'] = metric_codes
    if unresolved_refs:
        body['u'] = unresolved_refs
    if evidence_refs:
        body['e'] = evidence_refs
    if artifact_refs:
        body['a'] = artifact_refs
    if confidence_milli is not None:
        body['c'] = confidence_milli
    if delta:
        body['d'] = delta
    return body
