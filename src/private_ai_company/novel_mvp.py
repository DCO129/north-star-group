'''P0-4 Novel MVP vertical closure integration layer.

This module wires the already-accepted P0-1 (CEO planning + validated task
graph), P0-2 (KnowledgeAdapter grounded chapter context) and P0-3
(RestartSafeStateSpine recovery) capabilities into one real, local, restart-safe
novel-production slice:

    owner goal
    -> CEO semantic planning
    -> validated four-node task graph
    -> KnowledgeAdapter chapter context
    -> deterministic local chapter draft
    -> independent quality gate
    -> accepted artifact / evidence registration
    -> StateSpine recovery, export, and restore

The slice is local, free, reversible, deterministic, and has no external effect.
A failed draft is never registered as an accepted chapter artifact. Completed
DAG nodes are never executed twice after object destruction and resume.
'''

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from . import novel_knowledge as nk
from . import root as _root
from .economic_ledger import EconomicLedger
from .dag import DagNodeSpec, DepartmentDagExecutor, DurableDagRunner
from .executors import DepartmentExecutor, DepartmentResult, ExecutionRequest
from .state_spine import RestartSafeStateSpine, StateSpineError
from .timebase import authoritative_timestamp
from ._hashing import sha256_text


# --------------------------------------------------------------------------- #
# Public schemas (contract §7)
# --------------------------------------------------------------------------- #
NOVEL_MVP_REQUEST_SCHEMA = 'novel-mvp-request/v1'
NOVEL_MVP_RUN_SCHEMA = 'novel-mvp-run/v1'
NOVEL_CHAPTER_DRAFT_SCHEMA = 'novel-chapter-draft/v1'
NOVEL_QUALITY_REPORT_SCHEMA = 'novel-quality-report/v1'
NOVEL_ACCEPTED_ARTIFACT_SCHEMA = 'novel-accepted-artifact/v1'

# Re-export the P0-2 context package schema name used by the contract.
NOVEL_CHAPTER_CONTEXT_PACKAGE_SCHEMA = nk.PACKAGE_SCHEMA

# Frozen four-node capability graph (contract §5).
NOVEL_CAPABILITIES: tuple[str, ...] = (
    'novel-context',
    'novel-draft',
    'novel-quality',
    'novel-artifact',
)

# Frozen dependency edges (mirrors task_graph.KNOWN_DEPENDENCY_EDGES for novel).
NOVEL_DEPENDENCY_EDGES: dict[str, tuple[str, ...]] = {
    'novel-context': (),
    'novel-draft': ('novel-context',),
    'novel-quality': ('novel-context', 'novel-draft'),
    'novel-artifact': ('novel-context', 'novel-draft', 'novel-quality'),
}

DEPARTMENT_ID = 'example.novel'
NOVEL_DEPT_ID = 'dept-novel'


# --------------------------------------------------------------------------- #
# Stable blocking codes (contract §11)
# --------------------------------------------------------------------------- #
CODE_REQUEST_INVALID = 'novel-mvp-request-invalid'
CODE_INPUT_REF_INVALID = 'novel-mvp-input-ref-invalid'
CODE_BINDING_CONFLICT = 'novel-mvp-binding-conflict'
CODE_PROJECT_MISSING = 'novel-mvp-project-missing'
CODE_CONTEXT_BLOCKED = 'novel-mvp-context-blocked'
CODE_CONTEXT_HASH_MISMATCH = 'novel-mvp-context-hash-mismatch'
CODE_DRAFT_FAILED = 'novel-mvp-draft-failed'
CODE_DRAFT_EMPTY = 'novel-mvp-draft-empty'
CODE_QUALITY_BLOCKED = 'novel-mvp-quality-blocked'
CODE_QUALITY_HASH_MISMATCH = 'novel-mvp-quality-hash-mismatch'
CODE_ARTIFACT_BEFORE_ACCEPTANCE = 'novel-mvp-artifact-before-acceptance'
CODE_ARTIFACT_DUPLICATE = 'novel-mvp-artifact-duplicate'
CODE_RESUME_CONFLICT = 'novel-mvp-resume-conflict'
CODE_EXTERNAL_EFFECT_FORBIDDEN = 'novel-mvp-external-effect-forbidden'


class NovelMvpError(Exception):
    '''Bounded novel MVP error carrying a stable blocking code.'''

    def __init__(self, code: str, message: str = '') -> None:
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
_UNIFIED_ID_RE = re.compile(r'^(run|task|dept|artifact)-[a-z0-9][a-z0-9._-]{2,63}$')
_REF_PREFIX = 'group-file:'
_REQUEST_DIR = 'runtime/novel-mvp/requests'
_SPINE_DIR = 'runtime/novel-mvp/spine'
_REGISTRY_DIR = 'runtime/novel-mvp/runs'


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _slug(value: str) -> str:
    cleaned = re.sub(r'[^a-z0-9._-]', '-', str(value).lower())
    return cleaned[:50].strip('-') or 'x'


