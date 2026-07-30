'''CEO-controlled durable DAG orchestration for the private AI group.'''

from __future__ import annotations

import uuid
from dataclasses import dataclass
from time import monotonic

from .acceptance import IndependentAcceptanceValidator
from .budget import BudgetLedger
from .economic_ledger import EconomicBudgetTruth
from .dag import DagNodeSpec, DurableDagRunner, validate_dag
from .executive import new_executive_task, transition_executive_task
from .executors import DepartmentExecutor
from .governed_dag import GovernedDepartmentDagExecutor
from .organization import OrganizationGraph
from .permissions import PermissionPolicy
from .root import resolve_portable_path
from .storage import ContentAddressedStore
from .task_ledger import TaskLedger
from .timebase import authoritative_timestamp
from .trace import JsonlTraceSink, TraceContext, TraceEvent, TraceSink


@dataclass(frozen=True)
class OrchestrationResult:
    task: dict[str, object]
    events: list[dict[str, object]]
    evidence: list[dict[str, object]]
    artifacts: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return {
            'task': self.task,
            'events': self.events,
            'evidence': self.evidence,
            'artifacts': self.artifacts,
        }


class GroupOrchestrator:
    '''The CEO is the only human-facing entry point and owns the execution DAG.'''

    def __init__(
        self,
        graph: OrganizationGraph,
        executors: dict[str, DepartmentExecutor],
        validator: IndependentAcceptanceValidator | None = None,
        budget_ledger: BudgetLedger | None = None,
        permission_policy: PermissionPolicy | None = None,
        trace_sink: TraceSink | None = None,
        max_workers: int = 4,
        economic_budget: EconomicBudgetTruth | None = None,
    ):
        self.graph = graph
        self.executors = executors
        self.validator = validator or IndependentAcceptanceValidator()
        self.budget_ledger = budget_ledger
        self.economic_budget = economic_budget
        declared_actions = {'execute'} | {
            str(policy['action'])
            for department in graph.departments.values()
            if isinstance((policy := department.get('execution_policy')), dict)
            and isinstance(policy.get('action'), str)
        }
        self.permission_policy = permission_policy or PermissionPolicy(
            maximum_level='L2',
            allowed_actions=frozenset(declared_actions),
            allowed_resource_prefixes=(
                'group://knowledge/', 'group://artifact/', 'group://task/',
            ),
        )
        paths = graph.group.get('paths')
        if not isinstance(paths, dict):
            raise ValueError('Group paths are missing.')
        tasks_root = resolve_portable_path(
            graph.root, paths.get('tasks'), 'paths.tasks',
        )
        checkpoints_root = resolve_portable_path(
            graph.root, paths.get('checkpoints'), 'paths.checkpoints',
        )
        provenance_root = resolve_portable_path(
            graph.root, paths.get('provenance'), 'paths.provenance',
        )
        self.ledger = TaskLedger(tasks_root / 'events', checkpoints_root)
        self.dag_ledger = TaskLedger(
            tasks_root / 'events', checkpoints_root / 'dag',
        )
        self.dag_runner = DurableDagRunner(
            self.dag_ledger, max_workers=max_workers,
        )
        self.store = ContentAddressedStore(
            graph.root, provenance_root / 'content',
        )
        self.trace_sink = trace_sink or JsonlTraceSink(provenance_root / 'traces')
        for executor in self.executors.values():
            setter = getattr(executor, 'set_trace_sink', None)
            if callable(setter):
                setter(self.trace_sink)

    def build_publication_service(self) -> 'PublicationService':
        '''Construct the W2-1 publication control plane (deny-all transport).

        The orchestrator is the CEO-side entry point. The publication service
        is platform-neutral and shadow-only; the production transport is
        deny-all, so no external platform effect can occur through this path.
        '''
        from .economic_ledger import EconomicLedger
        from .publication import PublicationService
        from .platform_adapter import DenyAllShadowTransport

        return PublicationService(
            self.graph.root,
            economic_ledger=EconomicLedger(self.graph.root / 'economics'),
            transport=DenyAllShadowTransport(),
        )

    def _persist(
        self, task: dict[str, object], kind: str, payload: dict[str, object],
    ) -> None:
        task_id = str(task['task_id'])
        self.ledger.append(task_id, kind, payload)
        self.ledger.write_checkpoint(task_id, task)

    @staticmethod
    def _assignment_cost_fen(assignment: dict[str, object]) -> int:
        metrics = assignment.get('metrics')
        if not isinstance(metrics, dict):
            return 0
        return int(metrics.get('cost_fen', 0)) + int(metrics.get('tool_cost_fen', 0))

    def _business_outputs(
        self, artifact_records: list[dict[str, object]],
    ) -> dict[str, dict[str, object]]:
        outputs: dict[str, dict[str, object]] = {}
        schema_keys = {
            'research-brief/v0': 'research',
            'execution-plan/v0': 'operations',
            'audit-report/v0': 'audit',
        }
        for record in artifact_records:
            try:
                content = self.store.read(record)
            except (OSError, ValueError):
                continue
            payload = content.get('payload') if isinstance(content, dict) else None
            if not isinstance(payload, dict):
                continue
            key = schema_keys.get(str(payload.get('schema_version') or ''))
            if key:
                outputs[key] = dict(payload)
        return outputs

    def _build_assignments(
        self,
        capabilities: list[str],
        dependencies: dict[str, list[str]],
    ) -> list[dict[str, object]]:
        assignments: list[dict[str, object]] = []
        for index, capability in enumerate(capabilities, 1):
            routes = self.graph.route(capability)
            if not routes:
                raise ValueError(f'No department can execute capability {capability!r}.')
            department_id = str(routes[0]['department_id'])
            if department_id not in self.executors:
                raise ValueError(f'No executor is registered for {department_id!r}.')
            assignments.append({
                'assignment_id': f'assignment-{index:03d}',
                'sequence': index,
                'capability': capability,
                'department_id': department_id,
                'dependencies': [],
                'status': 'queued',
            })

        known = set(capabilities)
        unknown = set(dependencies) - known
        unknown.update(
            dependency
            for values in dependencies.values()
            for dependency in values
            if dependency not in known
        )
        if unknown:
            raise ValueError(f'Unknown DAG capabilities: {sorted(unknown)}.')
        assignment_ids = {
            str(item['capability']): str(item['assignment_id'])
            for item in assignments
        }
        for assignment in assignments:
            capability = str(assignment['capability'])
            assignment['dependencies'] = [
                assignment_ids[item]
                for item in dict.fromkeys(dependencies.get(capability, []))
            ]
        return assignments

    def _build_nodes(
        self,
        task_id: str,
        instruction: str,
        assignments: list[dict[str, object]],
    ) -> tuple[DagNodeSpec, ...]:
        nodes = []
        for assignment in assignments:
            department_id = str(assignment['department_id'])
            policy = self.graph.departments[department_id].get('execution_policy')
            if not isinstance(policy, dict):
                policy = {}
            compensation = policy.get('compensation')
            nodes.append(DagNodeSpec(
                node_id=str(assignment['assignment_id']),
                department_id=department_id,
                capability=str(assignment['capability']),
                objective=instruction,
                dependencies=tuple(str(item) for item in assignment['dependencies']),
                max_attempts=int(policy.get('max_attempts', 1)),
                timeout_seconds=float(policy.get('timeout_seconds', 60.0)),
                idempotency_key=(
                    task_id + '.' + str(assignment['assignment_id'])
                ),
                compensation=(
                    str(compensation) if isinstance(compensation, str) else None
                ),
            ))
        return tuple(nodes)

    @staticmethod
    def _collect_dag_outputs(
        assignments: list[dict[str, object]],
        dag_state: dict[str, object],
    ) -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        completed_assignments: list[dict[str, object]] = []
        evidence_records: list[dict[str, object]] = []
        artifact_records: list[dict[str, object]] = []
        node_states = dag_state['nodes']
        for original in assignments:
            node_state = node_states[str(original['assignment_id'])]
            result = node_state.get('result')
            if isinstance(result, dict) and isinstance(result.get('assignment'), dict):
                assignment = dict(result['assignment'])
                evidence_records.extend(
                    dict(item) for item in result.get('evidence_records', [])
                    if isinstance(item, dict)
                )
                artifact_records.extend(
                    dict(item) for item in result.get('artifact_records', [])
                    if isinstance(item, dict)
                )
            else:
                error = node_state.get('error')
                error = error if isinstance(error, dict) else {}
                code = str(error.get('code') or 'dag-node-incomplete')
                exception_type = error.get('exception_type')
                unresolved = [
                    f'{code}:{exception_type}' if exception_type else code,
                ]
                assignment = {
                    **original,
                    'status': str(node_state.get('status') or 'failed'),
                    'outcome_code': code,
                    'evidence_refs': [], 'artifact_refs': [], 'metrics': {},
                    'confidence_milli': 0,
                    'unresolved_codes': unresolved,
                    'provider_id': None, 'model_id': None,
                    'acceptance': {'passed': False, 'codes': unresolved},
                }
            if node_state.get('status') == 'compensated':
                assignment['status'] = 'compensated'
                assignment['compensation_result'] = node_state.get(
                    'compensation_result',
                )
                assignment['unresolved_codes'] = list(dict.fromkeys([
                    *assignment.get('unresolved_codes', []),
                    'compensated-after-dag-failure',
                ]))
            completed_assignments.append(assignment)
        return completed_assignments, evidence_records, artifact_records

    def run(
        self,
        *,
        instruction: str,
        capabilities: list[str],
        acceptance_criteria: list[str],
        input_refs: list[str] | None = None,
        task_id: str | None = None,
        approval_tickets: dict[str, str] | None = None,
        dependencies: dict[str, list[str]] | None = None,
        resume: bool = False,
        max_new_terminal_nodes: int | None = None,
    ) -> OrchestrationResult:
        normalized_capabilities = list(dict.fromkeys(
            item.strip().lower() for item in capabilities if item.strip()
        ))
        if not instruction.strip():
            raise ValueError('instruction must be non-empty.')
        if not normalized_capabilities:
            raise ValueError('At least one capability is required.')
        if not acceptance_criteria:
            raise ValueError('At least one acceptance criterion is required.')
        if max_new_terminal_nodes is not None and max_new_terminal_nodes < 1:
            raise ValueError('max_new_terminal_nodes must be positive.')
        normalized_input_refs = list(dict.fromkeys(
            item.strip() for item in (input_refs or []) if item.strip()
        ))
        approval_tickets = approval_tickets or {}
        assignments = self._build_assignments(
            normalized_capabilities, dependencies or {},
        )

        task_id = task_id or f'task-{uuid.uuid4().hex[:12]}'
        nodes = self._build_nodes(task_id, instruction, assignments)
        validate_dag(nodes)
        checkpoint = self.ledger.read_checkpoint(task_id)
        existing_events = any(self.ledger.events(task_id))
        task_started_at = authoritative_timestamp()
        task_started = monotonic()
        if checkpoint is not None or existing_events:
            if not resume:
                raise ValueError(f'task_id {task_id!r} already exists.')
            if checkpoint is None or not isinstance(checkpoint.get('state'), dict):
                raise ValueError('CEO checkpoint is missing for DAG resume.')
            task = dict(checkpoint['state'])
            if task.get('state') != 'execute':
                raise ValueError('Only an executing CEO task can resume.')
            if task.get('original_instruction') != instruction:
                raise ValueError('Resume instruction does not match the checkpoint.')
            if task.get('acceptance_criteria') != acceptance_criteria:
                raise ValueError('Resume acceptance criteria do not match the checkpoint.')
            if task.get('input_refs', []) != normalized_input_refs:
                raise ValueError('Resume input refs do not match the checkpoint.')
            trace = task.get('trace_context')
            if not isinstance(trace, dict):
                raise ValueError('CEO trace context is missing for DAG resume.')
            task_trace = TraceContext(
                trace_id=str(trace['trace_id']), task_id=task_id,
                span_id=str(trace['span_id']), actor_id=str(trace['actor_id']),
                parent_span_id=(
                    str(trace['parent_span_id'])
                    if isinstance(trace.get('parent_span_id'), str) else None
                ),
            )
            self.ledger.append(task_id, 'execution-resumed', {
                'execution_model': 'durable-dag/v0',
            })
            trace_kind = 'orchestration-resume'
        else:
            if resume:
                raise ValueError(f'task_id {task_id!r} has no resumable checkpoint.')
            command_id = f'command-{uuid.uuid4().hex[:12]}'
            executive_id = str(self.graph.executive['executive_id'])
            task_trace = TraceContext.root(task_id, executive_id)
            trace_kind = 'orchestration'
        self.trace_sink.emit(TraceEvent(
            context=task_trace, component='task', kind=trace_kind,
            status='started', started_at=task_started_at,
            attributes={
                'capability_count': len(normalized_capabilities),
                'assignment_count': len(assignments),
                'execution_model': 'durable-dag/v0',
                'resumed': resume,
            },
        ))

        if not resume:
            task = new_executive_task(
                task_id=task_id, command_id=command_id,
                executive_id=executive_id,
                original_instruction=instruction,
                acceptance_criteria=acceptance_criteria,
                idempotency_key=f'idem-{task_id}',
            )
            task['trace_context'] = {
                'trace_id': task_trace.trace_id,
                'span_id': task_trace.span_id,
                'actor_id': task_trace.actor_id,
                'parent_span_id': task_trace.parent_span_id,
            }
            task['input_refs'] = normalized_input_refs
            self._persist(task, 'human-command-received', {
                'command_id': command_id, 'instruction': instruction,
                'acceptance_criteria': acceptance_criteria,
            })
            task = transition_executive_task(task, 'ceo_intake')
            self._persist(task, 'state-transition', {'state': task['state']})
            task = transition_executive_task(task, 'clarify_or_accept')
            self._persist(task, 'command-accepted', {'state': task['state']})
            plan = [
                {
                    'step': item['sequence'],
                    'capability': item['capability'],
                    'dependencies': item['dependencies'],
                }
                for item in assignments
            ]
            task = transition_executive_task(task, 'decompose', plan=plan)
            self._persist(task, 'plan-created', {
                'plan': plan, 'execution_model': 'durable-dag/v0',
            })
            task = transition_executive_task(task, 'route', assignments=assignments)
            self._persist(task, 'assignments-routed', {'assignments': assignments})
            task = transition_executive_task(task, 'execute')
            self._persist(task, 'execution-started', {
                'count': len(assignments), 'execution_model': 'durable-dag/v0',
            })

        governed_executor = GovernedDepartmentDagExecutor(
            task_id=task_id, graph=self.graph,
            assignments={str(item['assignment_id']): item for item in assignments},
            executors=self.executors,
            acceptance_criteria=acceptance_criteria,
            input_refs=normalized_input_refs,
            validator=self.validator,
            permission_policy=self.permission_policy,
            budget_ledger=self.budget_ledger,
            economic_budget=self.economic_budget,
            approval_tickets=approval_tickets,
            ledger=self.ledger, store=self.store,
            trace_sink=self.trace_sink, task_trace=task_trace,
        )
        dag_result = self.dag_runner.run(
            task_id=task_id,
            nodes=nodes,
            executor=governed_executor,
            max_new_terminal_nodes=max_new_terminal_nodes,
        )
        completed_assignments, evidence_records, artifact_records = (
            self._collect_dag_outputs(assignments, dag_result.state)
        )

        if dag_result.state['status'] == 'paused':
            task['assignments'] = completed_assignments
            task['evidence_refs'] = [item['ref'] for item in evidence_records]
            task['artifact_refs'] = [item['ref'] for item in artifact_records]
            task['updated_at'] = authoritative_timestamp()
            self._persist(task, 'execution-paused', {
                'completed': sum(
                    item.get('status') in {'completed', 'compensated'}
                    for item in completed_assignments
                ),
                'remaining': sum(
                    item.get('status') not in {
                        'completed', 'compensated', 'failed', 'blocked', 'cancelled',
                    }
                    for item in completed_assignments
                ),
                'dag_checkpoint': str(
                    self.dag_ledger.checkpoints_dir
                    / f'{task_id}.checkpoint.json'
                ),
            })
            return OrchestrationResult(
                task=task, events=list(self.ledger.events(task_id)),
                evidence=evidence_records, artifacts=artifact_records,
            )

        failed_assignments = [
            item for item in completed_assignments if item['status'] == 'failed'
        ]
        if failed_assignments:
            unresolved = list(dict.fromkeys(
                code
                for item in failed_assignments
                for code in item.get('unresolved_codes', [])
                if isinstance(code, str)
            ))
            final_report = {
                'status': 'failed',
                'summary': (
                    f'任务执行失败：执行器发生异常，{len(failed_assignments)} 个部门节点失败，'
                    '已保留可用的部分成果和补偿记录。'
                ),
                'execution_model': 'durable-dag/v0',
                'assignments': completed_assignments,
                'evidence_refs': [item['ref'] for item in evidence_records],
                'artifact_refs': [item['ref'] for item in artifact_records],
                'unresolved': unresolved,
                'cost_fen': sum(
                    int(item.get('metrics', {}).get('cost_fen', 0))
                    for item in completed_assignments
                ),
            }
            task = transition_executive_task(
                task, 'failed', assignments=completed_assignments,
                evidence_refs=final_report['evidence_refs'],
                artifact_refs=final_report['artifact_refs'],
                unresolved=unresolved, final_report=final_report,
            )
            self._persist(task, 'task-failed', final_report)
            self.trace_sink.emit(TraceEvent(
                context=task_trace, component='task', kind='orchestration',
                status='failed', started_at=task_started_at,
                finished_at=authoritative_timestamp(),
                duration_ms=round((monotonic() - task_started) * 1000),
                error={'code': 'dag-node-failed', 'count': len(failed_assignments)},
                attributes={'completed_assignments': len(completed_assignments)},
            ))
            raise RuntimeError(final_report['summary'])

        task = transition_executive_task(
            task, 'return', assignments=completed_assignments,
            evidence_refs=[item['ref'] for item in evidence_records],
            artifact_refs=[item['ref'] for item in artifact_records],
        )
        self._persist(task, 'department-returns-received', {
            'completed': len(completed_assignments),
            'dag_status': dag_result.state['status'],
        })
        task = transition_executive_task(task, 'verify')
        self._persist(task, 'verification-started', {
            'assignments': len(completed_assignments),
        })
        blocked = [
            item for item in completed_assignments
            if item.get('status') != 'completed'
        ]
        acceptance = self.validator.validate(
            completed_assignments, evidence_records, artifact_records,
        )
        total_cost_fen = sum(
            self._assignment_cost_fen(item) for item in completed_assignments
        )
        business_outputs = self._business_outputs(artifact_records)
        if blocked or not acceptance.passed:
            unresolved = [
                code
                for item in blocked
                for code in item.get('unresolved_codes', [])
                if isinstance(code, str)
            ]
            unresolved.extend(acceptance.codes)
            unresolved = list(dict.fromkeys(unresolved))
            final_report = {
                'status': 'blocked',
                'summary': (
                    f'任务受阻：成功 {len(completed_assignments) - len(blocked)} 个，'
                    f'失败或阻塞 {len(blocked)} 个，待解决问题 {len(unresolved)} 个。'
                ),
                'execution_model': 'durable-dag/v0',
                'dag_status': dag_result.state['status'],
                'assignments': completed_assignments,
                'evidence_refs': [item['ref'] for item in evidence_records],
                'artifact_refs': [item['ref'] for item in artifact_records],
                'unresolved': unresolved,
                'cost_fen': total_cost_fen,
                'currency': 'CNY',
                'business_outputs': business_outputs,
                'acceptance': {
                    'passed': acceptance.passed,
                    'codes': list(acceptance.codes),
                    'metrics': acceptance.metrics,
                },
            }
            task = transition_executive_task(
                task, 'blocked', unresolved=unresolved,
                final_report=final_report,
            )
            self._persist(task, 'task-blocked', final_report)
            self.trace_sink.emit(TraceEvent(
                context=task_trace, component='task', kind='orchestration',
                status='blocked', started_at=task_started_at,
                finished_at=authoritative_timestamp(),
                duration_ms=round((monotonic() - task_started) * 1000),
                error={
                    'code': 'acceptance-blocked',
                    'unresolved_count': len(unresolved),
                },
                attributes={'completed_assignments': len(completed_assignments)},
            ))
            return OrchestrationResult(
                task=task, events=list(self.ledger.events(task_id)),
                evidence=evidence_records, artifacts=artifact_records,
            )

        self._persist(task, 'verification-passed', {
            'assignments': len(completed_assignments),
            'evidence': len(evidence_records),
            'artifacts': len(artifact_records),
            'acceptance_metrics': acceptance.metrics,
        })
        task = transition_executive_task(task, 'aggregate')
        self._persist(task, 'results-aggregated', {
            'outcome_codes': [
                item['outcome_code'] for item in completed_assignments
            ],
            'cost_fen': total_cost_fen,
        })
        research = business_outputs.get('research', {})
        operations = business_outputs.get('operations', {})
        audit = business_outputs.get('audit', {})
        business_summary = None
        source_records = research.get('source_records', []) if isinstance(research, dict) else []
        source_count = len(source_records) if isinstance(source_records, list) else 0
        source_comparison = research.get('comparison', {}) if isinstance(research, dict) else {}
        if research and operations and audit:
            business_summary = (
                f"\u516c\u53f8\u8fd0\u884c\u5b8c\u6210\u3002Research \u7ed3\u8bba\uff1a{research.get('conclusion')} "
                f"Operations \u5df2\u5f62\u6210 {len(operations.get('work_items', []))} \u9879\u884c\u52a8\u8ba1\u5212\uff1b"
                f"Audit \u7ed3\u8bba\uff1a{audit.get('verdict')}\uff1b"
                f"\u603b\u6210\u672c {total_cost_fen / 100:.2f} \u5143\u4eba\u6c11\u5e01\u3002"
            )
        final_report = {
            'status': 'completed',
            'summary': business_summary or (
                f'任务完成：{len(completed_assignments)} 个部门节点全部通过独立验收，'
                f'登记 {len(evidence_records)} 条证据、{len(artifact_records)} 个产物，'
                f'成本 {total_cost_fen / 100:.2f} 元人民币。'
            ),
            'execution_model': 'durable-dag/v0',
            'dag_status': dag_result.state['status'],
            'assignments': completed_assignments,
            'evidence_refs': [item['ref'] for item in evidence_records],
            'artifact_refs': [item['ref'] for item in artifact_records],
            'cost_fen': total_cost_fen,
            'currency': 'CNY',
            'research_conclusion': research.get('conclusion'),
            'research_source_count': source_count,
            'source_comparison': source_comparison,
            'operations_plan': operations.get('work_items', []),
            'audit_verdict': audit.get('verdict'),
            'recommended_next_action': (
                audit.get('recommended_next_action')
                or operations.get('immediate_next_action')
            ),
            'business_outputs': business_outputs,
            'acceptance': {
                'passed': acceptance.passed,
                'codes': list(acceptance.codes),
                'metrics': acceptance.metrics,
            },
        }
        task = transition_executive_task(
            task, 'human_report', final_report=final_report,
        )
        self._persist(task, 'human-report-ready', final_report)
        self.trace_sink.emit(TraceEvent(
            context=task_trace, component='task', kind='orchestration',
            status='completed', started_at=task_started_at,
            finished_at=authoritative_timestamp(),
            duration_ms=round((monotonic() - task_started) * 1000),
            cost_fen=total_cost_fen,
            attributes={
                'completed_assignments': len(completed_assignments),
                'evidence_count': len(evidence_records),
                'artifact_count': len(artifact_records),
                'execution_model': 'durable-dag/v0',
            },
        ))
        return OrchestrationResult(
            task=task, events=list(self.ledger.events(task_id)),
            evidence=evidence_records, artifacts=artifact_records,
        )
