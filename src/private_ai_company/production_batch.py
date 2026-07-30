'''W2-2 Novel production batch and release-candidate service.

Local, restart-safe, append-only orchestration of an ordered multi-chapter
novel release. It reuses :class:`NovelMvpService` (P0-4) for every chapter,
binds accepted chapters to W1-1 work/version/zero-cost facts, assembles one
immutable local release artifact + manifest, and enters W2-1 through a single
verified ``release_bundle`` publication candidate plus one authorized shadow
receipt.

No network, browser, credential resolution, real publication, payment,
settlement, positive revenue, or irreversible deletion occurs. All writes are
idempotent and replay-safe; the authoritative history is ``production/events.jsonl``.

Frozen schemas (contract §7):
    novel-production-batch-request/v1
    novel-production-batch-state/v1
    novel-production-chapter-slot/v1
    novel-production-attempt/v1
    novel-production-revision/v1
    novel-release-candidate/v1
    novel-release-manifest/v1
    novel-production-summary/v1
    novel-production-export/v1
'''

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import novel_knowledge as _nk
from .economic_ledger import EconomicLedger
from .novel_mvp import (
    NOVEL_MVP_REQUEST_SCHEMA,
    NOVEL_DEPT_ID,
    NovelMvpService,
    NovelMvpError,
    DeterministicLocalDraftProvider,
    validate_novel_mvp_request,
    _UNIFIED_ID_RE,
)
from .platform_adapter import (
    ALLOWED_ACTIONS,
    ALLOWED_MODES,
    DenyAllShadowTransport,
    FakeShadowTransport,
    PlatformAdapterError,
    PlatformAuthorizationLease,
    canonical_json,
    detect_raw_secret,
    sha256_text,
    validate_id,
    validate_idempotency_key,
    validate_local_ref,
)
from .publication import (
    SCHEMA_CANDIDATE,
    PublicationService,
)


# --------------------------------------------------------------------------- #
# Frozen schema versions (contract §7)
# --------------------------------------------------------------------------- #
SCHEMA_REQUEST = 'novel-production-batch-request/v1'
SCHEMA_STATE = 'novel-production-batch-state/v1'
SCHEMA_SLOT = 'novel-production-chapter-slot/v1'
SCHEMA_ATTEMPT = 'novel-production-attempt/v1'
SCHEMA_REVISION = 'novel-production-revision/v1'
SCHEMA_RELEASE_CANDIDATE = 'novel-release-candidate/v1'
SCHEMA_RELEASE_MANIFEST = 'novel-release-manifest/v1'
SCHEMA_SUMMARY = 'novel-production-summary/v1'
SCHEMA_EXPORT = 'novel-production-export/v1'
SCHEMA_EVENT = 'novel-production-event/v1'

RELEASE_WORK_ID = 'work-novel-release'
PUBLICATION_DEPT_ID = 'dept-publication'

_BATCH_ID_RE = re.compile(r'^batch-[a-z0-9][a-z0-9._-]{2,63}$')
_URL_RE = re.compile(r'https?://')
_SLOT_TASK_RE = re.compile(r'^(task|run|dept|artifact)-[a-z0-9][a-z0-9._-]{2,63}$')


# --------------------------------------------------------------------------- #
# Stable blocking codes (contract §11)
# --------------------------------------------------------------------------- #
PROD_REQUEST_INVALID = 'novel-production-request-invalid'
PROD_SCHEMA_UNSUPPORTED = 'novel-production-schema-unsupported'
PROD_SLOT_DUPLICATE_ORDER = 'novel-production-slot-duplicate-order'
PROD_SLOT_DUPLICATE_ID = 'novel-production-slot-duplicate-id'
PROD_SLOT_UNSAFE_REF = 'novel-production-slot-unsafe-ref'
PROD_SLOT_EMPTY_OBJECTIVE = 'novel-production-slot-empty-objective'
PROD_RAW_SECRET = 'novel-production-raw-secret'
PROD_URL_FORBIDDEN = 'novel-production-url-forbidden'
PROD_UNKNOWN_FIELD = 'novel-production-unknown-field'
PROD_METADATA_UNBOUNDED = 'novel-production-metadata-unbounded'
PROD_BATCH_DUPLICATE = 'novel-production-batch-duplicate'
PROD_BATCH_MISSING = 'novel-production-batch-missing'
PROD_SLOT_MISSING = 'novel-production-slot-missing'
PROD_SLOT_NOT_BLOCKED = 'novel-production-slot-not-blocked'
PROD_SLOT_ALREADY_ACCEPTED = 'novel-production-slot-already-accepted'
PROD_SLOT_FORGE = 'novel-production-slot-forgery'
PROD_RELEASE_PREMATURE = 'novel-production-release-premature'
PROD_RELEASE_STALE_ATTEMPT = 'novel-production-release-stale-attempt'
PROD_RELEASE_HASH_MISMATCH = 'novel-production-release-hash-mismatch'
PROD_RELEASE_ORDER_MISMATCH = 'novel-production-release-order-mismatch'
PROD_RELEASE_DUPLICATE_VERSION = 'novel-production-release-duplicate-version'
PROD_RELEASE_DUPLICATE = 'novel-production-release-duplicate'
PROD_RELEASE_MISSING = 'novel-production-release-missing'
PROD_CONTENT_CHANGED = 'novel-production-content-changed'
PROD_FORGE_BATCH = 'novel-production-forge-batch'
PROD_EXPORT_TRAVERSAL = 'novel-production-export-traversal'
PROD_EXPORT_INCOMPLETE = 'novel-production-export-incomplete'
PROD_EXPORT_NONEMPTY = 'novel-production-export-nonempty'
PROD_LIVE_MODE_FORBIDDEN = 'novel-production-live-mode-forbidden'
PROD_FAKE_REVENUE = 'novel-production-fake-revenue'
PROD_REVISION_INVALID = 'novel-production-revision-invalid'


class NovelProductionBatchError(Exception):
    '''Bounded novel-production error carrying a stable blocking code.'''

    def __init__(self, code: str, message: str = '') -> None:
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _slug(value: str) -> str:
    cleaned = re.sub(r'[^a-z0-9._-]', '-', str(value).lower())
    return cleaned[:50].strip('-') or 'x'


def _now_iso() -> str:
    from .timebase import authoritative_timestamp

    return authoritative_timestamp()


def compute_batch_request_hash(request: Mapping[str, object]) -> str:
    '''Deterministic SHA-256 of a batch request minus its own request_hash.'''
    clean = dict(request)
    clean.pop('request_hash', None)
    return sha256_text(canonical_json(clean))


def _scan_urls(value: object) -> bool:
    if isinstance(value, str):
        return bool(_URL_RE.search(value))
    if isinstance(value, (list, tuple)):
        return any(_scan_urls(v) for v in value)
    if isinstance(value, dict):
        return any(_scan_urls(v) for v in value.values())
    return False


# --------------------------------------------------------------------------- #
# Injected blocking draft provider (contract §3)
# --------------------------------------------------------------------------- #
class FirstAttemptBlockingDraftProvider:
    '''Test/evidence helper: omits the chapter_goal on the FIRST draft of one
    designated chapter so the P0-4 quality gate blocks it. Subsequent drafts
    (revisions) use the base provider and pass. No external effect.
    '''

    PROVIDER_ID = 'first-attempt-blocking-draft/v1'

    def __init__(self, block_chapter_id: str, base: Any | None = None) -> None:
        self.block_chapter_id = block_chapter_id
        self.base = base or DeterministicLocalDraftProvider()
        self._blocked_once: set[str] = set()

    def draft(self, request: Mapping[str, object], context: Mapping[str, object]) -> Mapping[str, object]:
        cid = request.get('chapter_id')
        if cid == self.block_chapter_id and cid not in self._blocked_once:
            self._blocked_once.add(cid)
            record = dict(self.base.draft(request, context))
            goal = str(request.get('chapter_goal', ''))
            prose = str(record.get('prose', ''))
            if goal:
                prose = prose.replace(goal, '')
            record['prose'] = prose
            record['char_count'] = len(prose)
            record['draft_sha256'] = sha256_text(prose)
            return record
        return self.base.draft(request, context)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
_ALLOWED_REQUEST_FIELDS = {
    'schema_version', 'batch_id', 'project_id', 'batch_key', 'title',
    'objective', 'min_required_chapters', 'provider_mode', 'cost_mode',
    'release_metadata', 'chapter_slots', 'idempotency_key', 'request_hash',
}
_ALLOWED_SLOT_FIELDS = {
    'slot_id', 'order', 'chapter_objective', 'required', 'chapter_id',
    'chapter_number', 'task_id', 'request_ref', 'token_budget', 'min_chars',
    'max_chars', 'required_story_anchor_ids', 'required_craft_technique_ids',
    'forbidden_terms', 'continuity_requirements', 'acceptance_criteria',
    'provider_mode', 'cost_mode',
}


