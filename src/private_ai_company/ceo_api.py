'''Local HTTP/OpenAPI boundary for CEO-controlled command execution.'''

from __future__ import annotations

import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .contracts import has_errors, load_json
from .budget import BudgetLedger, BudgetPolicy
from .business_executors import build_business_executors
from .company_events import CompanyEventStore, mount_company_event_routes
from .organization import load_organization_graph
from .orchestrator import GroupOrchestrator
from .semantic_planner import (
    IntentRequest, PlanningError, PlanningGateway, RuntimePlanningGateway,
    validate_intent_request,
)
from .task_graph import execute_intent, load_budget_truth
from .task_ledger import TaskLedger
from .economic_ledger import (
    EconomicLedger,
    EconomicBudgetPolicy,
    EconomicBudgetTruth,
    EconomicLedgerError,
    ECON_BUDGET_TRUTH_UNAVAILABLE,
    SCHEMA_SUMMARY,
    SCHEMA_EXPORT,
)
from .platform_adapter import (
    PlatformAdapterError,
    PlatformAuthorizationLease,
    DenyAllShadowTransport,
    FakeShadowTransport,
)
from .publication import PublicationService
from .production_batch import (
    NovelProductionBatchService,
    NovelProductionBatchError,
)
from .novel_mvp import DeterministicLocalDraftProvider, NOVEL_CHAPTER_DRAFT_SCHEMA
from .novel_operations import (
    NovelOperationsService, NovelOperationsError, sha256_text, canonical_json, CallReceiptSink,
)
from .providers import (
    ModelProvider,
    ModelRequest,
    ModelMessage,
    ModelUsage,
    ModelResult,
    RoutingModelProvider,
    build_runtime_provider,
)
from .timebase import authoritative_timestamp


SHANGHAI_TZ = timezone(timedelta(hours=8))
TIMEBASE_NAME = 'Asia/Shanghai'
REQUEST_SCHEMA = 'human-command-request/v0'
ACCEPTED_SCHEMA = 'command-accepted/v0'
TASK_STATUS_SCHEMA = 'ceo-task-status/v0'
HEALTH_SCHEMA = 'health-check/v0'
TASK_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
CAPABILITY_PATTERN = re.compile(r'^[a-z0-9][a-z0-9._-]{0,79}$')
ALLOWED_REQUEST_FIELDS = {
    'schema_version', 'instruction', 'capabilities',
    'acceptance_criteria', 'task_id', 'source_url', 'source_urls', 'input_refs',
}


class ValidationError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(f'{field}: {message}')
        self.field = field
        self.message = message


class DuplicateTaskError(RuntimeError):
    pass


class GatewayUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class ValidatedCommand:
    instruction: str
    capabilities: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    task_id: str | None = None
    source_url: str | None = None
    source_urls: tuple[str, ...] = ()
    input_refs: tuple[str, ...] = ()
    dependencies: dict[str, list[str]] | None = None


class CommandGateway(Protocol):
    def submit(self, command: ValidatedCommand) -> str: ...

    def status(self, task_id: str) -> Mapping[str, object]: ...