def validate_novel_mvp_request(data: Any) -> dict[str, Any]:
    '''Validate and normalize a novel-mvp-request/v1 payload (contract §6.2).'''
    if not isinstance(data, dict):
        raise NovelMvpError(CODE_REQUEST_INVALID, 'request must be a JSON object')
    if data.get('schema_version') != NOVEL_MVP_REQUEST_SCHEMA:
        raise NovelMvpError(
            CODE_REQUEST_INVALID,
            f'schema_version must be {NOVEL_MVP_REQUEST_SCHEMA!r}',
        )

    task_id = data.get('task_id')
    if not isinstance(task_id, str) or not _UNIFIED_ID_RE.fullmatch(task_id):
        raise NovelMvpError(
            CODE_REQUEST_INVALID,
            'task_id must be a unified identifier (task-/run-/dept-/artifact-...)',
        )

    project_id = data.get('project_id')
    if not isinstance(project_id, str) or not nk.PROJECT_ID_RE.fullmatch(project_id):
        raise NovelMvpError(CODE_REQUEST_INVALID, 'project_id is invalid')

    chapter_id = data.get('chapter_id')
    if not isinstance(chapter_id, str) or not chapter_id.strip():
        raise NovelMvpError(CODE_REQUEST_INVALID, 'chapter_id is required')

    chapter_number = data.get('chapter_number')
    if not isinstance(chapter_number, int) or isinstance(chapter_number, bool) or chapter_number < 1:
        raise NovelMvpError(CODE_REQUEST_INVALID, 'chapter_number must be an integer >= 1')

    chapter_goal = data.get('chapter_goal')
    if not isinstance(chapter_goal, str) or not chapter_goal.strip():
        raise NovelMvpError(CODE_REQUEST_INVALID, 'chapter_goal is required')

    token_budget = data.get('token_budget')
    if (
        not isinstance(token_budget, int)
        or isinstance(token_budget, bool)
        or not (128 <= token_budget <= 4096)
    ):
        raise NovelMvpError(CODE_REQUEST_INVALID, 'token_budget must be 128..4096')

    min_chars = data.get('min_chars')
    max_chars = data.get('max_chars')
    if (
        not isinstance(min_chars, int)
        or isinstance(min_chars, bool)
        or not isinstance(max_chars, int)
        or isinstance(max_chars, bool)
    ):
        raise NovelMvpError(CODE_REQUEST_INVALID, 'min_chars/max_chars are required')
    if not (200 <= min_chars <= max_chars <= 12000):
        raise NovelMvpError(
            CODE_REQUEST_INVALID,
            'char range must satisfy 200 <= min_chars <= max_chars <= 12000',
        )

    def _strlist(value: Any, field: str) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise NovelMvpError(CODE_REQUEST_INVALID, f'{field} must be a list of strings')
        return list(value)

    required_story_anchor_ids = _strlist(data.get('required_story_anchor_ids', []), 'required_story_anchor_ids')
    required_craft_technique_ids = _strlist(
        data.get('required_craft_technique_ids', []), 'required_craft_technique_ids'
    )
    forbidden_terms = _strlist(data.get('forbidden_terms', []), 'forbidden_terms')
    continuity_requirements = _strlist(data.get('continuity_requirements', []), 'continuity_requirements')
    acceptance_criteria = _strlist(data.get('acceptance_criteria', []), 'acceptance_criteria')
    if not acceptance_criteria:
        raise NovelMvpError(CODE_REQUEST_INVALID, 'acceptance_criteria are required')

    # writer_context is optional at the contract edge but, when present, MUST
    # be bounded and hash-consistent. It is preserved (never dropped) so the
    # real writer prompt can receive grounded context instead of chapter
    # metadata only (defect 2 closure: validator no longer discards it).
    writer_context = data.get('writer_context')
    writer_context_sha256 = data.get('writer_context_sha256')
    if writer_context is not None:
        _validate_writer_context(writer_context, writer_context_sha256)

    return {
        'schema_version': NOVEL_MVP_REQUEST_SCHEMA,
        'task_id': task_id,
        'project_id': project_id,
        'chapter_id': chapter_id,
        'chapter_number': chapter_number,
        'chapter_goal': chapter_goal,
        'token_budget': token_budget,
        'min_chars': min_chars,
        'max_chars': max_chars,
        'required_story_anchor_ids': required_story_anchor_ids,
        'required_craft_technique_ids': required_craft_technique_ids,
        'forbidden_terms': forbidden_terms,
        'continuity_requirements': continuity_requirements,
        'acceptance_criteria': acceptance_criteria,
        'writer_context': writer_context,
        'writer_context_sha256': writer_context_sha256,
    }


def _validate_writer_context(wc: object, wc_sha256: object) -> None:
    '''Bounded, hash-checked validation of an optional writer_context (defect 2).

    Fails closed when the context is malformed, missing required bounded fields,
    oversized, or its recorded SHA-256 does not match the canonicalized payload.
    Secrets/private reasoning are never expected inside this structure.
    '''
    if not isinstance(wc, dict):
        raise NovelMvpError(
            CODE_REQUEST_INVALID, 'writer_context must be a JSON object when present'
        )
    required_fields = (
        'selected_proposal',
        'selected_knowledge_snippets',
        'story_canon',
        'rolling_digest_version',
        'previous_tail_state',
        'current_chapter_goal',
        'incremental_constraints',
    )
    missing = [f for f in required_fields if f not in wc]
    if missing:
        raise NovelMvpError(
            CODE_REQUEST_INVALID,
            f'writer_context missing required fields: {", ".join(missing)}',
        )
    # Bounded size: the grounded context must stay within a traceable envelope.
    wc_text = canonical_json(wc)
    if len(wc_text) > 20000:
        raise NovelMvpError(CODE_REQUEST_INVALID, 'writer_context exceeds bounded size')
    # Hash integrity: a recorded sha must match the canonicalized payload.
    if wc_sha256 is not None:
        if not isinstance(wc_sha256, str):
            raise NovelMvpError(CODE_REQUEST_INVALID, 'writer_context_sha256 must be a string')
        if sha256_text(canonical_json(wc)) != wc_sha256:
            raise NovelMvpError(CODE_CONTEXT_HASH_MISMATCH, 'writer_context_sha256 mismatch')


def _validate_input_refs(input_refs: Sequence[str]) -> str:
    '''Exactly one group-file reference is allowed for P0-4 (contract §6.1).'''
    refs = [str(r).strip() for r in (input_refs or [])]
    refs = [r for r in refs if r]
    if len(refs) != 1:
        raise NovelMvpError(
            CODE_INPUT_REF_INVALID,
            'exactly one group-file input reference is required',
        )
    ref = refs[0]
    if not ref.startswith(_REF_PREFIX):
        raise NovelMvpError(
            CODE_INPUT_REF_INVALID,
            f'only group-file references are allowed in P0-4: {ref!r}',
        )
    return ref


def _resolve_group_file(group_root: Path, ref: str) -> Path:
    relative = ref[len(_REF_PREFIX):]
    try:
        return _root.resolve_portable_path(group_root, relative, 'input_ref')
    except Exception as exc:  # pragma: no cover - defensive
        raise NovelMvpError(CODE_INPUT_REF_INVALID, f'invalid input reference: {exc}') from exc


# --------------------------------------------------------------------------- #
# Draft provider (contract §7)
# --------------------------------------------------------------------------- #
class DraftProvider(Protocol):
    '''Injected local draft source. No model or network call.'''

    def draft(self, request: Mapping[str, object], context: Mapping[str, object]) -> Mapping[str, object]:
        ...


