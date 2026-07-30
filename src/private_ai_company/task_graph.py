'''P0-1B Validated Candidate -> Governed Task Graph.

Deterministically compiles a P0-1A ``planning-decision/v1`` (status=validated)
into a ``task-graph/v1`` and produces a ``task-graph-decision/v1`` by re-running
the live organization routes, selected department policy, registered executor,
budget truth, DAG validity, and idempotency. Only an ``executable`` graph may be
dispatched, and only through the existing ``RuntimeCommandGateway`` /
``GroupOrchestrator`` / ``durable-dag/v0`` path. This module never calls a
department executor directly and never weakens L2-L4 policy.

Flow:

    planning-decision/v1(status=validated)
    -> task-graph/v1 (deterministic, hash-bound)
    -> task-graph-decision/v1(executable | blocked | requires_owner)
    -> intent-execution-result/v1
    -> (only if executable) RuntimeCommandGateway.submit once
'''

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from .dag import DagNodeSpec, validate_dag
from .executors import DepartmentExecutor
from .organization import OrganizationGraph
from .semantic_planner import (
    BudgetTruth,
    CandidateCommand,
    IntentRequest,
    PlanningCheck,
    PlanningDecision,
)


GRAPH_SCHEMA_VERSION = 'task-graph/v1'
DECISION_SCHEMA_VERSION = 'task-graph-decision/v1'
RESULT_SCHEMA_VERSION = 'intent-execution-result/v1'
EXECUTION_MODEL = 'durable-dag/v0'

TASK_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')

# Stable failure codes (contract §6).
CODE_SOURCE_DECISION_NOT_VALIDATED = 'source-decision-not-validated'
CODE_REQUEST_HASH_MISMATCH = 'request-hash-mismatch'
CODE_CANDIDATE_HASH_MISMATCH = 'candidate-hash-mismatch'
CODE_ROUTE_UNAVAILABLE = 'route-unavailable'
CODE_EXECUTOR_UNAVAILABLE = 'executor-unavailable'
CODE_POLICY_MISSING = 'policy-missing'
CODE_POLICY_REQUIRES_OWNER = 'policy-requires-owner'
CODE_EXTERNAL_EFFECT_REQUIRES_OWNER = 'external-effect-requires-owner'
CODE_IRREVERSIBLE_REQUIRES_OWNER = 'irreversible-action-requires-owner'
CODE_NONZERO_BUDGET_REQUIRES_OWNER = 'nonzero-budget-requires-owner'
CODE_BUDGET_TRUTH_UNAVAILABLE = 'budget-truth-unavailable'
CODE_DAG_INVALID = 'dag-invalid'
CODE_IDEMPOTENCY_INVALID = 'idempotency-invalid'
CODE_GATE_BLOCKED_BY_PRIOR = 'gate-blocked-by-prior'

# Failure codes that mean "needs the owner" rather than a hard block.
REQUIRES_OWNER_CODES = frozenset({
    CODE_POLICY_REQUIRES_OWNER,
    CODE_EXTERNAL_EFFECT_REQUIRES_OWNER,
    CODE_IRREVERSIBLE_REQUIRES_OWNER,
    CODE_NONZERO_BUDGET_REQUIRES_OWNER,
})

# Known, reviewed dependency chain (contract §7). Edges are capability -> [deps].
KNOWN_DEPENDENCY_EDGES: dict[str, tuple[str, ...]] = {
    'task-orchestration': ('research',),
    'audit': ('research', 'task-orchestration'),
    # P0-4 novel MVP vertical closure (contract §5).
    'novel-context': (),
    'novel-draft': ('novel-context',),
    'novel-quality': ('novel-context', 'novel-draft'),
    'novel-artifact': ('novel-context', 'novel-draft', 'novel-quality'),
}


def _canonical_sha256(obj: object) -> str:
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TaskGraphNode:
    node_id: str
    department_id: str
    capability: str
    objective: str
    dependencies: tuple[str, ...]
    policy_level: str
    policy_action: str
    policy_resource: str
    reversible: bool
    external_effect: bool
    estimated_budget_fen: int
    idempotency_key: str

    def to_dict(self) -> dict[str, object]:
        return {
            'node_id': self.node_id,
            'department_id': self.department_id,
            'capability': self.capability,
            'objective': self.objective,
            'dependencies': list(self.dependencies),
            'policy_level': self.policy_level,
            'policy_action': self.policy_action,
            'policy_resource': self.policy_resource,
            'reversible': self.reversible,
            'external_effect': self.external_effect,
            'estimated_budget_fen': self.estimated_budget_fen,
            'idempotency_key': self.idempotency_key,
        }