def _normalized_unique_strings(
    value: object,
    *,
    field: str,
    maximum_items: int,
    maximum_length: int,
    pattern: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValidationError(field, 'must be a list')
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValidationError(field, 'each item must be a string')
        text = item.strip()
        if not text or text in seen:
            continue
        if len(text) > maximum_length:
            raise ValidationError(field, f'item exceeds {maximum_length} characters')
        if pattern is not None and not pattern.fullmatch(text):
            raise ValidationError(field, f'invalid item {text!r}')
        seen.add(text)
        normalized.append(text)
    if not normalized:
        raise ValidationError(field, 'at least one item is required')
    if len(normalized) > maximum_items:
        raise ValidationError(field, f'maximum is {maximum_items} items')
    return tuple(normalized)


def validate_command_request(data: object) -> ValidatedCommand:
    if not isinstance(data, dict):
        raise ValidationError('request', 'must be a JSON object')
    extra = set(data) - ALLOWED_REQUEST_FIELDS
    if extra:
        raise ValidationError('request', f'unexpected fields: {", ".join(sorted(extra))}')
    if data.get('schema_version') != REQUEST_SCHEMA:
        raise ValidationError('schema_version', f'must be {REQUEST_SCHEMA!r}')

    instruction = data.get('instruction')
    if not isinstance(instruction, str):
        raise ValidationError('instruction', 'must be a string')
    instruction = instruction.strip()
    if not instruction:
        raise ValidationError('instruction', 'must be non-empty')
    if len(instruction) > 4000:
        raise ValidationError('instruction', 'maximum is 4000 characters')

    capabilities = _normalized_unique_strings(
        data.get('capabilities'), field='capabilities', maximum_items=8,
        maximum_length=80, pattern=CAPABILITY_PATTERN,
    )
    criteria = _normalized_unique_strings(
        data.get('acceptance_criteria'), field='acceptance_criteria',
        maximum_items=12, maximum_length=500,
    )

    task_id = data.get('task_id')
    if task_id is not None:
        if not isinstance(task_id, str):
            raise ValidationError('task_id', 'must be a string or null')
        task_id = task_id.strip() or None
        if task_id is not None and not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValidationError('task_id', 'must be a portable identifier')

    def normalize_source(value: object, field: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValidationError(field, 'must be a string')
        normalized = value.strip() or None
        if normalized is None:
            return None
        if len(normalized) > 2048:
            raise ValidationError(field, 'maximum is 2048 characters')
        parsed = urlsplit(normalized)
        if parsed.scheme.lower() != 'https' or not parsed.hostname:
            raise ValidationError(field, 'must be an HTTPS URL')
        if parsed.username is not None or parsed.password is not None:
            raise ValidationError(field, 'credentials are forbidden')
        return normalized

    source_url = normalize_source(data.get('source_url'), 'source_url')
    raw_source_urls = data.get('source_urls', [])
    if raw_source_urls is None:
        raw_source_urls = []
    if not isinstance(raw_source_urls, list):
        raise ValidationError('source_urls', 'must be a list or null')
    normalized_sources: list[str] = []
    raw_sources = ([source_url] if source_url else []) + raw_source_urls
    for index, raw_source in enumerate(raw_sources):
        normalized = normalize_source(raw_source, f'source_urls[{index}]')
        if normalized and normalized not in normalized_sources:
            normalized_sources.append(normalized)
    if len(normalized_sources) > 5:
        raise ValidationError('source_urls', 'maximum is 5 items')

    # P0-4 portable group-file input references (contract §6.1). Optional; when
    # present it must be a non-empty list of deduplicated, trimmed references.
    input_refs: tuple[str, ...] = ()
    if 'input_refs' in data:
        raw_input_refs = data['input_refs']
        if not isinstance(raw_input_refs, list):
            raise ValidationError('input_refs', 'must be a list')
        if not raw_input_refs:
            raise ValidationError('input_refs', 'empty list is not allowed')
        input_refs = _normalized_unique_strings(
            raw_input_refs, field='input_refs', maximum_items=8, maximum_length=512,
        )
        for ref in input_refs:
            lowered = ref.lower()
            if not ref.startswith('group-file:'):
                raise ValidationError(
                    'input_refs', f'only group-file references allowed: {ref!r}'
                )
            rest = ref[len('group-file:'):]
            if rest.startswith(('/', '\\')) or '..' in rest or ':' in rest:
                raise ValidationError('input_refs', f'invalid path reference: {ref!r}')
            if any(tok in lowered for tok in ('http://', 'https://', 'secret', 'token', 'credential')):
                raise ValidationError('input_refs', f'forbidden reference: {ref!r}')

    return ValidatedCommand(
        instruction=instruction,
        capabilities=capabilities,
        acceptance_criteria=criteria,
        task_id=task_id,
        source_url=normalized_sources[0] if normalized_sources else None,
        source_urls=tuple(normalized_sources),
        input_refs=input_refs,
        dependencies=None,
    )


def build_accepted_response(task_id: str) -> dict[str, str]:
    return {
        'schema_version': ACCEPTED_SCHEMA,
        'task_id': task_id,
        'state': 'accepted',
        'accepted_at': datetime.now(SHANGHAI_TZ).isoformat(),
    }


def build_health_response() -> dict[str, str]:
    return {
        'schema_version': HEALTH_SCHEMA,
        'status': 'ok',
        'timebase': TIMEBASE_NAME,
    }


def _cost_projection(assignments: list[dict[str, object]]) -> dict[str, object]:
    estimated_fen = 0
    actual_fen = 0
    for assignment in assignments:
        metrics = assignment.get('metrics')
        if not isinstance(metrics, dict):
            continue
        estimated = metrics.get('estimated_cost_fen', 0)
        actual = metrics.get('actual_cost_fen', metrics.get('cost_fen', 0))
        tool_actual = metrics.get('tool_cost_fen', 0)
        if isinstance(estimated, int) and estimated >= 0:
            estimated_fen += estimated
        if isinstance(actual, int) and actual >= 0:
            actual_fen += actual
        if isinstance(tool_actual, int) and tool_actual >= 0:
            actual_fen += tool_actual
    return {
        'currency': 'CNY',
        'estimated_fen': estimated_fen,
        'actual_fen': actual_fen,
    }


def _project_task(
    task_id: str,
    record: Mapping[str, object] | None,
    checkpoint: Mapping[str, object] | None,
    events: list[dict[str, object]],
) -> dict[str, object]:
    task: dict[str, object] = {}
    if isinstance(checkpoint, Mapping) and isinstance(checkpoint.get('state'), dict):
        task = dict(checkpoint['state'])
    result = record.get('result') if isinstance(record, Mapping) else None
    if isinstance(result, Mapping) and isinstance(result.get('task'), dict):
        task = dict(result['task'])
    raw_assignments = task.get('assignments', [])
    assignments = [dict(item) for item in raw_assignments if isinstance(item, dict)]
    record_state = str((record or {}).get('state') or '')
    state = (
        record_state
        if record_state in {'failed', 'blocked'}
        else str(task.get('state') or record_state or 'unknown')
    )
    ceo_report = task.get('final_report')
    business = (
        ceo_report.get('business_outputs', {})
        if isinstance(ceo_report, dict) else {}
    )
    return {
        'schema_version': TASK_STATUS_SCHEMA,
        'task_id': task_id,
        'state': state,
        'accepted_at': (record or {}).get('accepted_at'),
        'updated_at': task.get('updated_at') or (record or {}).get('updated_at'),
        'dag': assignments,
        'cost': _cost_projection(assignments),
        'trace': [
            {
                'event_id': event.get('event_id'),
                'kind': event.get('kind'),
                'timestamp': event.get('timestamp'),
            }
            for event in events[-100:]
        ],
        'ceo_report': ceo_report,
        'business': business,
        'failure': (record or {}).get('failure'),
    }


class RuntimeCommandGateway:
    '''Asynchronous local gateway that delegates only through GroupOrchestrator.'''

    def __init__(self, group_root: Path, *, max_workers: int = 1) -> None:
        self.group_root = group_root.resolve()
        manifest = load_json(self.group_root / 'group.manifest.json')
        graph, findings = load_organization_graph(self.group_root, manifest)
        if has_errors(findings):
            codes = ', '.join(item.code for item in findings if item.severity == 'error')
            raise ValueError(f'GroupPack validation failed: {codes}')
        budget_data = load_json(
            self.group_root / 'shared/policies/rmb-4000-budget-policy.json',
        )
        budget_policy = BudgetPolicy(
            total_fen=int(budget_data['total_fen']),
            reserve_fen=int(budget_data['reserve_fen']),
            category_caps_fen={
                str(key): int(value)
                for key, value in budget_data['category_caps_fen'].items()
            },
            currency=str(budget_data.get('currency') or 'CNY'),
        )
        budget_ledger = BudgetLedger(
            self.group_root / 'audit/budget-usage.jsonl', budget_policy,
        )
        tool_ledger = TaskLedger(
            self.group_root / 'tasks/events', self.group_root / 'checkpoints',
        )
        executors = build_business_executors(
            graph, ledger=tool_ledger, budget_ledger=budget_ledger,
        )
        self.orchestrator = GroupOrchestrator(
            graph, executors, budget_ledger=budget_ledger,
        )
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='ceo-api')
        self._lock = RLock()
        self._records: dict[str, dict[str, object]] = {}
        self.company_events = CompanyEventStore(
            self.group_root / 'tasks/company-events.jsonl',
        )

    def submit(self, command: ValidatedCommand) -> str:
        task_id = command.task_id or f'task-{uuid.uuid4().hex[:12]}'
        with self._lock:
            if task_id in self._records:
                raise DuplicateTaskError(task_id)
            if self.orchestrator.ledger.read_checkpoint(task_id) is not None:
                raise DuplicateTaskError(task_id)
            if any(self.orchestrator.ledger.events(task_id)):
                raise DuplicateTaskError(task_id)
            now = datetime.now(SHANGHAI_TZ).isoformat()
            self._records[task_id] = {
                'state': 'accepted', 'accepted_at': now, 'updated_at': now,
                'source_url': command.source_url,
                'source_urls': list(command.source_urls),
            }
            try:
                self.company_events.append(
                    'command.accepted',
                    source={'type': 'human', 'id': 'zero'},
                    target={'type': 'ceo', 'id': 'group-ceo'},
                    summary='Human command accepted by the CEO gateway.',
                    task_id=task_id,
                    status='pending',
                    visibility='human',
                    meta={
                        'capabilities': list(command.capabilities),
                        'acceptance_criteria_count': len(command.acceptance_criteria),
                    },
                )
                self.company_events.append(
                    'task.created',
                    source={'type': 'ceo', 'id': 'group-ceo'},
                    target={'type': 'system', 'id': 'durable-dag'},
                    summary='CEO created a durable execution task.',
                    task_id=task_id,
                    status='planning',
                    visibility='organization',
                )
                self._pool.submit(self._execute, task_id, command)
            except RuntimeError as exc:
                self._records.pop(task_id, None)
                raise GatewayUnavailableError('CEO execution pool is unavailable') from exc
            except Exception:
                self._records.pop(task_id, None)
                raise
        return task_id

    def _execute(self, task_id: str, command: ValidatedCommand) -> None:
        with self._lock:
            self._records[task_id]['state'] = 'running'
            self._records[task_id]['updated_at'] = datetime.now(SHANGHAI_TZ).isoformat()
        self.company_events.append(
            'task.status.changed',
            source={'type': 'ceo', 'id': 'group-ceo'},
            target={'type': 'workflow', 'id': 'durable-dag'},
            summary='CEO task execution started.',
            task_id=task_id,
            status='working',
            visibility='organization',
        )
        try:
            capabilities = list(command.capabilities)
            input_refs: list[str] = []
            dependencies: dict[str, list[str]] = (
                dict(command.dependencies)
                if command.dependencies is not None else {}
            )
            if command.source_urls:
                capabilities = ['research', 'task-orchestration', 'audit']
                input_refs = [f'web-source:{source}' for source in command.source_urls]
                dependencies = {
                    'task-orchestration': ['research'],
                    'audit': ['research', 'task-orchestration'],
                }
            else:
                # P0-4 portable group-file references (contract §6.1). Must not be
                # mixed with the research source_urls override above.
                input_refs = list(command.input_refs)
            self.company_events.append(
                'routing.decided',
                source={'type': 'ceo', 'id': 'group-ceo'},
                target={'type': 'workflow', 'id': 'durable-dag'},
                summary='CEO selected the execution capabilities and dependency graph.',
                task_id=task_id,
                status='planning',
                visibility='ceo',
                meta={'capabilities': capabilities, 'dependencies': dependencies},
            )
            result = self.orchestrator.run(
                instruction=command.instruction,
                capabilities=capabilities,
                acceptance_criteria=list(command.acceptance_criteria),
                input_refs=input_refs,
                dependencies=dependencies,
                task_id=task_id,
            ).to_dict()
        except Exception:
            with self._lock:
                record = self._records[task_id]
                record['state'] = 'failed'
                record['failure'] = {'code': 'execution-failed'}
                record['updated_at'] = datetime.now(SHANGHAI_TZ).isoformat()
            self.company_events.append(
                'task.failed',
                source={'type': 'workflow', 'id': 'durable-dag'},
                target={'type': 'ceo', 'id': 'group-ceo'},
                summary='Durable task execution failed; inspect the task ledger for evidence.',
                task_id=task_id,
                status='failed',
                visibility='human',
                error={
                    'code': 'execution-failed', 'message': 'Execution failed.',
                    'retryable': True,
                },
            )
            return
        with self._lock:
            record = self._records[task_id]
            record['result'] = result
            task = result.get('task') if isinstance(result, dict) else None
            record['state'] = task.get('state', 'completed') if isinstance(task, dict) else 'completed'
            record['updated_at'] = datetime.now(SHANGHAI_TZ).isoformat()
            final_state = str(record['state'])
        if final_state == 'blocked':
            terminal_type, terminal_status = 'task.blocked', 'blocked'
        elif final_state in {'failed', 'error', 'terminated'}:
            terminal_type, terminal_status = 'task.failed', 'failed'
        else:
            terminal_type, terminal_status = 'task.completed', 'completed'
        self.company_events.append(
            terminal_type,
            source={'type': 'workflow', 'id': 'durable-dag'},
            target={'type': 'ceo', 'id': 'group-ceo'},
            summary='Durable task reached a CEO-reportable terminal state.',
            task_id=task_id,
            status=terminal_status,
            visibility='human',
            meta={'runtime_state': final_state},
        )

    def status(self, task_id: str) -> Mapping[str, object]:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise KeyError(task_id)
        with self._lock:
            record = dict(self._records[task_id]) if task_id in self._records else None
        checkpoint = self.orchestrator.ledger.read_checkpoint(task_id)
        events = list(self.orchestrator.ledger.events(task_id))
        if record is None and checkpoint is None and not events:
            raise KeyError(task_id)
        return _project_task(task_id, record, checkpoint, events)

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)