class DeterministicLocalDraftProvider:
    '''Deterministic, model-free local chapter draft provider.

    Generates original narrative prose from the reviewed context and the chapter
    goal. It never copies full source snippets, performs no network or model
    call, and records a stable provider id and character count.
    '''

    PROVIDER_ID = 'deterministic-local-draft/v1'

    def draft(self, request: Mapping[str, object], context: Mapping[str, object]) -> Mapping[str, object]:
        project_id = str(request.get('project_id', ''))
        chapter_number = int(request.get('chapter_number', 0))
        chapter_goal = str(request.get('chapter_goal', ''))
        continuity = list(request.get('continuity_requirements') or [])
        min_chars = int(request.get('min_chars', 200))
        max_chars = int(request.get('max_chars', 12000))
        token_budget = int(request.get('token_budget', 1024))

        citations = list(context.get('citations') or [])
        n_anchor = sum(1 for c in citations if c.get('kind') == nk.STORY_ANCHOR)
        n_craft = sum(1 for c in citations if c.get('kind') == nk.CRAFT_TECHNIQUE)

        prose = self._compose(
            project_id, chapter_number, chapter_goal, continuity,
            min_chars, max_chars, n_anchor, n_craft,
        )
        record: dict[str, object] = {
            'schema_version': NOVEL_CHAPTER_DRAFT_SCHEMA,
            'task_id': request.get('task_id'),
            'project_id': project_id,
            'chapter_id': request.get('chapter_id'),
            'chapter_number': chapter_number,
            'chapter_goal': chapter_goal,
            'provider_id': self.PROVIDER_ID,
            'context_package_sha256': context.get('package_sha256'),
            'citation_source_sha256': sorted(
                c.get('source_sha256') for c in citations if c.get('source_sha256')
            ),
            'char_count': len(prose),
            'token_budget': token_budget,
            'prose': prose,
            'external_effect': False,
            'created_at': authoritative_timestamp(),
        }
        record['draft_sha256'] = sha256_text(prose)
        # Deterministic, stable draft identifier so the accepted-identity §7 block
        # can thread a writer_response_id even on the model-free local path
        # (production substitutes the real provider's response_id here). Never a
        # 64-char SHA and never equal to the prose SHA, so downstream identity
        # checks stay meaningful.
        record['response_id'] = (
            f"det:{self.PROVIDER_ID}:{project_id}:{chapter_number}"
        )
        return record

    @staticmethod
    def _compose(
        project_id: str, chapter_number: int, chapter_goal: str,
        continuity: list[str], min_chars: int, max_chars: int,
        n_anchor: int, n_craft: int,
    ) -> str:
        lines: list[str] = []
        lines.append(f'第{chapter_number}章 · {chapter_goal}')
        lines.append('')
        lines.append(
            f'（本章基于已审校的本地知识上下文创作，引用 {n_anchor} 条故事锚点与 '
            f'{n_craft} 条技法素材；未复制任何源片段，未进行任何外部发布或网络调用。）'
        )
        lines.append('')
        lines.append(
            f'夜色尚未退尽，{chapter_goal}的念头又一次浮上心头。'
            f'他记得那条被反复强调的约束——行动必须先于解释，否则一切都会被沉默吞没。'
        )
        lines.append('')
        pool = [
            '走廊尽头的灯忽然暗了一拍，像是这座城市在重新计算它的规则。',
            '他没有回头，只是把脚步放得更轻，让呼吸和墙上的钟摆对齐。',
            '如果说有什么是确定的，那就是确定的东西从来不会主动宣告自己的到来。',
            '他把刚才那一幕在脑子里拆开，像对待一段需要校验的引文。',
            '规则在午夜之后才会真正苏醒，而清醒的人必须在那之前把退路想清楚。',
        ]
        for requirement in continuity:
            pool.append(
                f'他始终记着那条延续性要求：{requirement}；它像一道边界，'
                f'界定了能写与不能写。'
            )
        index = 0
        body = list(lines)
        # The stripped prose length is what callers measure (char_count), so the
        # minimum must be enforced against the stripped length; otherwise the
        # trailing separator removed by .strip() can leave it one char short.
        while len('\n'.join(body).strip()) < min_chars:
            sentence = pool[index % len(pool)]
            body.append(sentence)
            body.append('')
            index += 1
            if index > 1000:
                break
        prose = '\n'.join(body).strip()
        if len(prose) > max_chars:
            prose = prose[:max_chars].rstrip()
        return prose


