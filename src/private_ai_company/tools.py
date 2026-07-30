'''Controlled local tool registry and dispatch boundary for group departments.'''

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Callable, Mapping
from urllib.parse import unquote, urlsplit

from .budget import BudgetLedger, UsageRecord
from .permissions import ActionProposal, LEVELS, PermissionPolicy
from .task_ledger import TaskLedger
from .timebase import authoritative_timestamp
from .trace import TraceContext, TraceEvent, TraceSink


ToolHandler = Callable[['ToolRequest'], Mapping[str, object]]


def _valid_resource(resource: str) -> bool:
    if not resource or '\\' in resource or '\x00' in resource:
        return False
    parsed = urlsplit(resource)
    if not parsed.scheme:
        return False
    segments = unquote(parsed.path).split('/')
    return all(segment not in {'.', '..'} for segment in segments)


def _resource_is_within(resource: str, prefix: str) -> bool:
    if not _valid_resource(resource) or not _valid_resource(prefix):
        return False
    normalized = prefix.rstrip('/')
    return resource == normalized or resource.startswith(normalized + '/')


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str
    action: str
    permission_level: str
    resource_prefixes: tuple[str, ...]
    budget_category: str | None
    external_effect: bool
    reversible: bool
    timeout_seconds: int
    idempotent: bool
    required_capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.tool_id.strip() or any(
            character in self.tool_id for character in '/\\:'
        ):
            raise ValueError('tool_id must be a portable identifier.')
        if not self.action.strip():
            raise ValueError('Tool action must be non-empty.')
        if self.permission_level not in LEVELS:
            raise ValueError('Tool permission_level must be one of L0-L4.')
        if not self.resource_prefixes or not all(
            _valid_resource(prefix) for prefix in self.resource_prefixes
        ):
            raise ValueError('Tool resource prefixes must be valid URIs.')
        if self.timeout_seconds <= 0:
            raise ValueError('Tool timeout_seconds must be positive.')
        if self.budget_category is not None and not self.budget_category.strip():
            raise ValueError('Tool budget_category must be non-empty when set.')


@dataclass(frozen=True)
class ToolAccessPolicy:
    department_id: str
    allowed_tool_ids: frozenset[str]
    capabilities: frozenset[str]
    permission_policy: PermissionPolicy


@dataclass(frozen=True)
class ToolRequest:
    task_id: str
    department_id: str
    tool_id: str
    resource: str
    arguments: Mapping[str, object] = field(default_factory=dict)
    estimated_cost_fen: int = 0
    approval_ticket: str | None = None
    trace_context: TraceContext | None = None


@dataclass(frozen=True)
class ToolResult:
    status: str
    code: str
    output: Mapping[str, object] = field(default_factory=dict)
    elapsed_ms: int = 0
    charged_cost_fen: int = 0


class ToolRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, tuple[ToolSpec, ToolHandler]] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.tool_id in self._entries:
            raise ValueError(f'Duplicate tool_id {spec.tool_id!r}.')
        if not callable(handler):
            raise TypeError('Tool handler must be callable.')
        self._entries[spec.tool_id] = (spec, handler)

    def resolve(self, tool_id: str) -> tuple[ToolSpec, ToolHandler] | None:
        return self._entries.get(tool_id)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(entry[0] for entry in self._entries.values())


