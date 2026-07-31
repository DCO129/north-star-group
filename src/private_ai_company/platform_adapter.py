'''Platform adapter shadow spike: platform-neutral adapter boundary.

W2-1 contract (20260724-w2-1-platform-adapter-shadow-spike-contract.md).

This module defines the platform-neutral adapter protocol, a deterministic
local shadow adapter backed by an injected fake transport, versioned policy /
capability / authorization-lease / request / attempt / checkpoint / receipt /
state / export schemas, and failure classification. It contains **no network,
browser, SDK, account, credential, or external-effect code**. The production
default transport is deny-all; tests inject a deterministic fake transport.
'''

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from ._hashing import sha256_text

# --------------------------------------------------------------------------- #
# Frozen schema versions (contract §6)
# --------------------------------------------------------------------------- #
SCHEMA_CANDIDATE = 'publication-candidate/v1'
SCHEMA_POLICY = 'platform-adapter-policy/v1'
SCHEMA_CAPABILITY = 'platform-capability/v1'
SCHEMA_LEASE = 'platform-authorization-lease/v1'
SCHEMA_REQUEST = 'platform-action-request/v1'
SCHEMA_ATTEMPT = 'platform-action-attempt/v1'
SCHEMA_CHECKPOINT = 'platform-recovery-checkpoint/v1'
SCHEMA_RECEIPT = 'platform-shadow-receipt/v1'
SCHEMA_STATE = 'platform-adapter-state/v1'
SCHEMA_EXPORT = 'platform-adapter-export/v1'

PUBLICATION_SCHEMAS = {
    SCHEMA_CANDIDATE, SCHEMA_POLICY, SCHEMA_CAPABILITY, SCHEMA_LEASE,
    SCHEMA_REQUEST, SCHEMA_ATTEMPT, SCHEMA_CHECKPOINT, SCHEMA_RECEIPT,
    SCHEMA_STATE, SCHEMA_EXPORT,
}

# --------------------------------------------------------------------------- #
# Error codes (stable, blocking)
# --------------------------------------------------------------------------- #
PLATFORM_MODE_EXTERNAL_FORBIDDEN = 'platform-mode-external-forbidden'
PLATFORM_LIVE_MODE_FORBIDDEN = 'platform-live-mode-forbidden'
PLATFORM_CAPABILITY_UNAVAILABLE = 'platform-capability-unavailable'
PLATFORM_CAPABILITY_UNKNOWN = 'platform-capability-unknown'
PLATFORM_LEASE_MISSING = 'platform-lease-missing'
PLATFORM_LEASE_EXPIRED = 'platform-lease-expired'
PLATFORM_LEASE_NOT_BEFORE = 'platform-lease-not-before'
PLATFORM_LEASE_SCOPE_MISMATCH = 'platform-lease-scope-mismatch'
PLATFORM_LEASE_MODE_MISMATCH = 'platform-lease-mode-mismatch'
PLATFORM_LEASE_ACTION_MISMATCH = 'platform-lease-action-mismatch'
PLATFORM_LEASE_OVER_BROAD = 'platform-lease-over-broad'
PLATFORM_LEASE_CAP_EXCEEDED = 'platform-lease-action-cap-exceeded'
PLATFORM_ACTION_NOT_ALLOWED = 'platform-action-not-allowed'
PLATFORM_PAYLOAD_OVERSIZED = 'platform-payload-oversized'
PLATFORM_RAW_SECRET = 'platform-raw-secret'
PLATFORM_TRAVERSAL = 'platform-path-traversal'
PLATFORM_IDEMPOTENCY_CONFLICT = 'platform-idempotency-conflict'
PLATFORM_RATE_LIMIT_EXCEEDED = 'platform-rate-limit-exceeded'
PLATFORM_STATE_MISSING = 'platform-state-missing'
PLATFORM_STATE_CORRUPT = 'platform-state-corrupt'
PLATFORM_UNSUPPORTED_SCHEMA = 'platform-unsupported-schema'
PLATFORM_TRANSPORT_DENIED = 'platform-transport-denied'
PLATFORM_RETRY_EXHAUSTED = 'platform-retry-exhausted'
PLATFORM_MALFORMED_RECEIPT = 'platform-malformed-receipt'
PLATFORM_CANDIDATE_WITHDRAWN = 'platform-candidate-withdrawn'
PLATFORM_CANDIDATE_MISSING = 'platform-candidate-missing'
PLATFORM_RECEIPT_REPLAY = 'platform-receipt-replay'
PLATFORM_VERSION_BINDING = 'platform-version-binding-mismatch'