# --------------------------------------------------------------------------- #
# Provider-backed draft adapter (N1-1A Fix A)
# --------------------------------------------------------------------------- #
class ModelProviderDraftAdapter:
    '''Adapts a ``ModelProvider`` (real or test-injected) into the novel
    ``DraftProvider`` contract used by ``NovelMvpService``.

    The adapter converts the chapter request + reviewed context into a bounded
    ``ModelRequest`` and converts the returned ``ModelResult`` into the existing
    draft record shape WITHOUT exposing any private model reasoning. Actual
    provider id, model id, response id, usage, and a trace-safe evidence record
    are preserved on the draft record.
    '''

    PROVIDER_ID = 'model-provider-draft-adapter/v1'

    def __init__(self, model_provider: ModelProvider, call_receipt_sink: 'CallReceiptSink | None' = None) -> None:
        if not hasattr(model_provider, 'complete'):
            raise TypeError('ModelProviderDraftAdapter requires a ModelProvider')
        self._model_provider = model_provider
        self._call_receipt_sink = call_receipt_sink

    @property
    def provider_id(self) -> str:
        return getattr(self._model_provider, 'provider_id', self.PROVIDER_ID)

    @property
    def model(self) -> str:
        return getattr(self._model_provider, 'model', '')

    def _build_grounded_writer_prompt(
        self, writer_context: Mapping[str, object], chapter_number: int,
        chapter_goal: str, min_chars: int, max_chars: int,
    ) -> str:
        '''Bounded, trace-safe grounded writer prompt (N1-2A defect-2 closure).

        Surfaces the validated writer_context: knowledge snippet text + SHA,
        selected proposal, world rules, character/timeline/hook state, rolling
        digest version, previous tail, current chapter goal, and incremental
        constraints. Never embeds secrets or private reasoning.
        '''
        wc = writer_context
        proposal = wc.get('selected_proposal') or {}
        snippets = list(wc.get('selected_knowledge_snippets') or [])
        canon = wc.get('story_canon') or {}
        lines: list[str] = []
        lines.append('你是本地原创小说创作助手，基于已审校的本地知识上下文创作原创叙事章节。')
        lines.append('禁止复制任何源片段、禁止外部发布或网络调用、禁止暴露内部推理过程。')
        lines.append('')
        lines.append('# 选定立项方案')
        lines.append(f"书名：{proposal.get('title', '')}")
        lines.append(f"前提：{proposal.get('premise', '')}")
        lines.append(f"主角：{proposal.get('protagonist', '')}")
        lines.append(f"核心冲突：{proposal.get('central_conflict', '')}")
        lines.append(f"开篇钩子：{proposal.get('opening_hook', '')}")
        lines.append('')
        lines.append('# 知识片段（已脱敏：正文摘要 + SHA256）')
        for s in snippets:
            if isinstance(s, dict):
                lines.append(f"- [{s.get('source_sha256', '')}] {s.get('title', '')}：{(s.get('text') or '')[:600]}")
        lines.append('')
        lines.append('# 世界规则')
        for r in (canon.get('world_rules') or []):
            lines.append(f'- {r}')
        lines.append('')
        lines.append('# 人物状态')
        for k, v in (canon.get('character_states') or {}).items():
            lines.append(f'- {k}：{v}')
        lines.append('')
        lines.append('# 时间线')
        for t in (canon.get('timeline') or []):
            lines.append(f'- {t}')
        lines.append('')
        lines.append('# 伏笔 / 钩子')
        for h in (canon.get('hooks') or []):
            lines.append(f'- {h}')
        lines.append('')
        lines.append(f"# 滚动连续性摘要版本：{wc.get('rolling_digest_version')}")
        lines.append(f"# 上一章尾部：{(wc.get('previous_tail_state') or '')[-200:]}")
        lines.append(f"# 本章目标：{wc.get('current_chapter_goal') or chapter_goal}")
        lines.append('# 增量约束')
        for c in (wc.get('incremental_constraints') or []):
            lines.append(f'- {c}')
        lines.append('')
        lines.append(
            f'请创作第{chapter_number}章。正文必须以「第{chapter_number}章」'
            f'（阿拉伯数字）作为开篇第一句，目标字符数约 {min_chars}-{max_chars}。'
        )
        return '\n'.join(lines)

    def draft(self, request: Mapping[str, object], context: Mapping[str, object]) -> Mapping[str, object]:
        chapter_number = int(request.get('chapter_number', 0))
        chapter_goal = str(request.get('chapter_goal', ''))
        min_chars = int(request.get('min_chars', 200))
        max_chars = int(request.get('max_chars', 12000))
        token_budget = int(request.get('token_budget', 1024))
        writer_context = request.get('writer_context')
        wc_sha256 = request.get('writer_context_sha256')

        system_text = (
            '你是本地原创小说创作助手。基于已审校的本地知识上下文创作原创叙事章节，'
            '禁止复制任何源片段、禁止进行任何外部发布或网络调用、禁止暴露内部推理过程。'
        )
        if isinstance(writer_context, dict):
            # Bounded, hash-checked grounded prompt (defect-2 closure). The real
            # production pilot always supplies a validated writer_context.
            if wc_sha256 is not None and sha256_text(canonical_json(writer_context)) != wc_sha256:
                raise ValueError('writer_context_sha256 mismatch; failing closed')
            user_text = self._build_grounded_writer_prompt(
                writer_context, chapter_number, chapter_goal, min_chars, max_chars,
            )
        else:
            # Minimal fallback: no grounded context (test/compat path). Never
            # used by the real pilot, which always provides writer_context.
            user_text = (
                f'请创作第{chapter_number}章：{chapter_goal}。'
                f'约束：本地原创、无外部发布、目标字符数约 {min_chars}-{max_chars}。'
            )
        model_request = ModelRequest(
            messages=(
                ModelMessage(role='system', content=system_text),
                ModelMessage(role='user', content=user_text),
            ),
            max_output_tokens=max(token_budget, 256),
            temperature=0.0,
            response_format='text',
            thinking='disabled',
        )
        result = self._model_provider.complete(model_request)
        prose = result.content or ''
        if not isinstance(prose, str):
            prose = str(prose)
        # Enforce writer bounds; truncate gracefully if the model over-produced.
        if len(prose) > max_chars:
            prose = prose[:max_chars]
        # Fail closed on too-short output for the grounded (real) path; never pad
        # a short response to fake a qualified chapter (N1-2A defect closure).
        if isinstance(writer_context, dict) and len(prose.strip()) < min_chars:
            raise ValueError(
                f'writer output too short ({len(prose.strip())} < {min_chars}); failing closed'
            )

        citations = list(context.get('citations') or [])
        record: dict[str, object] = {
            'schema_version': NOVEL_CHAPTER_DRAFT_SCHEMA,
            'task_id': request.get('task_id'),
            'project_id': str(request.get('project_id', '')),
            'chapter_id': request.get('chapter_id'),
            'chapter_number': chapter_number,
            'chapter_goal': chapter_goal,
            'provider_id': self.provider_id,
            'provider_model': self.model,
            'response_id': result.response_id,
            'context_package_sha256': context.get('package_sha256'),
            'citation_source_sha256': sorted(
                c.get('source_sha256') for c in citations if c.get('source_sha256')
            ),
            'char_count': len(prose),
            'token_budget': token_budget,
            'prose': prose,
            'external_effect': False,
            'created_at': authoritative_timestamp(),
            'model_usage': {
                'input_tokens': result.usage.input_tokens,
                'output_tokens': result.usage.output_tokens,
                'total_tokens': result.usage.total_tokens,
            },
            'finish_reason': result.finish_reason,
        }
        record['draft_sha256'] = sha256_text(prose)
        # Real provenance: request/output hashes + idempotency key + usage.
        # draft_sha256 is NEVER masqueraded as the writer response id.
        request_sha256 = sha256_text(canonical_json({'system': system_text, 'user': user_text}))
        output_sha256 = sha256_text(prose)
        record['request_sha256'] = request_sha256
        record['output_sha256'] = output_sha256
        record['idempotency_key'] = f'writer:{chapter_number}:{request_sha256[:16]}'
        if self._call_receipt_sink is not None:
            self._call_receipt_sink.emit(
                phase='writer',
                provider=getattr(self._model_provider, 'provider_id', self.provider_id),
                model=getattr(self._model_provider, 'model', self.model),
                response_id=result.response_id,
                usage={
                    'input_tokens': result.usage.input_tokens,
                    'output_tokens': result.usage.output_tokens,
                    'total_tokens': result.usage.total_tokens,
                },
                request_sha256=request_sha256,
                output_sha256=output_sha256,
                idempotency_key=record['idempotency_key'],
            )
        return record


