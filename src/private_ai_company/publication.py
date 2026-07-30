'''Publication service for the W2-1 platform-adapter shadow spike.

Owns the local publication control plane: immutable candidates, shadow action
requests, attempts, recovery checkpoints, hash-bound shadow receipts, and the
binding to one W1-1 economic platform fact per accepted shadow action.

Storage layout (contract §5):
    publication/events.jsonl                 (authoritative, append-only)
    publication/index.json                   (derived, rebuildable)
    publication/candidates/{id}/manifest.json (immutable)
    publication/candidates/{id}/payload.ref.json (immutable)
    publication/checkpoints/{action_id}.json
    publication/exports/{export_id}/manifest.json

No network, browser, credential resolution, payment, publication, or positive
revenue occurs. Raw novel content never enters the ledger, receipt, checkpoint,
or API list output — only IDs, local refs, byte counts, and hashes.
'''

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .platform_adapter import (
    ALLOWED_ACTIONS,
    ALLOWED_MODES,
    ACTION_BLOCKED,
    ACTION_DISPATCHED,
    ACTION_PREPARED,
    ACTION_RETRY_WAIT,
    ACTION_SHADOW_ACCEPTED,
    ACTION_SHADOW_REJECTED,
    ACTION_WITHDRAWN,
    CANDIDATE_READY,
    CANDIDATE_WITHDRAWN,
    FAILURE_TERMINAL,
    FAILURE_TRANSIENT,
    PLATFORM_CANDIDATE_MISSING,
    PLATFORM_CANDIDATE_WITHDRAWN,
    PLATFORM_IDEMPOTENCY_CONFLICT,
    PLATFORM_LEASE_MISSING,
    PLATFORM_LEASE_CAP_EXCEEDED,
    PLATFORM_RATE_LIMIT_EXCEEDED,
    PLATFORM_RECEIPT_REPLAY,
    PLATFORM_RETRY_EXHAUSTED,
    PLATFORM_STATE_CORRUPT,
    PLATFORM_UNSUPPORTED_SCHEMA,
    PLATFORM_VERSION_BINDING,
    SCHEMA_CANDIDATE,
    SCHEMA_CHECKPOINT,
    SCHEMA_EXPORT,
    SCHEMA_RECEIPT,
    SCHEMA_REQUEST,
    SCHEMA_STATE,
    PlatformAdapter,
    PlatformAdapterError,
    PlatformAuthorizationLease,
    PlatformPolicy,
    PlatformAdapterRegistry,
    DenyAllShadowTransport,
    build_default_registry,
    canonical_json,
    detect_raw_secret,
    sha256_text,
    validate_id,
    validate_idempotency_key,
    validate_local_ref,
)

PUBLICATION_DEPT_ID = 'dept-publication'