# --------------------------------------------------------------------------- #
# Failure classes
# --------------------------------------------------------------------------- #
FAILURE_NONE = 'none'
FAILURE_TRANSIENT = 'transient'
FAILURE_TERMINAL = 'terminal'

RETRYABLE_FAILURE_CLASSES = frozenset({FAILURE_TRANSIENT})
TERMINAL_FAILURE_CLASSES = frozenset({FAILURE_TERMINAL})

# Allowed modes and actions (contract §3, §6.2)
ALLOWED_MODES = ('shadow', 'dry_run')
ALLOWED_ACTIONS = ('validate_candidate', 'submit_shadow', 'withdraw_shadow')

# Allowed shadow receipt terminal statuses
RECEIPT_STATUSES = ('shadow_accepted', 'shadow_rejected', 'blocked', 'withdrawn')

# Candidate / action states (contract §6.4)
CANDIDATE_READY = 'ready'
CANDIDATE_WITHDRAWN = 'withdrawn'

ACTION_PREPARED = 'prepared'
ACTION_DISPATCHED = 'dispatched'
ACTION_RETRY_WAIT = 'retry_wait'
ACTION_SHADOW_ACCEPTED = 'shadow_accepted'
ACTION_SHADOW_REJECTED = 'shadow_rejected'
ACTION_BLOCKED = 'blocked'
ACTION_WITHDRAWN = 'withdrawn'

# State transition graph (contract §6.4) — no transition produces `published`.
ALLOWED_ACTION_TRANSITIONS: dict[str, set[str]] = {
    ACTION_PREPARED: {ACTION_DISPATCHED, ACTION_BLOCKED, ACTION_WITHDRAWN},
    ACTION_DISPATCHED: {
        ACTION_SHADOW_ACCEPTED, ACTION_SHADOW_REJECTED, ACTION_RETRY_WAIT,
        ACTION_BLOCKED, ACTION_WITHDRAWN,
    },
    ACTION_RETRY_WAIT: {
        ACTION_DISPATCHED, ACTION_SHADOW_ACCEPTED, ACTION_SHADOW_REJECTED,
        ACTION_BLOCKED, ACTION_WITHDRAWN,
    },
    ACTION_SHADOW_ACCEPTED: set(),
    ACTION_SHADOW_REJECTED: set(),
    ACTION_BLOCKED: set(),
    ACTION_WITHDRAWN: set(),
}

_ID_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,127}$')
_IDEMPOTENCY_RE = re.compile(r'^[A-Za-z0-9._:/-]{8,256}$')
_SECRET_HINT_RE = re.compile(r'(?i)(secret|token|credential|password|api[_-]?key|sk-[a-z0-9]{8,}|AKIA[0-9A-Z]{8,})')
_REF_TRAVERSAL_RE = re.compile(r'(\.\.|^\s*/|\\\\|^\s*[A-Za-z]:)')

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class PlatformAdapterError(Exception):
    '''Bounded platform-adapter error carrying a stable blocking code.'''

    def __init__(self, code: str, message: str = '') -> None:
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def validate_id(value: str, *, kind: str = 'id') -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise PlatformAdapterError(
            PLATFORM_STATE_CORRUPT, f'{kind} must be a portable identifier: {value!r}'
        )
    return value


def validate_idempotency_key(value: str) -> str:
    if not isinstance(value, str) or not _IDEMPOTENCY_RE.fullmatch(value):
        raise PlatformAdapterError(
            PLATFORM_IDEMPOTENCY_CONFLICT,
            f'idempotency key must match {_IDEMPOTENCY_RE.pattern}: {value!r}',
        )
    return value