def build_production_draft_provider(model_provider: object | None) -> object | None:
    '''Resolve the production draft provider for the CEO API.

    - A ``ModelProvider`` (real or test-injected) is wrapped with
      ``ModelProviderDraftAdapter`` so the production path never silently falls
      back to ``DeterministicLocalDraftProvider``.
    - An already-suitable ``DraftProvider`` (e.g. ``DeterministicLocalDraftProvider``
      injected explicitly by a test) is returned as-is.
    - ``None`` (no provider configured) returns ``None`` so the caller fails
      closed before drafting.
    '''
    if model_provider is None:
        return None
    if hasattr(model_provider, 'complete'):
        return ModelProviderDraftAdapter(model_provider)
    if hasattr(model_provider, 'draft'):
        return model_provider
    return None


def create_app(gateway: CommandGateway, planning_gateway: PlanningGateway | None = None, group_root: Path | None = None, model_provider: object | None = None, call_receipt_sink: 'CallReceiptSink | None' = None):
    app = FastAPI(
        title='Private AI Group CEO API', version='0.1.0',
        docs_url='/docs', redoc_url=None,
    )

    @app.get('/health')
    def health() -> dict[str, str]:
        return build_health_response()

    @app.post('/api/commands', status_code=202)
    async def submit_command(request: Request):
        try:
            data = await request.json()
            command = validate_command_request(data)
            task_id = gateway.submit(command)
        except ValidationError as exc:
            return JSONResponse(
                {'code': 'validation-error', 'message': str(exc)}, status_code=422,
            )
        except DuplicateTaskError:
            return JSONResponse(
                {'code': 'duplicate-task', 'message': 'task_id already exists'},
                status_code=409,
            )
        except GatewayUnavailableError:
            return JSONResponse(
                {'code': 'gateway-unavailable', 'message': 'CEO gateway unavailable'},
                status_code=503,
            )
        except Exception:
            return JSONResponse(
                {'code': 'internal-error', 'message': 'Internal server error'},
                status_code=500,
            )
        return JSONResponse(build_accepted_response(task_id), status_code=202)

    @app.get('/api/tasks/{task_id}')
    def task_status(task_id: str):
        try:
            return dict(gateway.status(task_id))
        except KeyError:
            return JSONResponse(
                {'code': 'task-not-found', 'message': 'Task not found'}, status_code=404,
            )
        except Exception:
            return JSONResponse(
                {'code': 'internal-error', 'message': 'Internal server error'},
                status_code=500,
            )

    if planning_gateway is not None:
        @app.post('/api/intents/plan', status_code=200)
        async def plan_intent(request: Request):
            try:
                data = await request.json()
                intent = validate_intent_request(data)
            except PlanningError:
                return JSONResponse(
                    {'code': 'validation-error', 'message': 'Invalid intent request.'},
                    status_code=422,
                )
            except Exception:
                return JSONResponse(
                    {'code': 'invalid-request', 'message': 'Request body must be JSON.'},
                    status_code=400,
                )
            try:
                decision = planning_gateway.plan(intent)
            except Exception:
                return JSONResponse(
                    {'code': 'planning-failed', 'message': 'Planning failed.'},
                    status_code=500,
                )
            return JSONResponse(decision.to_dict(), status_code=200)

        orchestrator = getattr(gateway, 'orchestrator', None)
        execution_graph = getattr(orchestrator, 'graph', None) if orchestrator else None
        execution_executors = (
            getattr(orchestrator, 'executors', None) if orchestrator else None
        )
        execution_group_root = getattr(gateway, 'group_root', None)
        execution_budget_truth = (
            load_budget_truth(execution_group_root)
            if execution_group_root is not None else None
        )

        @app.post('/api/intents/execute', status_code=200)
        async def execute_intent_route(request: Request):
            try:
                data = await request.json()
                intent = validate_intent_request(data)
            except PlanningError:
                return JSONResponse(
                    {'code': 'validation-error', 'message': 'Invalid intent request.'},
                    status_code=422,
                )
            except Exception:
                return JSONResponse(
                    {'code': 'invalid-request', 'message': 'Request body must be JSON.'},
                    status_code=400,
                )
            if execution_graph is None or execution_executors is None or (
                execution_budget_truth is None
            ):
                return JSONResponse(
                    {'code': 'gateway-unavailable', 'message': 'CEO execution gateway unavailable'},
                    status_code=503,
                )
            try:
                result = execute_intent(
                    intent,
                    planning_gateway=planning_gateway,
                    command_gateway=gateway,
                    graph=execution_graph,
                    executors=execution_executors,
                    budget_truth=execution_budget_truth,
                )
            except DuplicateTaskError:
                return JSONResponse(
                    {'code': 'duplicate-task', 'message': 'task_id already exists'},
                    status_code=409,
                )
            except Exception:
                return JSONResponse(
                    {'code': 'execution-failed', 'message': 'Intent execution failed.'},
                    status_code=500,
                )
            return JSONResponse(result.to_dict(), status_code=200)

    company_events = getattr(gateway, 'company_events', None)
    if isinstance(company_events, CompanyEventStore):
        mount_company_event_routes(app, company_events)

    # -- W1-1 local read-only economic routes (contract §9.4) ------------- #
    _ECON_ROOT = getattr(gateway, 'group_root', None)
    if _ECON_ROOT is not None:
        _econ_root = Path(_ECON_ROOT) / 'economics'

        def _open_econ_ledger() -> EconomicLedger:
            return EconomicLedger(_econ_root)

        def _load_econ_policy() -> EconomicBudgetPolicy | None:
            candidate = Path(_ECON_ROOT) / 'governance' / 'economics.policy.json'
            if not candidate.is_file():
                candidate = Path(_ECON_ROOT) / 'shared' / 'policies' / 'economics.policy.json'
            if not candidate.is_file():
                return None
            try:
                return EconomicBudgetPolicy.from_dict(load_json(candidate))
            except Exception:
                return None

        @app.get('/api/economics/budget-status')
        def economics_budget_status():
            policy = _load_econ_policy()
            if policy is None:
                return JSONResponse(
                    {'available': False, 'code': ECON_BUDGET_TRUTH_UNAVAILABLE},
                    status_code=200,
                )
            truth = EconomicBudgetTruth(policy, _open_econ_ledger())
            return JSONResponse(truth.status(), status_code=200)

        @app.get('/api/economics/summary')
        def economics_summary():
            root = _econ_root
            if not root.exists():
                return JSONResponse(
                    {'schema_version': SCHEMA_SUMMARY, 'currency': 'CNY',
                     'entry_count': 0, 'spent_fen': 0, 'reserved_active_fen': 0,
                     'work_count': 0, 'version_count': 0, 'platform_count': 0,
                     'settlement_count': 0, 'by_event_type': {}},
                    status_code=200,
                )
            return JSONResponse(_open_econ_ledger().summary(), status_code=200)

        @app.get('/api/economics/entries')
        def economics_entries(
            event_type: str | None = None,
            work_id: str | None = None,
            version_id: str | None = None,
            platform_id: str | None = None,
            limit: int = 200,
        ):
            entries = _open_econ_ledger().entries(
                event_type=event_type, work_id=work_id,
                version_id=version_id, platform_id=platform_id, limit=limit,
            )
            return JSONResponse(
                {'count': len(entries), 'entries': entries}, status_code=200,
            )

        @app.get('/api/economics/work/{work_id}')
        def economics_work(work_id: str):
            entries = _open_econ_ledger().entries(event_type='work', work_id=work_id)
            return JSONResponse(
                {'work_id': work_id, 'entries': entries}, status_code=200,
            )

        @app.get('/api/economics/version/{version_id}')
        def economics_version(version_id: str):
            entries = _open_econ_ledger().entries(
                event_type='version', version_id=version_id,
            )
            return JSONResponse(
                {'version_id': version_id, 'entries': entries}, status_code=200,
            )

        @app.post('/api/economics/export')
        def economics_export(payload: dict):
            target_dir = (payload or {}).get('target_dir')
            if not target_dir:
                return JSONResponse({'error': 'target_dir required'}, status_code=422)
            try:
                manifest = _open_econ_ledger().export(target_dir)
            except EconomicLedgerError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse({'schema_version': SCHEMA_EXPORT, 'export': manifest}, status_code=200)

        @app.post('/api/economics/restore')
        def economics_restore(payload: dict):
            source_dir = (payload or {}).get('source_dir')
            target_root = (payload or {}).get('target_root')
            if not source_dir or not target_root:
                return JSONResponse(
                    {'error': 'source_dir and target_root required'}, status_code=422,
                )
            try:
                ledger = EconomicLedger.restore(source_dir, target_root)
            except EconomicLedgerError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(
                {'summary': ledger.summary(), 'entry_count': ledger.count()},
                status_code=200,
            )

    # -- W2-1 local publication control-plane routes (contract §9) -------- #
    # Production default transport is deny-all: no external platform action
    # can ever occur through this API. Accepted shadow receipts are exercised
    # at the service layer with an injected fake transport.
    _PUB_ROOT = group_root or getattr(gateway, 'group_root', None)
    if _PUB_ROOT is not None:
        _pub_root = Path(_PUB_ROOT)

        def _open_publication_service() -> PublicationService:
            ledger = EconomicLedger(_pub_root / 'economics')
            return PublicationService(
                _pub_root, economic_ledger=ledger,
                transport=DenyAllShadowTransport(),
            )

        def _pub_ok(payload: dict | None) -> dict:
            if not isinstance(payload, dict):
                return {'error': 'json body required', 'code': 'bad-request'}
            return {}

        @app.post('/api/publication-candidates')
        def publication_candidates_create(payload: dict):
            err = _pub_ok(payload)
            if err:
                return JSONResponse(err, status_code=422)
            body = payload or {}
            try:
                candidate = _open_publication_service().create_candidate(
                    work_id=str(body.get('work_id') or ''),
                    version_id=str(body.get('version_id') or ''),
                    artifact_id=str(body.get('artifact_id') or ''),
                    artifact_sha256=str(body.get('artifact_sha256') or ''),
                    content_ref=str(body.get('content_ref') or ''),
                    content_bytes=int(body.get('content_bytes') or 0),
                    quality_evidence_refs=list(body.get('quality_evidence_refs') or []),
                    title=str(body.get('title') or ''),
                    summary=str(body.get('summary') or ''),
                    idempotency_key=body.get('idempotency_key'),
                )
            except PlatformAdapterError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(candidate, status_code=201)

        @app.get('/api/publication-candidates/{candidate_id}')
        def publication_candidates_get(candidate_id: str):
            try:
                candidate = _open_publication_service().get_candidate(candidate_id)
            except PlatformAdapterError as exc:
                return _service_error_response(exc, 404)
            return JSONResponse(candidate, status_code=200)

        @app.post('/api/publication-candidates/{candidate_id}/withdraw')
        def publication_candidates_withdraw(candidate_id: str):
            try:
                candidate = _open_publication_service().withdraw_candidate(candidate_id)
            except PlatformAdapterError as exc:
                return _service_error_response(exc, 404)
            return JSONResponse(candidate, status_code=200)

        @app.post('/api/platform-shadow/actions')
        def platform_shadow_actions_create(payload: dict):
            err = _pub_ok(payload)
            if err:
                return JSONResponse(err, status_code=422)
            body = payload or {}
            lease = None
            lease_raw = body.get('lease')
            if isinstance(lease_raw, dict):
                lease = PlatformAuthorizationLease.from_dict(lease_raw)
            try:
                receipt = _open_publication_service().dispatch_shadow(
                    str(body.get('candidate_id') or ''),
                    platform_id=str(body.get('platform_id') or 'local-shadow'),
                    account_ref=str(body.get('account_ref') or ''),
                    mode=str(body.get('mode') or 'shadow'),
                    action=str(body.get('action') or 'submit_shadow'),
                    lease=lease,
                    idempotency_key=str(body.get('idempotency_key') or ''),
                    adapter_id=str(body.get('adapter_id') or 'local-shadow/v1'),
                )
            except PlatformAdapterError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(receipt, status_code=200)

        @app.get('/api/platform-shadow/actions/{action_id}')
        def platform_shadow_actions_get(action_id: str):
            try:
                action = _open_publication_service().get_action(action_id)
            except PlatformAdapterError as exc:
                return _service_error_response(exc, 404)
            return JSONResponse(action, status_code=200)

        @app.post('/api/platform-shadow/actions/{action_id}/resume')
        def platform_shadow_actions_resume(action_id: str, payload: dict | None = None):
            body = payload or {}
            lease = None
            lease_raw = body.get('lease')
            if isinstance(lease_raw, dict):
                lease = PlatformAuthorizationLease.from_dict(lease_raw)
            try:
                receipt = _open_publication_service().resume_action(
                    action_id, lease=lease,
                )
            except PlatformAdapterError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(receipt, status_code=200)

        @app.post('/api/platform-shadow/export')
        def platform_shadow_export(payload: dict):
            target_dir = (payload or {}).get('target_dir')
            if not target_dir:
                return JSONResponse(
                    {'error': 'target_dir required'}, status_code=422,
                )
            try:
                manifest = _open_publication_service().export(target_dir)
            except PlatformAdapterError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(
                {'schema_version': SCHEMA_EXPORT, 'export': manifest}, status_code=200,
            )

        @app.post('/api/platform-shadow/restore')
        def platform_shadow_restore(payload: dict):
            source_dir = (payload or {}).get('source_dir')
            target_root = (payload or {}).get('target_root')
            if not source_dir or not target_root:
                return JSONResponse(
                    {'error': 'source_dir and target_root required'}, status_code=422,
                )
            try:
                svc = PublicationService.restore(source_dir, target_root)
            except PlatformAdapterError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(
                {'candidate_count': len(svc.list_candidates()),
                 'event_count': len(svc._events)},
                status_code=200,
            )

    # -- W2-2 novel production batch + release-candidate routes (§11) ----- #
    # Production default transport is deny-all: no external platform action
    # can ever occur through this API. The publish route accepts a local-only
    # 'fake' shadow transport (no network, no real publication) for acceptance
    # testing; any other value falls back to the deny-all default.
    if _PUB_ROOT is not None:
        _pb_root = _pub_root

        def _open_production_batch_service(transport: object = None):
            ledger = EconomicLedger(_pb_root / 'economics')
            tport = transport or DenyAllShadowTransport()
            pub = PublicationService(
                _pb_root, economic_ledger=ledger, transport=tport,
            )
            provider = build_production_draft_provider(model_provider)
            if provider is None:
                raise NovelProductionBatchError(
                    'provider-unavailable',
                    'No production draft provider configured; refusing to draft.',
                )
            return NovelProductionBatchService(
                _pb_root, economic_ledger=ledger,
                draft_provider=provider,
                publication_service=pub, transport=tport,
            )

        def _pb_ok(payload: dict | None) -> dict:
            if not isinstance(payload, dict):
                return {'error': 'json body required', 'code': 'bad-request'}
            return {}

        @app.post('/api/production-batches')
        def production_batches_create(payload: dict):
            err = _pb_ok(payload)
            if err:
                return JSONResponse(err, status_code=422)
            try:
                state = _open_production_batch_service().submit(payload)
            except NovelProductionBatchError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(state, status_code=201)

        @app.get('/api/production-batches/{batch_id}')
        def production_batches_get(batch_id: str):
            try:
                state = _open_production_batch_service().get_state(batch_id)
            except NovelProductionBatchError as exc:
                return _service_error_response(exc, 404)
            return JSONResponse(state, status_code=200)

        @app.post('/api/production-batches/{batch_id}/resume')
        def production_batches_resume(batch_id: str):
            try:
                state = _open_production_batch_service().resume(batch_id)
            except NovelProductionBatchError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(state, status_code=200)

        @app.post('/api/production-batches/{batch_id}/slots/{slot_id}/revise')
        def production_batches_revise(batch_id: str, slot_id: str, payload: dict | None = None):
            body = payload or {}
            reason = body.get('reason') or 'ceo-directed revision'
            try:
                state = _open_production_batch_service().revise_slot(
                    batch_id, slot_id, reason=reason,
                    idempotency_key=body.get('idempotency_key'),
                )
            except NovelProductionBatchError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(state, status_code=200)

        @app.post('/api/production-batches/{batch_id}/release')
        def production_batches_release(batch_id: str):
            try:
                release = _open_production_batch_service().assemble_release(batch_id)
            except NovelProductionBatchError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(release, status_code=201)

        @app.post('/api/production-batches/{batch_id}/release/publish')
        def production_batches_publish(batch_id: str, payload: dict | None = None):
            body = payload or {}
            transport = None
            if body.get('transport') == 'fake':
                transport = FakeShadowTransport()
            svc = _open_production_batch_service(transport=transport)
            try:
                release_id = None
                for rid, rel in svc._releases.items():
                    if rel.get('batch_id') == batch_id and rel.get('status') != 'withdrawn':
                        release_id = rid
                        break
                if release_id is None:
                    return JSONResponse(
                        {'error': 'no-ready-release', 'message': 'assemble a release first'},
                        status_code=409,
                    )
                out = svc.publish_release(release_id)
            except (NovelProductionBatchError, PlatformAdapterError) as exc:
                code = getattr(exc, 'code', 'error')
                return JSONResponse(
                    {'error': code, 'message': getattr(exc, 'message', str(exc))},
                    status_code=409,
                )
            return JSONResponse(out, status_code=200)

        @app.post('/api/production-batches/{batch_id}/release/withdraw')
        def production_batches_withdraw(batch_id: str, payload: dict | None = None):
            body = payload or {}
            reason = body.get('reason') or 'ceo-directed withdrawal'
            svc = _open_production_batch_service()
            release_id = None
            for rid, rel in svc._releases.items():
                if rel.get('batch_id') == batch_id and rel.get('status') != 'withdrawn':
                    release_id = rid
                    break
            if release_id is None:
                return JSONResponse(
                    {'error': 'no-ready-release', 'message': 'no active release to withdraw'},
                    status_code=409,
                )
            try:
                rel = svc.withdraw_release(release_id, reason=reason)
            except NovelProductionBatchError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(rel, status_code=200)

        @app.post('/api/production-batches/export')
        def production_batches_export(payload: dict):
            target_dir = (payload or {}).get('target_dir')
            if not target_dir:
                return JSONResponse(
                    {'error': 'target_dir required'}, status_code=422,
                )
            try:
                manifest = _open_production_batch_service().export(target_dir)
            except NovelProductionBatchError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(manifest, status_code=200)

        @app.post('/api/production-batches/restore')
        def production_batches_restore(payload: dict):
            source_dir = (payload or {}).get('source_dir')
            target_root = (payload or {}).get('target_root')
            if not source_dir or not target_root:
                return JSONResponse(
                    {'error': 'source_dir and target_root required'}, status_code=422,
                )
            try:
                svc = NovelProductionBatchService.restore(source_dir, target_root)
            except NovelProductionBatchError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(
                {'batch_count': len(svc._batches), 'release_count': len(svc._releases)},
                status_code=200,
            )

        # -- Novel Subsidiary rolling operations (N1-1) -------------------- #
        # Mirrors the W2-2 production-batch pattern: a factory opens the service
        # against the group root. The production path uses an injected or
        # configured ModelProvider-compatible adapter and NEVER silently falls
        # back to DeterministicLocalDraftProvider. State persists via the
        # append-only operations log, so separate HTTP calls replay the same
        # operation.
        def _open_novel_operations_service() -> 'NovelOperationsService':
            if isinstance(model_provider, DeterministicOwnerModelProvider):
                provider: object = DeterministicLocalDraftProvider()
            else:
                provider = build_production_draft_provider(model_provider)
                if provider is None:
                    raise NovelOperationsError(
                        'provider-unavailable',
                        'No production draft provider configured; refusing to draft.',
                    )
            return NovelOperationsService(
                _pb_root, draft_provider=provider, model_provider=model_provider,
                call_receipt_sink=call_receipt_sink,
            )

        def _nov_body(payload: object, *, required: tuple[str, ...]):
            if not isinstance(payload, dict):
                return None, {'error': 'json body required', 'code': 'bad-request'}
            # Treat only None / empty-string as missing so a valid numeric
            # proposal_index of 0 is not rejected by falsy coercion.
            missing = [k for k in required if payload.get(k) is None or payload.get(k) == '']
            if missing:
                return None, {'error': f'missing fields: {missing}', 'code': 'bad-request'}
            return payload, None

        @app.post('/api/novel-operations/proposals')
        def novel_ops_proposals(payload: dict):
            body, err = _nov_body(payload, required=('op_id', 'goal', 'project_id'))
            if err:
                return JSONResponse(err, status_code=422)
            try:
                ps = _open_novel_operations_service().request_proposals(
                    body['op_id'], body['goal'], body['project_id'],
                )
            except NovelOperationsError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(ps, status_code=200)

        @app.post('/api/novel-operations/select')
        def novel_ops_select(payload: dict):
            body, err = _nov_body(payload, required=('op_id', 'proposal_index'))
            if err:
                return JSONResponse(err, status_code=422)
            try:
                sel = _open_novel_operations_service().select_proposal(
                    body['op_id'], int(body['proposal_index']),
                )
            except (NovelOperationsError, ValueError, TypeError) as exc:
                code = getattr(exc, 'code', 'bad-request')
                return JSONResponse({'error': code, 'message': str(exc)}, status_code=409)
            return JSONResponse(sel, status_code=200)

        @app.post('/api/novel-operations/init-buffer')
        def novel_ops_init_buffer(payload: dict):
            body, err = _nov_body(payload, required=('op_id',))
            if err:
                return JSONResponse(err, status_code=422)
            try:
                state = _open_novel_operations_service().initialize_buffer(body['op_id'])
            except NovelOperationsError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(state, status_code=200)

        @app.post('/api/novel-operations/confirm-chapter-1')
        def novel_ops_confirm(payload: dict):
            body, err = _nov_body(payload, required=('op_id',))
            if err:
                return JSONResponse(err, status_code=422)
            try:
                conf = _open_novel_operations_service().confirm_chapter_1(body['op_id'])
            except NovelOperationsError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(conf, status_code=200)

        @app.post('/api/novel-operations/advance')
        def novel_ops_advance(payload: dict | None = None):
            body, err = _nov_body(payload or {}, required=('op_id',))
            if err:
                return JSONResponse(err, status_code=422)
            try:
                state = _open_novel_operations_service().advance(
                    body['op_id'],
                    force_full_window=bool((body or {}).get('force_full_window', False)),
                )
            except NovelOperationsError as exc:
                return _service_error_response(exc, 409)
            return JSONResponse(state, status_code=200)

        @app.get('/api/novel-operations/{op_id}/state')
        def novel_ops_state(op_id: str):
            try:
                state = _open_novel_operations_service().get_state(op_id)
            except NovelOperationsError as exc:
                return _service_error_response(exc, 404)
            return JSONResponse(state, status_code=200)

        @app.get('/api/novel-operations/provider-status')
        def novel_ops_provider_status():
            # Owner-visible provider readiness: routed DeepSeek is bound, the
            # Secret is referenced (never resolved/persisted here), and the
            # fallback is bounded to 1. No model call is made on this probe.
            svc = _open_novel_operations_service()
            provider = getattr(svc, 'model_provider', None)
            ready = isinstance(provider, RoutingModelProvider)
            deep = None
            if ready:
                entry = provider.registry.resolve('deepseek')
                if entry is not None:
                    deep = entry.provider
            return JSONResponse({
                'provider_bound': ready,
                'provider': 'deepseek' if deep is not None else None,
                'model': getattr(deep, 'model', None),
                'secret_ref': getattr(deep, 'secret_ref', None),
                'secret_value_persisted': False,
                'maximum_fallback_attempts': getattr(provider, 'maximum_fallback_attempts', None),
                'local_only': True,
                'external_publication': False,
            }, status_code=200)

    return app


