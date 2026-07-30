'''Stable, provider-neutral execution tracing with defensive redaction.'''

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Mapping, Protocol

from .timebase import authoritative_timestamp


_ID_PATTERN = re.compile(r'^[0-9a-f]{32}$')
_PORTABLE_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
_SENSITIVE_KEYS = {
    'api_key', 'apikey', 'authorization', 'body', 'chain_of_thought',
    'content', 'credential', 'headers', 'messages', 'prompt', 'reasoning',
    'request_body', 'response', 'response_body', 'secret', 'tool_arguments',
}
_SENSITIVE_KEY_PARTS = ('password', 'private_key', 'access_token', 'refresh_token')
_SECRET_VALUE_PATTERNS = (
    re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*'),
    re.compile(r'\bsk-[A-Za-z0-9_-]{12,}\b'),
)


def _validate_portable_id(value: str, label: str) -> None:
    if not _PORTABLE_ID_PATTERN.fullmatch(value):
        raise ValueError(f'{label} must be a portable identifier.')


def _validate_hex_id(value: str, label: str) -> None:
    if not _ID_PATTERN.fullmatch(value):
        raise ValueError(f'{label} must be a 32-character lowercase hex identifier.')


def _safe_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
            return '[redacted]'
        return value[:512]
    if isinstance(value, Mapping):
        cleaned: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            normalized = key.casefold().replace('-', '_')
            if normalized in _SENSITIVE_KEYS or any(
                part in normalized for part in _SENSITIVE_KEY_PARTS
            ):
                continue
            cleaned[key] = _safe_value(raw_value)
        return cleaned
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_value(item) for item in list(value)[:64]]
    return str(value)[:512]


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    task_id: str
    span_id: str
    actor_id: str
    parent_span_id: str | None = None

    def __post_init__(self) -> None:
        _validate_hex_id(self.trace_id, 'trace_id')
        _validate_hex_id(self.span_id, 'span_id')
        _validate_portable_id(self.task_id, 'task_id')
        _validate_portable_id(self.actor_id, 'actor_id')
        if self.parent_span_id is not None:
            _validate_hex_id(self.parent_span_id, 'parent_span_id')
            if self.parent_span_id == self.span_id:
                raise ValueError('A trace span cannot be its own parent.')

    @classmethod
    def root(cls, task_id: str, actor_id: str) -> 'TraceContext':
        return cls(
            trace_id=uuid.uuid4().hex, task_id=task_id,
            span_id=uuid.uuid4().hex, actor_id=actor_id,
        )

    def child(self, actor_id: str) -> 'TraceContext':
        return TraceContext(
            trace_id=self.trace_id, task_id=self.task_id,
            span_id=uuid.uuid4().hex, actor_id=actor_id,
            parent_span_id=self.span_id,
        )


@dataclass(frozen=True)
class TraceEvent:
    context: TraceContext
    component: str
    kind: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    usage: Mapping[str, int] = field(default_factory=dict)
    cost_fen: int | None = None
    error: Mapping[str, object] = field(default_factory=dict)
    attributes: Mapping[str, object] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=authoritative_timestamp)

    def __post_init__(self) -> None:
        _validate_hex_id(self.event_id, 'event_id')
        if self.component not in {'task', 'provider', 'tool', 'router'}:
            raise ValueError('Trace component must be task, provider, tool, or router.')
        if not self.kind.strip():
            raise ValueError('Trace kind must be non-empty.')
        if self.status not in {'started', 'completed', 'blocked', 'denied', 'failed'}:
            raise ValueError('Unsupported trace status.')
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError('Trace duration_ms must not be negative.')
        if self.cost_fen is not None and self.cost_fen < 0:
            raise ValueError('Trace cost_fen must not be negative.')

    def to_dict(self) -> dict[str, object]:
        return {
            'schema_version': 'execution-trace/v0',
            'event_id': self.event_id,
            'timestamp': self.timestamp,
            'trace_id': self.context.trace_id,
            'task_id': self.context.task_id,
            'span_id': self.context.span_id,
            'parent_span_id': self.context.parent_span_id,
            'actor_id': self.context.actor_id,
            'component': self.component,
            'kind': self.kind,
            'status': self.status,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'duration_ms': self.duration_ms,
            'usage': _safe_value(dict(self.usage)),
            'cost_fen': self.cost_fen,
            'error': _safe_value(dict(self.error)),
            'attributes': _safe_value(dict(self.attributes)),
        }


class TraceSink(Protocol):
    def emit(self, event: TraceEvent) -> dict[str, object]: ...


class _TraceRelationValidator:
    def __init__(self) -> None:
        self._spans: dict[str, tuple[str, str, str | None]] = {}

    def validate(self, event: TraceEvent) -> None:
        context = event.context
        existing = self._spans.get(context.span_id)
        identity = (context.trace_id, context.task_id, context.parent_span_id)
        if existing is not None and existing != identity:
            raise ValueError('Trace span identity changed after first use.')
        if context.parent_span_id is not None:
            parent = self._spans.get(context.parent_span_id)
            if parent is None:
                raise ValueError('Trace parent span must be recorded before its child.')
            if parent[0] != context.trace_id or parent[1] != context.task_id:
                raise ValueError('Trace child cannot cross trace or task boundaries.')
        self._spans[context.span_id] = identity


class MemoryTraceSink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self._validator = _TraceRelationValidator()
        self._lock = Lock()

    def emit(self, event: TraceEvent) -> dict[str, object]:
        with self._lock:
            self._validator.validate(event)
            payload = event.to_dict()
            self.events.append(payload)
        return payload


class JsonlTraceSink:
    def __init__(self, root: Path):
        self.root = root
        self._validator = _TraceRelationValidator()
        self._loaded_tasks: set[str] = set()
        self._lock = Lock()

    def _load_existing(self, task_id: str) -> None:
        if task_id in self._loaded_tasks:
            return
        path = self.root / f'{task_id}.jsonl'
        if path.exists():
            for number, line in enumerate(
                path.read_text(encoding='utf-8').splitlines(), 1,
            ):
                try:
                    row = json.loads(line)
                    context = TraceContext(
                        trace_id=str(row['trace_id']),
                        task_id=str(row['task_id']),
                        span_id=str(row['span_id']),
                        actor_id=str(row['actor_id']),
                        parent_span_id=row.get('parent_span_id'),
                    )
                    self._validator.validate(TraceEvent(
                        context=context,
                        component=str(row['component']),
                        kind=str(row['kind']),
                        status=str(row['status']),
                    ))
                except (
                    KeyError, TypeError, ValueError, json.JSONDecodeError,
                ) as exc:
                    raise ValueError(
                        f'Corrupt trace event at line {number}: {exc}',
                    ) from exc
        self._loaded_tasks.add(task_id)

    def emit(self, event: TraceEvent) -> dict[str, object]:
        with self._lock:
            self._load_existing(event.context.task_id)
            self._validator.validate(event)
            payload = event.to_dict()
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / f'{event.context.task_id}.jsonl'
            with path.open('a', encoding='utf-8', newline='\n') as handle:
                handle.write(json.dumps(
                    payload, ensure_ascii=False, separators=(',', ':'),
                ) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            return payload