def validate_local_ref(value: str, *, kind: str = 'ref') -> str:
    '''Target-relative local reference only; absolute paths / traversal block.'''
    if not isinstance(value, str) or not value.strip():
        raise PlatformAdapterError(PLATFORM_TRAVERSAL, f'{kind} must be a non-empty string')
    if value.startswith('group-file:'):
        rest = value[len('group-file:'):]
    else:
        rest = value
    if rest.startswith(('/', '\\')) or '..' in rest or ':' in rest:
        raise PlatformAdapterError(
            PLATFORM_TRAVERSAL, f'{kind} must be a target-relative reference: {value!r}'
        )
    return value


def detect_raw_secret(*values: object) -> str | None:
    '''Return the offending fragment if a raw-secret-like value is detected.'''
    for value in values:
        if not isinstance(value, str):
            continue
        if _SECRET_HINT_RE.search(value):
            return value
    return None


# --------------------------------------------------------------------------- #
# Transport boundary (injected; production default is deny-all)
# --------------------------------------------------------------------------- #
class ShadowTransport(Protocol):
    def call(
        self,
        *,
        adapter_id: str,
        platform_id: str,
        account_ref: str,
        action: str,
        mode: str,
        payload: Mapping[str, object],
        request_id: str,
    ) -> dict[str, object]:
        '''Return a normalized transport result.

        Result keys: status ('ok' | 'rejected' | 'error'), transport_call_id,
        failure_class ('none' | 'transient' | 'terminal'), evidence (sanitized).
        '''
        ...


_OUTCOME_RESULTS: dict[str, dict[str, str]] = {
    'success': {'status': 'ok', 'failure_class': FAILURE_NONE, 'reason': 'shadow-accepted'},
    'timeout': {'status': 'error', 'failure_class': FAILURE_TRANSIENT, 'reason': 'timeout'},
    'rate_limit': {'status': 'error', 'failure_class': FAILURE_TRANSIENT, 'reason': 'rate-limited'},
    'malformed': {'status': 'error', 'failure_class': FAILURE_TRANSIENT, 'reason': 'malformed-response'},
    'auth': {'status': 'error', 'failure_class': FAILURE_TERMINAL, 'reason': 'auth-failed'},
    'connection': {'status': 'error', 'failure_class': FAILURE_TRANSIENT, 'reason': 'connection-reset'},
    'permanent_reject': {'status': 'rejected', 'failure_class': FAILURE_TERMINAL, 'reason': 'policy-rejected'},
}


class FakeShadowTransport:
    '''Deterministic, injected fake transport. Never touches a socket/browser.

    Tests push outcomes (or set a default) and count calls. Outcomes are one of
    the keys of ``_OUTCOME_RESULTS`` plus ``'success'``.
    '''

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._queue: list[str] = []
        self.default_outcome: str = 'success'
        self._lock = threading.Lock()

    def queue(self, outcome: str) -> None:
        if outcome not in _OUTCOME_RESULTS:
            raise PlatformAdapterError(
                PLATFORM_UNSUPPORTED_SCHEMA, f'unknown fake outcome: {outcome!r}'
            )
        with self._lock:
            self._queue.append(outcome)

    def clear_queue(self) -> None:
        with self._lock:
            self._queue.clear()

    def set_default(self, outcome: str) -> None:
        if outcome not in _OUTCOME_RESULTS:
            raise PlatformAdapterError(
                PLATFORM_UNSUPPORTED_SCHEMA, f'unknown fake outcome: {outcome!r}'
            )
        with self._lock:
            self.default_outcome = outcome

    def call(
        self,
        *,
        adapter_id: str,
        platform_id: str,
        account_ref: str,
        action: str,
        mode: str,
        payload: Mapping[str, object],
        request_id: str,
    ) -> dict[str, object]:
        with self._lock:
            outcome = self._queue.pop(0) if self._queue else self.default_outcome
            call_index = len(self.calls)
            self.calls.append({
                'request_id': request_id,
                'adapter_id': adapter_id,
                'platform_id': platform_id,
                'account_ref': account_ref,
                'action': action,
                'mode': mode,
                'outcome': outcome,
                'call_index': call_index,
            })
        spec = _OUTCOME_RESULTS[outcome]
        call_id = 'tcall-' + sha256_text(f'{request_id}|{call_index}|{outcome}')[:16]
        return {
            'status': spec['status'],
            'transport_call_id': call_id,
            'failure_class': spec['failure_class'],
            'evidence': {'outcome': outcome, 'reason': spec['reason']},
        }


