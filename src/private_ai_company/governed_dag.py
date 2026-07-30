'''CEO governance adapter for durable department DAG execution.'''

from __future__ import annotations

from threading import RLock
from time import monotonic
from typing import Mapping

from .acceptance import IndependentAcceptanceValidator
from .budget import BudgetLedger, UsageRecord
from .economic_ledger import EconomicBudgetTruth, EconomicLedgerError
from .dag import DagNodeSpec, DepartmentDagExecutor
from .executors import DepartmentExecutor, DepartmentResult
from .internal_ir import (
    delegation_body, encode_ir_text, new_ir_packet, report_body,
)
from .organization import OrganizationGraph
from .permissions import ActionProposal, PermissionPolicy
from .storage import ContentAddressedStore
from .task_ledger import TaskLedger
from .timebase import authoritative_timestamp
from .trace import TraceContext, TraceEvent, TraceSink


def _result_from_mapping(value: Mapping[str, object]) -> DepartmentResult:
    metrics = value.get('metrics')
    return DepartmentResult(
        status=str(value.get('status') or 'failed'),
        outcome_code=str(value.get('outcome_code') or 'executor-result-invalid'),
        evidence=[dict(item) for item in value.get('evidence', []) if isinstance(item, dict)],
        artifacts=[dict(item) for item in value.get('artifacts', []) if isinstance(item, dict)],
        metrics=dict(metrics) if isinstance(metrics, dict) else {},
        confidence_milli=int(value.get('confidence_milli') or 0),
        unresolved_codes=[
            str(item) for item in value.get('unresolved_codes', [])
            if isinstance(item, str)
        ],
        provider_id=(
            str(value['provider_id']) if isinstance(value.get('provider_id'), str)
            else None
        ),
        model_id=(
            str(value['model_id']) if isinstance(value.get('model_id'), str)
            else None
        ),
    )