@dataclass(frozen=True)
class TaskGraph:
    graph_id: str
    source_request_sha256: str
    source_candidate_sha256: str
    task_id: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    nodes: tuple[TaskGraphNode, ...]
    graph_sha256: str
    execution_model: str = EXECUTION_MODEL
    schema_version: str = GRAPH_SCHEMA_VERSION

    def payload_for_hash(self) -> dict[str, object]:
        return {
            'schema_version': self.schema_version,
            'graph_id': self.graph_id,
            'source_request_sha256': self.source_request_sha256,
            'source_candidate_sha256': self.source_candidate_sha256,
            'task_id': self.task_id,
            'execution_model': self.execution_model,
            'objective': self.objective,
            'acceptance_criteria': list(self.acceptance_criteria),
            'nodes': [node.to_dict() for node in self.nodes],
        }

    def to_dict(self) -> dict[str, object]:
        data = self.payload_for_hash()
        data['graph_sha256'] = self.graph_sha256
        return data


@dataclass(frozen=True)
class TaskGraphCheck:
    gate: str
    passed: bool
    code: str

    def to_dict(self) -> dict[str, object]:
        return {'id': self.gate, 'passed': self.passed, 'code': self.code}


@dataclass(frozen=True)
class TaskGraphDecision:
    status: str
    graph: TaskGraph
    checks: tuple[TaskGraphCheck, ...]
    blocking_codes: tuple[str, ...]
    schema_version: str = DECISION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            'schema_version': self.schema_version,
            'status': self.status,
            'graph': self.graph.to_dict(),
            'checks': [check.to_dict() for check in self.checks],
            'blocking_codes': list(self.blocking_codes),
        }


@dataclass(frozen=True)
class IntentExecutionResult:
    status: str
    planning: PlanningDecision
    graph: TaskGraphDecision
    task_id: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            'schema_version': RESULT_SCHEMA_VERSION,
            'status': self.status,
            'planning': self.planning.to_dict(),
            'graph': self.graph.to_dict(),
            'task_id': self.task_id,
        }


class CommandGateway(Protocol):
    def submit(self, command: object) -> str: ...


# --------------------------------------------------------------------------- #
# Deterministic graph construction (contract §7)
# --------------------------------------------------------------------------- #
def build_task_graph(
    decision: PlanningDecision,
    graph: OrganizationGraph,
    executors: Mapping[str, DepartmentExecutor],
    task_id: str,
) -> TaskGraph:
    '''Build a deterministic task graph from a validated candidate.

    Never invents a capability; deduplicates candidate capabilities while
    preserving order; creates exactly one node per capability; selects the same
    first live route the orchestrator uses; and applies the reviewed dependency
    chain for the known research -> task-orchestration -> audit set.
    '''
    candidate = decision.candidate
    capabilities = list(dict.fromkeys(candidate.capabilities))
    node_id_by_capability: dict[str, str] = {}
    draft_nodes: list[dict[str, object]] = []
    for index, capability in enumerate(capabilities):
        routes = graph.route(capability)
        department_id = str(routes[0]['department_id']) if routes else ''
        department = graph.departments.get(department_id, {})
        policy = (
            department.get('execution_policy')
            if isinstance(department, dict) else None
        )
        if not isinstance(policy, dict):
            policy = {}
        node_id = f'node-{index:03d}'
        node_id_by_capability[capability] = node_id
        draft_nodes.append({
            'node_id': node_id,
            'department_id': department_id,
            'capability': capability,
            'policy': policy,
        })

    present_capabilities = set(capabilities)
    nodes: list[TaskGraphNode] = []
    for draft in draft_nodes:
        capability = str(draft['capability'])
        policy = draft['policy']
        dependency_caps = [
            edge for edge in KNOWN_DEPENDENCY_EDGES.get(capability, ())
            if edge in present_capabilities
        ]
        dependency_node_ids = tuple(
            node_id_by_capability[edge] for edge in dependency_caps
        )
        nodes.append(TaskGraphNode(
            node_id=str(draft['node_id']),
            department_id=str(draft['department_id']),
            capability=capability,
            objective=str(candidate.objective),
            dependencies=dependency_node_ids,
            policy_level=str(policy.get('level', '')),
            policy_action=str(policy.get('action', '')),
            policy_resource=str(policy.get('resource', '')),
            reversible=bool(policy.get('reversible', False)),
            external_effect=bool(policy.get('external_effect', False)),
            estimated_budget_fen=int(candidate.estimated_budget_fen),
            idempotency_key=f'{task_id}.{draft["node_id"]}',
        ))

    graph_id = f'graph-{task_id}-{decision.candidate_sha256[:12]}'
    payload = {
        'schema_version': GRAPH_SCHEMA_VERSION,
        'graph_id': graph_id,
        'source_request_sha256': decision.request_sha256,
        'source_candidate_sha256': decision.candidate_sha256,
        'task_id': task_id,
        'execution_model': EXECUTION_MODEL,
        'objective': candidate.objective,
        'acceptance_criteria': list(candidate.acceptance_criteria),
        'nodes': [node.to_dict() for node in nodes],
    }
    graph_sha256 = _canonical_sha256(payload)
    return TaskGraph(
        graph_id=graph_id,
        source_request_sha256=decision.request_sha256,
        source_candidate_sha256=decision.candidate_sha256,
        task_id=task_id,
        objective=str(candidate.objective),
        acceptance_criteria=tuple(str(item) for item in candidate.acceptance_criteria),
        nodes=tuple(nodes),
        graph_sha256=graph_sha256,
    )


