'''Durable dependency-DAG execution with retry, resume, cancel, and compensation.'''

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from time import monotonic
from typing import Mapping, Protocol

from .executors import DepartmentExecutor, ExecutionRequest
from .task_ledger import TaskLedger
from .timebase import authoritative_timestamp
from .trace import TraceContext


TERMINAL_NODE_STATES = {
    'completed', 'failed', 'blocked', 'cancelled', 'compensated',
}


@dataclass(frozen=True)
class DagNodeSpec:
    node_id: str
    department_id: str
    capability: str
    objective: str
    dependencies: tuple[str, ...] = ()
    max_attempts: int = 1
    timeout_seconds: float = 60.0
    idempotency_key: str = ''
    compensation: str | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ('node_id', self.node_id), ('department_id', self.department_id),
            ('capability', self.capability), ('objective', self.objective),
        ):
            if not value.strip():
                raise ValueError(f'{label} must be non-empty.')
        if any(char in self.node_id for char in '/\\:'):
            raise ValueError('node_id must be a portable identifier.')
        if self.max_attempts < 1:
            raise ValueError('max_attempts must be positive.')
        if self.timeout_seconds <= 0:
            raise ValueError('timeout_seconds must be positive.')
        if self.max_attempts > 1 and not self.idempotency_key.strip():
            raise ValueError('Retried nodes require an idempotency_key.')