class DeterministicOwnerModelProvider:
    '''Opt-in deterministic model provider for local owner-operable acceptance.

    Active ONLY when ``N1_3_DETERMINISTIC=1`` is set in the environment. It is
    never used in production. It returns a fixed, well-formed three-proposal
    set and a fixed PASS audit verdict so the full owner flow
    (proposals -> select -> buffer -> confirm -> advance) executes with zero
    real model calls and zero external effect. The fixed response id keeps
    restart/replay equality stable for the acceptance evidence. It never
    resolves, prints, or persists any secret and never touches the network.
    '''

    provider_id = 'deterministic-owner'
    model = 'deterministic-owner-v1'

    def complete(self, request: 'ModelRequest') -> 'ModelResult':
        prompt = ''
        for message in getattr(request, 'messages', []) or []:
            prompt += str(getattr(message, 'content', '') or '')
        if '你是小说子公司立项模型' in prompt:
            parsed: dict[str, object] = self._proposal_payload()
        elif '你是小说子公司独立审计模型' in prompt:
            parsed = {
                'verdict': 'PASS',
                'issue_codes': [],
                'affected_facts': [],
                'repair_instructions': [],
                'checks': {
                    'character': True, 'timeline': True, 'hooks': True,
                    'canon': True, 'transitions': True, 'citations': True, 'tail': True,
                },
            }
        else:
            parsed = {'verdict': 'PASS', 'ok': True}
        usage = ModelUsage(input_tokens=12, output_tokens=18, total_tokens=30)
        return ModelResult(
            provider=self.provider_id,
            model=self.model,
            response_id='deterministic-owner-fixed-response',
            content='',
            parsed_json=parsed,
            tool_calls=(),
            finish_reason='stop',
            usage=usage,
        )

    @staticmethod
    def _proposal_payload() -> dict[str, object]:
        def proposal(title, premise, audience, protagonist, conflict, hook, directions, risks):
            return {
                'title': title,
                'premise': premise,
                'genre_audience': audience,
                'protagonist': protagonist,
                'central_conflict': conflict,
                'opening_hook': hook,
                'five_chapter_direction': directions,
                'risk': risks,
                'knowledge_citations': [],
            }

        return {'proposals': [
            proposal(
                '规则苏醒',
                '当全城规则在午夜苏醒，主角必须在系统重写前找到退路。',
                '科幻悬疑 / 免费阅读爆款', '林夜',
                '个体意志与自发规则的对抗', '规则将在午夜之后苏醒。',
                ['第一卷 规则异变', '第二卷 暗网追踪', '第三卷 身份置换', '第四卷 系统反噬', '第五卷 黎明重置'],
                ['强设定节奏风险', '信息密度偏高'],
            ),
            proposal(
                '墨白长歌',
                '落魄琴师以一曲墨白引动王朝记忆，揭开被抹去的史册。',
                '古风权谋 / 女性向爆款', '苏砚',
                '记忆真相与皇权叙事的冲突', '史册从不记载失败者。',
                ['第一卷 断弦', '第二卷 残卷', '第三卷 禁庭', '第四卷 焚稿', '第五卷 长歌'],
                ['古风考据风险', '权谋节奏风险'],
            ),
            proposal(
                '深海牧歌',
                '近未来海洋牧场里，少女与共生体共同守护最后的蓝。',
                '治愈科幻 / 全年龄爆款', '阿蓝',
                '生态存续与人类贪欲的冲突', '海会记得每一个守护者。',
                ['第一卷 潮起', '第二卷 礁语', '第三卷 暗流', '第四卷 蔚蓝', '第五卷 归海'],
                ['治愈向节奏风险', '设定科普密度'],
            ),
        ]}


