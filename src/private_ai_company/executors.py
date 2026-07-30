'''Department executor contracts and the first deterministic local executor.'''

from __future__ import annotations

import json
from dataclasses import dataclass
from time import monotonic
from typing import Mapping, Protocol

from .contracts import load_json
from .budget import ModelRate
from .organization import OrganizationGraph
from .providers import (
    ModelMessage, ModelProvider, ModelRequest, ModelRoutingRequirements,
    ProviderError,
)
from .root import resolve_portable_path
from .timebase import authoritative_timestamp
from .trace import TraceContext, TraceEvent, TraceSink
from .work_products import (
    research_decision_json_contract,
    validate_research_decision,
)


@dataclass(frozen=True)
class ExecutionRequest:
    task_id: str
    assignment_id: str
    department_id: str
    capability: str
    objective: str
    acceptance_criteria: list[str]
    input_refs: list[str]
    dependency_results: Mapping[str, Mapping[str, object]] | None = None
    trace_context: TraceContext | None = None


@dataclass(frozen=True)
class DepartmentResult:
    status: str
    outcome_code: str
    evidence: list[dict[str, object]]
    artifacts: list[dict[str, object]]
    metrics: dict[str, int | float]
    confidence_milli: int
    unresolved_codes: list[str]
    provider_id: str | None = None
    model_id: str | None = None


class DepartmentExecutor(Protocol):
    def estimate_cost_fen(self, request: ExecutionRequest) -> int: ...

    def execute(self, request: ExecutionRequest) -> DepartmentResult: ...


class ContextResolver(Protocol):
    def resolve(self, input_refs: list[str]) -> list[dict[str, str]]: ...


class PortableFileContextResolver:
    '''Resolve reviewed group files by reference without exposing arbitrary paths.'''

    prefix = 'group-file:'

    def __init__(self, graph: OrganizationGraph, *, max_chars_per_file: int = 6000):
        if max_chars_per_file < 1:
            raise ValueError('max_chars_per_file must be positive.')
        self.graph = graph
        self.max_chars_per_file = max_chars_per_file

    def resolve(self, input_refs: list[str]) -> list[dict[str, str]]:
        resolved = []
        for ref in input_refs:
            if not ref.startswith(self.prefix):
                continue
            relative = ref[len(self.prefix):]
            path = resolve_portable_path(
                self.graph.root, relative, f'input_ref:{ref}',
            )
            if not path.is_file():
                raise ValueError(f'Input reference is not a file: {ref!r}.')
            resolved.append({
                'ref': ref,
                'content': path.read_text(encoding='utf-8')[:self.max_chars_per_file],
            })
        return resolved