def validate_graph_dag(graph: TaskGraph) -> None:
    '''Run the existing durable-DAG validation against the graph nodes.'''
    nodes = tuple(
        DagNodeSpec(
            node_id=node.node_id,
            department_id=node.department_id or 'missing',
            capability=node.capability,
            objective=node.objective,
            dependencies=node.dependencies,
            idempotency_key=node.idempotency_key,
        )
        for node in graph.nodes
    )
    validate_dag(nodes)


# --------------------------------------------------------------------------- #
# Governed decision (contract §6) - eight ordered gates
# --------------------------------------------------------------------------- #
def decide_task_graph(
    decision: PlanningDecision,
    graph: OrganizationGraph,
    executors: Mapping[str, DepartmentExecutor],
    task_id: str,
    budget_truth: BudgetTruth,
    *,
    request_sha256: str | None = None,
) -> TaskGraphDecision:
    graph_obj = build_task_graph(decision, graph, executors, task_id)
    checks: list[TaskGraphCheck] = []
    prior_failed = False

    def record(gate: str, passed: bool, code: str) -> None:
        checks.append(TaskGraphCheck(gate, passed, code))

    # 1. source-decision
    if (
        decision.to_dict().get('schema_version') == 'planning-decision/v1'
        and decision.status == 'validated'
    ):
        record('source-decision', True, 'source-decision-validated')
    else:
        record('source-decision', False, CODE_SOURCE_DECISION_NOT_VALIDATED)
        prior_failed = True

    # 2. integrity
    if prior_failed:
        record('integrity', False, CODE_GATE_BLOCKED_BY_PRIOR)
    else:
        recomputed_candidate = _canonical_sha256(decision.candidate.to_dict())
        candidate_ok = recomputed_candidate == decision.candidate_sha256
        request_ok = (
            request_sha256 is None
            or request_sha256 == decision.request_sha256
        )
        if not candidate_ok:
            record('integrity', False, CODE_CANDIDATE_HASH_MISMATCH)
            prior_failed = True
        elif not request_ok:
            record('integrity', False, CODE_REQUEST_HASH_MISMATCH)
            prior_failed = True
        else:
            record('integrity', True, 'integrity-verified')

    # 3. route
    if prior_failed:
        record('route', False, CODE_GATE_BLOCKED_BY_PRIOR)
    elif all(graph.route(node.capability) for node in graph_obj.nodes):
        record('route', True, 'route-available')
    else:
        record('route', False, CODE_ROUTE_UNAVAILABLE)
        prior_failed = True

    # 4. executor
    if prior_failed:
        record('executor', False, CODE_GATE_BLOCKED_BY_PRIOR)
    elif all(
        node.department_id and node.department_id in executors
        for node in graph_obj.nodes
    ):
        record('executor', True, 'executor-available')
    else:
        record('executor', False, CODE_EXECUTOR_UNAVAILABLE)
        prior_failed = True

    # 5. policy
    if prior_failed:
        record('policy', False, CODE_GATE_BLOCKED_BY_PRIOR)
    else:
        policy_problem: str | None = None
        for node in graph_obj.nodes:
            if not node.policy_level:
                policy_problem = CODE_POLICY_MISSING
                break
            if node.policy_level not in ('L0', 'L1'):
                policy_problem = CODE_POLICY_REQUIRES_OWNER
                break
            if node.external_effect:
                policy_problem = CODE_EXTERNAL_EFFECT_REQUIRES_OWNER
                break
            if not node.reversible:
                policy_problem = CODE_IRREVERSIBLE_REQUIRES_OWNER
                break
        if policy_problem is None:
            record('policy', True, 'policy-within-bounds')
        else:
            record('policy', False, policy_problem)
            prior_failed = True

    # 6. budget
    if prior_failed:
        record('budget', False, CODE_GATE_BLOCKED_BY_PRIOR)
    elif not budget_truth.is_available():
        record('budget', False, CODE_BUDGET_TRUTH_UNAVAILABLE)
        prior_failed = True
    elif any(node.estimated_budget_fen != 0 for node in graph_obj.nodes):
        record('budget', False, CODE_NONZERO_BUDGET_REQUIRES_OWNER)
        prior_failed = True
    else:
        record('budget', True, 'budget-zero-truth-ok')

    # 7. dag
    if prior_failed:
        record('dag', False, CODE_GATE_BLOCKED_BY_PRIOR)
    else:
        try:
            validate_graph_dag(graph_obj)
            record('dag', True, 'dag-valid')
        except Exception:
            record('dag', False, CODE_DAG_INVALID)
            prior_failed = True

    # 8. idempotency
    if prior_failed:
        record('idempotency', False, CODE_GATE_BLOCKED_BY_PRIOR)
    else:
        idempotency_ok = bool(TASK_ID_PATTERN.fullmatch(task_id))
        if idempotency_ok and all(
            node.idempotency_key
            and not any(char in node.idempotency_key for char in '/\\:')
            for node in graph_obj.nodes
        ):
            record('idempotency', True, 'idempotency-stable')
        else:
            record('idempotency', False, CODE_IDEMPOTENCY_INVALID)

    failed_codes = tuple(
        check.code for check in checks
        if not check.passed and check.code != CODE_GATE_BLOCKED_BY_PRIOR
    )
    if not failed_codes:
        status = 'executable'
    elif any(code in REQUIRES_OWNER_CODES for code in failed_codes):
        status = 'requires_owner'
    else:
        status = 'blocked'

    return TaskGraphDecision(
        status=status,
        graph=graph_obj,
        checks=tuple(checks),
        blocking_codes=failed_codes,
    )