class DenyAllShadowTransport:
    '''Production default. Blocks every shadow call before any external effect.'''

    def call(self, **_: object) -> dict[str, object]:
        raise PlatformAdapterError(
            PLATFORM_TRANSPORT_DENIED,
            'production deny-all transport blocks all shadow calls',
        )


def classify_transport_failure(result: Mapping[str, object]) -> str:
    raw = result.get('failure_class')
    if raw in (FAILURE_TRANSIENT, FAILURE_TERMINAL, FAILURE_NONE):
        return str(raw)
    return FAILURE_TERMINAL


# --------------------------------------------------------------------------- #
# Policy / capability / lease
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlatformCapability:
    capability_id: str
    status: str  # available | unavailable | unknown

    def to_dict(self) -> dict[str, object]:
        return {
            'schema_version': SCHEMA_CAPABILITY,
            'capability_id': self.capability_id,
            'status': self.status,
        }

    @staticmethod
    def from_dict(value: Mapping[str, object]) -> 'PlatformCapability':
        return PlatformCapability(
            capability_id=str(value.get('capability_id') or ''),
            status=str(value.get('status') or 'unknown'),
        )


@dataclass(frozen=True)
class PlatformPolicy:
    platform_id: str
    enabled_modes: tuple[str, ...] = ('shadow', 'dry_run')
    allowed_actions: tuple[str, ...] = ALLOWED_ACTIONS
    capabilities: tuple[PlatformCapability, ...] = ()
    per_account_window_actions: int = 3
    per_account_window_seconds: int = 3600
    max_attempts_per_action: int = 5
    max_payload_bytes: int = 5_000_000
    allowed_content_kinds: tuple[str, ...] = ('novel-chapter',)
    retry_ceiling: int = 4
    retryable_failure_classes: tuple[str, ...] = ('transient',)
    required_scopes: tuple[str, ...] = ('publication:shadow',)
    optional_secret_ref_names: tuple[str, ...] = ()
    status: str = 'active'
    not_before: str | None = None
    expires_at: str | None = None
    policy_version: str = '1'

    def to_dict(self) -> dict[str, object]:
        return {
            'schema_version': SCHEMA_POLICY,
            'platform_id': self.platform_id,
            'enabled_modes': list(self.enabled_modes),
            'allowed_actions': list(self.allowed_actions),
            'capabilities': [c.to_dict() for c in self.capabilities],
            'per_account_window_actions': self.per_account_window_actions,
            'per_account_window_seconds': self.per_account_window_seconds,
            'max_attempts_per_action': self.max_attempts_per_action,
            'max_payload_bytes': self.max_payload_bytes,
            'allowed_content_kinds': list(self.allowed_content_kinds),
            'retry_ceiling': self.retry_ceiling,
            'retryable_failure_classes': list(self.retryable_failure_classes),
            'required_scopes': list(self.required_scopes),
            'optional_secret_ref_names': list(self.optional_secret_ref_names),
            'status': self.status,
            'not_before': self.not_before,
            'expires_at': self.expires_at,
            'policy_version': self.policy_version,
        }

    @staticmethod
    def from_dict(value: Mapping[str, object]) -> 'PlatformPolicy':
        raw_caps = value.get('capabilities') or []
        caps = tuple(
            PlatformCapability.from_dict(c) for c in raw_caps
            if isinstance(c, Mapping)
        )
        return PlatformPolicy(
            platform_id=str(value.get('platform_id') or ''),
            enabled_modes=tuple(value.get('enabled_modes') or ('shadow', 'dry_run')),
            allowed_actions=tuple(value.get('allowed_actions') or ALLOWED_ACTIONS),
            capabilities=caps,
            per_account_window_actions=int(value.get('per_account_window_actions', 3)),
            per_account_window_seconds=int(value.get('per_account_window_seconds', 3600)),
            max_attempts_per_action=int(value.get('max_attempts_per_action', 5)),
            max_payload_bytes=int(value.get('max_payload_bytes', 5_000_000)),
            allowed_content_kinds=tuple(value.get('allowed_content_kinds') or ('novel-chapter',)),
            retry_ceiling=int(value.get('retry_ceiling', 4)),
            retryable_failure_classes=tuple(value.get('retryable_failure_classes') or ('transient',)),
            required_scopes=tuple(value.get('required_scopes') or ('publication:shadow',)),
            optional_secret_ref_names=tuple(value.get('optional_secret_ref_names') or ()),
            status=str(value.get('status') or 'active'),
            not_before=value.get('not_before'),
            expires_at=value.get('expires_at'),
            policy_version=str(value.get('policy_version') or '1'),
        )

    def capability_status(self, capability_id: str) -> str:
        for cap in self.capabilities:
            if cap.capability_id == capability_id:
                return cap.status
        return 'unknown'

    def allows_mode(self, mode: str) -> bool:
        return mode in self.enabled_modes

    def allows_action(self, action: str) -> bool:
        return action in self.allowed_actions

    def retryable(self, failure_class: str) -> bool:
        return failure_class in self.retryable_failure_classes