_LOCK_EXT = '.tmp'


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + _LOCK_EXT)
    with tmp.open('w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# PublicationService
# --------------------------------------------------------------------------- #
class PublicationService:
    def __init__(
        self,
        group_root: Any,
        *,
        economic_ledger: Any = None,
        registry: PlatformAdapterRegistry | None = None,
        transport: Any = None,
        policy: PlatformPolicy | None = None,
        now: Any = None,
    ) -> None:
        self.group_root = Path(group_root).resolve()
        self.pub_root = self.group_root / 'publication'
        self.events_path = self.pub_root / 'events.jsonl'
        self.index_path = self.pub_root / 'index.json'
        self.candidates_dir = self.pub_root / 'candidates'
        self.checkpoints_dir = self.pub_root / 'checkpoints'
        self.exports_dir = self.pub_root / 'exports'
        self._lock = threading.RLock()
        self.economic_ledger = economic_ledger
        self.registry = registry or build_default_registry()
        self.transport = transport or DenyAllShadowTransport()
        self.policy = policy
        self._now = now or _utc_now_iso
        self._events: list[dict[str, object]] = []
        self._idempotency: dict[str, dict[str, object]] = {}
        if self.events_path.exists():
            self._load()

    # -- low-level load / append ------------------------------------------- #
    def _load(self) -> None:
        self._events = []
        self._idempotency = {}
        text = self.events_path.read_text(encoding='utf-8')
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PlatformAdapterError(
                    PLATFORM_STATE_CORRUPT, f'malformed events JSONL: {exc}'
                )
            if not isinstance(value, dict):
                raise PlatformAdapterError(
                    PLATFORM_STATE_CORRUPT, 'event line is not an object'
                )
            self._events.append(value)
            key = value.get('idempotency_key')
            if isinstance(key, str):
                self._idempotency[key] = value

    def _append_event(
        self, event_type: str, payload: dict[str, object], *, idempotency_key: str,
    ) -> dict[str, object]:
        validate_idempotency_key(idempotency_key)
        with self._lock:
            existing = self._idempotency.get(idempotency_key)
            if existing is not None:
                return dict(existing)
            envelope: dict[str, object] = {
                'schema_version': SCHEMA_STATE,
                'event_type': event_type,
                'idempotency_key': idempotency_key,
                'occurred_at': self._now(),
                'payload': payload,
            }
            with self.events_path.open('a', encoding='utf-8', newline='\n') as handle:
                handle.write(canonical_json(envelope) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            self._events.append(envelope)
            self._idempotency[idempotency_key] = envelope
            return envelope

    # -- policy resolution -------------------------------------------------- #
    def _resolve_policy(self, platform_id: str) -> PlatformPolicy:
        if self.policy is not None:
            return self.policy
        for candidate in (self.group_root / 'governance' / 'platforms.policy.json',
                          self.group_root / 'shared' / 'policies' / 'platforms.policy.json'):
            if candidate.is_file():
                try:
                    return PlatformPolicy.from_dict(json.loads(candidate.read_text(encoding='utf-8')))
                except (OSError, json.JSONDecodeError, PlatformAdapterError):
                    pass
        return PlatformPolicy(platform_id=platform_id)

    # -- candidate ---------------------------------------------------------- #
    def create_candidate(
        self,
        *,
        work_id: str,
        version_id: str,
        artifact_id: str,
        artifact_sha256: str,
        content_ref: str,
        content_bytes: int,
        quality_evidence_refs: Sequence[str],
        title: str,
        summary: str,
        task_id: str | None = None,
        run_id: str | None = None,
        idempotency_key: str | None = None,
        source_kind: str | None = None,
        release_bundle: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        validate_id(work_id, kind='work_id')
        validate_id(version_id, kind='version_id')
        validate_id(artifact_id, kind='artifact_id')
        validate_local_ref(content_ref, kind='content_ref')
        if not isinstance(title, str) or not title.strip():
            raise PlatformAdapterError(PLATFORM_STATE_CORRUPT, 'title required')
        if not isinstance(summary, str) or not summary.strip():
            raise PlatformAdapterError(PLATFORM_STATE_CORRUPT, 'summary required')
        if int(content_bytes) < 0:
            raise PlatformAdapterError(PLATFORM_STATE_CORRUPT, 'content_bytes must be non-negative')
        offending = detect_raw_secret(title, summary)
        if offending is not None:
            raise PlatformAdapterError(PLATFORM_STATE_CORRUPT, 'raw secret-like value in metadata')

        candidate_id = 'cand-' + sha256_text(
            f'{version_id}|{artifact_id}|{artifact_sha256}|{work_id}|{title}|{summary}'
        )[:24]
        key = idempotency_key or f'pub-v1:candidate:{candidate_id}'
        validate_idempotency_key(key)

        # Re-verify the W1-1 version binding before accepting a candidate.
        self._verify_version_binding(version_id, artifact_id, artifact_sha256)

        # W2-2 release-bundle candidates carry an immutable release manifest that
        # must be re-verified: every constituent chapter must bind to an accepted
        # W1-1 version fact, and the release artifact hash/bytes must match.
        release_bundle_sha256: str | None = None
        if source_kind == 'release_bundle':
            release_bundle_sha256 = self._verify_release_bundle(
                release_bundle, work_id, version_id, artifact_id,
                artifact_sha256, content_ref, int(content_bytes),
            )

        manifest = {
            'schema_version': SCHEMA_CANDIDATE,
            'candidate_id': candidate_id,
            'idempotency_key': key,
            'source_kind': source_kind or 'work-version',
            'work_id': work_id,
            'version_id': version_id,
            'artifact_id': artifact_id,
            'artifact_sha256': artifact_sha256,
            'content_ref': content_ref,
            'content_bytes': int(content_bytes),
            'quality_evidence_refs': list(quality_evidence_refs or []),
            'title': title,
            'summary': summary,
            'created_at': self._now(),
            'status': CANDIDATE_READY,
        }
        if release_bundle_sha256 is not None:
            manifest['release_bundle_sha256'] = release_bundle_sha256
        candidate_sha256 = sha256_text(canonical_json(manifest))

        with self._lock:
            existing = self._idempotency.get(key)
            if existing is not None:
                return self._candidate_from_event(existing)
            # Immutable manifest + payload reference written once.
            manifest_path = self.candidates_dir / candidate_id / 'manifest.json'
            if not manifest_path.exists():
                _atomic_write(manifest_path, canonical_json(manifest))
                payload_ref = {
                    'schema_version': SCHEMA_CANDIDATE + ':payload-ref',
                    'candidate_id': candidate_id,
                    'content_ref': content_ref,
                    'content_bytes': int(content_bytes),
                    'artifact_sha256': artifact_sha256,
                }
                _atomic_write(
                    self.candidates_dir / candidate_id / 'payload.ref.json',
                    canonical_json(payload_ref),
                )
            self._append_event(
                'candidate_created', manifest, idempotency_key=key,
            )
        result = dict(manifest)
        result['candidate_sha256'] = candidate_sha256
        return result

    def _verify_version_binding(
        self, version_id: str, artifact_id: str, artifact_sha256: str,
    ) -> None:
        ledger = self.economic_ledger
        if ledger is None:
            return
        try:
            entries = ledger.entries(event_type='version', version_id=version_id)
        except Exception:
            raise PlatformAdapterError(
                PLATFORM_VERSION_BINDING, 'version fact unreadable'
            )
        if not entries:
            raise PlatformAdapterError(
                PLATFORM_VERSION_BINDING, f'no W1-1 version fact for {version_id!r}'
            )
        payload = entries[-1].get('payload') or {}
        if str(payload.get('status')) != 'accepted':
            raise PlatformAdapterError(
                PLATFORM_VERSION_BINDING, 'version fact is not accepted'
            )
        if str(payload.get('accepted_artifact_id')) != str(artifact_id):
            raise PlatformAdapterError(
                PLATFORM_VERSION_BINDING, 'artifact_id mismatch with W1-1 version'
            )
        if str(payload.get('artifact_sha256')) != str(artifact_sha256):
            raise PlatformAdapterError(
                PLATFORM_VERSION_BINDING, 'artifact_sha256 mismatch with W1-1 version'
            )

    def _verify_release_bundle(
        self,
        release_bundle: Mapping[str, object] | None,
        work_id: str,
        release_version_id: str,
        release_artifact_id: str,
        release_sha256: str,
        content_ref: str,
        content_bytes: int,
    ) -> str:
        '''Verify an immutable W2-2 release manifest before a release-bundle candidate.

        Returns the canonical sha256 of the release bundle so the candidate can
        record it. Raises PlatformAdapterError on any inconsistency.
        '''
        if not isinstance(release_bundle, dict):
            raise PlatformAdapterError(
                PLATFORM_VERSION_BINDING, 'release_bundle required for source_kind=release_bundle'
            )
        if release_bundle.get('schema_version') != 'novel-release-manifest/v1':
            raise PlatformAdapterError(
                PLATFORM_VERSION_BINDING, 'release_bundle schema unsupported'
            )
        if str(release_bundle.get('status')) != 'ready':
            raise PlatformAdapterError(
                PLATFORM_VERSION_BINDING, 'release bundle is not in ready status'
            )
        if str(release_bundle.get('release_sha256')) != str(release_sha256):
            raise PlatformAdapterError(
                PLATFORM_VERSION_BINDING, 'release artifact hash mismatch with release bundle'
            )
        if int(release_bundle.get('total_bytes', -1)) != int(content_bytes):
            raise PlatformAdapterError(
                PLATFORM_VERSION_BINDING, 'release content bytes mismatch with release bundle'
            )
        # Every constituent chapter must bind to an accepted W1-1 version fact.
        chapters = release_bundle.get('chapters') or []
        if not chapters:
            raise PlatformAdapterError(
                PLATFORM_VERSION_BINDING, 'release bundle has no constituent chapters'
            )
        for ch in chapters:
            ch = ch if isinstance(ch, dict) else {}
            self._verify_version_binding(
                str(ch.get('version_id')),
                str(ch.get('artifact_id')),
                str(ch.get('artifact_sha256')),
            )
        # Best-effort: re-hash the release content file and confirm it matches
        # the bundle's release hash (no network, no content in the ledger).
        try:
            from .root import resolve_portable_path
            path = resolve_portable_path(self.group_root, content_ref, 'publication_content_ref')
            if path.is_file():
                data = path.read_bytes()
                if sha256_text(data.decode('utf-8', 'replace')) != str(release_sha256):
                    raise PlatformAdapterError(
                        PLATFORM_VERSION_BINDING, 'release content file hash mismatch'
                    )
        except PlatformAdapterError:
            raise
        except Exception:
            pass
        return sha256_text(canonical_json(release_bundle))

    def _candidate_from_event(self, event: Mapping[str, object]) -> dict[str, object]:
        manifest = dict(event.get('payload') or {})
        manifest_path = self.candidates_dir / manifest['candidate_id'] / 'manifest.json'
        candidate_sha256 = (
            sha256_text(manifest_path.read_text(encoding='utf-8'))
            if manifest_path.is_file() else ''
        )
        result = dict(manifest)
        result['candidate_sha256'] = candidate_sha256
        return result

    def get_candidate(self, candidate_id: str) -> dict[str, object]:
        validate_id(candidate_id, kind='candidate_id')
        with self._lock:
            status = CANDIDATE_READY
            manifest: dict[str, object] = {}
            for event in self._events:
                if event.get('event_type') != 'candidate_created':
                    continue
                payload = event.get('payload') or {}
                if payload.get('candidate_id') != candidate_id:
                    continue
                manifest = dict(payload)
            if not manifest:
                for event in self._events:
                    if event.get('event_type') != 'candidate_withdrawn':
                        continue
                    if (event.get('payload') or {}).get('candidate_id') == candidate_id:
                        raise PlatformAdapterError(
                            PLATFORM_CANDIDATE_MISSING, 'candidate withdrawn and gone'
                        )
                raise PlatformAdapterError(
                    PLATFORM_CANDIDATE_MISSING, f'candidate not found: {candidate_id!r}'
                )
            for event in self._events:
                if event.get('event_type') == 'candidate_withdrawn':
                    if (event.get('payload') or {}).get('candidate_id') == candidate_id:
                        status = CANDIDATE_WITHDRAWN
            manifest = dict(manifest)
            manifest['status'] = status
            manifest_path = self.candidates_dir / candidate_id / 'manifest.json'
            manifest['candidate_sha256'] = (
                sha256_text(manifest_path.read_text(encoding='utf-8'))
                if manifest_path.is_file() else ''
            )
            return manifest

    def list_candidates(self) -> list[dict[str, object]]:
        seen: dict[str, dict[str, object]] = {}
        with self._lock:
            for event in self._events:
                if event.get('event_type') == 'candidate_created':
                    payload = event.get('payload') or {}
                    seen[payload['candidate_id']] = dict(payload)
                elif event.get('event_type') == 'candidate_withdrawn':
                    cid = (event.get('payload') or {}).get('candidate_id')
                    if cid in seen:
                        seen[cid]['status'] = CANDIDATE_WITHDRAWN
        return [self._candidate_from_event({'payload': v}) for v in seen.values()]

    def withdraw_candidate(self, candidate_id: str) -> dict[str, object]:
        validate_id(candidate_id, kind='candidate_id')
        with self._lock:
            current = self.get_candidate(candidate_id)
            if current.get('status') == CANDIDATE_WITHDRAWN:
                return current
            key = f'pub-v1:withdraw-candidate:{candidate_id}'
            self._append_event(
                'candidate_withdrawn',
                {'candidate_id': candidate_id, 'idempotency_key': key},
                idempotency_key=key,
            )
            return self.get_candidate(candidate_id)

    # -- dispatch ----------------------------------------------------------- #
    def dispatch_shadow(
        self,
        candidate_id: str,
        *,
        platform_id: str,
        account_ref: str,
        mode: str,
        action: str,
        lease: PlatformAuthorizationLease,
        idempotency_key: str,
        transport: Any = None,
        request_timestamp: str | None = None,
        policy_version: str | None = None,
        adapter_id: str = 'local-shadow/v1',
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        validate_id(candidate_id, kind='candidate_id')
        validate_id(platform_id, kind='platform_id')
        validate_idempotency_key(idempotency_key)
        if mode not in ALLOWED_MODES:
            raise PlatformAdapterError(
                PLATFORM_STATE_CORRUPT, f'external-effect mode forbidden: {mode!r}'
            )
        if action not in ALLOWED_ACTIONS:
            raise PlatformAdapterError(
                PLATFORM_STATE_CORRUPT, f'action not allowed: {action!r}'
            )

        with self._lock:
            # Identical replay: return the original receipt, zero transport / ledger.
            existing_action = self._find_action_by_idempotency(idempotency_key)
            if existing_action is not None:
                receipt = existing_action.get('receipt')
                if receipt is not None:
                    return dict(receipt)
                # Prepared / retry_wait action exists; resume instead of duplicate.
                return self._resume_locked(
                    existing_action['action_id'], transport=transport, lease=lease,
                    request_timestamp=request_timestamp,
                )

            candidate = self.get_candidate(candidate_id)
            if candidate.get('status') == CANDIDATE_WITHDRAWN:
                raise PlatformAdapterError(
                    PLATFORM_CANDIDATE_WITHDRAWN, 'candidate withdrawn'
                )

            adapter = self.registry.get(adapter_id)
            policy = self._resolve_policy(platform_id)
            now_iso = self._now()
            request = {
                'schema_version': SCHEMA_REQUEST,
                'action_id': '',  # filled after action creation
                'candidate_id': candidate_id,
                'version_id': candidate.get('version_id'),
                'artifact_id': candidate.get('artifact_id'),
                'platform_id': platform_id,
                'account_ref': account_ref,
                'mode': mode,
                'action': action,
                'lease_id': lease.lease_id,
                'policy_version': policy_version or policy.policy_version,
                'attempt_no': 1,
                'capability_id': 'submit_shadow',
                'now_iso': now_iso,
                'requested_at': request_timestamp or now_iso,
                'secret_ref': '',
            }

            # Fail-closed validation before any transport call.
            try:
                adapter.validate(
                    request, candidate, policy, lease, now_iso=now_iso,
                )
            except PlatformAdapterError as exc:
                action_id = 'act-' + sha256_text(idempotency_key)[:20]
                self._create_action_locked(
                    action_id, idempotency_key, request, candidate, status=ACTION_PREPARED,
                )
                receipt = self._build_receipt(
                    action_id, candidate, attempt=None, status=ACTION_BLOCKED,
                    evidence={'code': exc.code, 'message': exc.message},
                )
                self._record_receipt_locked(action_id, idempotency_key, receipt)
                return receipt

            # Persistent rate limit + lease cap (restart-safe via events).
            self._enforce_rate_limit(policy, platform_id, account_ref, now_iso)
            self._enforce_lease_cap(lease)

            action_id = 'act-' + sha256_text(idempotency_key)[:20]
            request = dict(request)
            request['action_id'] = action_id
            self._create_action_locked(
                action_id, idempotency_key, request, candidate, status=ACTION_PREPARED,
            )
            return self._dispatch_locked(
                action_id, idempotency_key, request, candidate, adapter,
                transport or self.transport, lease, task_id=task_id, run_id=run_id,
            )

    def _dispatch_locked(
        self,
        action_id: str,
        idempotency_key: str,
        request: dict[str, object],
        candidate: dict[str, object],
        adapter: PlatformAdapter,
        transport: Any,
        lease: PlatformAuthorizationLease,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        now_iso = self._now()
        request = dict(request)
        request['now_iso'] = now_iso
        try:
            result = adapter.dispatch_shadow(request, candidate, transport)
        except PlatformAdapterError as exc:
            receipt = self._build_receipt(
                action_id, candidate, attempt=None, status=ACTION_BLOCKED,
                evidence={'code': exc.code, 'message': exc.message},
                platform_id=request.get('platform_id'),
                account_ref=request.get('account_ref'),
            )
            self._record_receipt_locked(action_id, idempotency_key, receipt)
            return receipt

        outcome = result.get('outcome')
        attempt = result.get('attempt') or {}
        self._append_event(
            'action_attempt', attempt,
            idempotency_key=f'pub-v1:attempt:{attempt.get("attempt_id")}',
        )

        if outcome == 'accepted':
            receipt = self._build_receipt(
                action_id, candidate, attempt=attempt, status=ACTION_SHADOW_ACCEPTED,
                evidence=attempt.get('evidence') or {},
                platform_id=request.get('platform_id'),
                account_ref=request.get('account_ref'),
            )
            self._record_receipt_locked(action_id, idempotency_key, receipt)
            self._bind_economic_platform(
                action_id, candidate, receipt, lease,
                platform_id=request.get('platform_id'),
                task_id=task_id, run_id=run_id,
            )
            return receipt
        if outcome == 'rejected':
            receipt = self._build_receipt(
                action_id, candidate, attempt=attempt, status=ACTION_SHADOW_REJECTED,
                evidence=attempt.get('evidence') or {},
                platform_id=request.get('platform_id'),
                account_ref=request.get('account_ref'),
            )
            self._record_receipt_locked(action_id, idempotency_key, receipt)
            return receipt
        # Transient error -> checkpoint + retry_wait, no receipt, no ledger.
        failure_class = result.get('failure_class')
        if failure_class == FAILURE_TRANSIENT:
            checkpoint = self._build_checkpoint(
                action_id, retry_count=1, prev_attempt_hash=str(attempt.get('attempt_id')),
                terminal=False,
            )
            self._append_event(
                'action_checkpoint', checkpoint,
                idempotency_key=f'pub-v1:checkpoint:{action_id}:1',
            )
            return {
                'schema_version': SCHEMA_STATE,
                'action_id': action_id,
                'status': ACTION_RETRY_WAIT,
                'attempt': attempt,
                'checkpoint': checkpoint,
                'receipt': None,
            }
        # Terminal error -> shadow_rejected receipt, no ledger.
        receipt = self._build_receipt(
            action_id, candidate, attempt=attempt, status=ACTION_SHADOW_REJECTED,
            evidence=attempt.get('evidence') or {},
            platform_id=request.get('platform_id'),
            account_ref=request.get('account_ref'),
        )
        self._record_receipt_locked(action_id, idempotency_key, receipt)
        return receipt

    def resume_action(
        self,
        action_id: str,
        *,
        transport: Any = None,
        lease: PlatformAuthorizationLease | None = None,
        request_timestamp: str | None = None,
    ) -> dict[str, object]:
        validate_id(action_id, kind='action_id')
        with self._lock:
            action = self._find_action(action_id)
            if action is None:
                raise PlatformAdapterError(
                    PLATFORM_STATE_CORRUPT, f'action not found: {action_id!r}'
                )
            receipt = action.get('receipt')
            if receipt is not None:
                # Terminal already; replay returns the original receipt.
                return dict(receipt)
            return self._resume_locked(
                action_id, transport=transport, lease=lease,
                request_timestamp=request_timestamp,
            )

    def _resume_locked(
        self,
        action_id: str,
        *,
        transport: Any = None,
        lease: PlatformAuthorizationLease | None = None,
        request_timestamp: str | None = None,
    ) -> dict[str, object]:
        action = self._find_action(action_id)
        if action is None or action.get('receipt') is not None:
            if action is not None and action.get('receipt') is not None:
                return dict(action['receipt'])
            raise PlatformAdapterError(
                PLATFORM_STATE_CORRUPT, f'action not found: {action_id!r}'
            )
        # Resume must carry an explicit authorization lease (contract §9). The
        # original lease object is intentionally not persisted (non-serializable
        # + must be re-validated against current time/scope on every retry).
        if lease is None:
            raise PlatformAdapterError(
                PLATFORM_LEASE_MISSING,
                f'resume requires an explicit authorization lease for {action_id!r}',
            )
        request = dict(action['request'])
        candidate = action['candidate']
        attempt_no = len(action['attempts']) + 1
        policy = self._resolve_policy(str(request.get('platform_id')))
        if attempt_no > policy.max_attempts_per_action:
            raise PlatformAdapterError(
                PLATFORM_RETRY_EXHAUSTED,
                f'max attempts exceeded for {action_id!r}',
            )
        request['attempt_no'] = attempt_no
        adapter = self.registry.get(str(request.get('adapter_id') or 'local-shadow/v1'))
        now_iso = self._now()
        request['now_iso'] = now_iso
        idempotency_key = action['idempotency_key']
        # Fail-closed re-validation of the (possibly rotated) lease on resume.
        try:
            adapter.validate(request, candidate, policy, lease, now_iso=now_iso)
        except PlatformAdapterError as exc:
            receipt = self._build_receipt(
                action_id, candidate, attempt=None, status=ACTION_BLOCKED,
                evidence={'code': exc.code, 'message': exc.message},
                platform_id=request.get('platform_id'),
                account_ref=request.get('account_ref'),
            )
            self._record_receipt_locked(action_id, idempotency_key, receipt)
            return receipt
        return self._dispatch_locked(
            action_id, idempotency_key, request, candidate, adapter,
            transport or self.transport, lease,
        )

    def _create_action_locked(
        self, action_id: str, idempotency_key: str, request: dict[str, object],
        candidate: dict[str, object], *, status: str,
    ) -> None:
        payload = {
            'action_id': action_id,
            'idempotency_key': idempotency_key,
            'candidate_id': candidate.get('candidate_id'),
            'version_id': candidate.get('version_id'),
            'artifact_id': candidate.get('artifact_id'),
            'platform_id': request.get('platform_id'),
            'account_ref': request.get('account_ref'),
            'mode': request.get('mode'),
            'action': request.get('action'),
            'lease_id': request.get('lease_id'),
            'policy_version': request.get('policy_version'),
            'status': status,
            'created_at': self._now(),
        }
        self._append_event(
            'action_created', payload, idempotency_key=idempotency_key,
        )

    def _record_receipt_locked(
        self, action_id: str, idempotency_key: str, receipt: dict[str, object],
    ) -> None:
        self._append_event(
            'action_receipt', receipt,
            idempotency_key=f'pub-v1:receipt:{action_id}',
        )

    # -- counters / guards -------------------------------------------------- #
    def _accepted_count(self, platform_id: str, account_ref: str, since_iso: str) -> int:
        count = 0
        for event in self._events:
            if event.get('event_type') != 'action_receipt':
                continue
            payload = event.get('payload') or {}
            if payload.get('status') != ACTION_SHADOW_ACCEPTED:
                continue
            if payload.get('platform_id') != platform_id:
                continue
            if payload.get('account_ref') != account_ref:
                continue
            occurred = str(event.get('occurred_at') or '')
            if occurred >= since_iso:
                count += 1
        return count

    def _enforce_rate_limit(
        self, policy: PlatformPolicy, platform_id: str, account_ref: str, now_iso: str,
    ) -> None:
        window_start = self._subtract_seconds(now_iso, policy.per_account_window_seconds)
        if self._accepted_count(platform_id, account_ref, window_start) >= policy.per_account_window_actions:
            raise PlatformAdapterError(
                PLATFORM_RATE_LIMIT_EXCEEDED,
                f'rate limit exceeded for {platform_id}/{account_ref}',
            )

    def _lease_action_count(self, lease_id: str) -> int:
        count = 0
        for event in self._events:
            if event.get('event_type') != 'action_created':
                continue
            if (event.get('payload') or {}).get('lease_id') == lease_id:
                count += 1
        return count

    def _enforce_lease_cap(self, lease: PlatformAuthorizationLease) -> None:
        if self._lease_action_count(lease.lease_id) >= lease.max_actions:
            raise PlatformAdapterError(
                PLATFORM_LEASE_CAP_EXCEEDED,
                f'lease action cap exceeded: {lease.lease_id!r}',
            )

    @staticmethod
    def _subtract_seconds(iso: str, seconds: int) -> str:
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return ''
        return (dt - timedelta(seconds=seconds)).isoformat()

    # -- receipt / checkpoint builders ------------------------------------- #
    def _build_receipt(
        self,
        action_id: str,
        candidate: Mapping[str, object],
        *,
        attempt: Mapping[str, object] | None,
        status: str,
        evidence: Mapping[str, object],
        platform_id: str | None = None,
        account_ref: str | None = None,
    ) -> dict[str, object]:
        request_hash = sha256_text(canonical_json({
            'candidate_id': candidate.get('candidate_id'),
            'version_id': candidate.get('version_id'),
            'artifact_id': candidate.get('artifact_id'),
            'action_id': action_id,
        }))
        candidate_hash = sha256_text(canonical_json({
            'candidate_id': candidate.get('candidate_id'),
            'artifact_sha256': candidate.get('artifact_sha256'),
            'content_bytes': candidate.get('content_bytes'),
        }))
        receipt_id = 'rcpt-' + sha256_text(f'{action_id}|{status}')[:20]
        receipt = {
            'schema_version': SCHEMA_RECEIPT,
            'receipt_id': receipt_id,
            'action_id': action_id,
            'candidate_id': candidate.get('candidate_id'),
            'version_id': candidate.get('version_id'),
            'artifact_id': candidate.get('artifact_id'),
            'platform_id': platform_id,
            'account_ref': account_ref,
            'status': status,
            'published': False,
            'external_effect': False,
            'external_url': None,
            'request_hash': request_hash,
            'attempt_hash': (attempt or {}).get('attempt_id'),
            'candidate_hash': candidate_hash,
            'artifact_hash': candidate.get('artifact_sha256'),
            'policy_hash': None,
            'evidence': {
                'outcome': evidence.get('outcome'),
                'reason': evidence.get('reason'),
                'code': evidence.get('code'),
            },
            'created_at': self._now(),
        }
        receipt['receipt_sha256'] = sha256_text(canonical_json(receipt))
        return receipt

    def _build_checkpoint(
        self, action_id: str, *, retry_count: int, prev_attempt_hash: str, terminal: bool,
    ) -> dict[str, object]:
        return {
            'schema_version': SCHEMA_CHECKPOINT,
            'action_id': action_id,
            'next_state': ACTION_RETRY_WAIT if not terminal else ACTION_BLOCKED,
            'retry_count': retry_count,
            'retry_not_before': self._now(),
            'prev_attempt_hash': prev_attempt_hash,
            'terminal': terminal,
            'created_at': self._now(),
        }

    def _bind_economic_platform(
        self, action_id: str, candidate: Mapping[str, object], receipt: Mapping[str, object],
        lease: PlatformAuthorizationLease, *, platform_id: str | None,
        task_id: str | None, run_id: str | None,
    ) -> None:
        ledger = self.economic_ledger
        if ledger is None:
            return
        platform_id_econ = f'plat-{platform_id or "shadow"}'
        ledger.register_platform(
            platform_id_econ,
            idempotency_key=f'econ-v1:platform:{action_id}',
            adapter_id='local-shadow/v1',
            capability_status='shadow',
            published=False,
            mode='shadow',
            external_cost_fen=0,
            evidence={
                'candidate_id': candidate.get('candidate_id'),
                'version_id': candidate.get('version_id'),
                'artifact_id': candidate.get('artifact_id'),
                'receipt_id': receipt.get('receipt_id'),
                'receipt_sha256': receipt.get('receipt_sha256'),
                'candidate_sha256': candidate.get('candidate_sha256'),
                'artifact_sha256': candidate.get('artifact_sha256'),
                'lease_id': lease.lease_id,
            },
            action_id=action_id,
            task_id=task_id,
            run_id=run_id,
            department_id=PUBLICATION_DEPT_ID,
        )

    # -- action queries ----------------------------------------------------- #
    def _find_action(self, action_id: str) -> dict[str, object] | None:
        created: dict[str, object] | None = None
        candidate: dict[str, object] = {}
        attempts: list[dict[str, object]] = []
        checkpoints: list[dict[str, object]] = []
        receipt: dict[str, object] | None = None
        withdrawn = False
        idempotency_key = ''
        for event in self._events:
            etype = event.get('event_type')
            payload = event.get('payload') or {}
            if etype == 'action_created' and payload.get('action_id') == action_id:
                created = dict(payload)
                idempotency_key = str(payload.get('idempotency_key') or '')
                cand_id = payload.get('candidate_id')
                if cand_id:
                    try:
                        candidate = self.get_candidate(str(cand_id))
                    except PlatformAdapterError:
                        candidate = {}
            elif etype == 'action_attempt' and (payload.get('action_id') == action_id):
                attempts.append(dict(payload))
            elif etype == 'action_checkpoint' and payload.get('action_id') == action_id:
                checkpoints.append(dict(payload))
            elif etype == 'action_receipt' and payload.get('action_id') == action_id:
                receipt = dict(payload)
            elif etype == 'action_withdrawn' and payload.get('action_id') == action_id:
                withdrawn = True
        if created is None:
            return None
        status = str(created.get('status') or ACTION_PREPARED)
        if checkpoints and receipt is None:
            status = ACTION_RETRY_WAIT
        if receipt is not None:
            status = str(receipt.get('status'))
        # Soft withdrawal (append-only) is the terminal display state.
        if withdrawn:
            status = ACTION_WITHDRAWN
        return {
            'action_id': action_id,
            'idempotency_key': idempotency_key,
            'request': created,
            'candidate': candidate,
            'attempts': attempts,
            'checkpoints': checkpoints,
            'receipt': receipt,
            'status': status,
        }

    def _find_action_by_idempotency(self, idempotency_key: str) -> dict[str, object] | None:
        event = self._idempotency.get(idempotency_key)
        if event is None:
            return None
        action_id = (event.get('payload') or {}).get('action_id')
        if not action_id:
            return None
        return self._find_action(str(action_id))

    def get_action(self, action_id: str) -> dict[str, object]:
        validate_id(action_id, kind='action_id')
        action = self._find_action(action_id)
        if action is None:
            raise PlatformAdapterError(
                PLATFORM_STATE_CORRUPT, f'action not found: {action_id!r}'
            )
        return action

    def withdraw_action(self, action_id: str) -> dict[str, object]:
        validate_id(action_id, kind='action_id')
        with self._lock:
            action = self._find_action(action_id)
            if action is None:
                raise PlatformAdapterError(
                    PLATFORM_STATE_CORRUPT, f'action not found: {action_id!r}'
                )
            # Withdrawal is append-only soft state; it can be applied even after
            # a terminal receipt (shadow has no external effect to undo). Already
            # withdrawn actions return their current state idempotently.
            if action.get('status') == ACTION_WITHDRAWN:
                return action
            key = f'pub-v1:withdraw-action:{action_id}'
            self._append_event(
                'action_withdrawn',
                {'action_id': action_id, 'idempotency_key': key},
                idempotency_key=key,
            )
            return self._find_action(action_id)  # type: ignore[return-value]

    # -- rebuild / export / restore ---------------------------------------- #
    def rebuild(self) -> dict[str, object]:
        with self._lock:
            self._load()
            index = {
                'schema_version': SCHEMA_STATE,
                'candidate_count': len(self.list_candidates()),
                'event_count': len(self._events),
                'rebuilt_at': self._now(),
            }
            _atomic_write(self.index_path, canonical_json(index))
            return index

    def export(self, target_dir: Any) -> dict[str, object]:
        target = Path(target_dir).resolve()
        target.mkdir(parents=True, exist_ok=True)
        export_id = 'exp-' + sha256_text(f'{self.group_root}|{self._now()}')[:20]
        files: list[dict[str, object]] = []

        def _record(src: Path) -> None:
            rel = src.relative_to(self.pub_root).as_posix()
            dest = target / rel
            data = src.read_bytes()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            files.append({
                'relative_path': rel,
                'sha256': sha256_text(data.decode('utf-8', 'replace')),
                'bytes': len(data),
            })

        if self.events_path.exists():
            _record(self.events_path)
        if self.index_path.exists():
            _record(self.index_path)
        for manifest in sorted(self.candidates_dir.rglob('manifest.json')):
            _record(manifest)
        for pref in sorted(self.candidates_dir.rglob('payload.ref.json')):
            _record(pref)
        manifest = {
            'schema_version': SCHEMA_EXPORT,
            'export_id': export_id,
            'source_root': str(self.pub_root),
            'files': files,
            'exported_at': self._now(),
        }
        # The export manifest itself is the restore entrypoint and is NOT part
        # of the publication state, so it is written but excluded from `files`.
        _atomic_write(target / 'manifest.json', canonical_json(manifest))
        return manifest

    @classmethod
    def restore(cls, source_dir: Any, target_root: Any) -> 'PublicationService':
        source = Path(source_dir).resolve()
        manifest_path = source / 'manifest.json'
        if not manifest_path.is_file():
            raise PlatformAdapterError(
                PLATFORM_STATE_CORRUPT, 'export manifest missing'
            )
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('schema_version') != SCHEMA_EXPORT:
            raise PlatformAdapterError(
                PLATFORM_UNSUPPORTED_SCHEMA, 'unsupported export schema'
            )
        target = Path(target_root).resolve()
        pub_root = target / 'publication'
        pub_root.mkdir(parents=True, exist_ok=True)
        rel_names = [
            str(f.get('relative_path'))
            for f in manifest.get('files', [])
            if str(f.get('relative_path')) != 'manifest.json'
        ]
        if pub_root.exists() and any(pub_root.iterdir()):
            if (pub_root / 'events.jsonl').exists() or (pub_root / 'candidates').exists():
                raise PlatformAdapterError(
                    PLATFORM_STATE_CORRUPT, 'restore target not empty'
                )
        for fname in rel_names:
            src = source / fname
            if not src.is_file():
                raise PlatformAdapterError(
                    PLATFORM_STATE_CORRUPT, f'export file missing: {fname!r}'
                )
            dest = pub_root / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
        return cls(target)