class DagExecutor(Protocol):
    def execute(self, node: DagNodeSpec) -> Mapping[str, object]: ...

    def compensate(
        self, node: DagNodeSpec, result: Mapping[str, object],
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class DagRunResult:
    state: dict[str, object]
    events: list[dict[str, object]]


class DepartmentDagExecutor:
    '''Adapter from durable DAG nodes to existing department executors.'''

    def __init__(
        self,
        executors: Mapping[str, DepartmentExecutor],
        *,
        acceptance_criteria: list[str],
        input_refs: list[str] | None = None,
        task_id: str = 'dag-runtime',
        trace_contexts: Mapping[str, TraceContext] | None = None,
    ):
        self.executors = dict(executors)
        self.acceptance_criteria = acceptance_criteria
        self.input_refs = input_refs or []
        self.task_id = task_id
        self.trace_contexts = dict(trace_contexts or {})

    def request_for(
        self,
        node: DagNodeSpec,
        dependency_results: Mapping[str, Mapping[str, object]] | None = None,
    ) -> ExecutionRequest:
        return ExecutionRequest(
            task_id=self.task_id, assignment_id=node.node_id,
            department_id=node.department_id, capability=node.capability,
            objective=node.objective,
            acceptance_criteria=self.acceptance_criteria,
            input_refs=self.input_refs,
            dependency_results=dependency_results or {},
            trace_context=self.trace_contexts.get(node.node_id),
        )

    def execute(self, node: DagNodeSpec) -> Mapping[str, object]:
        return self.execute_with_dependencies(node, {})

    def execute_with_dependencies(
        self,
        node: DagNodeSpec,
        dependency_results: Mapping[str, Mapping[str, object]],
    ) -> Mapping[str, object]:
        executor = self.executors.get(node.department_id)
        if executor is None:
            raise ValueError(f'No executor for {node.department_id!r}.')
        result = executor.execute(self.request_for(node, dependency_results))
        return {
            'status': result.status,
            'outcome_code': result.outcome_code,
            'evidence': result.evidence,
            'artifacts': result.artifacts,
            'metrics': result.metrics,
            'confidence_milli': result.confidence_milli,
            'unresolved_codes': result.unresolved_codes,
            'provider_id': result.provider_id,
            'model_id': result.model_id,
        }

    def compensate(
        self, node: DagNodeSpec, result: Mapping[str, object],
    ) -> Mapping[str, object]:
        return {'status': 'no-op', 'code': 'department-compensation-not-configured'}


def _plan_hash(nodes: tuple[DagNodeSpec, ...]) -> str:
    payload = [
        {
            'node_id': node.node_id,
            'department_id': node.department_id,
            'capability': node.capability,
            'objective': node.objective,
            'dependencies': node.dependencies,
            'max_attempts': node.max_attempts,
            'timeout_seconds': node.timeout_seconds,
            'idempotency_key': node.idempotency_key,
            'compensation': node.compensation,
        }
        for node in nodes
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_dag(nodes: tuple[DagNodeSpec, ...]) -> None:
    if not nodes:
        raise ValueError('DAG requires at least one node.')
    by_id = {node.node_id: node for node in nodes}
    if len(by_id) != len(nodes):
        raise ValueError('DAG node IDs must be unique.')
    for node in nodes:
        if node.node_id in node.dependencies:
            raise ValueError(f'Node {node.node_id!r} cannot depend on itself.')
        missing = set(node.dependencies) - set(by_id)
        if missing:
            raise ValueError(
                f'Node {node.node_id!r} has missing dependencies: {sorted(missing)}.',
            )
    pending = {node.node_id: set(node.dependencies) for node in nodes}
    while pending:
        ready = {node_id for node_id, deps in pending.items() if not deps}
        if not ready:
            raise ValueError('DAG contains a dependency cycle.')
        pending = {
            node_id: deps - ready
            for node_id, deps in pending.items()
            if node_id not in ready
        }


class DurableDagRunner:
    def __init__(
        self, ledger: TaskLedger, *, max_workers: int = 2,
        compensate_on_failure: bool = True,
    ):
        if max_workers < 1:
            raise ValueError('max_workers must be positive.')
        self.ledger = ledger
        self.max_workers = max_workers
        self.compensate_on_failure = compensate_on_failure

    def _save(self, task_id: str, state: dict[str, object]) -> None:
        state['updated_at'] = authoritative_timestamp()
        self.ledger.write_checkpoint(task_id, state)

    def _event(
        self, task_id: str, kind: str, payload: dict[str, object],
    ) -> None:
        self.ledger.append(task_id, kind, payload)

    def _load_or_create(
        self, task_id: str, nodes: tuple[DagNodeSpec, ...],
    ) -> dict[str, object]:
        fingerprint = _plan_hash(nodes)
        checkpoint = self.ledger.read_checkpoint(task_id)
        if checkpoint is not None:
            state = checkpoint.get('state')
            if not isinstance(state, dict) or state.get('schema_version') != 'durable-dag/v0':
                raise ValueError('Existing checkpoint is not a durable DAG state.')
            if state.get('plan_hash') != fingerprint:
                raise ValueError('DAG plan changed for an existing task_id.')
            for node_state in state['nodes'].values():
                if node_state.get('status') == 'running':
                    node_state['status'] = 'ready'
                    node_state['error'] = {'code': 'interrupted-before-checkpoint'}
            self._event(task_id, 'dag-resumed', {'status': state.get('status')})
            return state
        now = authoritative_timestamp()
        state = {
            'schema_version': 'durable-dag/v0', 'task_id': task_id,
            'plan_hash': fingerprint, 'status': 'running',
            'created_at': now, 'updated_at': now,
            'nodes': {
                node.node_id: {
                    'status': 'waiting', 'attempts': 0, 'result': None,
                    'error': None, 'compensation_result': None,
                    'started_at': None, 'finished_at': None,
                }
                for node in nodes
            },
        }
        self._event(task_id, 'dag-created', {'nodes': len(nodes)})
        self._save(task_id, state)
        return state

    @staticmethod
    def _refresh_readiness(
        state: dict[str, object], by_id: dict[str, DagNodeSpec],
    ) -> None:
        node_states = state['nodes']
        for node_id, node in by_id.items():
            current = node_states[node_id]
            if current['status'] not in {'waiting', 'ready'}:
                continue
            dependency_states = [
                node_states[dependency]['status'] for dependency in node.dependencies
            ]
            if any(item in {'failed', 'blocked', 'cancelled'} for item in dependency_states):
                current['status'] = 'blocked'
                current['error'] = {'code': 'dependency-not-completed'}
                current['finished_at'] = authoritative_timestamp()
            elif all(item in {'completed', 'compensated'} for item in dependency_states):
                current['status'] = 'ready'

    def _compensate(
        self, task_id: str, state: dict[str, object],
        nodes: tuple[DagNodeSpec, ...], executor: DagExecutor,
    ) -> None:
        node_states = state['nodes']
        for node in reversed(nodes):
            node_state = node_states[node.node_id]
            if node_state['status'] != 'completed' or node.compensation is None:
                continue
            result = node_state.get('result') or {}
            try:
                compensation = dict(executor.compensate(node, result))
            except Exception as exc:
                node_state['error'] = {
                    'code': 'compensation-failed',
                    'exception_type': type(exc).__name__,
                }
                self._event(task_id, 'dag-compensation-failed', {
                    'node_id': node.node_id,
                    'exception_type': type(exc).__name__,
                })
                continue
            node_state['status'] = 'compensated'
            node_state['compensation_result'] = compensation
            self._event(task_id, 'dag-node-compensated', {
                'node_id': node.node_id, 'action': node.compensation,
            })
            self._save(task_id, state)

    def run(
        self,
        *,
        task_id: str,
        nodes: tuple[DagNodeSpec, ...],
        executor: DagExecutor,
        cancel_requested: bool = False,
        max_new_terminal_nodes: int | None = None,
    ) -> DagRunResult:
        validate_dag(nodes)
        if max_new_terminal_nodes is not None and max_new_terminal_nodes < 1:
            raise ValueError('max_new_terminal_nodes must be positive.')
        by_id = {node.node_id: node for node in nodes}
        state = self._load_or_create(task_id, nodes)
        node_states = state['nodes']
        if state.get('status') in {'completed', 'failed', 'cancelled'}:
            return DagRunResult(state, list(self.ledger.events(task_id)))

        if cancel_requested:
            for node_state in node_states.values():
                if node_state['status'] not in TERMINAL_NODE_STATES:
                    node_state['status'] = 'cancelled'
                    node_state['error'] = {'code': 'task-cancelled'}
                    node_state['finished_at'] = authoritative_timestamp()
            self._compensate(task_id, state, nodes, executor)
            state['status'] = 'cancelled'
            self._event(task_id, 'dag-cancelled', {})
            self._save(task_id, state)
            return DagRunResult(state, list(self.ledger.events(task_id)))

        new_terminal = 0
        pool = ThreadPoolExecutor(max_workers=self.max_workers)
        try:
            while True:
                self._refresh_readiness(state, by_id)
                self._save(task_id, state)
                ready_ids = [
                    node.node_id for node in nodes
                    if node_states[node.node_id]['status'] == 'ready'
                ][:self.max_workers]
                if not ready_ids:
                    break
                futures = {}
                for node_id in ready_ids:
                    spec = by_id[node_id]
                    node_state = node_states[node_id]
                    node_state['status'] = 'running'
                    node_state['attempts'] += 1
                    node_state['started_at'] = authoritative_timestamp()
                    self._event(task_id, 'dag-node-started', {
                        'node_id': node_id,
                        'attempt': node_state['attempts'],
                        'idempotency_key': spec.idempotency_key,
                    })
                    dependency_results = {
                        dependency_id: dict(node_states[dependency_id]['result'])
                        for dependency_id in spec.dependencies
                        if isinstance(node_states[dependency_id].get('result'), dict)
                    }
                    dependency_executor = getattr(
                        executor, 'execute_with_dependencies', None,
                    )
                    if callable(dependency_executor):
                        future = pool.submit(
                            dependency_executor, spec, dependency_results,
                        )
                    else:
                        future = pool.submit(executor.execute, spec)
                    futures[node_id] = (future, monotonic())
                self._save(task_id, state)

                for node_id in ready_ids:
                    spec = by_id[node_id]
                    node_state = node_states[node_id]
                    future, started = futures[node_id]
                    remaining = max(0.0, spec.timeout_seconds - (monotonic() - started))
                    try:
                        result = dict(future.result(timeout=remaining))
                        result_status = str(result.get('status') or 'completed')
                        if result_status == 'blocked':
                            node_state['status'] = 'blocked'
                            node_state['error'] = {
                                'code': str(result.get('outcome_code') or 'executor-blocked'),
                            }
                        elif result_status in {'failed', 'cancelled'}:
                            node_state['status'] = result_status
                            node_state['error'] = {
                                'code': str(
                                    result.get('outcome_code')
                                    or f'executor-{result_status}'
                                ),
                            }
                        else:
                            node_state['status'] = 'completed'
                            node_state['error'] = None
                        node_state['result'] = result
                        node_state['finished_at'] = authoritative_timestamp()
                        self._event(task_id, 'dag-node-finished', {
                            'node_id': node_id,
                            'status': node_state['status'],
                            'attempt': node_state['attempts'],
                        })
                        new_terminal += 1
                    except FutureTimeout:
                        future.cancel()
                        error = {'code': 'node-timeout', 'retryable': True}
                        if node_state['attempts'] < spec.max_attempts:
                            node_state['status'] = 'ready'
                            self._event(task_id, 'dag-node-retry-scheduled', {
                                'node_id': node_id, **error,
                            })
                        else:
                            node_state['status'] = 'failed'
                            node_state['finished_at'] = authoritative_timestamp()
                            self._event(task_id, 'dag-node-failed', {
                                'node_id': node_id, **error,
                            })
                            new_terminal += 1
                        node_state['error'] = error
                    except Exception as exc:
                        error = {
                            'code': 'node-exception',
                            'exception_type': type(exc).__name__,
                            'retryable': True,
                        }
                        if node_state['attempts'] < spec.max_attempts:
                            node_state['status'] = 'ready'
                            self._event(task_id, 'dag-node-retry-scheduled', {
                                'node_id': node_id, **error,
                            })
                        else:
                            node_state['status'] = 'failed'
                            node_state['finished_at'] = authoritative_timestamp()
                            self._event(task_id, 'dag-node-failed', {
                                'node_id': node_id, **error,
                            })
                            new_terminal += 1
                        node_state['error'] = error
                    self._save(task_id, state)

                if (
                    max_new_terminal_nodes is not None
                    and new_terminal >= max_new_terminal_nodes
                ):
                    state['status'] = 'paused'
                    self._event(task_id, 'dag-paused', {
                        'new_terminal_nodes': new_terminal,
                    })
                    self._save(task_id, state)
                    return DagRunResult(state, list(self.ledger.events(task_id)))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        self._refresh_readiness(state, by_id)
        failed = any(
            item['status'] in {'failed', 'blocked'}
            for item in node_states.values()
        )
        incomplete = any(
            item['status'] not in TERMINAL_NODE_STATES
            for item in node_states.values()
        )
        if failed:
            if self.compensate_on_failure:
                self._compensate(task_id, state, nodes, executor)
            state['status'] = 'failed'
        elif incomplete:
            state['status'] = 'paused'
        else:
            state['status'] = 'completed'
        self._event(task_id, 'dag-finished', {'status': state['status']})
        self._save(task_id, state)
        return DagRunResult(state, list(self.ledger.events(task_id)))