# --------------------------------------------------------------------------- #
# Governed dispatch (contract §8)
# --------------------------------------------------------------------------- #
def _capability_dependencies(graph: TaskGraph) -> dict[str, list[str]]:
    node_id_to_capability = {
        node.node_id: node.capability for node in graph.nodes
    }
    dependencies: dict[str, list[str]] = {}
    for node in graph.nodes:
        deps = [
            node_id_to_capability[dep_id]
            for dep_id in node.dependencies
            if dep_id in node_id_to_capability
        ]
        if deps:
            dependencies[node.capability] = deps
    return dependencies


def execute_intent(
    intent: IntentRequest,
    *,
    planning_gateway: object,
    command_gateway: CommandGateway,
    graph: OrganizationGraph,
    executors: Mapping[str, DepartmentExecutor],
    budget_truth: BudgetTruth,
    task_id: str | None = None,
) -> IntentExecutionResult:
    '''Plan once, compile/validate once, dispatch at most once.

    The server runs P0-1A planning itself; a client-supplied decision is never
    trusted. The command gateway is invoked exactly once, and only when the
    graph decision is ``executable``.
    '''
    from .ceo_api import ValidatedCommand

    decision = planning_gateway.plan(intent)
    resolved_task_id = task_id or intent.task_id or f'task-{uuid.uuid4().hex[:12]}'
    request_sha256 = _canonical_sha256(intent.to_dict())

    graph_decision = decide_task_graph(
        decision, graph, executors, resolved_task_id, budget_truth,
        request_sha256=request_sha256,
    )

    if graph_decision.status == 'executable':
        compiled = graph_decision.graph
        command = ValidatedCommand(
            instruction=compiled.objective,
            capabilities=tuple(node.capability for node in compiled.nodes),
            acceptance_criteria=tuple(compiled.acceptance_criteria),
            task_id=resolved_task_id,
            source_url=None,
            source_urls=(),
            dependencies=_capability_dependencies(compiled),
        )
        command_gateway.submit(command)
        return IntentExecutionResult(
            status='accepted',
            planning=decision,
            graph=graph_decision,
            task_id=resolved_task_id,
        )

    return IntentExecutionResult(
        status=graph_decision.status,
        planning=decision,
        graph=graph_decision,
        task_id=None,
    )


def load_budget_truth(group_root: object) -> BudgetTruth:
    '''Load budget truth from the group's budget policy (mirrors planning).'''
    from .contracts import load_json

    from pathlib import Path

    policy = load_json(
        Path(group_root).resolve() / 'shared/policies/rmb-4000-budget-policy.json'
    )
    return BudgetTruth(
        available=True,
        total_fen=int(policy['total_fen']),
        reserve_fen=int(policy['reserve_fen']),
    )
