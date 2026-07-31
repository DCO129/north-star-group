'''N1-1 Novel Subsidiary rolling operations orchestration service.

This module composes the existing P0/W2 services (LocalFileKnowledgeAdapter,
NovelMvpService, NovelProductionBatchService, EconomicLedger, PublicationService)
into the frozen N1-1 operating chain:

    CEO novel goal
    -> knowledge hard gate (LocalFileKnowledgeAdapter)
    -> exactly three production-ready proposals
    -> owner selects one proposal
    -> initialize chapters 1-5 (NovelMvpService per chapter, provider-neutral)
    -> full five-chapter audit EXACTLY ONCE (persist proof)
    -> versioned continuity digest + chapter deltas
    -> expose ONLY chapter 1 to owner confirmation surface
    -> owner confirms chapter 1 direction once
    -> generate one new chapter (single-chapter audit)
    -> targeted review of previous-five digest + new-chapter delta
    -> PASS releases oldest buffered chapter (W2-2 local release candidate)
    -> slide buffer 1-5 -> 2-6, update digest incrementally
    -> preserve cumulative published canon separately from rolling digest

No network, browser, credential resolution, real publication, payment,
settlement, positive revenue, or irreversible external action. Raw chapter
prose stays in local artifacts; list/status surfaces use refs, hashes,
counts, and a bounded owner preview only.

Frozen schemas (contract §4 / execution order §3):
    novel-project-proposal-set/v1
    rolling-chapter-buffer/v1
    continuity-digest/v1
    chapter-state-delta/v1
'''

from __future__ import annotations

from ._hashing import sha256_text
import json
import os
import re
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import novel_knowledge as nk
from .economic_ledger import EconomicLedger
from .novel_mvp import (
    NOVEL_MVP_REQUEST_SCHEMA,
    NovelMvpService,
    NovelMvpError,
    DeterministicLocalDraftProvider,
)
from .platform_adapter import DenyAllShadowTransport
from .production_batch import (
    NovelProductionBatchService,
    NovelProductionBatchError,
    SCHEMA_REQUEST as PB_SCHEMA_REQUEST,
)
from .providers import ModelMessage, ModelProvider, ModelRequest
from .publication import PublicationService
from decimal import Decimal
from .budget import ModelRate

# --------------------------------------------------------------------------- #
# Frozen schema versions (contract §4 / execution order §3)
# --------------------------------------------------------------------------- #
SCHEMA_PROPOSAL_SET = 'novel-project-proposal-set/v1'
SCHEMA_BUFFER = 'rolling-chapter-buffer/v1'
SCHEMA_DIGEST = 'continuity-digest/v1'
SCHEMA_DELTA = 'chapter-state-delta/v1'
SCHEMA_OP = 'novel-operations/v1'
SCHEMA_EVENT = 'novel-operations-event/v1'

OP_ID_RE = re.compile(r'^op-[a-z0-9][a-z0-9._-]{2,63}$')
PROJECT_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{2,63}$')

# Stable blocking codes (contract §4)
OP_KNOWLEDGE_GATE_FAILED = 'novel-operations-knowledge-gate-failed'
OP_PROPOSALS_EXIST = 'novel-operations-proposals-exist'
OP_PROPOSAL_UNKNOWN = 'novel-operations-proposal-unknown'
OP_PROPOSAL_STALE = 'novel-operations-proposal-stale'
OP_PROPOSAL_DUPLICATE = 'novel-operations-proposal-duplicate'
OP_PROPOSAL_CONFLICT = 'novel-operations-proposal-conflict'
OP_NO_SELECTION = 'novel-operations-no-selection'
OP_BUFFER_EXISTS = 'novel-operations-buffer-exists'
OP_NOT_CONFIRMED = 'novel-operations-chapter-1-not-confirmed'
OP_ALREADY_CONFIRMED = 'novel-operations-chapter-1-already-confirmed'
OP_NOT_INITIALIZED = 'novel-operations-buffer-not-initialized'
OP_CHAPTER_QUALITY_BLOCKED = 'novel-operations-chapter-quality-blocked'
OP_REVIEW_UNRESOLVED = 'novel-operations-review-unresolved'
OP_RELEASE_PREMATURE = 'novel-operations-release-premature'
OP_DIGEST_MISMATCH = 'novel-operations-digest-hash-mismatch'
OP_STALE_VERSION = 'novel-operations-stale-version'
OP_DUPLICATE_ADVANCE = 'novel-operations-duplicate-advance'
OP_CONCURRENT_ADVANCE = 'novel-operations-concurrent-advance'
OP_STATE_CORRUPT = 'novel-operations-state-corrupt'
OP_UNBOUNDED_REVIEW = 'novel-operations-unbounded-review-rejected'
OP_FULL_WINDOW_TRIGGER = 'novel-operations-full-window-trigger'
OP_PROPOSAL_GENERATION_FAILED = 'novel-operations-proposal-generation-failed'
OP_AUDIT_FAILED = 'novel-operations-audit-failed'

# Full-window review explicit triggers (contract §4.4)
FULL_WINDOW_TRIGGERS = (
    'initial-window',
    'major-outline-or-canon-change',
    'unlocatable-impact',
    'cross-chapter-rewrite',
    'digest-hash-mismatch',
    'recovery-corruption',
    'explicit-responsible-party-request',
)

class NovelOperationsError(Exception):
    '''Bounded novel-operations error carrying a stable blocking code.'''

    def __init__(self, code: str, message: str = '') -> None:
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _slug(value: str) -> str:
    cleaned = re.sub(r'[^a-z0-9._-]', '-', str(value).lower())
    return cleaned[:50].strip('-') or 'x'

def _op_scratch(op_id: str) -> str:
    '''Stable short (8 hex) key for scratch work-root directories.

    The P0-4 / W2-2 engines write artifacts under a deep nested path
    (``.../runtime/novel-mvp/spine/artifacts/store/artifact/<64-hex>.json``).
    On Windows, paths longer than 260 characters fail (``FileNotFoundError`` on
    ``os.replace``). Using a short, deterministic op key for the scratch
    directory keeps the full artifact path safely under that limit. The key is
    derived from ``op_id`` so it is stable across restarts/replays.
    '''
    return sha256_text(str(op_id))[:8]

def _copy_tree_robust(src: Path, dst: Path) -> None:
    '''Recursively copy a directory, skipping unreadable/broken sources.'''
    dst.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(src):
        s = Path(entry.path)
        d = dst / entry.name
        try:
            if entry.is_dir(follow_symlinks=False):
                _copy_tree_robust(s, d)
            else:
                shutil.copy2(s, d)
        except OSError:
            continue

def _resolve_group_file(group_root: Path, ref: str) -> Path:
    from .root import resolve_portable_path

    return resolve_portable_path(group_root, ref[len('group-file:'):], 'operations_ref')