def validate_batch_request(data: Any) -> dict[str, Any]:
    '''Validate and normalize a novel-production-batch-request/v1 payload.'''
    if not isinstance(data, dict):
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, 'request must be a JSON object')
    extra = set(data) - _ALLOWED_REQUEST_FIELDS
    if extra:
        raise NovelProductionBatchError(
            PROD_UNKNOWN_FIELD, f'unknown request fields: {sorted(extra)}'
        )
    if data.get('schema_version') != SCHEMA_REQUEST:
        raise NovelProductionBatchError(
            PROD_SCHEMA_UNSUPPORTED,
            f'schema_version must be {SCHEMA_REQUEST!r}',
        )
    batch_id = data.get('batch_id')
    if not isinstance(batch_id, str) or not _BATCH_ID_RE.fullmatch(batch_id):
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, 'batch_id invalid')
    project_id = data.get('project_id')
    if not isinstance(project_id, str) or not _nk.PROJECT_ID_RE.fullmatch(project_id):
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, 'project_id invalid')
    if not isinstance(data.get('batch_key'), str) or not data['batch_key'].strip():
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, 'batch_key required')
    if not isinstance(data.get('title'), str) or not data['title'].strip():
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, 'title required')
    if not isinstance(data.get('objective'), str) or not data['objective'].strip():
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, 'objective required')

    min_required = data.get('min_required_chapters')
    if not isinstance(min_required, int) or isinstance(min_required, bool) or min_required < 3:
        raise NovelProductionBatchError(
            PROD_REQUEST_INVALID, 'min_required_chapters must be >= 3'
        )
    provider_mode = data.get('provider_mode')
    cost_mode = data.get('cost_mode')
    if not isinstance(provider_mode, str) or not provider_mode.strip():
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, 'provider_mode required')
    if not isinstance(cost_mode, str) or not cost_mode.strip():
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, 'cost_mode required')

    release_metadata = data.get('release_metadata')
    if not isinstance(release_metadata, dict):
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, 'release_metadata must be an object')
    if len(release_metadata) > 8:
        raise NovelProductionBatchError(PROD_METADATA_UNBOUNDED, 'release_metadata too many keys')
    if len(canonical_json(release_metadata)) > 4096:
        raise NovelProductionBatchError(PROD_METADATA_UNBOUNDED, 'release_metadata too large')

    slots = data.get('chapter_slots')
    if not isinstance(slots, list) or len(slots) < min_required:
        raise NovelProductionBatchError(
            PROD_REQUEST_INVALID,
            f'at least {min_required} chapter slots are required',
        )

    idem = data.get('idempotency_key')
    if not isinstance(idem, str) or not idem.strip():
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, 'idempotency_key required')
    validate_idempotency_key(idem)

    provided_hash = data.get('request_hash')
    if not isinstance(provided_hash, str) or not provided_hash.strip():
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, 'request_hash required')

    # Reject raw secrets and URLs anywhere in the request text.
    if detect_raw_secret(
        str(data.get('title')), str(data.get('objective')),
        str(data.get('batch_key')), canonical_json(release_metadata),
    ) is not None:
        raise NovelProductionBatchError(PROD_RAW_SECRET, 'raw secret-like value detected')
    if _scan_urls(data):
        raise NovelProductionBatchError(PROD_URL_FORBIDDEN, 'URLs are not allowed in batch requests')

    seen_orders: set[int] = set()
    seen_slot_ids: set[str] = set()
    norm_slots = []
    for idx, raw in enumerate(slots):
        slot = _validate_slot(raw, idx)
        if slot['order'] in seen_orders:
            raise NovelProductionBatchError(
                PROD_SLOT_DUPLICATE_ORDER, f"duplicate slot order: {slot['order']}"
            )
        if slot['slot_id'] in seen_slot_ids:
            raise NovelProductionBatchError(
                PROD_SLOT_DUPLICATE_ID, f"duplicate slot_id: {slot['slot_id']}"
            )
        seen_orders.add(slot['order'])
        seen_slot_ids.add(slot['slot_id'])
        norm_slots.append(slot)

    norm = {
        'schema_version': SCHEMA_REQUEST,
        'batch_id': batch_id,
        'project_id': project_id,
        'batch_key': data['batch_key'],
        'title': data['title'],
        'objective': data['objective'],
        'min_required_chapters': min_required,
        'provider_mode': provider_mode,
        'cost_mode': cost_mode,
        'release_metadata': dict(release_metadata),
        'chapter_slots': norm_slots,
        'idempotency_key': idem,
        'request_hash': provided_hash,
    }
    # Fail closed if the provided hash does not match the canonical payload.
    if compute_batch_request_hash(norm) != provided_hash:
        raise NovelProductionBatchError(
            PROD_REQUEST_INVALID, 'request_hash does not match request content'
        )
    return norm