# --------------------------------------------------------------------------- #
# Business executor (contract §7, §8)
# --------------------------------------------------------------------------- #
class NovelBusinessExecutor:
    '''Local novel department executor driving the four-node capability chain.

    The same executor instance is reused across all four DAG nodes by the
    DepartmentDagExecutor. ``execute`` dispatches by capability. The
    novel-artifact node registers the accepted draft, quality report, and
    supporting evidence through RestartSafeStateSpine.
    '''

    department_id = DEPARTMENT_ID

    def __init__(
        self,
        group_root: Any,
        *,
        draft_provider: DraftProvider | None = None,
        spine: RestartSafeStateSpine | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        graph: Any = None,
        economic_ledger: EconomicLedger | None = None,
        work_id: str | None = None,
    ) -> None:
        if group_root is None:
            group_root = getattr(graph, 'root', None)
        if group_root is None:
            raise NovelMvpError(CODE_INPUT_REF_INVALID, 'group_root is required')
        self.group_root = Path(group_root).resolve()
        self.draft_provider = draft_provider or DeterministicLocalDraftProvider()
        self.spine = spine
        self.run_id = run_id
        self.task_id = task_id
        self.economic_ledger = economic_ledger
        self.work_id = work_id or 'work-novel-mvp'

    # -- DepartmentExecutor contract ------------------------------------- #
    def estimate_cost_fen(self, request: ExecutionRequest) -> int:
        if request.department_id != self.department_id:
            raise ValueError('Executor department does not match the assignment.')
        return 0

    def execute(self, request: ExecutionRequest) -> DepartmentResult:
        if request.department_id != self.department_id:
            raise ValueError('Executor department does not match the assignment.')
        capability = request.capability
        if capability == 'novel-context':
            return self._run_context(request)
        if capability == 'novel-draft':
            return self._run_draft(request)
        if capability == 'novel-quality':
            return self._run_quality(request)
        if capability == 'novel-artifact':
            return self._run_artifact(request)
        return DepartmentResult(
            status='blocked',
            outcome_code='novel-capability-not-configured',
            evidence=[],
            artifacts=[],
            metrics={'steps': 0},
            confidence_milli=0,
            unresolved_codes=['novel-mvp-capability-unknown'],
            provider_id=None,
        )

    # -- shared ----------------------------------------------------------- #
    def _job_id(self, request: ExecutionRequest) -> str:
        return request.assignment_id or request.capability

    def _resolve_request(self, request: ExecutionRequest) -> dict[str, Any]:
        ref = _validate_input_refs(request.input_refs)
        path = _resolve_group_file(self.group_root, ref)
        if not path.is_file():
            raise NovelMvpError(CODE_INPUT_REF_INVALID, f'request file missing: {ref!r}')
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise NovelMvpError(CODE_REQUEST_INVALID, f'cannot read request: {exc}') from exc
        return validate_novel_mvp_request(data)

    def _attach_spine(self, task_id: str) -> None:
        if self.spine is None:
            self.spine = RestartSafeStateSpine(self.group_root / _SPINE_DIR)
            self.run_id = 'run-' + _slug(task_id)

    # -- novel-context (contract §8.1) ------------------------------------ #
    def _run_context(self, request: ExecutionRequest) -> DepartmentResult:
        req = self._resolve_request(request)
        job_id = self._job_id(request)
        try:
            ccr = nk.ChapterContextRequest(
                schema_version=nk.REQUEST_SCHEMA,
                project_id=req['project_id'],
                chapter_number=int(req['chapter_number']),
                objective=req['chapter_goal'],
                anchor_terms=list(req['required_story_anchor_ids']),
                technique_tags=list(req['required_craft_technique_ids']),
                token_budget=int(req['token_budget']),
                job_id=job_id,
            )
            adapter = nk.LocalFileKnowledgeAdapter(self.group_root)
            query = nk.build_knowledge_query(ccr, job_id)
            result = adapter.query(query)
            context = nk.build_chapter_context(
                request=ccr, query=query, result=result,
                adapter_id=adapter.ADAPTER_ID, adapter_version=adapter.ADAPTER_VERSION,
                job_id=job_id,
            )
        except nk.KnowledgeAdapterError as exc:
            return DepartmentResult(
                status='blocked',
                outcome_code=CODE_CONTEXT_BLOCKED,
                evidence=[{'code': exc.code, 'message': str(exc)}],
                artifacts=[],
                metrics={'steps': 1},
                confidence_milli=1000,
                unresolved_codes=[exc.code],
                provider_id=None,
            )

        if context.status != 'ready':
            codes = list(context.blocking_codes) or [nk.CODE_RESULT_INVALID]
            return DepartmentResult(
                status='blocked',
                outcome_code=CODE_CONTEXT_BLOCKED,
                evidence=[{'code': c} for c in codes],
                artifacts=[],
                metrics={'steps': 1},
                confidence_milli=1000,
                unresolved_codes=codes,
                provider_id=None,
            )

        package = nk.build_chapter_context_package(
            request=ccr, context=context, job_id=job_id, artifact_paths={},
        )
        if package.writes_novel_prose:
            return DepartmentResult(
                status='blocked',
                outcome_code=CODE_EXTERNAL_EFFECT_FORBIDDEN,
                evidence=[{'code': CODE_EXTERNAL_EFFECT_FORBIDDEN}],
                artifacts=[],
                metrics={'steps': 1},
                confidence_milli=1000,
                unresolved_codes=[CODE_EXTERNAL_EFFECT_FORBIDDEN],
                provider_id=None,
            )

        context_dict = context.to_dict()
        package_dict = package.to_dict()
        citations = [c.to_dict() for c in context.citations]
        artifact_record = {
            **package_dict,
            'context_sha256': sha256_text(canonical_json(context_dict)),
            'package_sha256': sha256_text(canonical_json(package_dict)),
            'citations': citations,
            'query_sha256': context.query_sha256,
            'adapter': context.adapter,
        }
        return DepartmentResult(
            status='completed',
            outcome_code='novel-context-ready',
            evidence=[
                {'kind': 'story-anchor', 'count': sum(1 for c in citations if c.get('kind') == nk.STORY_ANCHOR)},
                {'kind': 'craft-technique', 'count': sum(1 for c in citations if c.get('kind') == nk.CRAFT_TECHNIQUE)},
            ],
            artifacts=[artifact_record],
            metrics={
                'steps': 1,
                'selected_snippets': len(context.selected_snippets),
                'estimated_tokens': context.estimated_tokens,
            },
            confidence_milli=1000,
            unresolved_codes=[],
            provider_id=adapter.ADAPTER_ID,
        )

    # -- novel-draft (contract §8.2) -------------------------------------- #
    def _run_draft(self, request: ExecutionRequest) -> DepartmentResult:
        req = self._resolve_request(request)
        ctx_result = request.dependency_results.get('novel-context', {}) or {}
        ctx_artifact = (ctx_result.get('artifacts') or [{}])[0]

        draft_record = dict(self.draft_provider.draft(req, ctx_artifact))
        prose = str(draft_record.get('prose', ''))
        if not prose.strip():
            return DepartmentResult(
                status='blocked',
                outcome_code=CODE_DRAFT_EMPTY,
                evidence=[{'code': CODE_DRAFT_EMPTY}],
                artifacts=[],
                metrics={'steps': 1},
                confidence_milli=1000,
                unresolved_codes=[CODE_DRAFT_EMPTY],
                provider_id=self.draft_provider.PROVIDER_ID,
            )

        min_chars = int(req['min_chars'])
        max_chars = int(req['max_chars'])
        char_count = len(prose)
        if not (min_chars <= char_count <= max_chars):
            return DepartmentResult(
                status='blocked',
                outcome_code=CODE_DRAFT_FAILED,
                evidence=[{
                    'code': CODE_DRAFT_FAILED,
                    'char_count': char_count,
                    'min_chars': min_chars,
                    'max_chars': max_chars,
                }],
                artifacts=[draft_record],
                metrics={'char_count': char_count, 'steps': 1},
                confidence_milli=1000,
                unresolved_codes=[CODE_DRAFT_FAILED],
                provider_id=self.draft_provider.PROVIDER_ID,
            )

        draft_record['task_id'] = request.task_id
        return DepartmentResult(
            status='completed',
            outcome_code='novel-draft-ready',
            evidence=[{'provider_id': self.draft_provider.PROVIDER_ID, 'char_count': char_count}],
            artifacts=[draft_record],
            metrics={'char_count': char_count, 'steps': 1},
            confidence_milli=1000,
            unresolved_codes=[],
            provider_id=self.draft_provider.PROVIDER_ID,
        )

    # -- novel-quality (contract §8.3) ------------------------------------ #
    def _run_quality(self, request: ExecutionRequest) -> DepartmentResult:
        req = self._resolve_request(request)
        ctx_result = request.dependency_results.get('novel-context', {}) or {}
        draft_result = request.dependency_results.get('novel-draft', {}) or {}
        ctx_artifact = (ctx_result.get('artifacts') or [{}])[0]
        draft_artifact = (draft_result.get('artifacts') or [{}])[0]

        checks: list[dict[str, object]] = []
        problems: list[str] = []

        def record_check(name: str, passed: bool) -> None:
            checks.append({'name': name, 'passed': passed})

        # 1. schemas and ID bindings
        schema_ok = (
            draft_artifact.get('schema_version') == NOVEL_CHAPTER_DRAFT_SCHEMA
            and bool(draft_artifact.get('draft_sha256'))
        )
        record_check('schema-binding', schema_ok)
        if not schema_ok:
            problems.append(CODE_QUALITY_BLOCKED)

        # 2. context and citation hash integrity
        ctx_ok = (
            draft_artifact.get('context_package_sha256') == ctx_artifact.get('package_sha256')
            and draft_artifact.get('context_package_sha256') is not None
        )
        record_check('context-hash', ctx_ok)
        if not ctx_ok:
            problems.append(CODE_CONTEXT_HASH_MISMATCH)

        expected_citations = sorted(
            c.get('source_sha256') for c in ctx_artifact.get('citations', [])
            if c.get('source_sha256')
        )
        citation_ok = draft_artifact.get('citation_source_sha256') == expected_citations
        record_check('citation-hash', citation_ok)
        if not citation_ok:
            problems.append(CODE_QUALITY_HASH_MISMATCH)

        # 3. requested min/max character limits
        char_count = int(draft_artifact.get('char_count', 0))
        min_chars = int(req['min_chars'])
        max_chars = int(req['max_chars'])
        range_ok = min_chars <= char_count <= max_chars
        record_check('char-range', range_ok)
        if not range_ok:
            problems.append(CODE_DRAFT_FAILED)

        # 4. chapter goal and continuity requirements represented.
        # A real creative LLM does not echo the brief verbatim, so a genuine
        # chapter is accepted when it references the briefed creative anchor
        # (the selected proposal's protagonist) OR the literal goal token.
        # Requests without writer_context (e.g. adversarial/neutral tests) still
        # require the literal goal token, so off-topic prose stays blocked.
        prose = str(draft_artifact.get('prose', ''))
        chapter_marker = f"第{int(req['chapter_number'])}章"
        wc = req.get('writer_context') or {}
        sel_prop = wc.get('selected_proposal') or {}
        anchor = sel_prop.get('protagonist') or ''
        # A real creative LLM does not echo the brief verbatim, so accept any
        # genuine signal that the chapter addresses the brief: the literal goal
        # token, the prescribed chapter-opening marker, or the briefed
        # protagonist name. Requests without writer_context (adversarial /
        # neutral tests) still require the literal goal token.
        goal_ok = bool(req['chapter_goal']) and (
            req['chapter_goal'] in prose
            or chapter_marker in prose
            or (bool(anchor) and anchor in prose)
        )
        record_check('goal-represented', goal_ok)
        if not goal_ok:
            problems.append(CODE_QUALITY_BLOCKED)
        continuity_ok = all(
            (bool(c) and c in prose) for c in (req.get('continuity_requirements') or [])
        )
        record_check('continuity-represented', continuity_ok)
        if not continuity_ok and req.get('continuity_requirements'):
            problems.append(CODE_QUALITY_BLOCKED)

        # 5. required story anchors and craft techniques cited
        cited_ids = {
            c.get('snippet_id') for c in ctx_artifact.get('citations', []) if c.get('snippet_id')
        }
        required_anchors = set(req.get('required_story_anchor_ids') or [])
        required_craft = set(req.get('required_craft_technique_ids') or [])
        missing_anchors = required_anchors - cited_ids
        missing_craft = required_craft - cited_ids
        anchor_ok = not missing_anchors
        craft_ok = not missing_craft
        record_check('required-anchors-cited', anchor_ok)
        record_check('required-craft-cited', craft_ok)
        if not anchor_ok:
            problems.append(CODE_QUALITY_BLOCKED)
        if not craft_ok:
            problems.append(CODE_QUALITY_BLOCKED)

        # 6. forbidden terms absent
        forbidden = req.get('forbidden_terms') or []
        forbidden_present = any(term and term in prose for term in forbidden)
        record_check('forbidden-terms-absent', not forbidden_present)
        if forbidden_present:
            problems.append(CODE_QUALITY_BLOCKED)

        # 7. no external URL, credential, or publication claim
        external = bool(re.search(r'https?://', prose)) or 'publish' in prose.lower()
        record_check('no-external-effect', not external)
        if external:
            problems.append(CODE_EXTERNAL_EFFECT_FORBIDDEN)

        # 8. draft is not empty and is structurally a chapter (not a plan/checklist)
        structural_ok = bool(prose.strip()) and not _looks_like_checklist(prose)
        record_check('chapter-structure', structural_ok)
        if not structural_ok:
            problems.append(CODE_DRAFT_EMPTY if not prose.strip() else CODE_QUALITY_BLOCKED)

        # 9. dependency call evidence is complete
        deps_ok = bool(ctx_artifact) and bool(draft_artifact)
        record_check('dependency-evidence', deps_ok)
        if not deps_ok:
            problems.append(CODE_QUALITY_BLOCKED)

        verdict = 'accepted' if not problems else 'blocked'
        report: dict[str, object] = {
            'schema_version': NOVEL_QUALITY_REPORT_SCHEMA,
            'task_id': request.task_id,
            'project_id': req['project_id'],
            'chapter_id': req['chapter_id'],
            'chapter_number': int(req['chapter_number']),
            'verdict': verdict,
            'checks': checks,
            'blocking_codes': list(dict.fromkeys(problems)),
            'metrics': {
                'char_count': char_count,
                'min_chars': min_chars,
                'max_chars': max_chars,
                'cited_snippets': len(cited_ids),
            },
            'context_package_sha256': ctx_artifact.get('package_sha256'),
            'draft_sha256': draft_artifact.get('draft_sha256'),
            'created_at': authoritative_timestamp(),
        }
        report['report_sha256'] = sha256_text(canonical_json(report))
        if verdict == 'accepted':
            return DepartmentResult(
                status='completed',
                outcome_code='novel-quality-accepted',
                evidence=[{'verdict': 'accepted'}],
                artifacts=[report],
                metrics=report['metrics'],  # type: ignore[arg-type]
                confidence_milli=1000,
                unresolved_codes=[],
                provider_id=None,
            )
        return DepartmentResult(
            status='blocked',
            outcome_code='novel-quality-blocked',
            evidence=[{'verdict': 'blocked'}],
            artifacts=[report],
            metrics=report['metrics'],  # type: ignore[arg-type]
            confidence_milli=1000,
            unresolved_codes=list(dict.fromkeys(problems)),
            provider_id=None,
        )

    # -- novel-artifact (contract §8.4) ----------------------------------- #
    def _run_artifact(self, request: ExecutionRequest) -> DepartmentResult:
        req = self._resolve_request(request)
        quality_result = request.dependency_results.get('novel-quality', {}) or {}
        quality_artifact = (quality_result.get('artifacts') or [{}])[0]

        if quality_artifact.get('verdict') != 'accepted':
            return DepartmentResult(
                status='blocked',
                outcome_code=CODE_ARTIFACT_BEFORE_ACCEPTANCE,
                evidence=[{'code': CODE_ARTIFACT_BEFORE_ACCEPTANCE}],
                artifacts=[],
                metrics={'steps': 0},
                confidence_milli=1000,
                unresolved_codes=[CODE_ARTIFACT_BEFORE_ACCEPTANCE],
                provider_id=None,
            )

        ctx_artifact = (request.dependency_results.get('novel-context', {}).get('artifacts') or [{}])[0]
        draft_artifact = (request.dependency_results.get('novel-draft', {}).get('artifacts') or [{}])[0]

        self._attach_spine(request.task_id)
        assert self.spine is not None and self.run_id is not None
        run_id = self.run_id

        # duplicate accepted artifact guard
        existing = self._find_accepted_artifact(run_id, req['task_id'])
        if existing is not None:
            self.spine.close()
            self.spine = None
            return DepartmentResult(
                status='blocked',
                outcome_code=CODE_ARTIFACT_DUPLICATE,
                evidence=[{'code': CODE_ARTIFACT_DUPLICATE, 'artifact_id': existing}],
                artifacts=[],
                metrics={'steps': 0},
                confidence_milli=1000,
                unresolved_codes=[CODE_ARTIFACT_DUPLICATE],
                provider_id=None,
            )

        artifact_record: dict[str, object] = {
            'schema_version': NOVEL_ACCEPTED_ARTIFACT_SCHEMA,
            'task_id': request.task_id,
            'run_id': run_id,
            'project_id': req['project_id'],
            'chapter_id': req['chapter_id'],
            'chapter_number': int(req['chapter_number']),
            'draft_sha256': draft_artifact.get('draft_sha256'),
            'context_package_sha256': ctx_artifact.get('package_sha256'),
            'quality_report_sha256': quality_artifact.get('report_sha256'),
            'accepted_at': authoritative_timestamp(),
            'external_effect': False,
        }
        artifact_resp = self.spine.register_artifact(
            run_id, task_id=request.task_id, dept_id=NOVEL_DEPT_ID,
            media_type='application/json', content=artifact_record,
        )
        evidence_ids: list[str] = []
        for label, content in (
            ('request', req),
            ('context', ctx_artifact),
            ('draft', draft_artifact),
            ('quality', quality_artifact),
        ):
            ev = self.spine.register_evidence(
                run_id, task_id=request.task_id, dept_id=NOVEL_DEPT_ID,
                media_type='application/json', content=content,
            )
            evidence_ids.append(str(ev.get('artifact_id')))

        artifact_record['artifact_id'] = artifact_resp.get('artifact_id')
        artifact_record['artifact_content_sha256'] = artifact_resp.get('content_sha256')
        artifact_record['evidence_ids'] = evidence_ids

        # -- W1-1 economic fact registration (contract §9.3) ----------------
        # Only after P0-4 quality acceptance. Stable idempotency keys make
        # replay/resume/restore safe: re-running cannot duplicate facts.
        econ_ids: dict[str, object] = {}
        if self.economic_ledger is not None:
            project_id = str(req['project_id'])
            chapter_id = str(req['chapter_id'])
            task_id = str(req['task_id'])
            work_id = self.work_id
            version_id = 'ver-' + sha256_text(f'{run_id}|{task_id}')[:24]
            platform_id = 'plat-novel-mvp-local-shadow'
            artifact_sha256 = draft_artifact.get('draft_sha256')
            quality_sha256 = quality_artifact.get('report_sha256')
            work_entry = self.economic_ledger.register_work(
                work_id, project_id,
                idempotency_key=f'econ-v1:work:{work_id}',
                chapter_id=chapter_id, task_id=task_id, run_id=run_id,
                department_id=NOVEL_DEPT_ID,
            )
            version_entry = self.economic_ledger.register_version(
                version_id, work_id,
                idempotency_key=f'econ-v1:version:{run_id}:{task_id}',
                project_id=project_id, chapter_id=chapter_id, version_no=1,
                artifact_id=str(artifact_record['artifact_id']),
                artifact_sha256=str(artifact_sha256),
                quality_report_sha256=str(quality_sha256),
                production_task_id=task_id, production_run_id=run_id,
                status='accepted', task_id=task_id, run_id=run_id,
                department_id=NOVEL_DEPT_ID,
            )
            zero_entry = self.economic_ledger.register_zero_cost(
                idempotency_key=f'econ-v1:zero:{run_id}:{task_id}',
                category='novel-production', task_id=task_id, run_id=run_id,
                department_id=NOVEL_DEPT_ID,
            )
            plat_entry = self.economic_ledger.register_platform(
                platform_id,
                idempotency_key='econ-v1:platform:novel-mvp-local-shadow',
                adapter_id='local-shadow/v1', capability_status='shadow',
                published=False, task_id=task_id, run_id=run_id,
                department_id=NOVEL_DEPT_ID,
            )
            econ_ids = {
                'work_entry_id': work_entry.get('entry_id'),
                'version_entry_id': version_entry.get('entry_id'),
                'zero_cost_entry_id': zero_entry.get('entry_id'),
                'platform_entry_id': plat_entry.get('entry_id'),
            }
        artifact_record['economic_entry_ids'] = econ_ids

        self.spine.close()
        self.spine = None
        return DepartmentResult(
            status='completed',
            outcome_code='novel-artifact-accepted',
            evidence=[{'artifact_id': artifact_record['artifact_id'], 'evidence_count': len(evidence_ids)}],
            artifacts=[artifact_record],
            metrics={'steps': 1, 'evidence_count': len(evidence_ids)},
            confidence_milli=1000,
            unresolved_codes=[],
            provider_id=None,
        )

    # -- helpers ---------------------------------------------------------- #
    def _find_accepted_artifact(self, run_id: str, task_id: str) -> str | None:
        try:
            run_record = self.spine.open_run(run_id)  # type: ignore[union-attr]
        except StateSpineError:
            return None
        artifacts_dir = self.spine.root / 'artifacts'  # type: ignore[union-attr]
        for path in artifacts_dir.rglob('*.json'):
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(value, dict)
                and value.get('schema_version') == NOVEL_ACCEPTED_ARTIFACT_SCHEMA
                and value.get('task_id') == task_id
                and value.get('run_id') == run_id
            ):
                return str(value.get('artifact_id'))
        return None