class GovernedDepartmentDagExecutor:
    '''Apply permission, CNY budget, trace, storage, and acceptance per DAG node.'''

    def __init__(
        self,
        *,
        task_id: str,
        graph: OrganizationGraph,
        assignments: Mapping[str, dict[str, object]],
        executors: Mapping[str, DepartmentExecutor],
        acceptance_criteria: list[str],
        input_refs: list[str],
        validator: IndependentAcceptanceValidator,
        permission_policy: PermissionPolicy,
        budget_ledger: BudgetLedger | None,
        approval_tickets: Mapping[str, str],
        ledger: TaskLedger,
        store: ContentAddressedStore,
        trace_sink: TraceSink,
        task_trace: TraceContext,
        economic_budget: EconomicBudgetTruth | None = None,
    ):
        self.task_id = task_id
        self.graph = graph
        self.assignments = {key: dict(value) for key, value in assignments.items()}
        self.executors = dict(executors)
        self.validator = validator
        self.permission_policy = permission_policy
        self.budget_ledger = budget_ledger
        self.economic_budget = economic_budget
        self.approval_tickets = dict(approval_tickets)
        self.ledger = ledger
        self.store = store
        self.trace_sink = trace_sink
        self.task_trace = task_trace
        self._lock = RLock()
        self._reserved_by_category: dict[str, int] = {}
        trace_contexts = {
            node_id: task_trace.child(str(assignment['department_id']))
            for node_id, assignment in self.assignments.items()
        }
        self.trace_contexts = trace_contexts
        self.adapter = DepartmentDagExecutor(
            self.executors,
            acceptance_criteria=acceptance_criteria,
            input_refs=input_refs,
            task_id=task_id,
            trace_contexts=trace_contexts,
        )

    def _append(self, kind: str, payload: dict[str, object]) -> None:
        with self._lock:
            self.ledger.append(self.task_id, kind, payload)

    def _emit(self, event: TraceEvent) -> None:
        with self._lock:
            self.trace_sink.emit(event)

    def _budget_preflight(
        self, category: str, estimated_cost_fen: int,
    ) -> tuple[bool, str, dict[str, object]]:
        if self.budget_ledger is None:
            return True, 'budget-not-configured', {}
        with self._lock:
            decision = self.budget_ledger.preflight(category, estimated_cost_fen)
            payload = decision.to_dict()
            if decision.allowed:
                reserved_category = self._reserved_by_category.get(category, 0)
                reserved_total = sum(self._reserved_by_category.values())
                projected_category = (
                    self.budget_ledger.spent_fen(category)
                    + reserved_category + estimated_cost_fen
                )
                projected_operating = (
                    self.budget_ledger.spent_fen()
                    + reserved_total + estimated_cost_fen
                )
                category_cap = self.budget_ledger.policy.category_caps_fen[category]
                operating_cap = (
                    self.budget_ledger.policy.total_fen
                    - self.budget_ledger.policy.reserve_fen
                )
                if projected_category > category_cap:
                    payload.update({
                        'allowed': False,
                        'code': 'budget-category-hard-stop',
                        'projected_category_fen': projected_category,
                    })
                    return False, 'budget-category-hard-stop', payload
                if projected_operating > operating_cap:
                    payload.update({
                        'allowed': False,
                        'code': 'budget-reserve-hard-stop',
                        'projected_operating_fen': projected_operating,
                    })
                    return False, 'budget-reserve-hard-stop', payload
                self._reserved_by_category[category] = (
                    reserved_category + estimated_cost_fen
                )
            return decision.allowed, decision.code, payload

    def _release_reservation(self, category: str | None, amount_fen: int) -> None:
        if category is None or self.budget_ledger is None:
            return
        with self._lock:
            remaining = max(
                0, self._reserved_by_category.get(category, 0) - amount_fen,
            )
            if remaining:
                self._reserved_by_category[category] = remaining
            else:
                self._reserved_by_category.pop(category, None)

    @staticmethod
    def _blocked_result(code: str, *, estimated_cost_fen: int = 0) -> DepartmentResult:
        return DepartmentResult(
            status='blocked', outcome_code=(
                'budget-denied' if code.startswith('budget-') else 'permission-denied'
            ),
            evidence=[], artifacts=[],
            metrics={'estimated_cost_fen': estimated_cost_fen},
            confidence_milli=0, unresolved_codes=[code],
        )

    def execute(self, node: DagNodeSpec) -> Mapping[str, object]:
        return self.execute_with_dependencies(node, {})

    def execute_with_dependencies(
        self,
        node: DagNodeSpec,
        dependency_results: Mapping[str, Mapping[str, object]],
    ) -> Mapping[str, object]:
        assignment = dict(self.assignments[node.node_id])
        department_id = node.department_id
        capability = node.capability
        index = int(assignment['sequence'])
        assignment_trace = self.trace_contexts[node.node_id]
        assignment_started_at = authoritative_timestamp()
        assignment_started = monotonic()
        request = self.adapter.request_for(node, dependency_results)

        self._append('internal-dispatch-ir', {
            'assignment_id': node.node_id,
            'packet': encode_ir_text(new_ir_packet(
                dictionary_id=1, task_ref=1, sender_ref=1,
                receiver_ref=10 + index, act='delegate', status='queued',
                body=delegation_body(
                    objective_refs=[100 + index],
                    acceptance_refs=list(range(
                        200, 200 + len(request.acceptance_criteria),
                    )),
                    idempotency_key=node.idempotency_key,
                ),
            )),
        })

        executor = self.executors[department_id]
        estimated_cost_fen = 0
        estimator = getattr(executor, 'estimate_cost_fen', None)
        if callable(estimator):
            estimated_cost_fen = int(estimator(request))

        department = self.graph.departments[department_id]
        execution_policy = department.get('execution_policy')
        if not isinstance(execution_policy, dict):
            execution_policy = {
                'action': 'execute', 'level': 'L1',
                'resource': f'group://task/{department_id}',
                'reversible': True, 'external_effect': False,
            }
        proposal = ActionProposal(
            action=str(execution_policy['action']),
            level=str(execution_policy['level']),
            resource=str(execution_policy['resource']),
            reversible=bool(execution_policy['reversible']),
            external_effect=bool(execution_policy['external_effect']),
            approval_ticket=self.approval_tickets.get(capability),
        )
        authorization = self.permission_policy.authorize(proposal)
        self._append('authorization-decision', {
            'assignment_id': node.node_id, 'allowed': authorization.allowed,
            'code': authorization.code, 'action': proposal.action,
            'level': proposal.level, 'resource': proposal.resource,
        })

        result: DepartmentResult | None = None
        budget_category = execution_policy.get('budget_category')
        reserved_category: str | None = None
        reserved_econ_id: str | None = None
        if not authorization.allowed:
            result = self._blocked_result(authorization.code)
        elif estimated_cost_fen > 0 and not isinstance(budget_category, str):
            payload = {
                'assignment_id': node.node_id, 'allowed': False,
                'code': 'budget-category-missing',
                'estimated_amount_fen': estimated_cost_fen, 'currency': 'CNY',
            }
            self._append('budget-preflight', payload)
            result = self._blocked_result(
                'budget-category-missing', estimated_cost_fen=estimated_cost_fen,
            )
        elif self.economic_budget is not None and isinstance(budget_category, str):
            decision = self.economic_budget.preflight(budget_category, estimated_cost_fen)
            payload = {
                'assignment_id': node.node_id,
                'allowed': decision['allowed'],
                'code': decision['code'],
                'category': budget_category,
                'estimated_amount_fen': estimated_cost_fen,
                'currency': 'CNY',
                'warning': decision.get('warning', False),
            }
            self._append('budget-preflight', payload)
            if decision['allowed']:
                try:
                    econ_res = self.economic_budget.reserve(
                        self.task_id, budget_category, estimated_cost_fen,
                        idempotency_key=f'{self.task_id}.{node.node_id}.reserve',
                        run_id=self.task_id, department_id=department_id,
                    )
                    reserved_econ_id = econ_res['payload']['reservation_id']
                except EconomicLedgerError as exc:
                    self._append('budget-preflight', {
                        'assignment_id': node.node_id, 'allowed': False,
                        'code': exc.code, 'currency': 'CNY',
                    })
                    result = self._blocked_result(
                        exc.code, estimated_cost_fen=estimated_cost_fen,
                    )
            else:
                result = self._blocked_result(
                    decision['code'], estimated_cost_fen=estimated_cost_fen,
                )
        elif self.budget_ledger is not None and isinstance(budget_category, str):
            allowed, code, payload = self._budget_preflight(
                budget_category, estimated_cost_fen,
            )
            payload['assignment_id'] = node.node_id
            self._append('budget-preflight', payload)
            if allowed:
                reserved_category = budget_category
            else:
                result = self._blocked_result(
                    code, estimated_cost_fen=estimated_cost_fen,
                )

        if result is None:
            self._append('assignment-started', {
                'assignment_id': node.node_id,
                'department_id': department_id,
                'estimated_cost_fen': estimated_cost_fen,
            })
            self._emit(TraceEvent(
                context=assignment_trace, component='task',
                kind='department-assignment', status='started',
                started_at=assignment_started_at,
                attributes={
                    'assignment_id': node.node_id,
                    'department_id': department_id,
                    'capability': capability,
                    'estimated_cost_fen': estimated_cost_fen,
                },
            ))
            try:
                result = _result_from_mapping(
                    self.adapter.execute_with_dependencies(node, dependency_results),
                )
            except Exception as exc:
                self._emit(TraceEvent(
                    context=assignment_trace, component='task',
                    kind='department-assignment', status='failed',
                    started_at=assignment_started_at,
                    finished_at=authoritative_timestamp(),
                    duration_ms=round((monotonic() - assignment_started) * 1000),
                    error={
                        'code': f'executor-exception:{type(exc).__name__}',
                        'exception_type': type(exc).__name__,
                    },
                    attributes={'assignment_id': node.node_id},
                ))
                self._release_reservation(reserved_category, estimated_cost_fen)
                if reserved_econ_id is not None and self.economic_budget is not None:
                    try:
                        self.economic_budget.release(reserved_econ_id)
                    except EconomicLedgerError:
                        pass
                raise

        cost_fen = int(result.metrics.get('cost_fen', 0))
        try:
            if self.economic_budget is not None and reserved_econ_id is not None:
                self.economic_budget.commit(
                    reserved_econ_id, cost_fen,
                    source=f'provider:{result.provider_id or "unknown"}/{result.model_id or "unknown"}',
                    task_id=self.task_id, run_id=self.task_id,
                    department_id=department_id,
                )
            elif self.budget_ledger is not None and cost_fen:
                provider_id = result.provider_id or 'unknown'
                model_id = result.model_id or 'unknown'
                actual_category = (
                    str(budget_category)
                    if isinstance(budget_category, str) else 'model_api'
                )
                with self._lock:
                    self.budget_ledger.append(UsageRecord(
                        task_id=self.task_id, category=actual_category,
                        amount_fen=cost_fen,
                        source=f'provider:{provider_id}/{model_id}',
                        note=f'assignment:{node.node_id}',
                    ))
        finally:
            self._release_reservation(reserved_category, estimated_cost_fen)
            if (
                reserved_econ_id is not None
                and self.economic_budget is not None
                and result is not None
                and result.status != 'completed'
            ):
                # Reservation stays committed on success; release on any
                # non-completed terminal state so the cap is restored.
                try:
                    self.economic_budget.release(reserved_econ_id)
                except EconomicLedgerError:
                    pass

        with self._lock:
            local_evidence = [
                self.store.put('evidence', self.task_id, item)
                for item in result.evidence
            ]
            local_artifacts = [
                self.store.put('artifact', self.task_id, item)
                for item in result.artifacts
            ]

        assignment.update({
            'status': result.status,
            'outcome_code': result.outcome_code,
            'evidence_refs': [item['ref'] for item in local_evidence],
            'artifact_refs': [item['ref'] for item in local_artifacts],
            'metrics': result.metrics,
            'confidence_milli': result.confidence_milli,
            'unresolved_codes': list(result.unresolved_codes),
            'provider_id': result.provider_id,
            'model_id': result.model_id,
            'acceptance': {'passed': False, 'codes': []},
        })
        if result.status == 'completed':
            acceptance = self.validator.validate(
                [assignment], local_evidence, local_artifacts,
            )
            assignment['acceptance'] = {
                'passed': acceptance.passed,
                'codes': list(acceptance.codes),
                'metrics': acceptance.metrics,
            }
            if not acceptance.passed:
                assignment['status'] = 'blocked'
                assignment['outcome_code'] = 'acceptance-failed'
                assignment['unresolved_codes'] = list(dict.fromkeys([
                    *assignment['unresolved_codes'], *acceptance.codes,
                ]))

        self._append('assignment-returned', {
            'assignment_id': node.node_id, 'department_id': department_id,
            'status': assignment['status'],
            'outcome_code': assignment['outcome_code'],
            'estimated_cost_fen': estimated_cost_fen,
            'actual_cost_fen': cost_fen,
            'acceptance_passed': assignment['acceptance']['passed'],
        })
        self._emit(TraceEvent(
            context=assignment_trace, component='task',
            kind='department-assignment',
            status='completed' if assignment['status'] == 'completed' else 'blocked',
            started_at=assignment_started_at,
            finished_at=authoritative_timestamp(),
            duration_ms=round((monotonic() - assignment_started) * 1000),
            usage={
                key: int(result.metrics[key])
                for key in ('input_tokens', 'output_tokens', 'total_tokens')
                if key in result.metrics
            },
            cost_fen=cost_fen,
            attributes={
                'assignment_id': node.node_id,
                'outcome_code': assignment['outcome_code'],
                'provider_id': result.provider_id,
                'model_id': result.model_id,
                'acceptance_passed': assignment['acceptance']['passed'],
            },
        ))
        self._append('internal-return-ir', {
            'assignment_id': node.node_id,
            'packet': encode_ir_text(new_ir_packet(
                dictionary_id=1, task_ref=1, sender_ref=10 + index,
                receiver_ref=1, act='report', status=str(assignment['status']),
                body=report_body(
                    outcome_refs=[300 + index], metric_codes=result.metrics,
                    unresolved_refs=[
                        400 + offset for offset, _ in enumerate(
                            assignment['unresolved_codes'], 1,
                        )
                    ],
                    evidence_refs=[
                        500 + offset for offset, _ in enumerate(local_evidence, 1)
                    ],
                    artifact_refs=[
                        600 + offset for offset, _ in enumerate(local_artifacts, 1)
                    ],
                    confidence_milli=result.confidence_milli,
                ),
            )),
        })
        return {
            'status': assignment['status'],
            'outcome_code': assignment['outcome_code'],
            'assignment': assignment,
            'evidence_records': local_evidence,
            'artifact_records': local_artifacts,
        }

    def compensate(
        self, node: DagNodeSpec, result: Mapping[str, object],
    ) -> Mapping[str, object]:
        executor = self.executors[node.department_id]
        callback = getattr(executor, 'compensate', None)
        if callable(callback):
            return dict(callback(node, result))
        return {
            'status': 'no-op',
            'code': 'department-compensation-not-configured',
        }