class TemplateDepartmentExecutor:
    '''Execute reviewed department workflow templates without an API or model.'''

    def __init__(self, graph: OrganizationGraph, department_id: str):
        self.graph = graph
        self.department_id = department_id
        department = graph.departments[department_id]
        executor = department.get('executor')
        if not isinstance(executor, dict) or executor.get('kind') != 'template':
            raise ValueError(f'Department {department_id!r} has no template executor.')
        workflow = executor.get('workflow')
        self.workflow_path = resolve_portable_path(
            graph.root, workflow, f'{department_id}.executor.workflow',
        )
        self.workflow = load_json(self.workflow_path)
        if self.workflow.get('schema_version') != 'department-workflow/v0':
            raise ValueError(f'Unsupported workflow schema in {self.workflow_path}.')

    def estimate_cost_fen(self, request: ExecutionRequest) -> int:
        if request.department_id != self.department_id:
            raise ValueError('Executor department does not match the assignment.')
        return 0

    def execute(self, request: ExecutionRequest) -> DepartmentResult:
        if request.department_id != self.department_id:
            raise ValueError('Executor department does not match the assignment.')
        actions = self.workflow.get('capability_actions')
        if not isinstance(actions, dict):
            raise ValueError('Workflow capability_actions must be an object.')
        action = actions.get(request.capability)
        if not isinstance(action, dict):
            return DepartmentResult(
                status='blocked', outcome_code='capability-not-configured',
                evidence=[], artifacts=[], metrics={'steps': 0},
                confidence_milli=0, unresolved_codes=['workflow-action-missing'],
            )
        sections = action.get('sections', [])
        checks = action.get('quality_checks', [])
        if not isinstance(sections, list) or not all(
            isinstance(item, str) and item.strip() for item in sections
        ):
            raise ValueError('Workflow sections must be a string array.')
        if not isinstance(checks, list) or not all(
            isinstance(item, str) and item.strip() for item in checks
        ):
            raise ValueError('Workflow quality_checks must be a string array.')
        artifact = {
            'schema_version': 'department-output/v0',
            'department_id': request.department_id,
            'assignment_id': request.assignment_id,
            'capability': request.capability,
            'objective': request.objective,
            'sections': [
                {'name': name, 'status': 'prepared'} for name in sections
            ],
            'acceptance_criteria': request.acceptance_criteria,
            'input_refs': request.input_refs,
            'mode': 'deterministic-template',
        }
        evidence = {
            'schema_version': 'department-evidence/v0',
            'department_id': request.department_id,
            'assignment_id': request.assignment_id,
            'workflow': self.workflow_path.relative_to(
                self.graph.root,
            ).as_posix(),
            'quality_checks': [
                {'code': code, 'passed': True} for code in checks
            ],
        }
        return DepartmentResult(
            status='completed',
            outcome_code=str(action.get('outcome_code', 'template-output-prepared')),
            evidence=[evidence], artifacts=[artifact],
            metrics={'steps': len(sections), 'checks': len(checks)},
            confidence_milli=850, unresolved_codes=[],
        )


class PromptContextCompiler:
    '''Compile portable department context without provider-specific state.'''

    def __init__(
        self,
        graph: OrganizationGraph,
        department_id: str,
        resolver: ContextResolver | None = None,
    ):
        self.graph = graph
        self.department_id = department_id
        self.resolver = resolver

    def compile(self, request: ExecutionRequest) -> tuple[ModelMessage, ...]:
        department = self.graph.departments[self.department_id]
        prompt_contract = {
            'schema_version': 'department-result/v0',
            'status': 'completed|blocked',
            'outcome_code': 'portable short code',
            'confidence_milli': 'integer 0..1000',
            'unresolved_codes': ['portable short code'],
            'evidence': [{
                'schema_version': 'department-evidence/v0',
                'source_ref': 'traceable input or source identifier',
                'claim': 'claim supported by the source',
            }],
            'artifacts': [research_decision_json_contract()],
        }
        system = (
            'You are a department execution engine inside a private AI group. '
            'Return one JSON object only. The top-level schema_version field is '
            'mandatory and its literal value must be department-result/v0. '
            'Do not omit, rename, or wrap any required top-level field. '
            'Do not include chain-of-thought, prose '
            'outside JSON, credentials, or claims without traceable evidence. '
            'If evidence is insufficient, return blocked with unresolved codes.'
        )
        payload = {
            'department': {
                'department_id': self.department_id,
                'name': department.get('name'),
                'mission': department.get('mission'),
            },
            'request': {
                'task_id': request.task_id,
                'assignment_id': request.assignment_id,
                'capability': request.capability,
                'objective': request.objective,
                'acceptance_criteria': request.acceptance_criteria,
                'input_refs': request.input_refs,
            },
            'source_context': (
                self.resolver.resolve(request.input_refs)
                if self.resolver is not None else []
            ),
            'required_output': prompt_contract,
        }
        return (
            ModelMessage('system', system),
            ModelMessage(
                'user',
                json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
            ),
        )