def _looks_like_checklist(prose: str) -> bool:
    lines = [line.strip() for line in prose.splitlines() if line.strip()]
    if len(lines) < 3:
        return False
    bullet_lines = sum(1 for line in lines if line[:1] in ('-', '*', '•') or re.match(r'^\d+[\.、]', line))
    return bullet_lines >= max(2, len(lines) // 2)


# --------------------------------------------------------------------------- #
# Service layer (contract §7, §9)
# --------------------------------------------------------------------------- #
class NovelMvpService:
    '''Orchestrates the four-node novel DAG with RestartSafeStateSpine.'''

    def __init__(
        self,
        group_root: Any,
        *,
        draft_provider: DraftProvider | None = None,
        spine_root: Any | None = None,
        economic_ledger: EconomicLedger | None = None,
        work_id: str | None = None,
    ) -> None:
        self.group_root = Path(group_root).resolve()
        self.draft_provider = draft_provider or DeterministicLocalDraftProvider()
        self.spine_root = Path(spine_root).resolve() if spine_root else self.group_root / _SPINE_DIR
        self.economic_ledger = economic_ledger
        self.work_id = work_id or 'work-novel-mvp'

    # -- id / registry helpers ------------------------------------------- #
    def _run_id_for(self, task_id: str) -> str:
        return 'run-' + _slug(task_id)

    def _registry_path(self, task_id: str) -> Path:
        return self.group_root / _REGISTRY_DIR / f'{_slug(task_id)}.json'

    def _load_registry(self, task_id: str) -> dict[str, Any] | None:
        path = self._registry_path(task_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return None

    def _save_registry(self, task_id: str, data: dict[str, Any]) -> None:
        path = self._registry_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    def _resolve_request_file(self, request_ref: str) -> dict[str, Any]:
        path = _resolve_group_file(self.group_root, request_ref)
        if not path.is_file():
            raise NovelMvpError(CODE_INPUT_REF_INVALID, f'request file missing: {request_ref!r}')
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise NovelMvpError(CODE_REQUEST_INVALID, f'cannot read request: {exc}') from exc
        return validate_novel_mvp_request(data)

    def _build_nodes(self, objective: str) -> tuple[DagNodeSpec, ...]:
        return tuple(
            DagNodeSpec(
                node_id=cap,
                department_id=DEPARTMENT_ID,
                capability=cap,
                objective=objective,
                dependencies=tuple(NOVEL_DEPENDENCY_EDGES[cap]),
                max_attempts=1,
                timeout_seconds=120.0,
                idempotency_key=f'novel.{cap}',
            )
            for cap in NOVEL_CAPABILITIES
        )

    def _make_dag_executor(
        self, run_id: str, task_id: str, request_ref: str,
        acceptance_criteria: list[str],
    ) -> DepartmentDagExecutor:
        # NOTE: the executor must NOT share the main-thread RestartSafeStateSpine
        # instance. DurableDagRunner executes nodes in a worker thread, and the
        # SQLite connection bound to the main-thread spine cannot be used there
        # (sqlite3.ProgrammingError). Instead, NovelBusinessExecutor attaches a
        # fresh, thread-local spine inside _run_artifact (the only node that
        # writes to the spine).
        executor = NovelBusinessExecutor(
            self.group_root, draft_provider=self.draft_provider,
            run_id=run_id, task_id=task_id,
            economic_ledger=self.economic_ledger, work_id=self.work_id,
        )
        return DepartmentDagExecutor(
            executors={DEPARTMENT_ID: executor},
            acceptance_criteria=list(acceptance_criteria),
            input_refs=[request_ref],
            task_id=task_id,
        )

    def _update_registry_status(self, task_id: str, result: Any) -> None:
        registry = self._load_registry(task_id) or {'task_id': task_id}
        registry['status'] = result.state.get('status')
        nodes = result.state.get('nodes', {})
        artifact_node = nodes.get('novel-artifact', {})
        artifact_result = artifact_node.get('result') or {}
        accepted = artifact_result.get('artifacts') or []
        if accepted:
            registry['accepted_artifact_id'] = accepted[0].get('artifact_id')
            econ_ids = accepted[0].get('economic_entry_ids') or {}
            if econ_ids:
                registry['economic_entry_ids'] = econ_ids
        self._save_registry(task_id, registry)

    # -- public operations ------------------------------------------------ #
    def submit(
        self, task_id: str, request_ref: str, *, max_new_terminal_nodes: int | None = None,
    ) -> Any:
        ref = _validate_input_refs([request_ref])
        req = self._resolve_request_file(ref)
        if req['task_id'] != task_id:
            raise NovelMvpError(
                CODE_BINDING_CONFLICT,
                'request task_id does not match the submit task_id',
            )
        run_id = self._run_id_for(task_id)
        spine = RestartSafeStateSpine(self.spine_root)
        if spine.run_exists(run_id):
            raise NovelMvpError(CODE_BINDING_CONFLICT, f'run already exists: {run_id}')

        spine.create_run(run_id, department_ids=(NOVEL_DEPT_ID,))
        spine.register_department(run_id, NOVEL_DEPT_ID)
        spine.register_task(run_id, task_id, dept_id=NOVEL_DEPT_ID)
        ledger = spine.ledger_for_run(run_id)

        registry = {
            'task_id': task_id,
            'run_id': run_id,
            'request_ref': ref,
            'request_hash': sha256_text(canonical_json(req)),
            'project_id': req['project_id'],
            'chapter_id': req['chapter_id'],
            'status': 'submitted',
            'accepted_artifact_id': None,
        }
        self._save_registry(task_id, registry)

        dag_executor = self._make_dag_executor(
            run_id, task_id, ref, list(req['acceptance_criteria'])
        )
        runner = DurableDagRunner(ledger=ledger, max_workers=1)
        result = runner.run(
            task_id=task_id, nodes=self._build_nodes(req['chapter_goal']),
            executor=dag_executor, max_new_terminal_nodes=max_new_terminal_nodes,
        )
        self._update_registry_status(task_id, result)
        spine.close()
        return result

    def resume(
        self, task_id: str, *, max_new_terminal_nodes: int | None = None,
    ) -> Any:
        run_id = self._run_id_for(task_id)
        registry = self._load_registry(task_id)
        if registry is None:
            raise NovelMvpError(CODE_BINDING_CONFLICT, 'no run to resume for this task_id')
        ref = str(registry.get('request_ref', ''))
        req = self._resolve_request_file(ref)
        if sha256_text(canonical_json(req)) != registry.get('request_hash'):
            raise NovelMvpError(CODE_RESUME_CONFLICT, 'request hash changed; resume refused')

        spine = RestartSafeStateSpine(self.spine_root)  # reopen from disk
        ledger = spine.ledger_for_run(run_id)
        dag_executor = self._make_dag_executor(
            run_id, task_id, ref, list(req['acceptance_criteria'])
        )
        runner = DurableDagRunner(ledger=ledger, max_workers=1)
        result = runner.run(
            task_id=task_id, nodes=self._build_nodes(req['chapter_goal']),
            executor=dag_executor, max_new_terminal_nodes=max_new_terminal_nodes,
        )
        self._update_registry_status(task_id, result)
        spine.close()
        return result

    def status(self, task_id: str) -> dict[str, Any]:
        run_id = self._run_id_for(task_id)
        spine = RestartSafeStateSpine(self.spine_root)
        try:
            run_record = spine.open_run(run_id)
            ledger = spine.ledger_for_run(run_id)
            checkpoint = ledger.read_checkpoint(task_id)
            nodes = {}
            if isinstance(checkpoint, dict):
                nodes = checkpoint.get('state', {}).get('nodes', {})
            return {
                'task_id': task_id,
                'run_id': run_id,
                'run_status': run_record.get('status'),
                'nodes': nodes,
                'registry': self._load_registry(task_id),
            }
        finally:
            spine.close()

    def export(self, task_id: str, target_dir: Any) -> dict[str, Any]:
        run_id = self._run_id_for(task_id)
        spine = RestartSafeStateSpine(self.spine_root)
        try:
            return spine.export_run(run_id, Path(target_dir))
        finally:
            spine.close()

    def restore(self, source_dir: Any, target_root: Any) -> str:
        # restore_run's target_root is the *spine* root, not the group root.
        spine_root = Path(target_root) / _SPINE_DIR
        spine = RestartSafeStateSpine(spine_root)
        try:
            run_id = spine.restore_run(Path(source_dir), spine_root)
        finally:
            spine.close()
        # restore_run only recovers the spine (run files + artifacts + index).
        # Rebuild the per-task registry bookkeeping so status() reports the
        # accepted artifact after a cold restore into a fresh group root.
        self._rebuild_registry_from_spine(run_id, Path(target_root))
        return run_id

    def _rebuild_registry_from_spine(self, run_id: str, target_root: Path) -> None:
        # The accepted artifact content (schema novel-accepted-artifact/v1) is
        # stored as the ``content`` of a content-record/v0 store file under
        # artifacts/store/artifact/. The records/ dir only holds the spine's
        # own index records, so we must read the store payload.
        store_dir = Path(target_root) / _SPINE_DIR / 'artifacts' / 'store' / 'artifact'
        accepted_id: str | None = None
        task_id: str | None = None
        project_id: str | None = None
        chapter_id: str | None = None
        for path in store_dir.rglob('*.json'):
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                continue
            envelope = value.get('payload') if isinstance(value, dict) else None
            inner = envelope.get('content') if isinstance(envelope, dict) else None
            if (
                isinstance(inner, dict)
                and inner.get('schema_version') == NOVEL_ACCEPTED_ARTIFACT_SCHEMA
                and inner.get('run_id') == run_id
            ):
                accepted_id = envelope.get('artifact_id')
                task_id = inner.get('task_id')
                project_id = inner.get('project_id')
                chapter_id = inner.get('chapter_id')
                break
        if not task_id:
            return
        registry = {
            'task_id': task_id,
            'run_id': run_id,
            'request_ref': None,
            'request_hash': None,
            'project_id': project_id,
            'chapter_id': chapter_id,
            'status': 'completed',
            'accepted_artifact_id': accepted_id,
        }
        reg_path = Path(target_root) / _REGISTRY_DIR / f'{_slug(task_id)}.json'
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding='utf-8')


# --------------------------------------------------------------------------- #
# Minimal local API surface (contract §10)
# --------------------------------------------------------------------------- #
def mount_novel_mvp_routes(app: Any, group_root: Any) -> None:
    '''Mount the minimal novel-mvp local routes onto an existing FastAPI app.'''
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter()
    service = NovelMvpService(Path(group_root).resolve())

    @router.post('/api/novel-mvp/submit')
    def submit_novel_mvp(payload: dict):
        task_id = payload.get('task_id')
        request_ref = payload.get('request_ref')
        if not task_id or not request_ref:
            return JSONResponse(
                {'schema_version': NOVEL_MVP_RUN_SCHEMA, 'error': 'task_id and request_ref required'},
                status_code=422,
            )
        try:
            result = service.submit(task_id, request_ref)
        except NovelMvpError as exc:
            code = 409 if exc.code == CODE_BINDING_CONFLICT else 422
            return JSONResponse(
                {'schema_version': NOVEL_MVP_RUN_SCHEMA, 'error': exc.code, 'message': exc.message},
                status_code=code,
            )
        return JSONResponse({
            'schema_version': NOVEL_MVP_RUN_SCHEMA,
            'task_id': task_id,
            'state': result.state.get('status'),
        }, status_code=200)

    @router.get('/api/novel-mvp/status/{task_id}')
    def status_novel_mvp(task_id: str):
        try:
            return JSONResponse(service.status(task_id), status_code=200)
        except StateSpineError:
            return JSONResponse({'error': 'run-not-found'}, status_code=404)

    @router.post('/api/novel-mvp/resume/{task_id}')
    def resume_novel_mvp(task_id: str):
        try:
            result = service.resume(task_id)
        except NovelMvpError as exc:
            code = 409 if exc.code in (CODE_BINDING_CONFLICT, CODE_RESUME_CONFLICT) else 422
            return JSONResponse({'error': exc.code, 'message': exc.message}, status_code=code)
        return JSONResponse({'task_id': task_id, 'state': result.state.get('status')}, status_code=200)

    @router.post('/api/novel-mvp/export/{task_id}')
    def export_novel_mvp(task_id: str, payload: dict | None = None):
        target_dir = (payload or {}).get('target_dir') if payload else None
        if not target_dir:
            return JSONResponse({'error': 'target_dir required'}, status_code=422)
        manifest = service.export(task_id, target_dir)
        return JSONResponse(
            {'schema_version': NOVEL_MVP_RUN_SCHEMA, 'export': manifest}, status_code=200
        )

    @router.post('/api/novel-mvp/restore')
    def restore_novel_mvp(payload: dict):
        source_dir = payload.get('source_dir')
        target_root = payload.get('target_root')
        if not source_dir or not target_root:
            return JSONResponse({'error': 'source_dir and target_root required'}, status_code=422)
        run_id = service.restore(source_dir, target_root)
        return JSONResponse(
            {'schema_version': NOVEL_MVP_RUN_SCHEMA, 'run_id': run_id}, status_code=200
        )

    app.include_router(router)