def _validate_slot(raw: Any, idx: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, f'slot {idx} must be an object')
    extra = set(raw) - _ALLOWED_SLOT_FIELDS
    if extra:
        raise NovelProductionBatchError(
            PROD_UNKNOWN_FIELD, f'unknown slot fields: {sorted(extra)}'
        )
    slot_id = raw.get('slot_id')
    if not isinstance(slot_id, str) or not slot_id.strip():
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, f'slot {idx} slot_id required')
    order = raw.get('order')
    if not isinstance(order, int) or isinstance(order, bool) or order < 1:
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, f'slot {idx} order must be >=1')
    objective = raw.get('chapter_objective')
    if not isinstance(objective, str) or not objective.strip():
        raise NovelProductionBatchError(
            PROD_SLOT_EMPTY_OBJECTIVE, f'slot {slot_id} chapter_objective required'
        )
    required = raw.get('required', True)
    if not isinstance(required, bool):
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, f'slot {slot_id} required must be bool')
    chapter_id = raw.get('chapter_id')
    if not isinstance(chapter_id, str) or not chapter_id.strip():
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, f'slot {slot_id} chapter_id required')
    chapter_number = raw.get('chapter_number')
    if not isinstance(chapter_number, int) or isinstance(chapter_number, bool) or chapter_number < 1:
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, f'slot {slot_id} chapter_number >=1')

    task_id = raw.get('task_id')
    if not isinstance(task_id, str) or not _SLOT_TASK_RE.fullmatch(task_id):
        raise NovelProductionBatchError(
            PROD_REQUEST_INVALID, f'slot {slot_id} task_id must be a unified id'
        )
    ref = raw.get('request_ref')
    if ref is not None:
        if not isinstance(ref, str) or not ref.startswith('group-file:'):
            raise NovelProductionBatchError(
                PROD_SLOT_UNSAFE_REF, f'slot {slot_id} request_ref must be a group-file ref'
            )
        rest = ref[len('group-file:'):]
        if rest.startswith(('/', '\\')) or '..' in rest or ':' in rest:
            raise NovelProductionBatchError(
                PROD_SLOT_UNSAFE_REF, f'slot {slot_id} unsafe request_ref'
            )

    token_budget = int(raw.get('token_budget', 1024))
    min_chars = int(raw.get('min_chars', 200))
    max_chars = int(raw.get('max_chars', 12000))
    if not (128 <= token_budget <= 4096):
        raise NovelProductionBatchError(PROD_REQUEST_INVALID, f'slot {slot_id} token_budget 128..4096')
    if not (200 <= min_chars <= max_chars <= 12000):
        raise NovelProductionBatchError(
            PROD_REQUEST_INVALID, f'slot {slot_id} char range 200<=min<=max<=12000'
        )

    def _strlist(v: Any, field: str) -> list[str]:
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise NovelProductionBatchError(PROD_REQUEST_INVALID, f'slot {slot_id} {field} list[str]')
        return list(v)

    required_story_anchor_ids = _strlist(raw.get('required_story_anchor_ids', []), 'required_story_anchor_ids')
    required_craft_technique_ids = _strlist(raw.get('required_craft_technique_ids', []), 'required_craft_technique_ids')
    forbidden_terms = _strlist(raw.get('forbidden_terms', []), 'forbidden_terms')
    continuity_requirements = _strlist(raw.get('continuity_requirements', []), 'continuity_requirements')
    acceptance_criteria = _strlist(raw.get('acceptance_criteria', []), 'acceptance_criteria')
    if not acceptance_criteria:
        acceptance_criteria = ['本地创作', '无外部发布']

    if detect_raw_secret(objective, chapter_id, canonical_json(acceptance_criteria)) is not None:
        raise NovelProductionBatchError(PROD_RAW_SECRET, f'slot {slot_id} raw secret-like value')

    return {
        'slot_id': slot_id,
        'order': order,
        'chapter_objective': objective,
        'required': required,
        'chapter_id': chapter_id,
        'chapter_number': chapter_number,
        'task_id': task_id,
        'request_ref': ref,
        'token_budget': token_budget,
        'min_chars': min_chars,
        'max_chars': max_chars,
        'required_story_anchor_ids': required_story_anchor_ids,
        'required_craft_technique_ids': required_craft_technique_ids,
        'forbidden_terms': forbidden_terms,
        'continuity_requirements': continuity_requirements,
        'acceptance_criteria': acceptance_criteria,
    }


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class NovelProductionBatchService:
    '''Restart-safe ordered multi-chapter novel production + release service.'''

    def __init__(
        self,
        group_root: Any,
        *,
        economic_ledger: EconomicLedger | None = None,
        draft_provider: Any | None = None,
        publication_service: PublicationService | None = None,
        transport: Any | None = None,
        policy: Any | None = None,
        now: Any = None,
    ) -> None:
        self.group_root = Path(group_root).resolve()
        self.prod_root = self.group_root / 'production'
        self.events_path = self.prod_root / 'events.jsonl'
        self.index_path = self.prod_root / 'index.json'
        self.checkpoints_dir = self.prod_root / 'checkpoints'
        self.batches_dir = self.prod_root / 'batches'
        self.releases_dir = self.prod_root / 'releases'
        self.exports_dir = self.prod_root / 'exports'
        self._lock = threading.RLock()
        self.economic_ledger = economic_ledger or EconomicLedger(self.group_root / 'economics')
        self.draft_provider = draft_provider or DeterministicLocalDraftProvider()
        self.transport = transport or DenyAllShadowTransport()
        self.policy = policy
        self._now = now or _now_iso
        self._events: list[dict[str, object]] = []
        self._idempotency: dict[str, dict[str, object]] = {}
        self._batches: dict[str, dict[str, object]] = {}
        self._releases: dict[str, dict[str, object]] = {}
        if self.events_path.exists():
            self._replay()

        if publication_service is not None:
            self.publication_service = publication_service
        else:
            self.publication_service = PublicationService(
                self.group_root, economic_ledger=self.economic_ledger,
                transport=self.transport,
            )

    @staticmethod
    def _copy_tree_robust(src: Path, dst: Path) -> None:
        '''Recursively copy a directory, skipping unreadable/broken sources.'''
        dst.mkdir(parents=True, exist_ok=True)
        for entry in os.scandir(src):
            s = Path(entry.path)
            d = dst / entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    NovelProductionBatchService._copy_tree_robust(s, d)
                else:
                    shutil.copy2(s, d)
            except OSError:
                continue

    # -- low-level persistence -------------------------------------------- #
    def _append_event(
        self, event_type: str, payload: dict[str, object], *, idempotency_key: str | None = None,
    ) -> dict[str, object]:
        key = idempotency_key or f'{event_type}:{canonical_json(payload)}'
        with self._lock:
            existing = self._idempotency.get(key)
            if existing is not None:
                return dict(existing)
            envelope: dict[str, object] = {
                'schema_version': SCHEMA_EVENT,
                'event_type': event_type,
                'idempotency_key': key,
                'occurred_at': self._now(),
                'payload': payload,
            }
            self.prod_root.mkdir(parents=True, exist_ok=True)
            with self.events_path.open('a', encoding='utf-8', newline='\n') as handle:
                handle.write(canonical_json(envelope) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            self._events.append(envelope)
            self._idempotency[key] = envelope
            return envelope

    def _replay(self) -> None:
        self._events = []
        self._idempotency = {}
        self._batches = {}
        self._releases = {}
        text = self.events_path.read_text(encoding='utf-8')
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                env = json.loads(line)
            except json.JSONDecodeError as exc:
                raise NovelProductionBatchError(
                    PROD_REQUEST_INVALID, f'corrupt events JSONL: {exc}'
                )
            self._events.append(env)
            key = env.get('idempotency_key')
            if isinstance(key, str):
                self._idempotency[key] = env
            self._apply_event(env)

    def _apply_event(self, env: dict[str, object]) -> None:
        etype = str(env.get('event_type'))
        payload = dict(env.get('payload') or {})
        if etype == 'batch_submitted':
            self._batches[payload['batch_id']] = payload['state']
        elif etype == 'slot_attempt':
            bid = payload['batch_id']
            state = self._batches.get(bid)
            if state is None:
                return
            slot = self._find_slot(state, payload['slot_id'])
            if slot is None:
                return
            slot.setdefault('attempts', []).append(payload['attempt'])
            slot['current_attempt'] = payload['attempt']['attempt_id']
        elif etype == 'slot_state_changed':
            bid = payload['batch_id']
            state = self._batches.get(bid)
            if state is None:
                return
            slot = self._find_slot(state, payload['slot_id'])
            if slot is None:
                return
            slot['state'] = payload['state']
            if payload.get('accepted_attempt'):
                slot['accepted_attempt'] = payload['accepted_attempt']
            if payload.get('accepted_version_id'):
                slot['accepted_version_id'] = payload['accepted_version_id']
            if payload.get('accepted_artifact_id'):
                slot['accepted_artifact_id'] = payload['accepted_artifact_id']
            if payload.get('accepted_artifact_sha256'):
                slot['accepted_artifact_sha256'] = payload['accepted_artifact_sha256']
        elif etype == 'batch_state_changed':
            state = self._batches.get(payload['batch_id'])
            if state is not None:
                state['state'] = payload['state']
                state['updated_at'] = env.get('occurred_at')
        elif etype == 'release_assembled':
            self._releases[payload['release_id']] = payload['release']
        elif etype == 'release_withdrawn':
            rel = self._releases.get(payload['release_id'])
            if rel is not None:
                rel['status'] = 'withdrawn'
        elif etype == 'release_published':
            rel = self._releases.get(payload['release_id'])
            if rel is not None:
                rel['published'] = payload.get('published', {})

    @staticmethod
    def _find_slot(state: dict[str, object], slot_id: str) -> dict[str, object] | None:
        for slot in state.get('slots', []):
            if slot.get('slot_id') == slot_id:
                return slot
        return None

    # -- request file + mvp glue ------------------------------------------- #
    def _build_mvp_request(self, batch: dict[str, object], slot: dict[str, object], task_id: str) -> dict[str, object]:
        return {
            'schema_version': NOVEL_MVP_REQUEST_SCHEMA,
            'task_id': task_id,
            'project_id': batch['project_id'],
            'chapter_id': slot['chapter_id'],
            'chapter_number': slot['chapter_number'],
            'chapter_goal': slot['chapter_objective'],
            'token_budget': slot['token_budget'],
            'min_chars': slot['min_chars'],
            'max_chars': slot['max_chars'],
            'required_story_anchor_ids': list(slot['required_story_anchor_ids']),
            'required_craft_technique_ids': list(slot['required_craft_technique_ids']),
            'forbidden_terms': list(slot['forbidden_terms']),
            'continuity_requirements': list(slot['continuity_requirements']),
            'acceptance_criteria': list(slot['acceptance_criteria']),
        }

    def _write_mvp_request(self, task_id: str, mvp_request: dict[str, object]) -> str:
        req_dir = self.group_root / 'runtime' / 'novel-mvp' / 'requests'
        req_dir.mkdir(parents=True, exist_ok=True)
        path = req_dir / f'{_slug(task_id)}.json'
        path.write_text(canonical_json(mvp_request), encoding='utf-8')
        return f'group-file:runtime/novel-mvp/requests/{_slug(task_id)}.json'

    def _read_draft_prose(self, task_id: str) -> str:
        # Deprecated helper retained for backward compatibility; the live MVP
        # status is preferred (see _prose_from_status) because each attempt uses
        # an isolated P0-4 work root.
        work_root = self.prod_root / 'work'
        for batch_dir in work_root.iterdir() if work_root.is_dir() else ():
            for slot_dir in batch_dir.iterdir():
                for attempt_dir in slot_dir.iterdir():
                    try:
                        mvp = NovelMvpService(attempt_dir, economic_ledger=self.economic_ledger)
                        return self._prose_from_status(mvp.status(task_id))
                    except Exception:
                        continue
        return ''

    @staticmethod
    def _prose_from_status(st: dict[str, object]) -> str:
        draft = (st.get('nodes') or {}).get('novel-draft', {}).get('result') or {}
        arts = draft.get('artifacts') or []
        if arts and isinstance(arts[0], dict) and 'prose' in arts[0]:
            return str(arts[0]['prose'])
        return ''

    def _register_shared_facts(
        self, batch: dict[str, object], slot: dict[str, object], task_id: str,
        run_id: str, version_id: str, artifact_id: str, artifact_sha256: str,
        quality_sha256: str,
    ) -> dict[str, object]:
        '''Promote an accepted chapter into the shared group W1-1 ledger.

        P0-4 binds a fixed work/platform idempotency key that can only register
        once per group spine, so the batch re-binds each accepted chapter to the
        shared ledger under production-batch-controlled, per-chapter keys while
        reusing P0-4's deterministic ``version_id`` for release verification.
        '''
        ledger = self.economic_ledger
        if ledger is None:
            return {}
        bid = _slug(batch['batch_id'])
        sid = _slug(slot['slot_id'])
        work_id = f'work-novel-batch-{bid}'
        project_id = str(batch['project_id'])
        chapter_id = str(slot['chapter_id'])
        dept = NOVEL_DEPT_ID
        # The batch-level work fact is registered once (its payload embeds a
        # timestamp, so idempotent re-registration still collides). Reuse the
        # existing entry if already present.
        existing = [e for e in ledger.entries(event_type='work')
                    if e.get('payload', {}).get('work_id') == work_id]
        if existing:
            work_entry = existing[-1]
        else:
            work_entry = ledger.register_work(
                work_id, project_id,
                idempotency_key=f'econ-v2:work:{bid}',
                chapter_id=None, status='active',
                task_id=None, run_id=None, department_id=dept,
            )
        version_entry = ledger.register_version(
            version_id, work_id,
            idempotency_key=f'econ-v2:version:{bid}:{sid}:{task_id}',
            project_id=project_id, chapter_id=chapter_id, version_no=1,
            artifact_id=artifact_id, artifact_sha256=artifact_sha256,
            quality_report_sha256=quality_sha256,
            production_task_id=task_id, production_run_id=run_id,
            status='accepted', task_id=task_id, run_id=run_id, department_id=dept,
        )
        zero_entry = ledger.register_zero_cost(
            idempotency_key=f'econ-v2:zero:{bid}:{sid}:{task_id}',
            category='novel-production', task_id=task_id, run_id=run_id,
            department_id=dept,
        )
        plat_entry = ledger.register_platform(
            'plat-novel-mvp-local-shadow',
            idempotency_key=f'econ-v2:platform:{bid}:{sid}:{task_id}',
            adapter_id='local-shadow/v1', capability_status='shadow',
            published=False, task_id=task_id, run_id=run_id, department_id=dept,
        )
        return {
            'work_entry_id': work_entry.get('entry_id'),
            'version_entry_id': version_entry.get('entry_id'),
            'zero_cost_entry_id': zero_entry.get('entry_id'),
            'platform_entry_id': plat_entry.get('entry_id'),
        }

    def _store_chapter_content(self, batch_id: str, slot_id: str, prose: str) -> tuple[str, int]:
        out_dir = self.batches_dir / batch_id / 'chapters' / slot_id
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / 'content.txt'
        data = prose.encode('utf-8')
        path.write_bytes(data)
        ref = f'group-file:production/batches/{_slug(batch_id)}/chapters/{_slug(slot_id)}/content.txt'
        return ref, len(data)

    def _run_slot_attempt(
        self, batch: dict[str, object], slot: dict[str, object], attempt_task_id: str,
    ) -> dict[str, object]:
        '''Execute one P0-4 attempt for a slot. Returns an attempt record.'''
        mvp_request = self._build_mvp_request(batch, slot, attempt_task_id)

        # Each attempt runs in an isolated P0-4 work root: a private copy of the
        # group's novel knowledge plus its own state spine. This keeps the
        # single dept-novel binding constraint of P0-4 satisfied per attempt
        # (the group spine and economics ledger stay authoritative and shared).
        work_root = (
            self.prod_root / 'work' / _slug(batch['batch_id'])
            / _slug(slot['slot_id']) / _slug(attempt_task_id)
        )
        work_root.mkdir(parents=True, exist_ok=True)
        # Copy the novel knowledge + assembled story bible so the isolated P0-4
        # run can resolve context. Transient run artifacts (e.g. dangling
        # checkpoint links) are skipped. The economics ledger is shared.
        self._copy_tree_robust(self.group_root / 'shared', work_root / 'shared')
        sb_src = self.group_root / 'runtime' / 'novel-studio'
        if sb_src.is_dir():
            self._copy_tree_robust(sb_src, work_root / 'runtime' / 'novel-studio')

        req_dir = work_root / 'runtime' / 'novel-mvp' / 'requests'
        req_dir.mkdir(parents=True, exist_ok=True)
        req_path = req_dir / f'{_slug(attempt_task_id)}.json'
        req_path.write_text(canonical_json(mvp_request), encoding='utf-8')
        ref = f'group-file:runtime/novel-mvp/requests/{_slug(attempt_task_id)}.json'

        attempt_ledger = EconomicLedger(work_root / 'economics')
        mvp = NovelMvpService(
            work_root, economic_ledger=attempt_ledger,
            draft_provider=self.draft_provider,
        )
        run_id = 'run-' + _slug(attempt_task_id)
        attempt_id = 'att-' + sha256_text(f'{batch["batch_id"]}|{slot["slot_id"]}|{attempt_task_id}')[:20]
        attempt: dict[str, object] = {
            'schema_version': SCHEMA_ATTEMPT,
            'attempt_id': attempt_id,
            'slot_id': slot['slot_id'],
            'task_id': attempt_task_id,
            'run_id': run_id,
            'request_ref': ref,
            'status': 'pending',
        }
        try:
            mvp.submit(attempt_task_id, ref)
        except NovelMvpError as exc:
            attempt['status'] = 'blocked'
            attempt['failure_code'] = exc.code
            attempt['failure_detail'] = exc.message
            return attempt
        except Exception as exc:  # noqa: BLE001 - defensive fail-closed
            attempt['status'] = 'blocked'
            attempt['failure_code'] = 'novel-mvp-unexpected'
            attempt['failure_detail'] = str(exc)[:200]
            return attempt

        st = mvp.status(attempt_task_id)
        nodes = st.get('nodes') or {}
        artifact_node = nodes.get('novel-artifact', {})
        artifact_result = artifact_node.get('result') or {}
        artifacts = artifact_result.get('artifacts') or []
        if artifact_node.get('status') == 'completed' and artifacts:
            ar = artifacts[0]
            artifact_id = ar.get('artifact_id')
            draft_sha256 = ar.get('draft_sha256')
            quality_sha256 = ar.get('quality_report_sha256')
            # Read P0-4's version binding from the isolated chapter ledger.
            version_id = ''
            if attempt_ledger is not None:
                vrecs = attempt_ledger.entries(event_type='version', task_id=attempt_task_id)
                if vrecs:
                    version_id = str(vrecs[-1].get('payload', {}).get('version_id', ''))

            # Promote the accepted chapter into the shared group ledger using
            # production-batch-controlled idempotency keys. P0-4 registers into
            # a fixed work/platform key that can bind only once per group, so
            # the batch owns the per-chapter economic binding here (contract §9).
            econ_ids = self._register_shared_facts(
                batch, slot, attempt_task_id, run_id, version_id,
                str(artifact_id), str(draft_sha256), str(quality_sha256),
            )

            prose = self._prose_from_status(st)
            content_ref, content_bytes = self._store_chapter_content(
                batch['batch_id'], slot['slot_id'], prose
            )
            attempt.update({
                'status': 'accepted',
                'artifact_id': artifact_id,
                'version_id': version_id,
                'artifact_sha256': draft_sha256,
                'draft_sha256': draft_sha256,
                'quality_report_sha256': quality_sha256,
                'content_ref': content_ref,
                'content_bytes': content_bytes,
                'quality_evidence_refs': list(ar.get('evidence_ids') or []),
                'economic_entry_ids': econ_ids,
            })
        else:
            quality_node = nodes.get('novel-quality', {})
            qres = quality_node.get('result') or {}
            codes = (
                qres.get('unresolved_codes')
                or artifact_result.get('unresolved_codes')
                or ['novel-mvp-quality-blocked']
            )
            attempt['status'] = 'blocked'
            attempt['failure_code'] = codes[0] if isinstance(codes, list) and codes else 'novel-mvp-quality-blocked'
        return attempt

    def _evaluate_batch_state(self, batch: dict[str, object]) -> str:
        slots = batch.get('slots', [])
        required = [s for s in slots if s.get('required')]
        if any(s.get('state') == 'blocked' for s in required):
            return 'blocked'
        if all(s.get('state') == 'accepted' for s in required):
            return 'ready_for_release'
        return 'running'

    # -- public: submit ---------------------------------------------------- #
    def submit(self, batch_request: dict[str, Any]) -> dict[str, Any]:
        norm = validate_batch_request(batch_request)
        batch_id = norm['batch_id']

        with self._lock:
            if batch_id in self._batches:
                raise NovelProductionBatchError(
                    PROD_BATCH_DUPLICATE, f'batch already submitted: {batch_id}'
                )
            # Persist the immutable request.
            req_dir = self.batches_dir / batch_id
            req_dir.mkdir(parents=True, exist_ok=True)
            (req_dir / 'request.json').write_text(
                canonical_json(norm), encoding='utf-8'
            )
            slots = []
            for s in norm['chapter_slots']:
                stored = dict(s)
                stored['schema_version'] = SCHEMA_SLOT
                stored['state'] = 'pending'
                stored['attempts'] = []
                stored['current_attempt'] = None
                stored['accepted_attempt'] = None
                stored['accepted_version_id'] = None
                stored['accepted_artifact_id'] = None
                stored['accepted_artifact_sha256'] = None
                slots.append(stored)
            state = self._empty_state(norm, slots)
            self._batches[batch_id] = state
            self._append_event(
                'batch_submitted',
                {'batch_id': batch_id, 'state': state},
                idempotency_key=f'batch-submitted:{batch_id}',
            )

        # Execute eligible slots in deterministic order.
        self._execute_incomplete(batch_id)
        state = self.get_state(batch_id)
        return state

    def _empty_state(self, norm: dict[str, Any], slots: list[dict[str, object]]) -> dict[str, object]:
        required = sum(1 for s in slots if s.get('required'))
        return {
            'schema_version': SCHEMA_STATE,
            'batch_id': norm['batch_id'],
            'project_id': norm['project_id'],
            'batch_key': norm['batch_key'],
            'title': norm['title'],
            'objective': norm['objective'],
            'min_required_chapters': norm['min_required_chapters'],
            'provider_mode': norm['provider_mode'],
            'cost_mode': norm['cost_mode'],
            'release_metadata': dict(norm['release_metadata']),
            'slot_count': len(slots),
            'required_slot_count': required,
            'slots': slots,
            'state': 'running',
            'created_at': self._now(),
            'updated_at': self._now(),
        }

    def _execute_incomplete(self, batch_id: str) -> None:
        with self._lock:
            state = self._batches[batch_id]
            ordered = sorted(state['slots'], key=lambda s: int(s['order']))
            for slot in ordered:
                if slot['state'] in ('accepted', 'withdrawn', 'blocked'):
                    continue
                attempt_task_id = slot['task_id']
                slot['state'] = 'running'
                attempt = self._run_slot_attempt(state, slot, attempt_task_id)
                slot.setdefault('attempts', []).append(attempt)
                slot['current_attempt'] = attempt['attempt_id']
                self._append_event(
                    'slot_attempt',
                    {'batch_id': batch_id, 'slot_id': slot['slot_id'], 'attempt': attempt},
                    idempotency_key=f'attempt:{batch_id}:{slot["slot_id"]}:{attempt["attempt_id"]}',
                )
                self._persist_slot_state(batch_id, slot, attempt)
            new_state = self._evaluate_batch_state(state)
            state['state'] = new_state
            state['updated_at'] = self._now()
            self._append_event(
                'batch_state_changed',
                {'batch_id': batch_id, 'state': new_state},
                idempotency_key=f'batch-state:{batch_id}:{new_state}:{state["updated_at"]}',
            )
            self._write_checkpoint(batch_id)

    def _persist_slot_state(
        self, batch_id: str, slot: dict[str, object], attempt: dict[str, object],
    ) -> None:
        if attempt.get('status') == 'accepted':
            slot['state'] = 'accepted'
            slot['accepted_attempt'] = attempt['attempt_id']
            slot['accepted_version_id'] = attempt.get('version_id')
            slot['accepted_artifact_id'] = attempt.get('artifact_id')
            slot['accepted_artifact_sha256'] = attempt.get('artifact_sha256')
        else:
            slot['state'] = 'blocked'
        self._append_event(
            'slot_state_changed',
            {
                'batch_id': batch_id,
                'slot_id': slot['slot_id'],
                'state': slot['state'],
                'accepted_attempt': slot.get('accepted_attempt'),
                'accepted_version_id': slot.get('accepted_version_id'),
                'accepted_artifact_id': slot.get('accepted_artifact_id'),
                'accepted_artifact_sha256': slot.get('accepted_artifact_sha256'),
            },
            idempotency_key=f'slot-state:{batch_id}:{slot["slot_id"]}:{slot["state"]}:{self._now()}',
        )

    def _write_checkpoint(self, batch_id: str) -> None:
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        path = self.checkpoints_dir / f'{_slug(batch_id)}.json'
        path.write_text(canonical_json(self._batches[batch_id]), encoding='utf-8')

    # -- public: resume ---------------------------------------------------- #
    def resume(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            if batch_id not in self._batches:
                raise NovelProductionBatchError(PROD_BATCH_MISSING, f'no batch: {batch_id}')
            state = self._batches[batch_id]
            ordered = sorted(state['slots'], key=lambda s: int(s['order']))
            progress_before = self._accepted_counts(state)
            for slot in ordered:
                if slot['state'] in ('accepted', 'withdrawn'):
                    continue  # reuse; never re-execute accepted siblings
                slot['state'] = 'running'
                attempt_no = len(slot.get('attempts', [])) + 1
                attempt_task_id = f'task-{_slug(batch_id)}-{_slug(slot["slot_id"])}-rev{attempt_no}'
                attempt = self._run_slot_attempt(state, slot, attempt_task_id)
                slot.setdefault('attempts', []).append(attempt)
                slot['current_attempt'] = attempt['attempt_id']
                self._append_event(
                    'slot_attempt',
                    {'batch_id': batch_id, 'slot_id': slot['slot_id'], 'attempt': attempt},
                    idempotency_key=f'attempt:{batch_id}:{slot["slot_id"]}:{attempt["attempt_id"]}',
                )
                self._persist_slot_state(batch_id, slot, attempt)
            new_state = self._evaluate_batch_state(state)
            state['state'] = new_state
            state['updated_at'] = self._now()
            self._append_event(
                'batch_state_changed',
                {'batch_id': batch_id, 'state': new_state},
                idempotency_key=f'batch-state:{batch_id}:{new_state}:{state["updated_at"]}',
            )
            self._write_checkpoint(batch_id)
            result = self.get_state(batch_id)
            result['_resume_reused_accepted'] = progress_before
            return result

    @staticmethod
    def _accepted_counts(state: dict[str, object]) -> dict[str, int]:
        accepted = [s for s in state.get('slots', []) if s.get('state') == 'accepted']
        return {
            'accepted_slots': len(accepted),
            'accepted_versions': sum(1 for s in accepted if s.get('accepted_version_id')),
        }

    # -- public: revise ---------------------------------------------------- #
    def revise_slot(
        self, batch_id: str, slot_id: str, *, reason: str,
        revision_request_ref: str | None = None, idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise NovelProductionBatchError(PROD_REVISION_INVALID, 'reason required')
        idem = idempotency_key or f'revise:{batch_id}:{slot_id}:{sha256_text(reason)}'
        validate_idempotency_key(idem)
        # Idempotency: a repeated revise with the same key is a replay, not a
        # new execution. The slot_revised event uses f'revise:{idem}', so we
        # probe that exact key before re-running any MVP attempt.
        if f'revise:{idem}' in self._idempotency:
            return self.get_state(batch_id)
        with self._lock:
            state = self._batches.get(batch_id)
            if state is None:
                raise NovelProductionBatchError(PROD_BATCH_MISSING, f'no batch: {batch_id}')
            slot = self._find_slot(state, slot_id)
            if slot is None:
                raise NovelProductionBatchError(PROD_SLOT_MISSING, f'no slot: {slot_id}')
            if slot['state'] == 'accepted':
                raise NovelProductionBatchError(
                    PROD_SLOT_ALREADY_ACCEPTED, 'cannot revise an accepted slot'
                )
            if slot['state'] == 'withdrawn':
                raise NovelProductionBatchError(
                    PROD_SLOT_ALREADY_ACCEPTED, 'cannot revise a withdrawn slot'
                )
            if slot['state'] != 'blocked':
                raise NovelProductionBatchError(
                    PROD_SLOT_NOT_BLOCKED, f'slot is not blocked: {slot["state"]}'
                )

            attempt_no = len(slot.get('attempts', [])) + 1
            attempt_task_id = f'task-{_slug(batch_id)}-{_slug(slot_id)}-rev{attempt_no}'
            prior_attempt = slot['current_attempt']
            attempt = self._run_slot_attempt(state, slot, attempt_task_id)

            revision: dict[str, object] = {
                'schema_version': SCHEMA_REVISION,
                'revision_id': 'rev-' + sha256_text(f'{batch_id}|{slot_id}|{attempt_task_id}')[:20],
                'batch_id': batch_id,
                'slot_id': slot_id,
                'prior_attempt_id': prior_attempt,
                'attempt_id': attempt['attempt_id'],
                'task_id': attempt_task_id,
                'reason': reason,
                'revision_request_ref': revision_request_ref,
                'idempotency_key': idem,
                'created_at': self._now(),
            }
            self._append_event(
                'slot_revised', {'batch_id': batch_id, 'slot_id': slot_id, 'revision': revision},
                idempotency_key=f'revise:{idem}',
            )
            slot.setdefault('attempts', []).append(attempt)
            slot['current_attempt'] = attempt['attempt_id']
            self._append_event(
                'slot_attempt',
                {'batch_id': batch_id, 'slot_id': slot_id, 'attempt': attempt},
                idempotency_key=f'attempt:{batch_id}:{slot_id}:{attempt["attempt_id"]}',
            )
            self._persist_slot_state(batch_id, slot, attempt)
            new_state = self._evaluate_batch_state(state)
            state['state'] = new_state
            state['updated_at'] = self._now()
            self._append_event(
                'batch_state_changed',
                {'batch_id': batch_id, 'state': new_state},
                idempotency_key=f'batch-state:{batch_id}:{new_state}:{state["updated_at"]}',
            )
            self._write_checkpoint(batch_id)
            return self.get_state(batch_id)

    # -- public: assemble release ------------------------------------------ #
    def assemble_release_for_audited_chapter(
        self, batch_id: str, *, project_id: str,
        chapter_payload: Mapping[str, object],
        release_metadata: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        '''Register an already-accepted, already-audited chapter artifact/version
        into the shared W1-1 ledger and assemble a local release candidate using
        the EXACT buffered bytes. Performs ZERO draft-provider calls.

        The release reuses the chapter's accepted artifact reference and accepted
        version (derived deterministically from ``artifact_id`` + ``chapter_id``)
        and never re-drafts the chapter. The audited prose bytes are stored
        verbatim; :meth:`assemble_release` then verifies the stored content hash,
        the ledger version fact, and the release manifest hash are all equal.

        ``chapter_payload`` must contain: ``chapter_id``, ``chapter_number``,
        ``artifact_id``, ``artifact_sha256``, ``version_id``,
        ``quality_report_sha256``, ``prose``.
        '''
        chapter_id = str(chapter_payload['chapter_id'])
        chapter_number = int(chapter_payload['chapter_number'])
        artifact_id = str(chapter_payload['artifact_id'])
        artifact_sha256 = str(chapter_payload['artifact_sha256'])
        version_id = str(chapter_payload['version_id'])
        quality_sha256 = str(chapter_payload.get('quality_report_sha256') or '')
        prose = str(chapter_payload['prose'])

        with self._lock:
            # Restart/replay safe: reuse an already-assembled release.
            if self._batches.get(batch_id) is not None:
                rel = next(
                    (r for r in self._releases.values()
                     if r.get('batch_id') == batch_id and r.get('status') != 'withdrawn'),
                    None,
                )
                if rel is not None:
                    return dict(rel)
            # Tamper/stale guard: the audited prose bytes MUST hash to the
            # artifact_sha256 bound during chapter production.
            prose_sha = sha256_text(prose)
            if prose_sha != artifact_sha256:
                raise NovelProductionBatchError(
                    PROD_RELEASE_HASH_MISMATCH,
                    'audited prose hash mismatch; tampered or stale bytes rejected',
                )

            bid = _slug(batch_id)
            slot_id = f'slot-{bid}-ch{chapter_number}'
            sid = _slug(slot_id)
            task_id = f'task-{bid}-audited-{_slug(chapter_id)}'
            run_id = f'run-{_slug(task_id)}'

            # 1) Register the accepted version fact into the shared W1-1 ledger.
            if self.economic_ledger is not None:
                work_id = f'work-novel-batch-{bid}'
                dept = NOVEL_DEPT_ID
                existing_works = [
                    e for e in self.economic_ledger.entries(event_type='work')
                    if e.get('payload', {}).get('work_id') == work_id
                ]
                if not existing_works:
                    self.economic_ledger.register_work(
                        work_id, str(project_id),
                        idempotency_key=f'econ-v2:work:{bid}',
                        chapter_id=None, status='active',
                        task_id=None, run_id=None, department_id=dept,
                    )
                self.economic_ledger.register_version(
                    version_id, work_id,
                    idempotency_key=f'econ-v2:version:{bid}:{sid}:{task_id}',
                    project_id=str(project_id), chapter_id=chapter_id, version_no=1,
                    artifact_id=artifact_id, artifact_sha256=artifact_sha256,
                    quality_report_sha256=quality_sha256,
                    production_task_id=task_id, production_run_id=run_id,
                    status='accepted', task_id=task_id, run_id=run_id, department_id=dept,
                )
                self.economic_ledger.register_zero_cost(
                    idempotency_key=f'econ-v2:zero:{bid}:{sid}:{task_id}',
                    category='novel-production', task_id=task_id, run_id=run_id,
                    department_id=dept,
                )

            # 2) Store the EXACT buffered bytes under the canonical content path.
            content_ref, content_bytes = self._store_chapter_content(batch_id, slot_id, prose)

            # 3) Build the accepted slot + attempt record exactly as assemble_release expects.
            attempt_id = 'att-' + sha256_text(f'{batch_id}|{slot_id}|{task_id}')[:20]
            attempt: dict[str, object] = {
                'schema_version': SCHEMA_ATTEMPT,
                'attempt_id': attempt_id,
                'slot_id': slot_id,
                'task_id': task_id,
                'run_id': run_id,
                'request_ref': None,
                'status': 'accepted',
                'artifact_id': artifact_id,
                'version_id': version_id,
                'artifact_sha256': artifact_sha256,
                'draft_sha256': artifact_sha256,
                'quality_report_sha256': quality_sha256,
                'content_ref': content_ref,
                'content_bytes': content_bytes,
                'quality_evidence_refs': [],
                'economic_entry_ids': {},
            }
            slot: dict[str, object] = {
                'schema_version': SCHEMA_SLOT,
                'slot_id': slot_id,
                'order': 1,
                'chapter_id': chapter_id,
                'chapter_number': chapter_number,
                'task_id': task_id,
                'objective': f'audited-release:{chapter_id}',
                'required': True,
                'state': 'accepted',
                'attempts': [attempt],
                'current_attempt': attempt_id,
                'accepted_attempt': attempt_id,
                'accepted_version_id': version_id,
                'accepted_artifact_id': artifact_id,
                'accepted_artifact_sha256': artifact_sha256,
            }
            # 4) Persist minimal batch state, then delegate the immutable assembly.
            state: dict[str, object] = {
                'schema_version': SCHEMA_STATE,
                'batch_id': batch_id,
                'project_id': str(project_id),
                'batch_key': f'audited-{bid}',
                'title': f'Audited release chapter {chapter_number}',
                'objective': f'Local release candidate for audited chapter {chapter_number}',
                'min_required_chapters': 1,
                'provider_mode': 'audited-bytes',
                'cost_mode': 'zero',
                'release_metadata': dict(release_metadata or {}),
                'slot_count': 1,
                'required_slot_count': 1,
                'slots': [slot],
                'state': 'ready_for_release',
                'created_at': self._now(),
                'updated_at': self._now(),
            }
            self._batches[batch_id] = state
            self._append_event(
                'batch_submitted',
                {'batch_id': batch_id, 'state': state},
                idempotency_key=f'audited-batch-submitted:{batch_id}',
            )

        # 5) Delegate the immutable assembly + hash verification to assemble_release.
        return self.assemble_release(batch_id)

    def assemble_release(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._batches.get(batch_id)
            if state is None:
                raise NovelProductionBatchError(PROD_BATCH_MISSING, f'no batch: {batch_id}')
            slots = sorted(state['slots'], key=lambda s: int(s['order']))
            required = [s for s in slots if s.get('required')]
            if any(s.get('state') != 'accepted' for s in required):
                blocked = [s['slot_id'] for s in required if s.get('state') != 'accepted']
                raise NovelProductionBatchError(
                    PROD_RELEASE_PREMATURE,
                    f'required slots not all accepted: {blocked}',
                )
            # Guard against duplicate release for the same batch.
            for rid, rel in self._releases.items():
                if rel.get('batch_id') == batch_id and rel.get('status') != 'withdrawn':
                    raise NovelProductionBatchError(
                        PROD_RELEASE_DUPLICATE, f'batch already released: {rid}'
                    )

            ordered_accepted = [s for s in slots if s.get('state') == 'accepted']
            seen_versions: set[str] = set()
            constituents: list[dict[str, object]] = []
            prose_parts: list[str] = []
            total_bytes = 0
            for slot in ordered_accepted:
                attempt = slot['current_attempt']
                attempt_rec = next(
                    (a for a in slot.get('attempts', []) if a.get('attempt_id') == attempt), None
                )
                if attempt_rec is None:
                    raise NovelProductionBatchError(
                        PROD_RELEASE_STALE_ATTEMPT, f'current attempt missing for {slot["slot_id"]}'
                    )
                # Re-verify the constituent against the W1-1 ledger.
                version_id = attempt_rec.get('version_id')
                artifact_id = attempt_rec.get('artifact_id')
                artifact_sha256 = attempt_rec.get('artifact_sha256')
                if self.economic_ledger is not None:
                    vers = self.economic_ledger.entries(event_type='version', version_id=version_id)
                    if not vers:
                        raise NovelProductionBatchError(
                            PROD_RELEASE_HASH_MISMATCH, f'no version fact {version_id}'
                        )
                    vpay = vers[-1].get('payload', {})
                    if str(vpay.get('status')) != 'accepted':
                        raise NovelProductionBatchError(
                            PROD_RELEASE_HASH_MISMATCH, 'version not accepted'
                        )
                    if str(vpay.get('accepted_artifact_id')) != str(artifact_id):
                        raise NovelProductionBatchError(
                            PROD_RELEASE_HASH_MISMATCH, 'artifact_id mismatch'
                        )
                    if str(vpay.get('artifact_sha256')) != str(artifact_sha256):
                        raise NovelProductionBatchError(
                            PROD_RELEASE_HASH_MISMATCH, 'artifact_sha256 mismatch'
                        )
                if version_id in seen_versions:
                    raise NovelProductionBatchError(
                        PROD_RELEASE_DUPLICATE_VERSION, f'duplicate version {version_id}'
                    )
                seen_versions.add(version_id)

                # Verify the stored chapter content matches the artifact hash.
                content_ref = attempt_rec.get('content_ref')
                content_path = self._resolve_production_file(content_ref)
                if not content_path.is_file():
                    raise NovelProductionBatchError(
                        PROD_CONTENT_CHANGED, f'chapter content missing: {content_ref}'
                    )
                raw = content_path.read_bytes()
                if sha256_text(raw.decode('utf-8', 'replace')) != artifact_sha256:
                    raise NovelProductionBatchError(
                        PROD_CONTENT_CHANGED, 'chapter content hash changed after acceptance'
                    )

                constituents.append({
                    'slot_id': slot['slot_id'],
                    'order': slot['order'],
                    'chapter_id': slot['chapter_id'],
                    'chapter_number': slot['chapter_number'],
                    'attempt_id': attempt_rec.get('attempt_id'),
                    'task_id': attempt_rec.get('task_id'),
                    'artifact_id': artifact_id,
                    'version_id': version_id,
                    'artifact_sha256': artifact_sha256,
                    'content_ref': content_ref,
                    'content_bytes': attempt_rec.get('content_bytes'),
                    'quality_evidence_refs': list(attempt_rec.get('quality_evidence_refs') or []),
                })
                prose = raw.decode('utf-8', 'replace')
                prose_parts.append(prose)
                total_bytes += len(raw)

            # Materialize the release artifact FIRST, fsync, verify, then manifest.
            release_id = 'rel-' + sha256_text(f'{batch_id}|{state["project_id"]}|{self._now()}')[:20]
            book = '\n\n'.join(prose_parts)
            book_bytes = book.encode('utf-8')
            release_sha256 = sha256_text(book)
            release_dir = self.releases_dir / release_id
            release_dir.mkdir(parents=True, exist_ok=True)
            book_path = release_dir / 'book.txt'
            with book_path.open('w', encoding='utf-8', newline='\n') as handle:
                handle.write(book)
                handle.flush()
                os.fsync(handle.fileno())
            # Verify bytes + hash of the written artifact.
            written = book_path.read_bytes()
            if written != book_bytes or sha256_text(written.decode('utf-8', 'replace')) != release_sha256:
                raise NovelProductionBatchError(
                    PROD_RELEASE_HASH_MISMATCH, 'release artifact write verification failed'
                )

            # Release-level W1-1 work/version fact bound to the release hash.
            release_version_id = 'ver-' + sha256_text(f'release|{release_id}|{batch_id}')[:24]
            if self.economic_ledger is not None:
                self.economic_ledger.register_work(
                    RELEASE_WORK_ID, state['project_id'],
                    idempotency_key=f'econ-v1:release-work:{release_id}',
                    chapter_id=None, status='active',
                    task_id=release_id, run_id=release_id,
                    department_id=PUBLICATION_DEPT_ID,
                )
                self.economic_ledger.register_version(
                    release_version_id, RELEASE_WORK_ID,
                    idempotency_key=f'econ-v1:release-version:{release_id}',
                    project_id=state['project_id'], chapter_id=None, version_no=1,
                    artifact_id=release_id, artifact_sha256=release_sha256,
                    quality_report_sha256='', production_task_id=release_id,
                    production_run_id=release_id, status='accepted',
                    task_id=release_id, run_id=release_id,
                    department_id=PUBLICATION_DEPT_ID,
                )

            release_hash = sha256_text(canonical_json({
                'project_id': state['project_id'],
                'ordered_slot_ids': [c['slot_id'] for c in constituents],
                'accepted_version_ids': [c['version_id'] for c in constituents],
                'artifact_hashes': [c['artifact_sha256'] for c in constituents],
                'release_metadata': dict(state['release_metadata']),
                'release_id': release_id,
            }))

            manifest: dict[str, object] = {
                'schema_version': SCHEMA_RELEASE_MANIFEST,
                'release_id': release_id,
                'batch_id': batch_id,
                'project_id': state['project_id'],
                'work_id': RELEASE_WORK_ID,
                'version_id': release_version_id,
                'title': state['title'],
                'summary': state['objective'],
                'release_metadata': dict(state['release_metadata']),
                'created_at': self._now(),
                'status': 'ready',
                'chapter_count': len(constituents),
                'ordered_slot_ids': [c['slot_id'] for c in constituents],
                'total_bytes': total_bytes,
                'release_sha256': release_sha256,
                'release_content_ref': f'group-file:production/releases/{_slug(release_id)}/book.txt',
                'release_hash': release_hash,
                'chapters': constituents,
            }
            (release_dir / 'manifest.json').write_text(
                canonical_json(manifest), encoding='utf-8'
            )

            release_record: dict[str, object] = {
                'release_id': release_id,
                'batch_id': batch_id,
                'project_id': state['project_id'],
                'title': state['title'],
                'summary': state['objective'],
                'status': 'ready',
                'chapter_count': len(constituents),
                'release_sha256': release_sha256,
                'release_content_bytes': total_bytes,
                'release_hash': release_hash,
                'work_id': RELEASE_WORK_ID,
                'version_id': release_version_id,
                'manifest_ref': f'group-file:production/releases/{_slug(release_id)}/manifest.json',
                'book_ref': f'group-file:production/releases/{_slug(release_id)}/book.txt',
                'created_at': manifest['created_at'],
                'published': None,
            }
            self._releases[release_id] = release_record
            self._append_event(
                'release_assembled',
                {'release_id': release_id, 'batch_id': batch_id, 'release': release_record,
                 'manifest': manifest},
                idempotency_key=f'release-assembled:{release_id}',
            )
            state['state'] = 'released'
            state['updated_at'] = self._now()
            self._append_event(
                'batch_state_changed',
                {'batch_id': batch_id, 'state': 'released'},
                idempotency_key=f'batch-state:{batch_id}:released:{state["updated_at"]}',
            )
            self._write_checkpoint(batch_id)
            return dict(release_record)

    def _resolve_production_file(self, ref: str) -> Path:
        if not isinstance(ref, str) or not ref.startswith('group-file:'):
            raise NovelProductionBatchError(PROD_SLOT_UNSAFE_REF, f'unsafe ref: {ref!r}')
        rel = ref[len('group-file:'):]
        from .root import resolve_portable_path

        return resolve_portable_path(self.group_root, rel, 'production_ref')

    # -- public: publish to W2-1 ------------------------------------------- #
    def publish_release(
        self, release_id: str, *, lease: PlatformAuthorizationLease | None = None,
        transport: Any | None = None, idempotency_key: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            rel = self._releases.get(release_id)
            if rel is None:
                raise NovelProductionBatchError(PROD_RELEASE_MISSING, f'no release: {release_id}')
            if rel.get('status') == 'withdrawn':
                raise NovelProductionBatchError(
                    PROD_RELEASE_MISSING, 'cannot publish a withdrawn release'
                )
            # Load the immutable release manifest.
            manifest_path = self._resolve_production_file(rel['manifest_ref'])
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))

            svc = self.publication_service
            if transport is not None:
                svc = PublicationService(
                    self.group_root, economic_ledger=self.economic_ledger, transport=transport,
                )
            candidate = svc.create_candidate(
                source_kind='release_bundle',
                release_bundle=manifest,
                work_id=RELEASE_WORK_ID,
                version_id=rel['version_id'],
                artifact_id=rel['release_id'],
                artifact_sha256=rel['release_sha256'],
                content_ref=rel['book_ref'],
                content_bytes=int(rel['release_content_bytes']),
                quality_evidence_refs=[c['content_ref'] for c in manifest.get('chapters', [])],
                title=rel['title'],
                summary=rel['summary'],
                idempotency_key=idempotency_key or f'pub-v1:release-candidate:{release_id}',
            )
            if lease is None:
                lease = self._build_lease(
                    candidate['candidate_id'], RELEASE_WORK_ID, rel['release_id'],
                )
            receipt = svc.dispatch_shadow(
                candidate['candidate_id'],
                platform_id='local-shadow',
                account_ref='local-shadow-account',
                mode='shadow',
                action='submit_shadow',
                lease=lease,
                idempotency_key=f'pub-v1:dispatch:{release_id}',
                transport=transport or self.transport,
            )
            rel['published'] = {
                'candidate_id': candidate['candidate_id'],
                'receipt_id': receipt.get('receipt_id'),
                'receipt_status': receipt.get('status'),
                'published': receipt.get('published'),
                'external_effect': receipt.get('external_effect'),
            }
            self._append_event(
                'release_published',
                {'release_id': release_id, 'published': rel['published']},
                idempotency_key=f'release-published:{release_id}',
            )
            return {'release_id': release_id, 'candidate': candidate, 'receipt': receipt}

    def _build_lease(
        self, candidate_id: str, work_id: str, release_id: str,
        platform_id: str = 'local-shadow',
    ) -> PlatformAuthorizationLease:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        not_before = (now - timedelta(minutes=5)).isoformat()
        expires_at = (now + timedelta(days=1)).isoformat()
        lease_sha256 = sha256_text(f'{candidate_id}|{work_id}|{release_id}|{not_before}|{expires_at}')
        return PlatformAuthorizationLease(
            lease_id=f'lease-{_slug(release_id)}',
            platform_id=platform_id,
            account_ref='local-shadow-account',
            allowed_candidate_scope=candidate_id,
            allowed_work_scope=work_id,
            allowed_actions=('submit_shadow',),
            allowed_modes=('shadow', 'dry_run'),
            not_before=not_before,
            expires_at=expires_at,
            max_actions=1,
            lease_sha256=lease_sha256,
        )

    # -- public: withdraw -------------------------------------------------- #
    def withdraw_release(self, release_id: str, *, reason: str | None = None) -> dict[str, object]:
        with self._lock:
            rel = self._releases.get(release_id)
            if rel is None:
                raise NovelProductionBatchError(PROD_RELEASE_MISSING, f'no release: {release_id}')
            if rel.get('status') == 'withdrawn':
                return dict(rel)
            if not isinstance(reason, str) or not reason.strip():
                reason = 'no reason provided'
            rel['status'] = 'withdrawn'
            rel['withdrawn_at'] = self._now()
            rel['withdraw_reason'] = reason
            self._append_event(
                'release_withdrawn',
                {'release_id': release_id, 'status': 'withdrawn', 'reason': reason},
                idempotency_key=f'release-withdrawn:{release_id}:{self._now()}',
            )
            return dict(rel)

    # -- rebuild / export / restore ---------------------------------------- #
    def rebuild(self) -> dict[str, object]:
        with self._lock:
            self._replay()
            for bid, state in self._batches.items():
                self._write_checkpoint(bid)
            index = self._build_index()
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            self.index_path.write_text(canonical_json(index), encoding='utf-8')
            return index

    def _build_index(self) -> dict[str, object]:
        return {
            'schema_version': SCHEMA_SUMMARY,
            'kind': 'novel-production-index',
            'batch_count': len(self._batches),
            'release_count': len(self._releases),
            'batches': {bid: s.get('state') for bid, s in self._batches.items()},
            'releases': {rid: r.get('status') for rid, r in self._releases.items()},
            'written_at': self._now(),
        }

    def export(self, target_dir: Any) -> dict[str, object]:
        target = Path(target_dir).resolve()
        # Reject path-traversal targets up front (defense in depth; the restore
        # side re-validates every file relative path as well).
        normalized = str(target_dir).replace('\\', '/')
        if '..' in normalized.split('/'):
            raise NovelProductionBatchError(
                PROD_EXPORT_TRAVERSAL, f'traversal rejected: {target_dir!r}'
            )
        if target.exists() and any(target.iterdir()):
            raise NovelProductionBatchError(
                PROD_EXPORT_NONEMPTY, 'export target directory must be empty'
            )
        if not self.events_path.exists():
            raise NovelProductionBatchError(PROD_EXPORT_INCOMPLETE, 'nothing to export')
        target.mkdir(parents=True, exist_ok=True)
        export_id = 'export-' + sha256_text(self.events_path.read_text(encoding='utf-8'))[:16]
        files: list[dict[str, object]] = []

        def _record(src: Path) -> None:
            rel = src.relative_to(self.prod_root).as_posix()
            dest = target / rel
            data = src.read_bytes()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            files.append({
                'relative_path': rel,
                'sha256': sha256_text(data.decode('utf-8', 'replace')),
                'bytes': len(data),
            })

        _record(self.events_path)
        if self.index_path.exists():
            _record(self.index_path)
        for cp in sorted(self.checkpoints_dir.glob('*.json')):
            _record(cp)
        for req in sorted((self.batches_dir).rglob('request.json')):
            _record(req)
        for man in sorted(self.releases_dir.rglob('manifest.json')):
            _record(man)
        for bk in sorted(self.releases_dir.rglob('book.txt')):
            _record(bk)
        manifest = {
            'schema_version': SCHEMA_EXPORT,
            'export_id': export_id,
            'source_root': str(self.prod_root),
            'files': files,
            'exported_at': self._now(),
        }
        (target / 'manifest.json').write_text(canonical_json(manifest), encoding='utf-8')
        self._append_event(
            'exported', {'export_id': export_id, 'target': str(target)},
            idempotency_key=f'export:{export_id}',
        )
        return manifest

    @classmethod
    def restore(cls, source_dir: Any, target_root: Any) -> 'NovelProductionBatchService':
        source = Path(source_dir).resolve()
        manifest_path = source / 'manifest.json'
        if not manifest_path.is_file():
            raise NovelProductionBatchError(PROD_EXPORT_INCOMPLETE, 'manifest.json missing')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('schema_version') != SCHEMA_EXPORT:
            raise NovelProductionBatchError(
                PROD_SCHEMA_UNSUPPORTED, 'manifest is not a novel-production-export/v1'
            )
        target = Path(target_root).resolve()
        prod_root = target / 'production'
        prod_root.mkdir(parents=True, exist_ok=True)
        for record in manifest.get('files', []):
            rel = str(record.get('relative_path')).replace('\\', '/')
            if rel.startswith('/') or '..' in rel:
                raise NovelProductionBatchError(
                    PROD_EXPORT_TRAVERSAL, f'traversal rejected: {rel!r}'
                )
            src = source / rel
            if not src.is_file():
                raise NovelProductionBatchError(
                    PROD_EXPORT_INCOMPLETE, f'missing export file: {rel!r}'
                )
            raw = src.read_text(encoding='utf-8')
            if sha256_text(raw) != record.get('sha256'):
                raise NovelProductionBatchError(
                    PROD_RELEASE_HASH_MISMATCH, f'hash mismatch for {rel!r}'
                )
            dest = prod_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(raw, encoding='utf-8', newline='\n')
        svc = cls(target)
        svc.rebuild()
        return svc

    # -- queries ------------------------------------------------------------ #
    def get_state(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._batches.get(batch_id)
            if state is None:
                raise NovelProductionBatchError(PROD_BATCH_MISSING, f'no batch: {batch_id}')
            return dict(state)

    def get_release(self, release_id: str) -> dict[str, Any]:
        with self._lock:
            rel = self._releases.get(release_id)
            if rel is None:
                raise NovelProductionBatchError(PROD_RELEASE_MISSING, f'no release: {release_id}')
            return dict(rel)

    def summary(self) -> dict[str, object]:
        with self._lock:
            return self._build_index()

    def list_batches(self) -> list[str]:
        with self._lock:
            return list(self._batches.keys())