class ControlledToolDispatcher:
    def __init__(
        self,
        registry: ToolRegistry,
        policies: Mapping[str, ToolAccessPolicy],
        ledger: TaskLedger,
        budget_ledger: BudgetLedger | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self.registry = registry
        self.policies = dict(policies)
        self.ledger = ledger
        self.budget_ledger = budget_ledger
        self.trace_sink = trace_sink

    def _event(
        self, request: ToolRequest, kind: str, payload: dict[str, object],
    ) -> None:
        self.ledger.append(request.task_id, kind, {
            'department_id': request.department_id,
            'tool_id': request.tool_id,
            **payload,
        })

    def _deny(self, request: ToolRequest, code: str) -> ToolResult:
        self._event(request, 'tool-denied', {
            'code': code,
            'resource': request.resource,
        })
        if self.trace_sink is not None and request.trace_context is not None:
            self.trace_sink.emit(TraceEvent(
                context=request.trace_context, component='tool',
                kind='tool-dispatch', status='denied',
                finished_at=authoritative_timestamp(),
                error={'code': code},
                attributes={
                    'department_id': request.department_id,
                    'tool_id': request.tool_id,
                    'resource': request.resource,
                },
            ))
        return ToolResult(status='denied', code=code)

    def dispatch(self, request: ToolRequest) -> ToolResult:
        entry = self.registry.resolve(request.tool_id)
        if entry is None:
            return self._deny(request, 'tool-unregistered')
        spec, handler = entry
        policy = self.policies.get(request.department_id)
        if policy is None:
            return self._deny(request, 'tool-department-policy-missing')
        if policy.department_id != request.department_id:
            return self._deny(request, 'tool-department-policy-mismatch')
        if request.tool_id not in policy.allowed_tool_ids:
            return self._deny(request, 'tool-not-allowlisted')
        if not spec.required_capabilities.issubset(policy.capabilities):
            return self._deny(request, 'tool-capability-denied')
        if not any(
            _resource_is_within(request.resource, prefix)
            for prefix in spec.resource_prefixes
        ):
            return self._deny(request, 'tool-resource-out-of-scope')
        if request.estimated_cost_fen < 0:
            return self._deny(request, 'tool-cost-invalid')

        authorization = policy.permission_policy.authorize(ActionProposal(
            action=spec.action,
            level=spec.permission_level,
            resource=request.resource,
            reversible=spec.reversible,
            external_effect=spec.external_effect,
            cost_fen=request.estimated_cost_fen,
            approval_ticket=request.approval_ticket,
        ))
        self._event(request, 'tool-authorization-decision', {
            'allowed': authorization.allowed,
            'code': authorization.code,
            'level': spec.permission_level,
            'resource': request.resource,
        })
        if not authorization.allowed:
            return self._deny(request, authorization.code)

        if request.estimated_cost_fen > 0 and spec.budget_category is None:
            return self._deny(request, 'tool-budget-category-required')
        if spec.budget_category is not None:
            if self.budget_ledger is None:
                return self._deny(request, 'tool-budget-ledger-required')
            budget = self.budget_ledger.preflight(
                spec.budget_category, request.estimated_cost_fen,
            )
            self._event(request, 'tool-budget-preflight', budget.to_dict())
            if not budget.allowed:
                return self._deny(request, budget.code)

        self._event(request, 'tool-dispatch-started', {
            'action': spec.action,
            'level': spec.permission_level,
            'resource': request.resource,
            'estimated_cost_fen': request.estimated_cost_fen,
            'timeout_seconds': spec.timeout_seconds,
            'idempotent': spec.idempotent,
        })
        started_at = authoritative_timestamp()
        if self.trace_sink is not None and request.trace_context is not None:
            self.trace_sink.emit(TraceEvent(
                context=request.trace_context, component='tool',
                kind='tool-dispatch', status='started',
                started_at=started_at,
                attributes={
                    'department_id': request.department_id,
                    'tool_id': request.tool_id,
                    'action': spec.action,
                    'permission_level': spec.permission_level,
                    'resource': request.resource,
                    'estimated_cost_fen': request.estimated_cost_fen,
                },
            ))
        started = monotonic()
        try:
            output = dict(handler(request))
        except Exception as exc:
            elapsed_ms = round((monotonic() - started) * 1000)
            self._event(request, 'tool-failed', {
                'code': 'tool-handler-error',
                'exception_type': type(exc).__name__,
                'elapsed_ms': elapsed_ms,
            })
            if self.trace_sink is not None and request.trace_context is not None:
                self.trace_sink.emit(TraceEvent(
                    context=request.trace_context, component='tool',
                    kind='tool-dispatch', status='failed',
                    started_at=started_at,
                    finished_at=authoritative_timestamp(),
                    duration_ms=elapsed_ms,
                    error={
                        'code': 'tool-handler-error',
                        'exception_type': type(exc).__name__,
                    },
                    attributes={'tool_id': request.tool_id},
                ))
            return ToolResult(
                status='failed', code='tool-handler-error',
                elapsed_ms=elapsed_ms,
            )

        elapsed_ms = round((monotonic() - started) * 1000)
        if spec.budget_category is not None and request.estimated_cost_fen:
            assert self.budget_ledger is not None
            self.budget_ledger.append(UsageRecord(
                task_id=request.task_id,
                category=spec.budget_category,
                amount_fen=request.estimated_cost_fen,
                source=f'tool:{spec.tool_id}',
                note=f'department:{request.department_id}',
            ))
        self._event(request, 'tool-completed', {
            'code': 'tool-completed',
            'elapsed_ms': elapsed_ms,
            'charged_cost_fen': request.estimated_cost_fen,
            'result_keys': sorted(output),
        })
        if self.trace_sink is not None and request.trace_context is not None:
            self.trace_sink.emit(TraceEvent(
                context=request.trace_context, component='tool',
                kind='tool-dispatch', status='completed',
                started_at=started_at,
                finished_at=authoritative_timestamp(),
                duration_ms=elapsed_ms,
                cost_fen=request.estimated_cost_fen,
                attributes={
                    'tool_id': request.tool_id,
                    'result_keys': sorted(output),
                },
            ))
        return ToolResult(
            status='completed', code='tool-completed', output=output,
            elapsed_ms=elapsed_ms,
            charged_cost_fen=request.estimated_cost_fen,
        )


def build_local_task_ledger(root: Path) -> TaskLedger:
    return TaskLedger(root / 'tasks' / 'events', root / 'checkpoints')