def _service_error_response(exc, status_code: int) -> JSONResponse:
    return JSONResponse(
        {'error': exc.code, 'message': exc.message},
        status_code=status_code,
    )


def create_runtime_app(group_root: Path):
    model_provider = build_runtime_provider()
    deterministic = os.environ.get('N1_3_DETERMINISTIC') == '1'
    if deterministic:
        model_provider = DeterministicOwnerModelProvider()
    # N1-4 real-model pilot instrumentation: the live serve-api path previously
    # never constructed a CallReceiptSink, so REAL DeepSeek calls were never
    # counted and model_usage.real_model_call_count was hard-wired to 0 -- the
    # N1-4 evidence/validator could not observe that real calls happened. Wire a
    # sink in REAL (non-deterministic) mode only, so deterministic mode keeps the
    # intended real_model_call_count == 0 unchanged. The sink file lives under
    # the runtime root and is NOT the validator's per-chapter receipt file.
    call_receipt_sink = None
    if not deterministic:
        sink_path = Path(group_root) / 'call-receipts.jsonl'
        call_receipt_sink = CallReceiptSink(
            sink_path,
            pilot_id=os.environ.get('N1_4_PILOT_ID') or 'n1-4-real-model-pilot',
        )
    app = create_app(
        RuntimeCommandGateway(group_root),
        planning_gateway=RuntimePlanningGateway(group_root),
        model_provider=model_provider,
        call_receipt_sink=call_receipt_sink,
    )
    from .novel_studio import mount_novel_routes

    mount_novel_routes(app, group_root)

    from .novel_mvp import mount_novel_mvp_routes

    mount_novel_mvp_routes(app, group_root)

    @app.get("/api/status")
    def packaged_dashboard_status():
        return {
            "schema_version": "novel-standalone/v0",
            "generated_at": datetime.now(SHANGHAI_TZ).isoformat(),
            "sources": [],
            "standalone": True,
        }

    dashboard_dist = Path(__file__).resolve().parents[2] / "dashboard" / "dist"
    if dashboard_dist.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=dashboard_dist, html=True), name="dashboard")
    return app
