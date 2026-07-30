'''Append-only company event stream and deterministic UI projections.'''

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from threading import RLock

from fastapi import FastAPI, Query, Request
from fastapi.responses import StreamingResponse

from .timebase import authoritative_timestamp


SCHEMA_VERSION = 'company-event/v1'
SNAPSHOT_SCHEMA_VERSION = 'company-snapshot/v1'
EVENT_TYPES = {
    'command.accepted', 'routing.decided', 'task.created', 'task.status.changed',
    'subsidiary.assigned', 'workflow.started', 'workflow.phase.started',
    'workflow.phase.completed', 'agent.started', 'agent.progress.reported',
    'agent.completed', 'service.call.started', 'service.call.completed',
    'knowledge.query.started', 'knowledge.query.completed', 'tool.call.started',
    'tool.call.completed', 'artifact.created', 'evidence.registered',
    'approval.requested', 'approval.resolved', 'budget.warning', 'task.blocked',
    'task.completed', 'task.failed',
}
STATUSES = {
    'pending', 'planning', 'working', 'awaiting_input', 'blocked', 'completed',
    'failed', 'error', 'terminated', 'archived',
}
VISIBILITIES = {'human', 'ceo', 'organization', 'audit', 'system'}
ACTOR_TYPES = {
    'human', 'ceo', 'subsidiary', 'agent', 'workflow', 'service', 'tool', 'system',
}
TARGET_TYPES = ACTOR_TYPES - {'human'}
_PATH_LOCKS: dict[str, RLock] = {}
_PATH_LOCKS_GUARD = RLock()


def _path_lock(path: Path) -> RLock:
    key = str(path.resolve(strict=False)).casefold()
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, RLock())


def _actor(value: Mapping[str, object], *, target: bool = False) -> dict[str, str]:
    allowed = TARGET_TYPES if target else ACTOR_TYPES
    actor_type = str(value.get('type') or '').strip()
    actor_id = str(value.get('id') or '').strip()
    if actor_type not in allowed or not actor_id:
        label = 'target' if target else 'source'
        raise ValueError(f'{label} must contain a valid type and non-empty id.')
    return {'type': actor_type, 'id': actor_id}


def _refs(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f'{field} must be a list of strings.')
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