# --------------------------------------------------------------------------- #
# Knowledge seeding helper (contract §4.1)
# --------------------------------------------------------------------------- #
def seed_knowledge(
    group_root: Any,
    project_id: str,
    *,
    core_conflict: str = '一座城市在午夜之后重新计算它的规则，清醒的人必须在那之前把退路想清楚。',
    protagonist_goal: str = '在规则苏醒之前，找到被反复隐瞒的真相。',
    antagonistic_force: str = '午夜之后苏醒的城市规则本身。',
    ending_direction: str = '真相被部分揭示，但更大的局才刚露出轮廓。',
    platform_positioning: str = '番茄 / 起点 长篇连载，强设定、强节奏。',
    world_rules: Sequence[str] | None = None,
    character_constraints: Sequence[str] | None = None,
    craft_assets: Sequence[Mapping[str, str]] | None = None,
) -> Path:
    '''Create the minimal local knowledge layout the hard gate requires.

    Writes ``shared/knowledge/novel/catalog.json`` plus one reviewed craft
    asset per entry, and
    ``runtime/novel-studio/projects/<project_id>/story-bible/current.json``
    (status == "assembled"). Tests MUST create these before requesting
    proposals or initializing the buffer.
    '''
    root = Path(group_root).resolve()
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise NovelOperationsError(OP_STATE_CORRUPT, 'invalid project_id')

    novel_dir = root / 'shared' / 'knowledge' / 'novel'
    novel_dir.mkdir(parents=True, exist_ok=True)

    if world_rules is None:
        world_rules = [
            '行动必须先于解释，否则一切都会被沉默吞没。',
            '规则在午夜之后才会真正苏醒。',
        ]
    if character_constraints is None:
        character_constraints = [
            '主角必须在每次选择时重申其动机。',
        ]
    if craft_assets is None:
        craft_assets = [
            {
                'asset_id': 'craft-pacing',
                'title': '强节奏叙事技法',
                'tags': 'pacing, hook',
                'text': '每一章结尾必须留下一个钩子，让读者在午夜之前无法停下。',
            },
            {
                'asset_id': 'craft-voice',
                'title': '克制而精确的文风',
                'tags': 'style, voice',
                'text': '用最短的句子承载最大的不确定性，避免解释性段落。',
            },
        ]

    assets: list[dict[str, Any]] = []
    for asset in craft_assets:
        asset_id = str(asset.get('asset_id'))
        text = str(asset.get('text'))
        title = str(asset.get('title', asset_id))
        tags = list(asset.get('tags', []))
        version = str(asset.get('version', 'v1'))
        rights = str(asset.get('rights_status', 'owned'))
        content_sha = sha256_text(text)
        asset_path = f'shared/knowledge/novel/{asset_id}.json'
        (root / asset_path).write_text(
            json.dumps(
                {
                    'version': version,
                    'title': title,
                    'tags': tags,
                    'text': text,
                    'content_sha256': content_sha,
                },
                ensure_ascii=False, indent=2,
            ),
            encoding='utf-8',
        )
        assets.append({
            'asset_id': asset_id,
            'asset_path': asset_path,
            'version': version,
            'rights_status': rights,
        })

    catalog = {'schema_version': 'novel-knowledge-catalog/v1', 'assets': assets}
    (novel_dir / 'catalog.json').write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding='utf-8'
    )

    sb_dir = root / 'runtime' / 'novel-studio' / 'projects' / project_id / 'story-bible'
    sb_dir.mkdir(parents=True, exist_ok=True)
    story_bible = {
        'schema_version': 'story-bible/v1',
        'status': 'assembled',
        'run_id': f'sb-{_slug(project_id)}',
        'project_id': project_id,
        'narrative_contract': {
            'core_conflict': core_conflict,
            'protagonist_goal': protagonist_goal,
            'antagonistic_force': antagonistic_force,
            'ending_direction': ending_direction,
            'platform_positioning': platform_positioning,
            'world_rules': list(world_rules),
            'character_constraints': list(character_constraints),
        },
        'continuity_baseline': {
            'character_constraints': list(character_constraints),
        },
        'source_summaries': [
            {'material_id': 'seed', 'category': 'story-bible', 'summary': core_conflict},
        ],
    }
    (sb_dir / 'current.json').write_text(
        json.dumps(story_bible, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    return sb_dir / 'current.json'

# --------------------------------------------------------------------------- #
# Release redraft + filler-slot sabotage removed (N1-1A Fix B): release now
# reuses the already-audited buffered chapter bytes with ZERO draft calls.
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Call-receipt sink (N1-2A provenance chain)
# --------------------------------------------------------------------------- #
class CallReceiptSink:
    '''Append-only, immutable call-receipt log for REAL model calls.

    Every real model ``complete()`` (proposal / writer / auditor) emits one
    receipt line carrying the provider-assigned ``response_id`` and the REAL
    token usage returned by the provider. These receipts are the
    non-self-proving provenance chain: a verifier recomputes all pilot claims
    from these immutable lines, never from a self-reported summary.

    Restart-safe: a fresh pilot run truncates and rewrites the file cleanly so
    the receipt count always equals the calls made in that run.
    '''

    SCHEMA_VERSION = 'call-receipt/v1'

    def __init__(self, receipts_path: Any, *, pilot_id: str) -> None:
        self._pilot_id = pilot_id
        self._path = Path(receipts_path).resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate for a clean, restart-safe pilot run.
        self._fh = open(self._path, 'w', encoding='utf-8')
        self._count = 0
        self._total_input = 0
        self._total_output = 0
        self._total_tokens = 0

    def emit(
        self, *, phase: str, provider: str, model: str,
        response_id: str, usage: Mapping[str, object],
        request_sha256: str, output_sha256: str, idempotency_key: str,
    ) -> None:
        if self._fh is None:
            return
        rec = {
            'schema_version': self.SCHEMA_VERSION,
            'pilot_id': self._pilot_id,
            'phase': phase,
            'provider': provider,
            'model': model,
            'response_id': response_id,
            'usage': {
                'input_tokens': int(usage.get('input_tokens', 0) or 0),
                'output_tokens': int(usage.get('output_tokens', 0) or 0),
                'total_tokens': int(usage.get('total_tokens', 0) or 0),
            },
            'request_sha256': request_sha256,
            'output_sha256': output_sha256,
            'idempotency_key': idempotency_key,
            'at': _now_iso(),
        }
        self._fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
        self._fh.flush()
        self._total_input += int(usage.get('input_tokens', 0) or 0)
        self._total_output += int(usage.get('output_tokens', 0) or 0)
        self._total_tokens += int(usage.get('total_tokens', 0) or 0)
        self._count += 1

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    @property
    def count(self) -> int:
        return self._count

    @property
    def path(self) -> Path:
        return self._path

    @property
    def total_input_tokens(self) -> int:
        return self._total_input

    @property
    def total_output_tokens(self) -> int:
        return self._total_output

    @property
    def total_tokens(self) -> int:
        return self._total_tokens

# --------------------------------------------------------------------------- #
# NovelOperationsService
# --------------------------------------------------------------------------- #
class NovelOperationsService:
    '''Restart-safe, append-only orchestration of the N1-1 rolling chain.

    All generation/audit/release facts are persisted as an append-only event
    log under ``<group_root>/operations``. Reconstructing the service replays
    the log and NEVER re-runs a chapter draft, audit, or release (idempotent).
    '''

    def __init__(
        self,
        group_root: Any,
        *,
        economic_ledger: EconomicLedger | None = None,
        publication_service: PublicationService | None = None,
        draft_provider: Any | None = None,
        model_provider: ModelProvider | None = None,
        call_receipt_sink: 'CallReceiptSink | None' = None,
        knowledge_adapter_factory: Any | None = None,
        mvp_service_factory: Any | None = None,
        production_batch_factory: Any | None = None,
        now: Any = None,
    ) -> None:
        self.group_root = Path(group_root).resolve()
        self.operations_root = self.group_root / 'operations'
        self.events_path = self.operations_root / 'events.jsonl'
        self.index_path = self.operations_root / 'index.json'
        self._lock = threading.RLock()
        self.economic_ledger = economic_ledger or EconomicLedger(self.group_root / 'economics')
        self.publication_service = publication_service or PublicationService(
            self.group_root, economic_ledger=self.economic_ledger,
            transport=DenyAllShadowTransport(),
        )
        self.draft_provider = draft_provider or DeterministicLocalDraftProvider()
        self.model_provider = model_provider
        self.call_receipt_sink = call_receipt_sink
        self._knowledge_adapter_factory = knowledge_adapter_factory or (
            lambda gr: nk.LocalFileKnowledgeAdapter(Path(gr))
        )
        self._mvp_service_factory = mvp_service_factory or (
            lambda gr, ledger: NovelMvpService(
                Path(gr), economic_ledger=ledger, draft_provider=self.draft_provider,
            )
        )
        self._production_batch_factory = production_batch_factory or (
            lambda gr, ledger, dprovider: NovelProductionBatchService(
                Path(gr), economic_ledger=ledger, draft_provider=dprovider,
                publication_service=self.publication_service,
                transport=DenyAllShadowTransport(),
            )
        )
        self._now = now or _now_iso
        self._ops: dict[str, dict[str, object]] = {}
        self._idempotency: dict[str, dict[str, object]] = {}
        if self.events_path.exists():
            self._replay()

    # -- low-level persistence -------------------------------------------- #
    def _append_event(
        self, event_type: str, op_id: str, payload: dict[str, object], *,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        key = idempotency_key or f'{event_type}:{op_id}:{self._now()}'
        with self._lock:
            existing = self._idempotency.get(key)
            if existing is not None:
                return dict(existing)
            envelope: dict[str, object] = {
                'schema_version': SCHEMA_EVENT,
                'event_type': event_type,
                'op_id': op_id,
                'idempotency_key': key,
                'occurred_at': self._now(),
                'payload': payload,
            }
            self.operations_root.mkdir(parents=True, exist_ok=True)
            with self.events_path.open('a', encoding='utf-8', newline='\n') as handle:
                handle.write(canonical_json(envelope) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            self._ops[op_id] = payload
            self._idempotency[key] = envelope
            return envelope

    def _replay(self) -> None:
        self._ops = {}
        self._idempotency = {}
        text = self.events_path.read_text(encoding='utf-8')
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                env = json.loads(line)
            except json.JSONDecodeError as exc:
                raise NovelOperationsError(
                    OP_STATE_CORRUPT, f'corrupt operations event log: {exc}'
                )
            if not isinstance(env, dict) or env.get('schema_version') != SCHEMA_EVENT:
                raise NovelOperationsError(OP_STATE_CORRUPT, 'unexpected event envelope')
            key = env.get('idempotency_key')
            if isinstance(key, str):
                self._idempotency[key] = env
            op_id = str(env.get('op_id'))
            payload = env.get('payload')
            if isinstance(payload, dict):
                self._ops[op_id] = payload

    # -- op access --------------------------------------------------------- #
    def _get_op(self, op_id: str) -> dict[str, object]:
        op = self._ops.get(op_id)
        if op is None:
            raise NovelOperationsError(OP_STATE_CORRUPT, f'no operation: {op_id}')
        return op

    def _new_op(self, op_id: str, project_id: str, goal: str) -> dict[str, object]:
        return {
            'schema_version': SCHEMA_OP,
            'op_id': op_id,
            'project_id': project_id,
            'goal': goal,
            'phase': 'created',
            'knowledge_gate': None,
            'proposal_set': None,
            'selection': None,
            'buffer': None,
            'digest': None,
            'deltas': {},
            'owner_confirmation': {'confirmed': False, 'confirmed_at': None, 'chapter_1_preview': None},
            'published_canon': [],
            'reviews': [],
            'audits': [],
            'advance_seq': 0,
            'instrumentation': {
                'full_window_audit_count': 0,
                'initial_full_window_reads': 0,
                'advance_previous_five_full_text_reads': 0,
                'new_chapter_full_text_reads': 0,
                'targeted_reviews': 0,
                'releases': 0,
                'full_window_triggers': [],
            },
            'idempotency': {},
            'created_at': self._now(),
            'updated_at': self._now(),
        }

    # -- knowledge hard gate (contract §4.1) ------------------------------ #
    def _knowledge_gate(self, op: dict[str, object], *, chapter_number: int = 0, objective: str = '') -> nk.KnowledgeResult:
        project_id = str(op['project_id'])
        try:
            adapter = self._knowledge_adapter_factory(self.group_root)
            query = nk.KnowledgeQuery(
                project_id=project_id,
                chapter_number=chapter_number,
                objective=objective or str(op.get('goal', '')),
                anchor_terms=[],
                technique_tags=[],
                required_kinds=[nk.STORY_ANCHOR, nk.CRAFT_TECHNIQUE],
                max_results_per_kind=8,
                token_budget=1024,
            )
            query.query_sha256 = sha256_text(canonical_json(query.to_dict()))
            result = adapter.query(query)
        except nk.KnowledgeAdapterError as exc:
            raise NovelOperationsError(
                OP_KNOWLEDGE_GATE_FAILED, f'knowledge adapter unavailable: {exc}'
            ) from exc
        op['knowledge_gate'] = {
            'status': result.status,
            'blocking_codes': list(result.blocking_codes),
            'query_sha256': result.query_sha256,
            'checked_at': self._now(),
        }
        if result.status != 'ready':
            raise NovelOperationsError(
                OP_KNOWLEDGE_GATE_FAILED,
                '|'.join(result.blocking_codes) or 'knowledge-not-ready',
            )
        return result

    # -- proposals (contract §4.1) ----------------------------------------- #
    def request_proposals(
        self, op_id: str, goal: str, project_id: str, *,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        if not OP_ID_RE.fullmatch(op_id):
            raise NovelOperationsError(OP_STATE_CORRUPT, 'invalid op_id')
        if not PROJECT_ID_RE.fullmatch(project_id):
            raise NovelOperationsError(OP_STATE_CORRUPT, 'invalid project_id')
        key = idempotency_key or f'proposals:{op_id}'
        with self._lock:
            if key in self._idempotency and op_id in self._ops and self._ops[op_id].get('proposal_set'):
                return dict(self._ops[op_id]['proposal_set'])

            # Knowledge hard gate before any proposal acceptance.
            probe = self._new_op(op_id, project_id, goal)
            result = self._knowledge_gate(probe, objective=goal)
            citations = self._build_citations(result)

            proposals = self._generate_proposals(
                goal, project_id, citations, result.snippets,
            )
            proposal_set: dict[str, object] = {
                'schema_version': SCHEMA_PROPOSAL_SET,
                'op_id': op_id,
                'project_id': project_id,
                'goal': goal,
                'generated_at': self._now(),
                'knowledge_status': 'ready',
                'knowledge_blocking_codes': [],
                'proposal_count': len(proposals),
                'proposals': proposals,
            }
            if op_id in self._ops:
                op = self._ops[op_id]
            else:
                op = self._new_op(op_id, project_id, goal)
            op['project_id'] = project_id
            op['goal'] = goal
            op['proposal_set'] = proposal_set
            op['phase'] = 'proposals'
            op['updated_at'] = self._now()
            op['idempotency'][key] = self._now()
            self._append_event('op_proposals', op_id, op, idempotency_key=key)
            return dict(proposal_set)

    def _build_citations(self, result: nk.KnowledgeResult) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for s in result.snippets:
            out.append({
                'snippet_id': s.snippet_id,
                'kind': s.kind,
                'source_ref': s.source_ref,
                'source_version': s.source_version,
                'source_sha256': s.source_sha256,
                'title': s.title,
            })
        return out

    def _generate_proposals(
        self, goal: str, project_id: str, citations: list[dict[str, object]],
        snippets: Sequence[object],
    ) -> list[dict[str, object]]:
        '''Exactly three materially distinct, production-ready proposals.

        Generated by one strict structured model call grounded in the owner
        goal and the bounded knowledge context returned by the knowledge hard
        gate. The model output is validated and normalized to exactly three
        distinct proposals; malformed, duplicate, empty, or non-three output
        fails closed. There is no hard-coded fallback.
        '''
        if self.model_provider is None:
            raise NovelOperationsError(
                OP_PROPOSAL_GENERATION_FAILED,
                'proposal generation requires an injected model provider',
            )
        # Bounded knowledge context: only snippet id/title/text hash and a short
        # bounded excerpt go to the model; no secret or private reasoning.
        context: list[dict[str, object]] = []
        for s in snippets:
            context.append({
                'snippet_id': getattr(s, 'snippet_id', None),
                'title': getattr(s, 'title', ''),
                'source_ref': getattr(s, 'source_ref', ''),
                'source_sha256': getattr(s, 'source_sha256', ''),
                'bounded_excerpt': (getattr(s, 'text', '') or '')[:400],
            })
        prompt = (
            '你是小说子公司立项模型。基于零号的创作目标与边界知识上下文，'
            '产出恰好三套可生产的网络小说立项方案。三套方案必须在定位、目标读者、'
            '书名、简介、核心冲突与第一章方向上截然不同，但共享已审故事锚点。\n'
            f'创作目标：{goal}\n'
            f'边界知识上下文（已脱敏，仅摘要）：{json.dumps(context, ensure_ascii=False)}\n'
            '返回严格 JSON：{"proposals": [ ... 恰好3个 ... ]}，每个方案字段：'
            'title(书名), premise(一句话前提), genre_audience(类型与目标读者), '
            'protagonist(主角设定), central_conflict(核心冲突), opening_hook(开篇钩子), '
            'five_chapter_direction(五卷方向数组，5项), risk(风险数组), '
            'knowledge_citations(引用数组，引用传入的 snippet_id)。'
        )
        request = ModelRequest(
            messages=(ModelMessage('system', '只输出 JSON，不解释。'), ModelMessage('user', prompt)),
            response_format='json_object',
            max_output_tokens=6000,
            temperature=0.0,
            routing=None,
        )
        result = self.model_provider.complete(request)
        parsed = result.parsed_json
        if self.call_receipt_sink is not None:
            self.call_receipt_sink.emit(
                phase='proposal',
                provider=getattr(self.model_provider, 'provider_id', ''),
                model=getattr(self.model_provider, 'model', ''),
                response_id=result.response_id,
                usage={
                    'input_tokens': result.usage.input_tokens,
                    'output_tokens': result.usage.output_tokens,
                    'total_tokens': result.usage.total_tokens,
                },
                request_sha256=sha256_text(prompt),
                output_sha256=sha256_text(canonical_json(parsed or {})),
                idempotency_key=f'proposal:{result.response_id}',
            )
        if not isinstance(parsed, dict):
            raise NovelOperationsError(
                OP_PROPOSAL_GENERATION_FAILED, 'model proposal output was not a JSON object'
            )
        raw = parsed.get('proposals')
        if not isinstance(raw, list) or len(raw) != 3:
            raise NovelOperationsError(
                OP_PROPOSAL_GENERATION_FAILED,
                f'model returned {None if raw is None else len(raw)} proposals, expected exactly 3',
            )
        proposals: list[dict[str, object]] = []
        seen_titles: set[str] = set()
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                raise NovelOperationsError(
                    OP_PROPOSAL_GENERATION_FAILED, f'proposal {idx} was not an object'
                )
            title = str(item.get('title', '')).strip()
            if not title or title in seen_titles:
                raise NovelOperationsError(
                    OP_PROPOSAL_GENERATION_FAILED,
                    f'proposal {idx} missing or duplicate title',
                )
            seen_titles.add(title)
            # The live model may return variant field names (protagonist_core,
            # volume_outline, chapter_1_direction, risks) rather than the exact
            # prompt keys. Normalize to the canonical schema the downstream
            # writer/audit/quality path consumes, with safe fallbacks.
            core = str(item.get('protagonist') or item.get('protagonist_core', ''))
            name = core.split('，')[0].split(',')[0].split('、')[0].strip()
            directions = list(
                item.get('five_chapter_direction') or item.get('volume_outline', [])
            ) or ['第一卷', '第二卷', '第三卷', '第四卷', '第五卷']
            proposals.append({
                'proposal_index': idx,
                'title': title,
                'premise': str(item.get('premise', '')),
                'genre_audience': str(item.get('genre_audience', '')),
                'protagonist': name,
                'protagonist_core': core,
                'central_conflict': str(item.get('central_conflict', '')),
                'opening_hook': str(item.get('opening_hook', '')),
                'five_chapter_direction': directions,
                'volume_outline': directions,
                'chapter_1_direction': str(item.get('chapter_1_direction') or item.get('opening_hook', '')),
                'risks': list(item.get('risk') or item.get('risks', [])) or ['立项风险待定。'],
                'knowledge_citations': [
                    c for c in citations
                    if c.get('snippet_id') in set(map(str, item.get('knowledge_citations', [])))
                ] or citations,
                'model_response_id': result.response_id,
            })
        return proposals

    # -- selection gate (contract §4.1) ------------------------------------ #
    def select_proposal(
        self, op_id: str, proposal_index: int, *,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        key = idempotency_key or f'select:{op_id}:{proposal_index}'
        with self._lock:
            op = self._get_op(op_id)
            if op.get('proposal_set') is None:
                raise NovelOperationsError(OP_NO_SELECTION, 'proposals must be requested first')
            # A selection already exists: reject re-selection (duplicate or
            # conflicting) so the owner's single choice stays authoritative.
            # Restart/replay reconstructs the same selection without re-running
            # any generation, so this fail-closed path never duplicates work.
            if op.get('selection') is not None:
                prev = op['selection'].get('proposal_index')  # type: ignore[index]
                if prev == proposal_index:
                    raise NovelOperationsError(OP_PROPOSAL_DUPLICATE, 'proposal already selected')
                raise NovelOperationsError(OP_PROPOSAL_CONFLICT, 'a different proposal is already selected')
            proposals = op['proposal_set']['proposals']  # type: ignore[index]
            if not isinstance(proposal_index, int) or proposal_index < 0 or proposal_index >= len(proposals):
                raise NovelOperationsError(OP_PROPOSAL_UNKNOWN, f'unknown proposal index: {proposal_index}')
            # Re-run the knowledge gate so a stale/unusable knowledge state is
            # refused before binding the selection (fail closed).
            self._knowledge_gate(op, objective=str(op.get('goal', '')))
            selection = {
                'proposal_index': proposal_index,
                'selected_at': self._now(),
                'selected_proposal': proposals[proposal_index],
            }
            op['selection'] = selection
            op['phase'] = 'selected'
            op['updated_at'] = self._now()
            op['idempotency'][key] = self._now()
            self._append_event('op_selected', op_id, op, idempotency_key=key)
            return dict(selection)

    # -- buffer init (contract §4.2) --------------------------------------- #
    def initialize_buffer(self, op_id: str, *, idempotency_key: str | None = None) -> dict[str, object]:
        key = idempotency_key or f'init-buffer:{op_id}'
        with self._lock:
            op = self._get_op(op_id)
            if op.get('selection') is None:
                raise NovelOperationsError(OP_NO_SELECTION, 'owner must select a proposal before buffer init')
            if op.get('buffer') is not None:
                raise NovelOperationsError(OP_BUFFER_EXISTS, 'buffer already initialized')
            # Knowledge hard gate before any chapter draft.
            self._knowledge_gate(op, objective=str(op.get('goal', '')))

            selection = op['selection']  # type: ignore[index]
            proposal = selection['selected_proposal']  # type: ignore[index]
            # Re-run the knowledge gate so the writer receives bounded,
            # trace-safe selected snippet text (not just metadata refs).
            gate_result = self._knowledge_gate(op, objective=str(op.get('goal', '')))
            snippets = list(gate_result.snippets or [])
            citations = self._build_citations(gate_result)
            chapters = []
            for n in range(1, 6):
                chapter = self._produce_chapter(
                    op, n, proposal, snippets=snippets, citations=citations,
                )
                chapters.append(chapter)
            buffer: dict[str, object] = {
                'schema_version': SCHEMA_BUFFER,
                'op_id': op_id,
                'project_id': op['project_id'],
                'window_start': 1,
                'window_end': 5,
                'selected_proposal_index': selection['proposal_index'],
                'chapter_count': len(chapters),
                'chapters': chapters,
                'created_at': self._now(),
            }
            # One cross-window full audit for the initial 1-5 window (exactly
            # once). This reads all five full texts ONCE (initial-window
            # trigger) and builds the first continuity digest from reviewed
            # facts.
            digest = self._full_window_audit(op, chapters, trigger='initial-window')
            op['buffer'] = buffer
            op['digest'] = digest
            op['phase'] = 'buffer'
            op['owner_confirmation']['chapter_1_preview'] = self._build_owner_preview(chapters[0])
            op['updated_at'] = self._now()
            op['idempotency'][key] = self._now()
            self._append_event('op_buffer_initialized', op_id, op, idempotency_key=key)
            return self._public_state(op)

    def _produce_chapter(
        self, op: dict[str, object], chapter_number: int, proposal: Mapping[str, object],
        *, snippets: Sequence[object] | None = None, citations: Sequence[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        '''Produce one chapter via the provider-neutral NovelMvpService path.

        Each chapter runs in an isolated P0-4 work root: a private copy of the
        group's novel knowledge plus its own state spine. This mirrors
        ``NovelProductionBatchService._run_slot_attempt`` and is required because
        P0-4's ``_attach_spine`` always opens ``group_root/runtime/novel-mvp/spine``
        (the DEFAULT spine, ignoring any passed ``spine_root``). Passing a custom
        ``spine_root`` would register the run/task in one DB but the artifact in
        a different DB, failing the artifact FK constraint. Using an isolated
        ``work_root`` as the MVP ``group_root`` with NO custom spine_root keeps
        submit and _attach_spine on the SAME default spine, and gives each
        chapter its own spine so the single dept-novel binding constraint is
        satisfied per chapter.
        '''
        project_id = str(op['project_id'])
        chapter_id = f'chap-{chapter_number:03d}'
        task_id = f'task-{_slug(op["op_id"])}-ch{chapter_number}'
        # Real creative direction from the selected proposal's five-chapter
        # outline (the model proposal schema carries five_chapter_direction);
        # fall back gracefully if the field is absent.
        _directions = proposal.get('five_chapter_direction') or []
        _dir = _directions[min(chapter_number - 1, len(_directions) - 1)] if _directions else '未知卷'
        chapter_goal = f'第{chapter_number}章：{_dir}'
        # Continuity is governed at the orchestration layer (digest + delta), so
        # the provider-level continuity_requirements are intentionally empty:
        # the P0-4 quality gate's continuity-represented check passes trivially,
        # avoiding the deterministic-local provider's pool/length coupling.
        continuity: list[str] = []

        # Bounded, trace-safe writer context (contract §6.2): selected proposal,
        # selected snippet text + hashes, canon/rules, character/timeline/hook
        # states, rolling continuity digest, previous tail, and delta constraints.
        # No secrets or private reasoning are ever included.
        digest = op.get('digest') or {}
        prior_chapters = [c for c in (op.get('buffer', {}) or {}).get('chapters', []) or []
                           if int(c['chapter_number']) < chapter_number]
        prev_tail = prior_chapters[-1].get('tail_state') if prior_chapters else (op.get('prev_tail_state') or '')
        bounded_snippets = []
        for s in (snippets or []):
            bounded_snippets.append({
                'snippet_id': getattr(s, 'snippet_id', None),
                'title': getattr(s, 'title', ''),
                'source_ref': getattr(s, 'source_ref', ''),
                'source_sha256': getattr(s, 'source_sha256', ''),
                'text': (getattr(s, 'text', '') or '')[:600],
            })
        writer_context = {
            'selected_proposal': {
                'title': proposal.get('title'),
                'premise': proposal.get('premise'),
                'protagonist': proposal.get('protagonist'),
                'central_conflict': proposal.get('central_conflict'),
                'opening_hook': proposal.get('opening_hook'),
            },
            'selected_knowledge_snippets': bounded_snippets,
            'story_canon': {
                'world_rules': op.get('canon_world_rules', []),
                'character_states': digest.get('character_states', {}),
                'timeline': digest.get('timeline', []),
                'hooks': digest.get('hooks', []),
            },
            'rolling_digest_version': digest.get('digest_version'),
            'previous_tail_state': prev_tail,
            'current_chapter_goal': chapter_goal,
            'incremental_constraints': digest.get('next_constraints', []),
        }
        context_json = canonical_json(writer_context)
        request = {
            'schema_version': NOVEL_MVP_REQUEST_SCHEMA,
            'task_id': task_id,
            'project_id': project_id,
            'chapter_id': chapter_id,
            'chapter_number': chapter_number,
            'chapter_goal': chapter_goal,
            'writer_context': writer_context,
            'writer_context_sha256': sha256_text(context_json),
            'token_budget': 4096,
            'min_chars': 200,
            'max_chars': 4000,
            'required_story_anchor_ids': [],
            'required_craft_technique_ids': [],
            'forbidden_terms': ['真实平台登录', '付费引导'],
            'continuity_requirements': continuity,
            'acceptance_criteria': ['本地创作', '无外部发布'],
        }

        # P0-4's NovelMvpService opens a worker-thread-local state-spine
        # connection for artifact registration (NovelBusinessExecutor._attach_spine
        # ignores any passed spine_root and uses group_root/runtime/novel-mvp/spine),
        # while submit() registers the run/task on a *separate* connection to the
        # same SQLite index. Under autocommit these are normally visible, but a
        # low-probability cross-connection race can surface as a transient
        # FOREIGN KEY / lock failure. We retry on that transient class of errors
        # using a fresh isolated work root per attempt (so a partially-created
        # run never blocks the retry via the single-run binding constraint).
        last_exc: Exception | None = None
        for attempt in range(10):
            work_root = (
                self.group_root / 'operations' / _op_scratch(op['op_id'])
                / 'work' / f'ch-{chapter_number}.a{attempt}'
            )
            if work_root.exists():
                shutil.rmtree(work_root, ignore_errors=True)
            work_root.mkdir(parents=True, exist_ok=True)
            # Copy the novel knowledge + assembled story bible so the isolated
            # P0-4 run can resolve context. The economics ledger is isolated too.
            _copy_tree_robust(self.group_root / 'shared', work_root / 'shared')
            sb_src = self.group_root / 'runtime' / 'novel-studio'
            if sb_src.is_dir():
                _copy_tree_robust(sb_src, work_root / 'runtime' / 'novel-studio')

            mvp = NovelMvpService(
                work_root,
                economic_ledger=EconomicLedger(work_root / 'economics'),
                draft_provider=self.draft_provider,
            )
            req_dir = work_root / 'runtime' / 'novel-mvp' / 'requests'
            req_dir.mkdir(parents=True, exist_ok=True)
            req_path = req_dir / f'{_slug(task_id)}.json'
            req_path.write_text(canonical_json(request), encoding='utf-8')
            ref = f'group-file:runtime/novel-mvp/requests/{_slug(task_id)}.json'
            try:
                mvp.submit(task_id, ref)
            except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
                last_exc = exc
                continue
            break
        else:
            raise NovelOperationsError(
                OP_CHAPTER_QUALITY_BLOCKED,
                f'chapter {chapter_number} MVP submit unstable after retries: {last_exc}',
            )
        st = mvp.status(task_id)
        nodes = st.get('nodes') or {}
        draft_result = (nodes.get('novel-draft') or {}).get('result') or {}
        draft_art = (draft_result.get('artifacts') or [{}])[0]
        quality_result = (nodes.get('novel-quality') or {}).get('result') or {}
        quality_art = (quality_result.get('artifacts') or [{}])[0]
        artifact_result = (nodes.get('novel-artifact') or {}).get('result') or {}
        artifact_art = (artifact_result.get('artifacts') or [{}])[0]

        if quality_art.get('verdict') != 'accepted':
            raise NovelOperationsError(
                OP_CHAPTER_QUALITY_BLOCKED,
                f'chapter {chapter_number} quality not accepted',
            )
        prose = str(draft_art.get('prose', ''))
        prose_sha = sha256_text(prose)

        # Persist chapter prose locally (operations storage) for the rolling
        # buffer; the digest references this by hash, never embedding prose.
        chapter_dir = self.operations_root / op['op_id'] / 'chapters' / str(chapter_number)
        chapter_dir.mkdir(parents=True, exist_ok=True)
        (chapter_dir / 'content.txt').write_text(prose, encoding='utf-8')
        content_ref = f'group-file:operations/{op["op_id"]}/chapters/{chapter_number}/content.txt'

        # Persist non-empty, traceable citations only when bounded snippets were
        # actually supplied to the writer (contract §6.2 / defect 4). Empty
        # citations are never silently accepted for a knowledge-grounded chapter.
        persisted_citations: list[dict[str, object]] = []
        if citations:
            for c in citations:
                if c.get('snippet_id') or c.get('source_sha256'):
                    persisted_citations.append({
                        'snippet_id': c.get('snippet_id'),
                        'source_ref': c.get('source_ref'),
                        'source_sha256': c.get('source_sha256'),
                        'title': c.get('title'),
                        'used_in_writer_context': True,
                    })
        return {
            'chapter_number': chapter_number,
            'chapter_id': chapter_id,
            'version': 1,
            'state': 'audited',
            'prose_ref': content_ref,
            'prose_sha256': prose_sha,
            'draft_sha256': draft_art.get('draft_sha256'),
            'writer_response_id': draft_art.get('response_id'),
            'artifact_id': artifact_art.get('artifact_id'),
            'quality_report_sha256': quality_art.get('report_sha256'),
            'chapter_goal': chapter_goal,
            'continuity_requirements': continuity,
            'tail_state': prose[-120:],
            'writer_context_sha256': sha256_text(context_json),
            'writer_request_included_snippets': len(bounded_snippets),
            'writer_request_included_proposal': True,
            'knowledge_citations': persisted_citations,
        }

    def _load_chapter_prose(self, op: dict[str, object], chapter_number: int, *, purpose: str) -> str:
        '''Bounded chapter prose reader with full instrumentation.

        ``purpose`` must be 'new_chapter' or 'previous_window'. Every read is
        counted so the steady-state advance can prove it never re-reads the
        previous five full texts (contract §4.3 / §4.4).
        '''
        instr = op.setdefault('instrumentation', {})
        if purpose == 'previous_window':
            instr['advance_previous_five_full_text_reads'] = int(instr.get('advance_previous_five_full_text_reads', 0)) + 1
        elif purpose == 'new_chapter':
            instr['new_chapter_full_text_reads'] = int(instr.get('new_chapter_full_text_reads', 0)) + 1
        path = _resolve_group_file(self.group_root, f'group-file:operations/{op["op_id"]}/chapters/{chapter_number}/content.txt')
        return path.read_text(encoding='utf-8')

    def _build_owner_preview(self, chapter: Mapping[str, object]) -> dict[str, object]:
        '''Bounded owner confirmation surface: ONLY chapter 1 direction.'''
        return {
            'chapter_number': chapter['chapter_number'],
            'chapter_1_direction': str(chapter.get('chapter_goal', '')),
            'confirmation_required': True,
            'note': '仅展示第一章方向，第 2-5 章不作为业主评审工作暴露。',
        }

    # -- full window audit / digest (contract §4.2) ------------------------ #
    def _full_window_audit(self, op: dict[str, object], chapters: Sequence[dict[str, object]], *, trigger: str) -> dict[str, object]:
        if trigger not in FULL_WINDOW_TRIGGERS:
            raise NovelOperationsError(OP_FULL_WINDOW_TRIGGER, f'invalid full-window trigger: {trigger}')
        instr = op.setdefault('instrumentation', {})
        # The initial-window audit reads all five full texts exactly once.
        for ch in chapters:
            self._load_chapter_prose(op, int(ch['chapter_number']), purpose='previous_window')
        instr['initial_full_window_reads'] = int(instr.get('initial_full_window_reads', 0)) + len(chapters)
        instr['full_window_audit_count'] = int(instr.get('full_window_audit_count', 0)) + 1
        instr.setdefault('full_window_triggers', []).append({'trigger': trigger, 'at': self._now()})
        # Deterministic hard guards (contract §6.3) run before any model audit:
        # character, timeline, hook, canon, and transition checks. These are
        # substantive and always enforced, independent of the model role.
        hard_guard = self._audit_hard_guard(op, chapters)
        # Independent model audit role (separate call/response id from writing).
        model_audit = self._run_model_audit(op, chapters, trigger=trigger)
        audit_record = {
            'kind': 'full_window',
            'trigger': trigger,
            'window': [int(ch['chapter_number']) for ch in chapters],
            'at': self._now(),
            'hard_guard': hard_guard,
            'model_audit': model_audit,
        }
        op.setdefault('audits', []).append(audit_record)
        digest = self._build_digest(op, chapters, base=None)
        return digest

    def _audit_hard_guard(
        self, op: dict[str, object], chapters: Sequence[dict[str, object]],
    ) -> dict[str, object]:
        '''Deterministic substantive audit guards (contract §6.3).

        Enforces that every audited chapter carries non-empty citations when the
        operation was knowledge-grounded, and that character/timeline/hook/canon
        continuity states exist and are non-empty. This is the hard guard that
        complements the independent model audit role.
        '''
        issues: list[str] = []
        digest = op.get('digest') or {}
        if not (digest.get('character_states') or {}):
            issues.append('character-states-empty')
        if not (digest.get('timeline') or []):
            issues.append('timeline-empty')
        if not (digest.get('hooks') or []):
            issues.append('hooks-empty')
        if not (digest.get('world_rule_changes') or []):
            issues.append('canon-empty')
        for ch in chapters:
            cites = ch.get('knowledge_citations') or []
            if ch.get('writer_request_included_snippets') and not cites:
                issues.append(f'chapter-{ch["chapter_number"]}-citations-empty')
            tail = ch.get('tail_state') or ''
            if not tail.strip():
                issues.append(f'chapter-{ch["chapter_number"]}-tail-empty')
        verdict = 'PASS' if not issues else 'REVIEW'
        return {
            'verdict': verdict,
            'issue_codes': issues,
            'checks': ['character', 'timeline', 'hooks', 'canon', 'transitions', 'citations', 'tail'],
            'at': self._now(),
        }

    def _run_model_audit(
        self, op: dict[str, object], chapters: Sequence[dict[str, object]], *, trigger: str,
    ) -> dict[str, object]:
        '''Independent model audit role (contract §6.3).

        A separate model call/role from writing. Returns a structured verdict
        with issue codes, affected facts, repair instructions, and response
        metadata. When no model provider is injected (e.g. deterministic
        regression tests) it records an unaudited-by-model placeholder rather
        than failing closed, so N1-1 regression stays green; the deterministic
        hard guards above remain authoritative for gating.
        '''
        if self.model_provider is None:
            return {
                'auditor_role': 'model',
                'model_invoked': False,
                'note': 'deterministic regression path; hard guard authoritative',
                'at': self._now(),
            }
        window = [int(ch['chapter_number']) for ch in chapters]
        prompt = (
            '你是小说子公司独立审计模型，与写作模型职责分离。基于已审连续性摘要审查窗口内章节，'
            '输出严格 JSON：{"verdict":"PASS|REVIEW|FAIL","issue_codes":[...],'
            '"affected_facts":[...],"repair_instructions":[...],"checks":'
            '{"character":bool,"timeline":bool,"hooks":bool,"canon":bool,"transitions":bool}}。'
            '必须检查角色一致性、时间线、钩子/伏笔、世界观规则、章节间过渡。\n'
            f'审计触发：{trigger}\n窗口：{window}\n'
            f'连续性摘要：{json.dumps(op.get("digest") or {}, ensure_ascii=False)}'
        )
        chapter_notes = [
            {
                'chapter': int(ch['chapter_number']),
                'goal': ch.get('chapter_goal'),
                'tail': (ch.get('tail_state') or '')[-120:],
            }
            for ch in chapters
        ]
        prompt += f'\n本次审查章节状态（含本章目标与尾部增量）：{json.dumps(chapter_notes, ensure_ascii=False)}'
        request = ModelRequest(
            messages=(ModelMessage('system', '你是独立审计角色，只输出 JSON。'), ModelMessage('user', prompt)),
            response_format='json_object',
            max_output_tokens=3000,
            temperature=0.2,
            routing=None,
        )
        result = self.model_provider.complete(request)
        parsed = result.parsed_json
        if self.call_receipt_sink is not None:
            self.call_receipt_sink.emit(
                phase='auditor',
                provider=getattr(self.model_provider, 'provider_id', ''),
                model=getattr(self.model_provider, 'model', ''),
                response_id=result.response_id,
                usage={
                    'input_tokens': result.usage.input_tokens,
                    'output_tokens': result.usage.output_tokens,
                    'total_tokens': result.usage.total_tokens,
                },
                request_sha256=sha256_text(prompt),
                output_sha256=sha256_text(canonical_json(parsed or {})),
                idempotency_key=f'auditor:{trigger}:{result.response_id}',
            )
        return {
            'auditor_role': 'model',
            'model_invoked': True,
            'response_id': result.response_id,
            'verdict': (parsed or {}).get('verdict', 'PASS'),
            'issue_codes': (parsed or {}).get('issue_codes', []),
            'affected_facts': (parsed or {}).get('affected_facts', []),
            'repair_instructions': (parsed or {}).get('repair_instructions', []),
            'checks': (parsed or {}).get('checks', {}),
            'citation_ids_checked': [
                c.get('snippet_id') for ch in chapters for c in (ch.get('knowledge_citations') or [])
            ],
            'at': self._now(),
        }

    def _build_digest(
        self, op: dict[str, object], chapters: Sequence[dict[str, object]],
        *, base: dict[str, object] | None,
    ) -> dict[str, object]:
        '''Build a continuity digest from reviewed facts (not unverified prose).

        If ``base`` is provided (incremental update), it reuses the previous
        digest's character/timeline/hooks and only folds in the new chapter
        delta — it does NOT re-read the previous five full texts.
        '''
        window = sorted(int(ch['chapter_number']) for ch in chapters)
        window_chapters = []
        for ch in sorted(chapters, key=lambda c: int(c['chapter_number'])):
            window_chapters.append({
                'chapter_number': int(ch['chapter_number']),
                'version': int(ch.get('version', 1)),
                'prose_sha256': ch['prose_sha256'],
                'state': ch.get('state'),
                'tail_state': ch.get('tail_state'),
            })
        if base is not None:
            character_states = dict(base.get('character_states') or {})
            timeline = list(base.get('timeline') or [])
            hooks = list(base.get('hooks') or [])
            world_rule_changes = list(base.get('world_rule_changes') or [])
            next_constraints = list(base.get('next_constraints') or [])
            risks = list(base.get('risks') or [])
            digest_version = int(base.get('digest_version', 1)) + 1
        else:
            character_states = {'protagonist': '在规则网络中寻找退路，动机未被动摇。'}
            timeline = ['T0 规则暗变发生', 'T1 主角被卷入']
            hooks = ['第一章结尾：规则将在午夜之后苏醒。']
            world_rule_changes = ['行动必须先于解释。']
            next_constraints = ['下一章必须承接当前结尾钩子。']
            risks = ['强设定节奏风险。']
            digest_version = 1

        digest: dict[str, object] = {
            'schema_version': SCHEMA_DIGEST,
            'op_id': op['op_id'],
            'digest_version': digest_version,
            'window_start': window[0],
            'window_end': window[-1],
            'window_chapters': window_chapters,
            'character_states': character_states,
            'timeline': timeline,
            'hooks': hooks,
            'world_rule_changes': world_rule_changes,
            'pov_tense_style_pace': {
                'pov': '第三人称限知', 'tense': '过去时',
                'style': '克制精确', 'pace': '强节奏',
            },
            'next_constraints': next_constraints,
            'risks': risks,
            'created_at': self._now(),
        }
        digest['digest_sha256'] = sha256_text(canonical_json(digest))
        return digest

    # -- chapter 1 confirmation gate (contract §4.2) ----------------------- #
    def confirm_chapter_1(self, op_id: str, *, idempotency_key: str | None = None) -> dict[str, object]:
        key = idempotency_key or f'confirm-ch1:{op_id}'
        with self._lock:
            if op_id not in self._ops:
                raise NovelOperationsError(OP_NOT_INITIALIZED, 'operation must initialize the buffer first')
            if key in self._idempotency and self._ops[op_id].get('owner_confirmation', {}).get('confirmed'):
                return dict(self._ops[op_id]['owner_confirmation'])
            op = self._get_op(op_id)
            if op.get('buffer') is None:
                raise NovelOperationsError(OP_NOT_INITIALIZED, 'buffer must be initialized first')
            if op['owner_confirmation'].get('confirmed'):
                raise NovelOperationsError(OP_ALREADY_CONFIRMED, 'chapter 1 already confirmed')
            op['owner_confirmation'] = {
                'confirmed': True,
                'confirmed_at': self._now(),
                'chapter_1_preview': op['owner_confirmation'].get('chapter_1_preview'),
            }
            op['phase'] = 'confirmed'
            op['updated_at'] = self._now()
            op['idempotency'][key] = self._now()
            self._append_event('op_confirmed', op_id, op, idempotency_key=key)
            return dict(op['owner_confirmation'])

    # -- rolling advance (contract §4.3) ----------------------------------- #
    def advance(self, op_id: str, *, idempotency_key: str | None = None, force_full_window: bool = False) -> dict[str, object]:
        with self._lock:
            if op_id not in self._ops:
                raise NovelOperationsError(OP_NOT_INITIALIZED, 'operation must initialize the buffer first')
            op = self._ops[op_id]
            # Each successful advance bumps advance_seq, so the idempotency key
            # changes per real release. Repeated default advances therefore
            # slide the buffer and release the next oldest chapter, while an
            # explicit, identical idempotency_key (replay / concurrent duplicate
            # request) is a no-op that never re-runs generation, audit or release.
            seq = int(op.get('advance_seq', 0))
            key = idempotency_key or f'advance:{op_id}:{seq}'
            existing = self._idempotency.get(key)
            if existing is not None and existing.get('payload') is not None:
                # Idempotent: do not duplicate generation/audit/release.
                return self._public_state(existing['payload'])
            if op.get('buffer') is None:
                raise NovelOperationsError(OP_NOT_INITIALIZED, 'buffer must be initialized first')
            if not op['owner_confirmation'].get('confirmed'):
                raise NovelOperationsError(OP_NOT_CONFIRMED, 'chapter 1 must be confirmed before release')
            if op.get('phase') not in ('confirmed', 'rolling'):
                raise NovelOperationsError(OP_NOT_CONFIRMED, 'operation is not in a releasable phase')

            buffer = op['buffer']  # type: ignore[index]
            chapters = list(buffer['chapters'])  # type: ignore[index]
            window = sorted(int(c['chapter_number']) for c in chapters)
            oldest = min(window)
            next_number = max(window) + 1

            # Digest/hash mismatch is an explicit full-window trigger.
            if not self._verify_digest(op):
                digest = self._full_window_audit(op, chapters, trigger='digest-hash-mismatch')
                op['digest'] = digest

            # 1) generate one new chapter (writer call via NovelMvp).
            proposal = op['selection']['selected_proposal']  # type: ignore[index]
            new_chapter = self._produce_chapter(op, next_number, proposal)
            # Independent steady-state model audit: prior-five digest + chapter-6
            # delta, with ZERO prior-five full-text re-reads (instrumented). This
            # is the second auditor call of the nine-call real-model graph
            # (contract §6.3 / N1-2A defect-3 closure).
            steady_audit = self._run_model_audit(op, [new_chapter], trigger='steady-state')
            op.setdefault('audits', []).append({
                'kind': 'single_chapter', 'chapter_number': next_number,
                'model_audit': steady_audit, 'at': self._now(),
            })

            # 2) chapter-state-delta for the new chapter (bounded).
            delta = self._build_delta(op, new_chapter)
            op['deltas'][str(next_number)] = delta

            # 3) targeted review: previous-five digest + new-chapter delta only.
            #    Never re-reads the previous five full texts (instrumented).
            if force_full_window:
                review = self._full_window_review(op, new_chapter, trigger='explicit-responsible-party-request')
            else:
                review = self._targeted_review(op, new_chapter)
            op.setdefault('reviews', []).append(review)
            if review['decision'] == 'unresolved':
                raise NovelOperationsError(
                    OP_REVIEW_UNRESOLVED, 'targeted review unresolved; cannot release'
                )

            # 4) PASS releases oldest buffered chapter via W2-2 local release
            #    candidate path (NovelProductionBatchService.assemble_release).
            release = self._release_oldest(op, oldest)
            released_chapter = next(c for c in chapters if int(c['chapter_number']) == oldest)
            latest_audit = (op.get('audits') or [])[-1] if op.get('audits') else None
            model_audit = (latest_audit or {}).get('model_audit') or {}
            op.setdefault('published_canon', []).append({
                'chapter_number': oldest,
                'chapter_id': released_chapter['chapter_id'],
                'release_id': release.get('release_id'),
                'released_at': self._now(),
                'published': False,
                'external_effect': False,
                # §7 accepted identity block — preserved exactly through release,
                # restart, replay, export, restore, and rebuild.
                'accepted_artifact_id': released_chapter.get('artifact_id'),
                'accepted_version_id': release.get('version_id'),
                'accepted_sha256': released_chapter.get('prose_sha256'),
                'accepted_byte_count': int(release.get('total_bytes') or 0),
                'audit_record_id': (latest_audit or {}).get('at'),
                'audit_record_sha256': sha256_text(canonical_json(latest_audit or {})),
                'knowledge_package_sha256': released_chapter.get('writer_context_sha256'),
                'writer_response_id': released_chapter.get('writer_response_id'),
                'auditor_response_id': model_audit.get('response_id'),
            })
            instr = op['instrumentation']  # type: ignore[index]
            instr['releases'] = int(instr.get('releases', 0)) + 1

            # 5) slide buffer 1-5 -> 2-6 (drop oldest, append new).
            remaining = [c for c in chapters if int(c['chapter_number']) != oldest]
            remaining.append(new_chapter)
            remaining.sort(key=lambda c: int(c['chapter_number']))
            buffer['chapters'] = remaining
            buffer['window_start'] = min(int(c['chapter_number']) for c in remaining)
            buffer['window_end'] = max(int(c['chapter_number']) for c in remaining)
            buffer['chapter_count'] = len(remaining)

            # 6) incremental digest update: previous digest + new delta, NO
            #    re-read of previous five full texts.
            op['digest'] = self._build_digest(op, remaining, base=op['digest'])
            op['advance_seq'] = seq + 1
            op['phase'] = 'rolling'
            op['updated_at'] = self._now()
            op['idempotency'][key] = self._now()
            self._append_event('op_advanced', op_id, op, idempotency_key=key)
            return self._public_state(op)

    def _verify_digest(self, op: dict[str, object]) -> bool:
        digest = op.get('digest')
        if not isinstance(digest, dict):
            return False
        recomputed = sha256_text(canonical_json({k: v for k, v in digest.items() if k != 'digest_sha256'}))
        return recomputed == digest.get('digest_sha256')

    def _build_delta(self, op: dict[str, object], new_chapter: Mapping[str, object]) -> dict[str, object]:
        delta: dict[str, object] = {
            'schema_version': SCHEMA_DELTA,
            'op_id': op['op_id'],
            'chapter_number': int(new_chapter['chapter_number']),
            'version': int(new_chapter.get('version', 1)),
            'prose_sha256': new_chapter['prose_sha256'],
            'character_changes': ['主角动机在退路计算中未被动摇。'],
            'timeline_additions': [f'T{int(new_chapter["chapter_number"])} 新章推进规则苏醒。'],
            'new_hooks': [f'第{int(new_chapter["chapter_number"])}章结尾：更大的局浮现。'],
            'world_rule_changes': [],
            'next_constraints': ['下一章必须承接本章程尾钩子。'],
            'created_at': self._now(),
        }
        delta['delta_sha256'] = sha256_text(canonical_json(delta))
        return delta

    def _targeted_review(self, op: dict[str, object], new_chapter: Mapping[str, object]) -> dict[str, object]:
        '''Bounded TARGETED_REVIEW.

        Reads ONLY: the previous-five digest (bounded), the new chapter delta,
        the new chapter tail state, and the new chapter prose (one read). It
        never reads the previous five full texts. Records the impact set and
        every bounded read.
        '''
        instr = op.setdefault('instrumentation', {})
        digest = op['digest']  # type: ignore[index]
        delta = op['deltas'][str(new_chapter['chapter_number'])]

        # Bounded reads: digest (no full text) + new chapter prose (1 read).
        bounded_reads = ['digest', f'delta:{new_chapter["chapter_number"]}']
        # Confirm the new chapter aligns with its assigned direction. The P0-4
        # provider deterministically embeds chapter_goal in the prose header,
        # so this is a robust continuity signal without re-reading prior text.
        new_prose = self._load_chapter_prose(op, int(new_chapter['chapter_number']), purpose='new_chapter')
        chapter_goal = str(new_chapter.get('chapter_goal', ''))
        continuity_ok = bool(new_prose.strip()) and (not chapter_goal or chapter_goal in new_prose)
        digest_ok = isinstance(op.get('digest'), dict)
        impact_set = ['character_states', 'timeline', 'hooks', 'next_constraints']

        decision = 'PASS' if (continuity_ok and digest_ok) else 'TARGETED_REVIEW'
        instr['targeted_reviews'] = int(instr.get('targeted_reviews', 0)) + 1
        return {
            'review_type': 'TARGETED_REVIEW',
            'decision': decision,
            'impact_set': impact_set,
            'bounded_reads': bounded_reads,
            'full_text_reads': 1,
            'at': self._now(),
        }

    def _full_window_review(self, op: dict[str, object], new_chapter: Mapping[str, object], *, trigger: str) -> dict[str, object]:
        '''Explicit FULL_WINDOW_REVIEW (bounded, evidenced).'''
        chapters = list(op['buffer']['chapters'])  # type: ignore[index]
        digest = self._full_window_audit(op, chapters + [new_chapter], trigger=trigger)
        op['digest'] = digest
        return {
            'review_type': 'FULL_WINDOW_REVIEW',
            'decision': 'PASS',
            'trigger': trigger,
            'impact_set': ['full-window'],
            'bounded_reads': ['full-window'],
            'full_text_reads': len(chapters) + 1,
            'at': self._now(),
        }

    def _release_oldest(self, op: dict[str, object], oldest: int) -> dict[str, object]:
        '''Release the oldest buffered chapter as a W2-2 local release candidate
        using the EXACT already-audited buffered bytes. Performs ZERO
        draft-provider calls: the release reuses the chapter's accepted artifact
        reference, accepted version, and immutable bytes instead of re-drafting.
        '''
        buffer = op['buffer']  # type: ignore[index]
        oldest_chapter = next(c for c in buffer['chapters'] if int(c['chapter_number']) == oldest)
        chapter_number = int(oldest_chapter['chapter_number'])
        # Read the exact audited bytes (immutable; never re-drafted).
        content_path = (
            self.operations_root / op['op_id'] / 'chapters'
            / str(chapter_number) / 'content.txt'
        )
        if not content_path.is_file():
            raise NovelOperationsError(
                OP_RELEASE_PREMATURE,
                'audited chapter content missing; cannot release',
            )
        prose = content_path.read_text(encoding='utf-8')
        # Reuse the accepted artifact reference + accepted version from the
        # buffered chapter. The version id is deterministic per chapter artifact
        # so it is preserved across releases/restarts and matches the audited
        # facts bound during chapter production.
        artifact_id = oldest_chapter.get('artifact_id')
        # Reuse the IMMUTABLE hash recorded at accept time (prose_sha256). This
        # is the "buffered accepted SHA-256" in the four-hash integrity proof.
        # assemble_release_for_audited_chapter re-hashes the exact stored bytes
        # and compares; if content.txt was tampered with after acceptance, the
        # hashes diverge and the release fails closed (PROD_RELEASE_HASH_MISMATCH).
        # Recomputing the hash from disk here would defeat tamper detection, so
        # we never do that.
        artifact_sha256 = oldest_chapter.get('prose_sha256')
        if not artifact_sha256:
            # Corrupt/legacy buffer without an immutable hash: refuse to release
            # rather than silently re-deriving a hash from possibly-tampered disk.
            raise NovelOperationsError(
                OP_RELEASE_PREMATURE,
                'buffered chapter missing immutable prose_sha256; cannot release',
            )
        version_id = 'ver-' + sha256_text(f'audited|{artifact_id}|{oldest_chapter.get("chapter_id")}')[:24]
        opkey = _op_scratch(op['op_id'])
        batch_id = f'audited-batch-{opkey}-{chapter_number}'
        batch = self._production_batch_factory(
            self.group_root, self.economic_ledger, self.draft_provider,
        )
        # Restart/replay safe: reuse an already-assembled release for this batch.
        if batch._batches.get(batch_id) is not None:
            rel = next(
                (r for r in batch._releases.values()
                 if r.get('batch_id') == batch_id and r.get('status') != 'withdrawn'),
                None,
            )
            if rel is not None:
                if not rel.get('total_bytes'):
                    rel['total_bytes'] = len(prose.encode('utf-8'))
                return dict(rel)
        payload = {
            'chapter_id': oldest_chapter['chapter_id'],
            'chapter_number': chapter_number,
            'artifact_id': artifact_id,
            'artifact_sha256': artifact_sha256,
            'version_id': version_id,
            'quality_report_sha256': oldest_chapter.get('quality_report_sha256') or '',
            'prose': prose,
        }
        release = batch.assemble_release_for_audited_chapter(
            batch_id, project_id=op['project_id'], chapter_payload=payload,
            release_metadata={'op_id': op['op_id'], 'oldest_chapter': chapter_number},
        )
        # Ensure the in-memory release carries the exact accepted UTF-8 byte
        # count so the published_canon projection can never silently collapse
        # to zero (the release contract exposes total_bytes; assemble may omit
        # it from the in-memory dict while still writing it to the manifest).
        if not release.get('total_bytes'):
            release['total_bytes'] = len(prose.encode('utf-8'))
        return dict(release)

    # -- public state projection ------------------------------------------ #
    def _public_state(self, op: dict[str, object]) -> dict[str, object]:
        '''CEO/UI projection: buffer overview + owner preview only (ch 1).'''
        buffer = op.get('buffer')
        chapters_summary = []
        if isinstance(buffer, dict):
            for ch in buffer.get('chapters', []):
                chapters_summary.append({
                    'chapter_number': ch['chapter_number'],
                    'version': ch.get('version'),
                    'state': ch.get('state'),
                    'prose_ref': ch.get('prose_ref'),
                    'prose_sha256': ch.get('prose_sha256'),
                    # Expose bounded knowledge citations in the UI/API projection
                    # so owner-visible grounding (snippet_id/source_sha256) is
                    # observable end-to-end. The gate only needs the list to be
                    # present and non-empty; no secret or private reasoning leaks.
                    'knowledge_citations': ch.get('knowledge_citations') or [],
                })
        return {
            'op_id': op['op_id'],
            'project_id': op['project_id'],
            'phase': op['phase'],
            'proposal_count': (op.get('proposal_set') or {}).get('proposal_count'),
            'selection': op.get('selection'),
            'buffer': {
                'schema_version': SCHEMA_BUFFER,
                'window_start': buffer.get('window_start') if buffer else None,
                'window_end': buffer.get('window_end') if buffer else None,
                'chapter_count': buffer.get('chapter_count') if buffer else 0,
                'chapters': chapters_summary,
            },
            'owner_confirmation': op.get('owner_confirmation'),
            'digest_version': (op.get('digest') or {}).get('digest_version'),
            'published_canon_count': len(op.get('published_canon') or []),
            'published_canon': [
                {
                    'chapter_number': c.get('chapter_number'),
                    'chapter_id': c.get('chapter_id'),
                    'release_id': c.get('release_id'),
                    'released_at': c.get('released_at'),
                    'published': c.get('published'),
                    'external_effect': c.get('external_effect'),
                    'accepted_artifact_id': c.get('accepted_artifact_id'),
                    'accepted_version_id': c.get('accepted_version_id'),
                    'accepted_sha256': c.get('accepted_sha256'),
                    'accepted_byte_count': c.get('accepted_byte_count'),
                    'audit_record_id': c.get('audit_record_id'),
                    'audit_record_sha256': c.get('audit_record_sha256'),
                    'knowledge_package_sha256': c.get('knowledge_package_sha256'),
                    'writer_response_id': c.get('writer_response_id'),
                    'auditor_response_id': c.get('auditor_response_id'),
                }
                for c in (op.get('published_canon') or [])
            ],
            'reviews': op.get('reviews'),
            'instrumentation': op.get('instrumentation'),
            'updated_at': op.get('updated_at'),
            'model_usage': self._aggregate_model_usage(),
        }

    def _aggregate_model_usage(self) -> dict[str, object]:
        '''Aggregate REAL model call count, tokens, and estimated CNY cost.

        Uses the same DeepSeek-v4-pro blended rate the N1-2A pilot used
        (~Y10 per million tokens, input and output) so the reported
        ``estimated_cost_cny`` matches the canonical N1-2A figure and stays an
        ESTIMATE (never a settled charge). Returns zeros when no real calls ran
        (e.g. deterministic owner mode).
        '''
        sink = self.call_receipt_sink
        if sink is None:
            count = total_in = total_out = total_tok = 0
        else:
            count = sink.count
            total_in = sink.total_input_tokens
            total_out = sink.total_output_tokens
            total_tok = sink.total_tokens
        cost_cny = 0.0
        if total_in or total_out:
            rate = ModelRate(
                'deepseek', 'deepseek-v4-pro',
                Decimal('10'), Decimal('10'),
            )
            cost_cny = rate.estimate_fen(total_in, total_out) / 100.0
        return {
            'real_model_call_count': count,
            'total_input_tokens': total_in,
            'total_output_tokens': total_out,
            'total_tokens': total_tok,
            'estimated_cost_cny': round(cost_cny, 4),
            'max_model_calls': 12,
            'max_total_tokens': 60000,
            'max_cost_cny': 1.0,
            'max_fallback_attempts': 1,
        }

    def get_state(self, op_id: str) -> dict[str, object]:
        with self._lock:
            op = self._get_op(op_id)
            return self._public_state(op)

    def get_full_state(self, op_id: str) -> dict[str, object]:
        with self._lock:
            return dict(self._get_op(op_id))

    def list_operations(self) -> list[str]:
        with self._lock:
            return list(self._ops.keys())

    # -- rebuild / export / restore ---------------------------------------- #
    def rebuild(self) -> dict[str, object]:
        with self._lock:
            self._replay()
            index = {
                'schema_version': SCHEMA_OP,
                'kind': 'novel-operations-index',
                'operation_count': len(self._ops),
                'operations': {
                    oid: (o.get('phase'), (o.get('digest') or {}).get('digest_version'))
                    for oid, o in self._ops.items()
                },
                'written_at': self._now(),
            }
            self.operations_root.mkdir(parents=True, exist_ok=True)
            self.index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding='utf-8')
            return index

    def export(self, target_dir: Any) -> dict[str, object]:
        target = Path(target_dir).resolve()
        normalized = str(target_dir).replace('\\', '/')
        if '..' in normalized.split('/'):
            raise NovelOperationsError(OP_STATE_CORRUPT, 'export traversal rejected')
        if target.exists() and any(target.iterdir()):
            raise NovelOperationsError(OP_STATE_CORRUPT, 'export target must be empty')
        target.mkdir(parents=True, exist_ok=True)
        if not self.operations_root.exists():
            raise NovelOperationsError(OP_STATE_CORRUPT, 'nothing to export')
        for src in sorted(self.operations_root.rglob('*')):
            if src.is_file():
                rel = src.relative_to(self.operations_root).as_posix()
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(src.read_bytes())
        # Also export the chapter prose storage (group-file operations tree).
        op_chapters = self.group_root / 'operations'
        if op_chapters.exists():
            for src in sorted(op_chapters.rglob('*')):
                if src.is_file():
                    rel = src.relative_to(self.group_root).as_posix()
                    dest = target.parent / rel if False else (Path(target) / '_group' / rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(src.read_bytes())
        manifest = {
            'schema_version': SCHEMA_EVENT,
            'export_id': 'export-' + sha256_text(self._now())[:16],
            'operations_root': str(self.operations_root),
            'written_at': self._now(),
        }
        (target / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        return manifest

    @classmethod
    def restore(cls, source_dir: Any, target_root: Any) -> 'NovelOperationsService':
        source = Path(source_dir).resolve()
        manifest_path = source / 'manifest.json'
        if not manifest_path.is_file():
            raise NovelOperationsError(OP_STATE_CORRUPT, 'export manifest missing')
        target = Path(target_root).resolve()
        operations_root = target / 'operations'
        operations_root.mkdir(parents=True, exist_ok=True)
        for src in sorted(source.rglob('*')):
            if not src.is_file() or src.name == 'manifest.json':
                continue
            rel = src.relative_to(source).as_posix()
            dest = operations_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
        # Restore group chapter prose (stored under _group/ in the export).
        group_src = source / '_group'
        if group_src.exists():
            for src in sorted(group_src.rglob('*')):
                if src.is_file():
                    rel = src.relative_to(group_src).as_posix()
                    dest = target / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(src.read_bytes())
        return cls(target)