class ModelDepartmentExecutor:
    '''Execute a department through a provider-neutral structured model call.'''

    def __init__(
        self,
        graph: OrganizationGraph,
        department_id: str,
        provider: ModelProvider,
        *,
        compiler: PromptContextCompiler | None = None,
        max_output_tokens: int = 2048,
        rate: ModelRate | None = None,
        routing_requirements: ModelRoutingRequirements | None = None,
        trace_sink: TraceSink | None = None,
    ):
        if department_id not in graph.departments:
            raise ValueError(f'Unknown department {department_id!r}.')
        self.graph = graph
        self.department_id = department_id
        self.provider = provider
        self.compiler = compiler or PromptContextCompiler(graph, department_id)
        self.max_output_tokens = max_output_tokens
        self.rate = rate
        self.routing_requirements = routing_requirements or ModelRoutingRequirements(
            required_capabilities=frozenset({'structured_output'}),
        )
        self.trace_sink = trace_sink
        self._bind_provider_trace_sink()

    def _bind_provider_trace_sink(self) -> None:
        if self.trace_sink is None:
            return
        setter = getattr(self.provider, 'set_trace_sink', None)
        if callable(setter):
            setter(self.trace_sink)

    def set_trace_sink(self, trace_sink: TraceSink) -> None:
        self.trace_sink = trace_sink
        self._bind_provider_trace_sink()

    def estimate_cost_fen(self, request: ExecutionRequest) -> int:
        if request.department_id != self.department_id:
            raise ValueError('Executor department does not match the assignment.')
        if self.rate is None:
            return 0
        messages = self.compiler.compile(request)
        input_token_upper_bound = sum(
            len(message.role.encode('utf-8'))
            + len(message.content.encode('utf-8'))
            + 16
            for message in messages
        )
        return self.rate.estimate_fen(
            input_token_upper_bound, self.max_output_tokens,
        )

    def execute(self, request: ExecutionRequest) -> DepartmentResult:
        if request.department_id != self.department_id:
            raise ValueError('Executor department does not match the assignment.')
        provider_context = (
            request.trace_context.child(f'provider.{self.provider.provider_id}')
            if request.trace_context is not None else None
        )
        model_request = ModelRequest(
            messages=self.compiler.compile(request),
            max_output_tokens=self.max_output_tokens,
            temperature=0.0,
            response_format='json_object',
            thinking='disabled',
            routing=self.routing_requirements,
            trace_context=provider_context,
        )
        started_at = authoritative_timestamp()
        started = monotonic()
        if self.trace_sink is not None and provider_context is not None:
            self.trace_sink.emit(TraceEvent(
                context=provider_context, component='provider',
                kind='model-completion', status='started',
                started_at=started_at,
                attributes={
                    'provider_id': self.provider.provider_id,
                    'model_id': self.provider.model,
                    'response_format': model_request.response_format,
                    'max_output_tokens': model_request.max_output_tokens,
                },
            ))
        try:
            result = self.provider.complete(model_request)
        except ProviderError as exc:
            if self.trace_sink is not None and provider_context is not None:
                self.trace_sink.emit(TraceEvent(
                    context=provider_context, component='provider',
                    kind='model-completion', status='failed',
                    started_at=started_at, finished_at=authoritative_timestamp(),
                    duration_ms=round((monotonic() - started) * 1000),
                    error={
                        'code': exc.code, 'retryable': exc.retryable,
                        'http_status': exc.http_status,
                        'exception_type': type(exc).__name__,
                    },
                ))
            raise
        except Exception as exc:
            if self.trace_sink is not None and provider_context is not None:
                self.trace_sink.emit(TraceEvent(
                    context=provider_context, component='provider',
                    kind='model-completion', status='failed',
                    started_at=started_at, finished_at=authoritative_timestamp(),
                    duration_ms=round((monotonic() - started) * 1000),
                    error={
                        'code': 'provider-exception',
                        'retryable': False,
                        'exception_type': type(exc).__name__,
                    },
                ))
            raise
        if self.trace_sink is not None and provider_context is not None:
            self.trace_sink.emit(TraceEvent(
                context=provider_context, component='provider',
                kind='model-completion', status='completed',
                started_at=started_at, finished_at=authoritative_timestamp(),
                duration_ms=round((monotonic() - started) * 1000),
                usage={
                    'input_tokens': result.usage.input_tokens,
                    'output_tokens': result.usage.output_tokens,
                    'total_tokens': result.usage.total_tokens,
                },
                attributes={
                    'provider_id': result.provider, 'model_id': result.model,
                    'finish_reason': result.finish_reason,
                    'tool_call_count': len(result.tool_calls),
                },
            ))
        payload = result.parsed_json
        if not isinstance(payload, dict):
            raise ValueError('Model department result must be a JSON object.')
        if payload.get('schema_version') != 'department-result/v0':
            raise ValueError('Unsupported model department result schema.')
        status = payload.get('status')
        if status not in {'completed', 'blocked'}:
            raise ValueError('Model department status must be completed or blocked.')
        outcome_code = payload.get('outcome_code')
        if not isinstance(outcome_code, str) or not outcome_code.strip():
            raise ValueError('Model department outcome_code must be non-empty.')
        evidence = payload.get('evidence')
        artifacts = payload.get('artifacts')
        unresolved = payload.get('unresolved_codes')
        confidence = payload.get('confidence_milli')
        if not isinstance(evidence, list) or not all(
            isinstance(item, dict) for item in evidence
        ):
            raise ValueError('Model department evidence must be an object array.')
        if not isinstance(artifacts, list) or not all(
            isinstance(item, dict) for item in artifacts
        ):
            raise ValueError('Model department artifacts must be an object array.')
        if not isinstance(unresolved, list) or not all(
            isinstance(item, str) and item.strip() for item in unresolved
        ):
            raise ValueError('Model department unresolved_codes must be strings.')
        if (
            not isinstance(confidence, int)
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1000
        ):
            raise ValueError('Model department confidence_milli is invalid.')
        if status == 'completed' and (not evidence or not artifacts or unresolved):
            raise ValueError('Completed model work requires evidence and artifacts.')
        if status == 'blocked' and not unresolved:
            raise ValueError('Blocked model work requires unresolved codes.')
        allowed_source_refs = set(request.input_refs)
        for item in evidence:
            source_ref = item.get('source_ref')
            if not isinstance(source_ref, str) or source_ref not in allowed_source_refs:
                raise ValueError('Model evidence references an unapproved source.')
        if request.capability == 'research' and status == 'completed':
            research_artifacts = [
                item for item in artifacts
                if item.get('schema_version') == 'research-decision/v0'
            ]
            if not research_artifacts:
                raise ValueError(
                    'Research execution requires a research decision artifact.'
                )
            errors = validate_research_decision(research_artifacts[0])
            if errors:
                raise ValueError('Invalid research decision: ' + ','.join(errors))
            for item in research_artifacts[0]['evidence']:
                if item.get('source_ref') not in allowed_source_refs:
                    raise ValueError(
                        'Research artifact references an unapproved source.'
                    )
        metrics = {
            'input_tokens': result.usage.input_tokens,
            'output_tokens': result.usage.output_tokens,
            'total_tokens': result.usage.total_tokens,
        }
        if self.rate is not None:
            metrics['cost_fen'] = self.rate.estimate_fen(
                result.usage.input_tokens, result.usage.output_tokens,
            )
        return DepartmentResult(
            status=status,
            outcome_code=outcome_code,
            evidence=evidence,
            artifacts=artifacts,
            metrics=metrics,
            confidence_milli=confidence,
            unresolved_codes=unresolved,
            provider_id=result.provider,
            model_id=result.model,
        )


def build_template_executors(
    graph: OrganizationGraph,
) -> dict[str, DepartmentExecutor]:
    executors: dict[str, DepartmentExecutor] = {}
    for department_id, department in graph.departments.items():
        executor = department.get('executor')
        if isinstance(executor, dict) and executor.get('kind') == 'template':
            executors[department_id] = TemplateDepartmentExecutor(
                graph, department_id,
            )
    return executors