class CompanyEventStore:
    '''Durable provider-neutral company event stream backed by UTF-8 JSONL.'''

    def __init__(self, path: Path, *, company_id: str = 'north-star-group') -> None:
        self.path = path
        self.company_id = company_id

    def append(
        self,
        event_type: str,
        *,
        source: Mapping[str, object],
        summary: str,
        target: Mapping[str, object] | None = None,
        visibility: str = 'organization',
        status: str | None = None,
        subsidiary_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        phase: str | None = None,
        evidence_refs: object = None,
        artifact_refs: object = None,
        tool_call: Mapping[str, object] | None = None,
        approval: Mapping[str, object] | None = None,
        cost: Mapping[str, object] | None = None,
        error: Mapping[str, object] | None = None,
        meta: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if event_type not in EVENT_TYPES:
            raise ValueError(f'Unsupported company event type: {event_type!r}')
        text = summary.strip()
        if not text or len(text) > 1000:
            raise ValueError('summary must contain 1 to 1000 characters.')
        if visibility not in VISIBILITIES:
            raise ValueError(f'Unsupported visibility: {visibility!r}')
        if status is not None and status not in STATUSES:
            raise ValueError(f'Unsupported status: {status!r}')

        with _path_lock(self.path):
            events = self._read_locked()
            event: dict[str, object] = {
                'schema_version': SCHEMA_VERSION,
                'event_id': uuid.uuid4().hex,
                'seq': (events[-1]['seq'] if events else 0) + 1,
                'occurred_at': authoritative_timestamp(),
                'company_id': self.company_id,
                'subsidiary_id': subsidiary_id,
                'task_id': task_id,
                'run_id': run_id,
                'session_id': session_id,
                'correlation_id': correlation_id,
                'causation_id': causation_id,
                'event_type': event_type,
                'source': _actor(source),
                'target': _actor(target, target=True) if target is not None else None,
                'phase': phase,
                'status': status,
                'visibility': visibility,
                'summary': text,
                'evidence_refs': _refs(evidence_refs, 'evidence_refs'),
                'artifact_refs': _refs(artifact_refs, 'artifact_refs'),
                'tool_call': dict(tool_call) if tool_call is not None else None,
                'approval': dict(approval) if approval is not None else None,
                'cost': dict(cost) if cost is not None else None,
                'error': dict(error) if error is not None else None,
                'meta': dict(meta or {}),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8', newline='\n') as handle:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            return event

    def list(self, *, after_seq: int = 0, limit: int = 200) -> list[dict[str, object]]:
        if after_seq < 0:
            raise ValueError('after_seq must be non-negative.')
        if not 1 <= limit <= 500:
            raise ValueError('limit must be between 1 and 500.')
        with _path_lock(self.path):
            events = self._read_locked()
        return [event for event in events if int(event['seq']) > after_seq][:limit]

    def snapshot(self) -> dict[str, object]:
        with _path_lock(self.path):
            events = self._read_locked()
        tasks: dict[str, dict[str, object]] = {}
        approvals: dict[str, dict[str, object]] = {}
        infrastructure: dict[str, dict[str, object]] = {}
        conversation: list[dict[str, object]] = []
        office: dict[str, dict[str, object]] = {}

        for event in events:
            task_id = event.get('task_id')
            if isinstance(task_id, str) and task_id:
                task = tasks.setdefault(task_id, {'task_id': task_id, 'status': None})
                if event.get('status') is not None:
                    task['status'] = event.get('status')
                task.update({
                    'summary': event.get('summary'),
                    'event_type': event.get('event_type'),
                    'updated_at': event.get('occurred_at'),
                    'seq': event.get('seq'),
                })
            if event.get('event_type') in {
                'command.accepted', 'routing.decided', 'approval.requested',
                'approval.resolved', 'task.completed', 'task.failed',
            }:
                conversation.append(event)
            approval = event.get('approval')
            if isinstance(approval, dict) and approval.get('request_id'):
                approvals[str(approval['request_id'])] = event
            source = event.get('source')
            target = event.get('target')
            for actor in (source, target):
                if not isinstance(actor, dict):
                    continue
                if actor.get('type') == 'service':
                    infrastructure[str(actor.get('id'))] = event
                if actor.get('type') in {'subsidiary', 'agent', 'workflow'}:
                    office[str(actor.get('id'))] = event

        return {
            'schema_version': SNAPSHOT_SCHEMA_VERSION,
            'generated_at': authoritative_timestamp(),
            'last_seq': events[-1]['seq'] if events else 0,
            'events': events[-100:],
            'projections': {
                'ceo_conversation': conversation[-50:],
                'virtual_office': list(office.values()),
                'tasks': list(tasks.values()),
                'infrastructure': list(infrastructure.values()),
                'approvals': list(approvals.values()),
            },
        }

    def _read_locked(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        events: list[dict[str, object]] = []
        expected_seq = 1
        for number, line in enumerate(self.path.read_text(encoding='utf-8').splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f'Corrupt company event at line {number}: {exc}') from exc
            if not isinstance(event, dict) or event.get('schema_version') != SCHEMA_VERSION:
                raise ValueError(f'Invalid company event at line {number}.')
            if event.get('seq') != expected_seq:
                raise ValueError(f'Non-monotonic company event sequence at line {number}.')
            events.append(event)
            expected_seq += 1
        return events


def mount_company_event_routes(app: FastAPI, store: CompanyEventStore) -> None:
    @app.get('/api/company/events')
    def company_events(
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, object]:
        events = store.list(after_seq=after, limit=limit)
        return {
            'schema_version': 'company-event-list/v1',
            'last_seq': events[-1]['seq'] if events else after,
            'events': events,
        }

    @app.get('/api/company/snapshot')
    def company_snapshot() -> dict[str, object]:
        return store.snapshot()

    @app.get('/api/company/events/stream')
    async def company_event_stream(
        request: Request,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        async def generate() -> AsyncIterator[str]:
            cursor = after
            while not await request.is_disconnected():
                events = store.list(after_seq=cursor, limit=100)
                if events:
                    for event in events:
                        cursor = int(event['seq'])
                        payload = json.dumps(event, ensure_ascii=False, separators=(',', ':'))
                        yield f'id: {cursor}\ndata: {payload}\n\n'
                else:
                    yield ': keepalive\n\n'
                await asyncio.sleep(0.75)

        return StreamingResponse(
            generate(), media_type='text/event-stream',
            headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'},
        )