@dataclass(frozen=True)
class PlatformAuthorizationLease:
    lease_id: str
    platform_id: str
    account_ref: str
    allowed_candidate_scope: str
    allowed_work_scope: str
    allowed_actions: tuple[str, ...]
    allowed_modes: tuple[str, ...]
    not_before: str
    expires_at: str
    max_actions: int
    lease_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            'schema_version': SCHEMA_LEASE,
            'lease_id': self.lease_id,
            'platform_id': self.platform_id,
            'account_ref': self.account_ref,
            'allowed_candidate_scope': self.allowed_candidate_scope,
            'allowed_work_scope': self.allowed_work_scope,
            'allowed_actions': list(self.allowed_actions),
            'allowed_modes': list(self.allowed_modes),
            'not_before': self.not_before,
            'expires_at': self.expires_at,
            'max_actions': self.max_actions,
            'lease_sha256': self.lease_sha256,
        }

    @staticmethod
    def from_dict(value: Mapping[str, object]) -> 'PlatformAuthorizationLease':
        return PlatformAuthorizationLease(
            lease_id=str(value.get('lease_id') or ''),
            platform_id=str(value.get('platform_id') or ''),
            account_ref=str(value.get('account_ref') or ''),
            allowed_candidate_scope=str(value.get('allowed_candidate_scope') or '*'),
            allowed_work_scope=str(value.get('allowed_work_scope') or '*'),
            allowed_actions=tuple(value.get('allowed_actions') or ALLOWED_ACTIONS),
            allowed_modes=tuple(value.get('allowed_modes') or ('shadow', 'dry_run')),
            not_before=str(value.get('not_before') or ''),
            expires_at=str(value.get('expires_at') or ''),
            max_actions=int(value.get('max_actions', 1)),
            lease_sha256=str(value.get('lease_sha256') or ''),
        )

    def is_scope_compatible(self, candidate_id: str, work_id: str) -> bool:
        cand_ok = (
            self.allowed_candidate_scope == '*'
            or self.allowed_candidate_scope == candidate_id
        )
        work_ok = (
            self.allowed_work_scope == '*'
            or self.allowed_work_scope == work_id
        )
        return cand_ok and work_ok

    def is_action_compatible(self, action: str) -> bool:
        return action in self.allowed_actions

    def is_mode_compatible(self, mode: str) -> bool:
        return mode in self.allowed_modes

    def is_active(self, now_iso: str) -> bool:
        if not self.not_before or not self.expires_at:
            return False
        return self.not_before <= now_iso <= self.expires_at


# --------------------------------------------------------------------------- #
# Adapter protocol + registry + local implementation
# --------------------------------------------------------------------------- #
class PlatformAdapter(Protocol):
    adapter_id: str
    version: str

    def capabilities(self) -> list[str]: ...

    def validate(
        self,
        request: Mapping[str, object],
        candidate: Mapping[str, object],
        policy: PlatformPolicy,
        lease: PlatformAuthorizationLease | None,
        *,
        now_iso: str,
    ) -> None:
        '''Raise PlatformAdapterError on any blocking condition.'''

    def dispatch_shadow(
        self,
        request: Mapping[str, object],
        candidate_ref: Mapping[str, object],
        transport: ShadowTransport,
    ) -> dict[str, object]:
        '''Return a normalized dispatch result dict.'''

    def classify_failure(self, error: BaseException) -> str: ...


class PlatformAdapterRegistry:
    '''Owns adapter lookup and duplicate registration checks.'''

    def __init__(self) -> None:
        self._adapters: dict[str, PlatformAdapter] = {}

    def register(self, adapter: PlatformAdapter) -> None:
        if adapter.adapter_id in self._adapters:
            raise PlatformAdapterError(
                PLATFORM_STATE_CORRUPT,
                f'duplicate adapter registration: {adapter.adapter_id!r}',
            )
        self._adapters[adapter.adapter_id] = adapter

    def get(self, adapter_id: str) -> PlatformAdapter:
        adapter = self._adapters.get(adapter_id)
        if adapter is None:
            raise PlatformAdapterError(
                PLATFORM_STATE_MISSING, f'no adapter registered: {adapter_id!r}'
            )
        return adapter

    def all(self) -> list[PlatformAdapter]:
        return list(self._adapters.values())


class LocalShadowPlatformAdapter:
    '''The only registered adapter in W2-1. Platform-neutral, local, shadow-only.'''

    adapter_id = 'local-shadow/v1'
    version = '1'

    def capabilities(self) -> list[str]:
        return ['submit_shadow', 'validate_candidate', 'withdraw_shadow']

    # -- validation (fail closed before transport) ------------------------- #
    def validate(
        self,
        request: Mapping[str, object],
        candidate: Mapping[str, object],
        policy: PlatformPolicy,
        lease: PlatformAuthorizationLease | None,
        *,
        now_iso: str,
    ) -> None:
        mode = str(request.get('mode') or '')
        action = str(request.get('action') or '')

        # Only shadow / dry_run modes exist.
        if mode not in ALLOWED_MODES:
            raise PlatformAdapterError(
                PLATFORM_LIVE_MODE_FORBIDDEN,
                f'external-effect mode is forbidden: {mode!r}',
            )
        if not policy.allows_mode(mode):
            raise PlatformAdapterError(
                PLATFORM_MODE_EXTERNAL_FORBIDDEN,
                f'policy does not enable mode: {mode!r}',
            )
        if not policy.allows_action(action):
            raise PlatformAdapterError(
                PLATFORM_ACTION_NOT_ALLOWED, f'action not allowed: {action!r}'
            )
        # Required capability must be available; unknown/unavailable blocks.
        capability_id = str(request.get('capability_id') or 'submit_shadow')
        cap_status = policy.capability_status(capability_id)
        if cap_status == 'unavailable':
            raise PlatformAdapterError(
                PLATFORM_CAPABILITY_UNAVAILABLE,
                f'capability unavailable: {capability_id!r}',
            )
        if cap_status == 'unknown':
            raise PlatformAdapterError(
                PLATFORM_CAPABILITY_UNKNOWN,
                f'capability unknown and not optimistically allowed: {capability_id!r}',
            )
        # Authorization lease is explicit, scoped, expiring, action-specific.
        if lease is None:
            raise PlatformAdapterError(
                PLATFORM_LEASE_MISSING, 'authorization lease is required'
            )
        if lease.platform_id != str(policy.platform_id):
            raise PlatformAdapterError(
                PLATFORM_LEASE_SCOPE_MISMATCH, 'lease platform mismatch'
            )
        if not lease.is_active(now_iso):
            raise PlatformAdapterError(
                PLATFORM_LEASE_EXPIRED, 'lease expired or not yet valid'
            )
        if not lease.is_scope_compatible(
            str(candidate.get('candidate_id') or ''),
            str(candidate.get('work_id') or ''),
        ):
            raise PlatformAdapterError(
                PLATFORM_LEASE_SCOPE_MISMATCH, 'lease scope mismatch'
            )
        if not lease.is_mode_compatible(mode):
            raise PlatformAdapterError(
                PLATFORM_LEASE_MODE_MISMATCH, 'lease mode mismatch'
            )
        if not lease.is_action_compatible(action):
            raise PlatformAdapterError(
                PLATFORM_LEASE_ACTION_MISMATCH, 'lease action mismatch'
            )
        # Candidate must be ready (not withdrawn).
        if str(candidate.get('status')) == CANDIDATE_WITHDRAWN:
            raise PlatformAdapterError(
                PLATFORM_CANDIDATE_WITHDRAWN, 'candidate already withdrawn'
            )
        # Payload size guard.
        content_bytes = int(candidate.get('content_bytes') or 0)
        if content_bytes > policy.max_payload_bytes:
            raise PlatformAdapterError(
                PLATFORM_PAYLOAD_OVERSIZED,
                f'payload {content_bytes} exceeds max {policy.max_payload_bytes}',
            )
        # No raw secret may enter the request / candidate.
        offending = detect_raw_secret(
            str(request.get('secret_ref') or ''),
            str(request.get('account_ref') or ''),
            str(candidate.get('title') or ''),
            str(candidate.get('summary') or ''),
        )
        if offending is not None:
            raise PlatformAdapterError(
                PLATFORM_RAW_SECRET, 'raw secret-like value detected'
            )

    # -- dispatch ----------------------------------------------------------- #
    def dispatch_shadow(
        self,
        request: Mapping[str, object],
        candidate_ref: Mapping[str, object],
        transport: ShadowTransport,
    ) -> dict[str, object]:
        action_id = str(request.get('action_id') or '')
        attempt_no = int(request.get('attempt_no') or 1)
        started_at = str(request.get('now_iso') or '')
        transport_result = transport.call(
            adapter_id=self.adapter_id,
            platform_id=str(request.get('platform_id') or ''),
            account_ref=str(request.get('account_ref') or ''),
            action=str(request.get('action') or ''),
            mode=str(request.get('mode') or ''),
            payload=dict(candidate_ref),
            request_id=action_id,
        )
        failure_class = classify_transport_failure(transport_result)
        attempt = {
            'schema_version': SCHEMA_ATTEMPT,
            'attempt_id': f'att-{sha256_text(action_id + "|" + str(attempt_no))[:20]}',
            'action_id': action_id,
            'attempt_no': attempt_no,
            'adapter_id': self.adapter_id,
            'adapter_version': self.version,
            'transport_call_id': str(transport_result.get('transport_call_id') or ''),
            'started_at': started_at,
            'ended_at': started_at,
            'failure_class': failure_class,
            'retryable': failure_class in RETRYABLE_FAILURE_CLASSES,
            'evidence': {
                'outcome': transport_result.get('evidence', {}).get('outcome'),
                'reason': transport_result.get('evidence', {}).get('reason'),
                'platform_id': str(request.get('platform_id') or ''),
                'account_ref': str(request.get('account_ref') or ''),
            },
        }
        status = str(transport_result.get('status') or 'error')
        if status == 'ok':
            outcome = 'accepted'
        elif status == 'rejected':
            outcome = 'rejected'
        else:
            outcome = 'error'
        return {
            'outcome': outcome,
            'failure_class': failure_class,
            'attempt': attempt,
            'transport_call_id': str(transport_result.get('transport_call_id') or ''),
        }

    def classify_failure(self, error: BaseException) -> str:
        if isinstance(error, PlatformAdapterError):
            code = error.code
            if code in (
                PLATFORM_TRANSPORT_DENIED, PLATFORM_RAW_SECRET,
                PLATFORM_LIVE_MODE_FORBIDDEN, PLATFORM_MODE_EXTERNAL_FORBIDDEN,
                PLATFORM_LEASE_EXPIRED, PLATFORM_LEASE_SCOPE_MISMATCH,
                PLATFORM_LEASE_MODE_MISMATCH, PLATFORM_LEASE_ACTION_MISMATCH,
                PLATFORM_CAPABILITY_UNAVAILABLE, PLATFORM_CAPABILITY_UNKNOWN,
                PLATFORM_ACTION_NOT_ALLOWED, PLATFORM_PAYLOAD_OVERSIZED,
                PLATFORM_TRAVERSAL, PLATFORM_STATE_CORRUPT,
                PLATFORM_UNSUPPORTED_SCHEMA, PLATFORM_VERSION_BINDING,
                PLATFORM_CANDIDATE_WITHDRAWN,
            ):
                return FAILURE_TERMINAL
        return FAILURE_TERMINAL


def build_default_registry() -> PlatformAdapterRegistry:
    registry = PlatformAdapterRegistry()
    registry.register(LocalShadowPlatformAdapter())
    return registry
