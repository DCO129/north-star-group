'''W1-1 unified economic fact ledger for the local novel-production company.

This module is the first WAVE 1 economic fact spine. It is an auditable, local,
append-only operating ledger. It does NOT spend money, publish externally, or
claim real revenue (contract W1-1 §1, §4).

Design invariants (contract W1-1 §2, §5, §6):
- Economic facts are append-only JSONL records; any index/snapshot is derived and
  rebuildable from the ledger.
- CNY integer fen is the only base-money representation. No float money.
- Every mutation carries a stable idempotency key; replays/retries do not
  duplicate facts or charges.
- Reservations count against available budget until committed or released.
- Accepted novel artifacts may create work/version facts only after P0-4 quality
  acceptance.
- A platform record is `local`/`shadow`; it never implies publication, account
  access, or platform acceptance.
'''

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .timebase import authoritative_timestamp


# --------------------------------------------------------------------------- #
# Stable blocking codes (contract §11)
# --------------------------------------------------------------------------- #
ECON_INVALID_ID = 'economic-invalid-id'
ECON_SCHEMA_UNSUPPORTED = 'economic-schema-unsupported'
ECON_ENTRY_CONFLICT = 'economic-entry-conflict'
ECON_IDEMPOTENCY_CONFLICT = 'economic-idempotency-conflict'
ECON_LEDGER_CORRUPT = 'economic-ledger-corrupt'
ECON_INDEX_REBUILD_FAILED = 'economic-index-rebuild-failed'
ECON_BUDGET_TRUTH_UNAVAILABLE = 'economic-budget-truth-unavailable'
ECON_BUDGET_POLICY_INVALID = 'economic-budget-policy-invalid'
ECON_BUDGET_CATEGORY_EXCEEDED = 'economic-budget-category-exceeded'
ECON_BUDGET_OPERATING_EXCEEDED = 'economic-budget-operating-exceeded'
ECON_BUDGET_RESERVE_PROTECTED = 'economic-budget-reserve-protected'
ECON_RESERVATION_CONFLICT = 'economic-reservation-conflict'
ECON_COST_WITHOUT_RESERVATION = 'economic-cost-without-reservation'
ECON_WORK_BINDING_CONFLICT = 'economic-work-binding-conflict'
ECON_VERSION_BEFORE_ACCEPTANCE = 'economic-version-before-acceptance'
ECON_VERSION_DUPLICATE = 'economic-version-duplicate'
ECON_PLATFORM_EXTERNAL_FORBIDDEN = 'economic-platform-external-forbidden'
ECON_SETTLEMENT_EVIDENCE_REQUIRED = 'economic-settlement-evidence-required'
ECON_EXPORT_INCOMPLETE = 'economic-export-incomplete'
ECON_RESTORE_CONFLICT = 'economic-restore-conflict'


# --------------------------------------------------------------------------- #
# Frozen schema versions (contract §6)
# --------------------------------------------------------------------------- #
SCHEMA_ENVELOPE = 'economic-ledger-entry/v1'
SCHEMA_POLICY = 'economic-budget-policy/v1'
SCHEMA_RESERVATION = 'economic-budget-reservation/v1'
SCHEMA_COST = 'economic-cost-record/v1'
SCHEMA_WORK = 'economic-work-record/v1'
SCHEMA_VERSION = 'economic-version-record/v1'
SCHEMA_PLATFORM = 'economic-platform-record/v1'
SCHEMA_SETTLEMENT = 'economic-settlement-record/v1'
SCHEMA_SUMMARY = 'economic-summary/v1'
SCHEMA_EXPORT = 'economic-export/v1'

LEDGER_SCHEMAS = {
    SCHEMA_ENVELOPE, SCHEMA_POLICY, SCHEMA_RESERVATION, SCHEMA_COST,
    SCHEMA_WORK, SCHEMA_VERSION, SCHEMA_PLATFORM, SCHEMA_SETTLEMENT,
    SCHEMA_SUMMARY, SCHEMA_EXPORT,
}

# payload schema versions that are valid inside an envelope's `payload`
PAYLOAD_SCHEMAS = {
    SCHEMA_POLICY, SCHEMA_RESERVATION, SCHEMA_COST, SCHEMA_WORK,
    SCHEMA_VERSION, SCHEMA_PLATFORM, SCHEMA_SETTLEMENT,
}

_ID_RE = re.compile(r'^(entry|work|ver|plat|res|settle|cost)-[a-z0-9][a-z0-9._-]{0,63}$')


class EconomicLedgerError(Exception):
    '''Bounded economic-ledger error carrying a stable blocking code.'''

    def __init__(self, code: str, message: str = '') -> None:
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _validate_id(value: str, *, kind: str = 'id') -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise EconomicLedgerError(
            ECON_INVALID_ID, f'{kind} must be a portable identifier: {value!r}'
        )
    return value


def _derive_entry_id(idempotency_key: str, payload_sha256: str) -> str:
    digest = sha256_text(f'{idempotency_key}|{payload_sha256}')
    return 'entry-' + digest[:32]


# --------------------------------------------------------------------------- #
# Budget policy (contract §6.2)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EconomicBudgetPolicy:
    total_fen: int
    reserve_fen: int
    category_caps_fen: dict[str, int]
    warning_ratio_milli: int = 800
    currency: str = 'CNY'
    policy_id: str = 'local-economic-policy'
    status: str = 'active'

    def validate(self) -> None:
        if self.currency != 'CNY':
            raise EconomicLedgerError(
                ECON_BUDGET_POLICY_INVALID, 'policy currency must be CNY'
            )
        if self.total_fen <= 0:
            raise EconomicLedgerError(
                ECON_BUDGET_POLICY_INVALID, 'total_fen must be positive'
            )
        if self.reserve_fen < 0:
            raise EconomicLedgerError(
                ECON_BUDGET_POLICY_INVALID, 'reserve_fen must be non-negative'
            )
        if any(v < 0 for v in self.category_caps_fen.values()):
            raise EconomicLedgerError(
                ECON_BUDGET_POLICY_INVALID, 'category caps must be non-negative'
            )
        if not 1 <= self.warning_ratio_milli <= 1000:
            raise EconomicLedgerError(
                ECON_BUDGET_POLICY_INVALID, 'warning_ratio_milli must be 1..1000'
            )
        allocated = sum(self.category_caps_fen.values()) + self.reserve_fen
        if allocated != self.total_fen:
            raise EconomicLedgerError(
                ECON_BUDGET_POLICY_INVALID,
                'category caps plus reserve must equal total_fen',
            )

    def operating_cap_fen(self) -> int:
        return self.total_fen - self.reserve_fen

    def to_dict(self) -> dict[str, object]:
        return {
            'schema_version': SCHEMA_POLICY,
            'policy_id': self.policy_id,
            'version': 1,
            'total_fen': self.total_fen,
            'reserve_fen': self.reserve_fen,
            'category_caps_fen': dict(self.category_caps_fen),
            'warning_ratio_milli': self.warning_ratio_milli,
            'currency': self.currency,
            'status': self.status,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> 'EconomicBudgetPolicy':
        if str(data.get('schema_version')) != SCHEMA_POLICY:
            raise EconomicLedgerError(
                ECON_SCHEMA_UNSUPPORTED,
                f'expected {SCHEMA_POLICY!r}, got {data.get("schema_version")!r}',
            )
        caps = {str(k): int(v) for k, v in (data.get('category_caps_fen') or {}).items()}
        policy = cls(
            total_fen=int(data['total_fen']),
            reserve_fen=int(data['reserve_fen']),
            category_caps_fen=caps,
            warning_ratio_milli=int(data.get('warning_ratio_milli', 800)),
            currency=str(data.get('currency', 'CNY')),
            policy_id=str(data.get('policy_id', 'local-economic-policy')),
            status=str(data.get('status', 'active')),
        )
        policy.validate()
        return policy


# --------------------------------------------------------------------------- #
# Unified economic ledger (contract §5, §7)
# --------------------------------------------------------------------------- #
class EconomicLedger:
    '''Append-only JSONL economic fact ledger with rebuildable derived index.'''

    def __init__(self, root: Any) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.root / 'ledger.jsonl'
        self.index_path = self.root / 'index.json'
        self.snapshot_path = self.root / 'snapshots' / 'latest.json'
        self.exports_dir = self.root / 'exports'
        self._lock = threading.RLock()
        self._idempotency: dict[str, dict[str, object]] = {}
        self._entries: list[dict[str, object]] = []
        if self.ledger_path.exists():
            self._load()

    # -- low-level load / append ------------------------------------------ #
    def _load(self) -> None:
        self._entries = []
        self._idempotency = {}
        try:
            text = self.ledger_path.read_text(encoding='utf-8')
        except OSError as exc:
            raise EconomicLedgerError(ECON_LEDGER_CORRUPT, str(exc)) from exc
        for line_no, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EconomicLedgerError(
                    ECON_LEDGER_CORRUPT,
                    f'malformed JSONL at line {line_no}: {exc}',
                ) from exc
            if not isinstance(value, dict):
                raise EconomicLedgerError(
                    ECON_LEDGER_CORRUPT,
                    f'ledger line {line_no} is not an object',
                )
            if value.get('schema_version') != SCHEMA_ENVELOPE:
                raise EconomicLedgerError(
                    ECON_LEDGER_CORRUPT,
                    f'ledger line {line_no} missing envelope schema',
                )
            self._entries.append(value)
            key = value.get('idempotency_key')
            if isinstance(key, str):
                self._idempotency[key] = value

    def _write_entry(self, envelope: dict[str, object]) -> None:
        encoded = canonical_json(envelope)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open('a', encoding='utf-8', newline='\n') as handle:
            handle.write(encoded + '\n')
            handle.flush()
            os.fsync(handle.fileno())

    # -- append ------------------------------------------------------------ #
    def append(
        self,
        event_type: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str,
        task_id: str | None = None,
        run_id: str | None = None,
        department_id: str | None = None,
        work_id: str | None = None,
        version_id: str | None = None,
        platform_id: str | None = None,
        settlement_id: str | None = None,
        occurred_at: str | None = None,
        evidence_refs: Sequence[str] | None = None,
    ) -> dict[str, object]:
        '''Append one economic fact. Idempotent on (key, canonical payload).'''
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise EconomicLedgerError(ECON_IDEMPOTENCY_CONFLICT, 'idempotency key required')
        payload = dict(payload)
        payload_schema = payload.get('schema_version')
        if payload_schema not in PAYLOAD_SCHEMAS:
            raise EconomicLedgerError(
                ECON_SCHEMA_UNSUPPORTED,
                f'unsupported payload schema: {payload_schema!r}',
            )
        payload_str = canonical_json(payload)
        payload_sha256 = sha256_text(payload_str)
        entry_id = _derive_entry_id(idempotency_key, payload_sha256)

        with self._lock:
            existing = self._idempotency.get(idempotency_key)
            if existing is not None:
                if existing.get('payload_sha256') == payload_sha256:
                    return dict(existing)
                raise EconomicLedgerError(
                    ECON_IDEMPOTENCY_CONFLICT,
                    f'idempotency key reused with different payload: {idempotency_key!r}',
                )
            envelope: dict[str, object] = {
                'schema_version': SCHEMA_ENVELOPE,
                'entry_id': entry_id,
                'idempotency_key': idempotency_key,
                'event_type': event_type,
                'occurred_at': occurred_at or authoritative_timestamp(),
                'currency': 'CNY',
                'task_id': task_id,
                'run_id': run_id,
                'department_id': department_id,
                'work_id': work_id,
                'version_id': version_id,
                'platform_id': platform_id,
                'settlement_id': settlement_id,
                'payload_schema_version': payload_schema,
                'payload': payload,
                'payload_sha256': payload_sha256,
                'evidence_refs': list(evidence_refs or []),
            }
            self._write_entry(envelope)
            self._entries.append(envelope)
            self._idempotency[idempotency_key] = envelope
            return envelope

    # -- queries ----------------------------------------------------------- #
    def entries(
        self,
        *,
        event_type: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        work_id: str | None = None,
        version_id: str | None = None,
        platform_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        with self._lock:
            result = list(self._entries)
        def matches(entry: dict[str, object]) -> bool:
            if event_type is not None and entry.get('event_type') != event_type:
                return False
            if task_id is not None and entry.get('task_id') != task_id:
                return False
            if run_id is not None and entry.get('run_id') != run_id:
                return False
            if work_id is not None and entry.get('work_id') != work_id:
                return False
            if version_id is not None and entry.get('version_id') != version_id:
                return False
            if platform_id is not None and entry.get('platform_id') != platform_id:
                return False
            return True
        result = [e for e in result if matches(e)]
        if limit is not None:
            result = result[-limit:]
        return result

    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def get_entry(self, entry_id: str) -> dict[str, object] | None:
        with self._lock:
            for entry in self._entries:
                if entry.get('entry_id') == entry_id:
                    return dict(entry)
        return None

    # -- derived index / snapshot ----------------------------------------- #
    def rebuild(self) -> dict[str, object]:
        '''Rebuild the derived index and snapshot from the authoritative ledger.'''
        with self._lock:
            if self.ledger_path.exists():
                self._load()
            summary = self._compute_summary()
            self._write_derived(summary)
        return summary

    def _write_derived(self, summary: dict[str, object]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        index = {
            'schema_version': SCHEMA_SUMMARY,
            'kind': 'economic-index',
            'entry_count': len(self._entries),
            'by_event_type': summary['by_event_type'],
            'written_at': authoritative_timestamp(),
        }
        self.index_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        snapshot = {
            'schema_version': SCHEMA_SUMMARY,
            'kind': 'economic-snapshot',
            'summary': summary,
            'written_at': authoritative_timestamp(),
        }
        self.snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8'
        )

    def _compute_summary(self) -> dict[str, object]:
        by_event: dict[str, int] = {}
        spent_fen = 0
        reserved_active_fen = 0
        works = set()
        versions = set()
        platforms = set()
        settlements = set()
        for entry in self._entries:
            etype = str(entry.get('event_type'))
            by_event[etype] = by_event.get(etype, 0) + 1
            payload = entry.get('payload') or {}
            if etype == 'cost':
                try:
                    spent_fen += int(payload.get('actual_fen', 0))
                except (TypeError, ValueError):
                    pass
            elif etype == 'reservation' and payload.get('state') == 'active':
                try:
                    reserved_active_fen += int(payload.get('amount_fen', 0))
                except (TypeError, ValueError):
                    pass
            elif etype == 'work':
                wid = entry.get('work_id')
                if wid:
                    works.add(wid)
            elif etype == 'version':
                vid = entry.get('version_id')
                if vid:
                    versions.add(vid)
            elif etype == 'platform':
                pid = entry.get('platform_id')
                if pid:
                    platforms.add(pid)
            elif etype == 'settlement':
                sid = entry.get('settlement_id')
                if sid:
                    settlements.add(sid)
        return {
            'schema_version': SCHEMA_SUMMARY,
            'currency': 'CNY',
            'entry_count': len(self._entries),
            'spent_fen': spent_fen,
            'reserved_active_fen': reserved_active_fen,
            'work_count': len(works),
            'version_count': len(versions),
            'platform_count': len(platforms),
            'settlement_count': len(settlements),
            'by_event_type': by_event,
            'written_at': authoritative_timestamp(),
        }

    def summary(self) -> dict[str, object]:
        with self._lock:
            return self._compute_summary()

    # -- high-level fact registration (contract §6.5, §6.6, §9.3) --------- #
    def register_work(
        self,
        work_id: str,
        project_id: str,
        *, idempotency_key: str, current_version_id: str | None = None,
        chapter_id: str | None = None, status: str = 'active',
        task_id: str | None = None, run_id: str | None = None,
        department_id: str | None = None,
    ) -> dict[str, object]:
        _validate_id(work_id, kind='work_id')
        payload = {
            'schema_version': SCHEMA_WORK,
            'work_id': work_id,
            'project_id': project_id,
            'chapter_id': chapter_id,
            'current_version_id': current_version_id,
            'status': status,
            'created_at': authoritative_timestamp(),
        }
        return self.append(
            'work', payload, idempotency_key=idempotency_key,
            task_id=task_id, run_id=run_id, department_id=department_id,
            work_id=work_id,
        )

    def register_version(
        self,
        version_id: str,
        work_id: str,
        *,
        idempotency_key: str,
        project_id: str, chapter_id: str, version_no: int,
        artifact_id: str, artifact_sha256: str,
        quality_report_sha256: str, production_task_id: str,
        production_run_id: str, production_cost_entry_id: str | None = None,
        status: str = 'accepted',
        task_id: str | None = None, run_id: str | None = None,
        department_id: str | None = None,
    ) -> dict[str, object]:
        _validate_id(version_id, kind='version_id')
        _validate_id(work_id, kind='work_id')
        if status == 'accepted' and not artifact_sha256:
            raise EconomicLedgerError(
                ECON_VERSION_BEFORE_ACCEPTANCE,
                'accepted version requires an artifact SHA-256',
            )
        payload = {
            'schema_version': SCHEMA_VERSION,
            'version_id': version_id,
            'work_id': work_id,
            'project_id': project_id,
            'chapter_id': chapter_id,
            'version_no': int(version_no),
            'accepted_artifact_id': artifact_id,
            'artifact_sha256': artifact_sha256,
            'quality_report_sha256': quality_report_sha256,
            'production_task_id': production_task_id,
            'production_run_id': production_run_id,
            'production_cost_entry_id': production_cost_entry_id,
            'status': status,
            'created_at': authoritative_timestamp(),
        }
        return self.append(
            'version', payload, idempotency_key=idempotency_key,
            task_id=task_id, run_id=run_id, department_id=department_id,
            work_id=work_id, version_id=version_id,
        )

    def register_zero_cost(
        self,
        *,
        idempotency_key: str, category: str, task_id: str,
        run_id: str | None = None, department_id: str | None = None,
        source: str = 'local-novel-production',
    ) -> dict[str, object]:
        cost_id = 'cost-' + sha256_text(
            f'zero|{task_id}|{idempotency_key}'
        )[:24]
        payload = {
            'schema_version': SCHEMA_COST,
            'cost_id': cost_id,
            'reservation_id': None,
            'task_id': task_id,
            'run_id': run_id,
            'department_id': department_id,
            'category': category,
            'estimated_fen': 0,
            'actual_fen': 0,
            'source': source,
            'provider_id': None,
            'tool_id': None,
            'currency': 'CNY',
            'created_at': authoritative_timestamp(),
        }
        return self.append(
            'cost', payload, idempotency_key=idempotency_key,
            task_id=task_id, run_id=run_id, department_id=department_id,
        )

    def register_platform(
        self,
        platform_id: str,
        *,
        idempotency_key: str, adapter_id: str = 'local-shadow/v1',
        capability_status: str = 'shadow',
        published: bool = False,
        mode: str | None = None,
        external_cost_fen: int = 0,
        evidence: Mapping[str, object] | None = None,
        action_id: str | None = None,
        task_id: str | None = None, run_id: str | None = None,
        department_id: str | None = None,
    ) -> dict[str, object]:
        _validate_id(platform_id, kind='platform_id')
        if published:
            raise EconomicLedgerError(
                ECON_PLATFORM_EXTERNAL_FORBIDDEN,
                'W1-1 platform records must have published=false',
            )
        payload = {
            'schema_version': SCHEMA_PLATFORM,
            'platform_id': platform_id,
            'adapter_id': adapter_id,
            'mode': mode or 'local-shadow',
            'capability_status': capability_status,
            'published': False,
            'external_cost_fen': int(external_cost_fen),
            'evidence': dict(evidence or {}),
            'action_id': action_id,
            'created_at': authoritative_timestamp(),
        }
        return self.append(
            'platform', payload, idempotency_key=idempotency_key,
            task_id=task_id, run_id=run_id, department_id=department_id,
            platform_id=platform_id,
        )

    def register_settlement(
        self,
        settlement_id: str,
        *,
        idempotency_key: str, status: str = 'observed',
        amount_fen: int = 0, platform_id: str | None = None,
        evidence_refs: Sequence[str] | None = None, receipt_hash: str | None = None,
        task_id: str | None = None, run_id: str | None = None,
        department_id: str | None = None,
    ) -> dict[str, object]:
        _validate_id(settlement_id, kind='settlement_id')
        if status == 'confirmed' and amount_fen > 0:
            if not evidence_refs or not receipt_hash:
                raise EconomicLedgerError(
                    ECON_SETTLEMENT_EVIDENCE_REQUIRED,
                    'positive confirmed settlement requires evidence + receipt hash',
                )
        payload = {
            'schema_version': SCHEMA_SETTLEMENT,
            'settlement_id': settlement_id,
            'status': status,
            'amount_fen': int(amount_fen),
            'platform_id': platform_id,
            'evidence_refs': list(evidence_refs or []),
            'receipt_hash': receipt_hash,
            'created_at': authoritative_timestamp(),
        }
        return self.append(
            'settlement', payload, idempotency_key=idempotency_key,
            task_id=task_id, run_id=run_id, department_id=department_id,
            settlement_id=settlement_id,
        )

    # -- export / restore -------------------------------------------------- #
    def export(self, target_dir: Any) -> dict[str, object]:
        target = Path(target_dir).resolve()
        if target.exists() and any(target.iterdir()):
            raise EconomicLedgerError(
                ECON_EXPORT_INCOMPLETE, 'export target directory must be empty'
            )
        target.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if not self.ledger_path.exists():
                raise EconomicLedgerError(ECON_EXPORT_INCOMPLETE, 'nothing to export')
            files: list[dict[str, object]] = []
            export_id = 'export-' + sha256_text(
                self.ledger_path.read_text(encoding='utf-8')
            )[:16]
            dest_ledger = target / 'ledger.jsonl'
            dest_ledger.write_text(
                self.ledger_path.read_text(encoding='utf-8'), encoding='utf-8', newline='\n'
            )
            files.append(self._file_record(target, dest_ledger))
            if self.index_path.exists():
                dest_index = target / 'index.json'
                dest_index.write_text(
                    self.index_path.read_text(encoding='utf-8'), encoding='utf-8', newline='\n'
                )
                files.append(self._file_record(target, dest_index))
            if self.snapshot_path.exists():
                dest_snap = target / 'snapshots' / 'latest.json'
                dest_snap.parent.mkdir(parents=True, exist_ok=True)
                dest_snap.write_text(
                    self.snapshot_path.read_text(encoding='utf-8'), encoding='utf-8', newline='\n'
                )
                files.append(self._file_record(target, dest_snap))
        manifest = {
            'schema_version': SCHEMA_EXPORT,
            'export_id': export_id,
            'source_root': str(self.root),
            'currency': 'CNY',
            'files': files,
            'entry_count': self.count(),
            'exported_at': authoritative_timestamp(),
        }
        manifest_path = target / 'manifest.json'
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        return manifest

    @staticmethod
    def _file_record(target: Path, path: Path) -> dict[str, object]:
        data = path.read_bytes()
        return {
            'relative_path': str(path.relative_to(target)).replace('\\', '/'),
            'sha256': sha256_text(data.decode('utf-8', 'replace')),
            'bytes': len(data),
        }

    @classmethod
    def restore(cls, source_dir: Any, target_root: Any) -> 'EconomicLedger':
        source = Path(source_dir).resolve()
        manifest_path = source / 'manifest.json'
        if not manifest_path.is_file():
            raise EconomicLedgerError(ECON_RESTORE_CONFLICT, 'manifest.json missing')
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise EconomicLedgerError(ECON_RESTORE_CONFLICT, f'bad manifest: {exc}') from exc
        if manifest.get('schema_version') != SCHEMA_EXPORT:
            raise EconomicLedgerError(
                ECON_SCHEMA_UNSUPPORTED, 'manifest is not an economic-export/v1'
            )
        target = Path(target_root).resolve()
        target.mkdir(parents=True, exist_ok=True)
        ledger = cls(target)
        for record in manifest.get('files', []):
            rel = str(record.get('relative_path')).replace('\\', '/')
            if rel.startswith('/') or '..' in rel:
                raise EconomicLedgerError(
                    ECON_RESTORE_CONFLICT, f'traversal rejected: {rel!r}'
                )
            src = source / rel
            if not src.is_file():
                raise EconomicLedgerError(
                    ECON_RESTORE_CONFLICT, f'missing export file: {rel!r}'
                )
            raw = src.read_text(encoding='utf-8')
            expected = str(record.get('sha256'))
            if sha256_text(raw) != expected:
                raise EconomicLedgerError(
                    ECON_RESTORE_CONFLICT, f'hash mismatch for {rel!r}'
                )
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(raw, encoding='utf-8', newline='\n')
        ledger.rebuild()
        return ledger


# --------------------------------------------------------------------------- #
# Budget truth (contract §7, §8)
# --------------------------------------------------------------------------- #
class EconomicBudgetTruth:
    '''Persistent budget truth with reservation / commit / release lifecycle.'''

    def __init__(
        self,
        policy: EconomicBudgetPolicy,
        ledger: EconomicLedger | None = None,
        *,
        unavailable: bool = False,
    ) -> None:
        self.policy = policy
        policy.validate()
        self.ledger = ledger
        self.unavailable = unavailable
        self._lock = threading.RLock()
        # reservation_id -> latest reservation payload
        self._reservations: dict[str, dict[str, object]] = {}
        self._reservation_index: dict[str, str] = {}  # idempotency -> reservation_id
        if ledger is not None:
            for entry in ledger.entries(event_type='reservation'):
                payload = entry.get('payload') or {}
                rid = str(payload.get('reservation_id'))
                self._reservations[rid] = payload
                key = str(entry.get('idempotency_key'))
                self._reservation_index[key] = rid
            for entry in ledger.entries(event_type='policy'):
                # a policy entry records the active policy; reconcile if needed
                pass

    # -- preflight --------------------------------------------------------- #
    def preflight(self, category: str, amount_fen: int) -> dict[str, object]:
        if amount_fen < 0:
            return self._decision(
                False, ECON_BUDGET_POLICY_INVALID, category, amount_fen,
            )
        if amount_fen == 0:
            # local, no-external-effect work is allowed even without truth.
            if self.unavailable:
                return self._decision(
                    True, 'economic-budget-allowed', category, 0,
                    warning=False, allowed_without_truth=True,
                )
            return self._decision(True, 'economic-budget-allowed', category, 0)
        if self.unavailable:
            return self._decision(
                False, ECON_BUDGET_TRUTH_UNAVAILABLE, category, amount_fen,
            )
        if category not in self.policy.category_caps_fen:
            return self._decision(
                False, ECON_BUDGET_CATEGORY_EXCEEDED, category, amount_fen,
            )
        state = self._running_state()
        category_cap = self.policy.category_caps_fen[category]
        operating_cap = self.policy.operating_cap_fen()
        projected_category = state['category_spent'][category] + state['category_reserved'][category] + amount_fen
        projected_operating = state['spent_fen'] + state['reserved_fen'] + amount_fen
        if projected_category > category_cap:
            return self._decision(
                False, ECON_BUDGET_CATEGORY_EXCEEDED, category, amount_fen,
                projected_category_fen=projected_category,
                category_cap_fen=category_cap,
            )
        if projected_operating > operating_cap:
            return self._decision(
                False, ECON_BUDGET_OPERATING_EXCEEDED, category, amount_fen,
                projected_operating_fen=projected_operating,
                operating_cap_fen=operating_cap,
            )
        threshold = self.policy.warning_ratio_milli
        warning = (
            category_cap > 0
            and projected_category * 1000 >= category_cap * threshold
        ) or (
            operating_cap > 0
            and projected_operating * 1000 >= operating_cap * threshold
        )
        code = 'economic-budget-warning' if warning else 'economic-budget-allowed'
        return self._decision(
            True, code, category, amount_fen, warning=warning,
            projected_category_fen=projected_category,
            category_cap_fen=category_cap,
            projected_operating_fen=projected_operating,
            operating_cap_fen=operating_cap,
        )

    @staticmethod
    def _decision(
        allowed: bool, code: str, category: str, amount_fen: int, *,
        warning: bool = False, **extra: object,
    ) -> dict[str, object]:
        return {
            'allowed': allowed,
            'code': code,
            'category': category,
            'estimated_amount_fen': amount_fen,
            'warning': warning,
            'currency': 'CNY',
            **extra,
        }

    def _running_state(self) -> dict[str, object]:
        category_spent: dict[str, int] = {c: 0 for c in self.policy.category_caps_fen}
        category_reserved: dict[str, int] = {c: 0 for c in self.policy.category_caps_fen}
        spent_fen = 0
        reserved_fen = 0
        latest_reservation: dict[str, dict[str, object]] = {}
        if self.ledger is not None:
            for entry in self.ledger.entries(event_type='cost'):
                payload = entry.get('payload') or {}
                cat = str(payload.get('category'))
                try:
                    amount = int(payload.get('actual_fen', 0))
                except (TypeError, ValueError):
                    amount = 0
                spent_fen += amount
                if cat in category_spent:
                    category_spent[cat] += amount
            # Reservations are append-only: commit/release append a new entry
            # with the transitioned state. Keep the LATEST state per
            # reservation_id so reserved_fen reflects only still-active holds.
            for entry in self.ledger.entries(event_type='reservation'):
                payload = entry.get('payload') or {}
                rid = str(payload.get('reservation_id'))
                latest_reservation[rid] = payload
        for payload in latest_reservation.values():
            if payload.get('state') != 'active':
                continue
            cat = str(payload.get('category'))
            try:
                amount = int(payload.get('amount_fen', 0))
            except (TypeError, ValueError):
                amount = 0
            reserved_fen += amount
            if cat in category_reserved:
                category_reserved[cat] += amount
        return {
            'spent_fen': spent_fen,
            'reserved_fen': reserved_fen,
            'category_spent': category_spent,
            'category_reserved': category_reserved,
        }

    # -- reserve ----------------------------------------------------------- #
    def reserve(
        self,
        task_id: str,
        category: str,
        amount_fen: int,
        idempotency_key: str,
        *,
        run_id: str | None = None,
        department_id: str | None = None,
    ) -> dict[str, object]:
        if amount_fen < 0:
            raise EconomicLedgerError(
                ECON_BUDGET_POLICY_INVALID, 'reservation amount must be non-negative'
            )
        decision = self.preflight(category, amount_fen)
        if not decision['allowed']:
            raise EconomicLedgerError(decision['code'], decision.get('message', ''))
        reservation_id = 'res-' + sha256_text(
            f'{task_id}|{category}|{amount_fen}|{idempotency_key}'
        )[:24]
        ts = authoritative_timestamp()
        payload = {
            'schema_version': SCHEMA_RESERVATION,
            'reservation_id': reservation_id,
            'task_id': task_id,
            'run_id': run_id,
            'department_id': department_id,
            'category': category,
            'amount_fen': int(amount_fen),
            'idempotency_key': idempotency_key,
            'state': 'active',
            'created_at': ts,
            'committed_cost_entry_id': None,
        }
        key = f'{reservation_id}:active'
        with self._lock:
            env = self._append_payload(
                'reservation', payload, idempotency_key=key,
                task_id=task_id, run_id=run_id, department_id=department_id,
            )
            self._reservations[reservation_id] = payload
            self._reservation_index[key] = reservation_id
        return env  # type: ignore[return-value]

    # -- commit ------------------------------------------------------------ #
    def commit(
        self,
        reservation_id: str,
        actual_fen: int,
        *,
        source: str = '',
        provider_id: str | None = None,
        tool_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        department_id: str | None = None,
    ) -> dict[str, object]:
        if actual_fen < 0:
            raise EconomicLedgerError(
                ECON_BUDGET_POLICY_INVALID, 'actual cost must be non-negative'
            )
        with self._lock:
            reservation = self._reservation(reservation_id)
            if reservation is None:
                raise EconomicLedgerError(
                    ECON_COST_WITHOUT_RESERVATION,
                    f'reservation not found: {reservation_id!r}',
                )
            if reservation.get('state') == 'committed':
                if int(reservation.get('amount_fen', 0)) == actual_fen:
                    raise EconomicLedgerError(
                        ECON_RESERVATION_CONFLICT,
                        'reservation already committed with identical amount',
                    )
                raise EconomicLedgerError(
                    ECON_RESERVATION_CONFLICT,
                    'reservation already committed with different amount',
                )
            if reservation.get('state') == 'released':
                raise EconomicLedgerError(
                    ECON_RESERVATION_CONFLICT,
                    'cannot commit a released reservation',
                )
            reserved = int(reservation.get('amount_fen', 0))
            category = str(reservation.get('category'))
            if actual_fen > reserved:
                delta = actual_fen - reserved
                decision = self.preflight(category, delta)
                if not decision['allowed']:
                    raise EconomicLedgerError(decision['code'], 'delta over cap')
            # mark reservation committed
            committed_payload = dict(reservation)
            committed_payload['state'] = 'committed'
            committed_payload['committed_at'] = authoritative_timestamp()
            key_commit = f'{reservation_id}:committed'
            self._append_payload(
                'reservation', committed_payload, idempotency_key=key_commit,
                task_id=str(reservation.get('task_id')),
                run_id=reservation.get('run_id'),
                department_id=reservation.get('department_id'),
            )
            self._reservations[reservation_id] = committed_payload
            # write the actual cost record
            cost_id = 'cost-' + sha256_text(
                f'{reservation_id}|{actual_fen}|{authoritative_timestamp()}'
            )[:24]
            cost_payload = {
                'schema_version': SCHEMA_COST,
                'cost_id': cost_id,
                'reservation_id': reservation_id,
                'task_id': task_id or reservation.get('task_id'),
                'run_id': run_id or reservation.get('run_id'),
                'department_id': department_id or reservation.get('department_id'),
                'category': category,
                'estimated_fen': reserved,
                'actual_fen': int(actual_fen),
                'source': source,
                'provider_id': provider_id,
                'tool_id': tool_id,
                'currency': 'CNY',
                'created_at': authoritative_timestamp(),
            }
            env = self._append_payload(
                'cost', cost_payload, idempotency_key=f'{cost_id}:cost',
                task_id=cost_payload['task_id'],
                run_id=cost_payload['run_id'],
                department_id=cost_payload['department_id'],
            )
            return env  # type: ignore[return-value]

    # -- release ----------------------------------------------------------- #
    def release(self, reservation_id: str) -> dict[str, object]:
        with self._lock:
            reservation = self._reservation(reservation_id)
            if reservation is None:
                raise EconomicLedgerError(
                    ECON_COST_WITHOUT_RESERVATION,
                    f'reservation not found: {reservation_id!r}',
                )
            if reservation.get('state') == 'released':
                return self._last_reservation_entry(reservation_id)  # type: ignore[return-value]
            if reservation.get('state') == 'committed':
                raise EconomicLedgerError(
                    ECON_RESERVATION_CONFLICT,
                    'cannot release a committed reservation',
                )
            released_payload = dict(reservation)
            released_payload['state'] = 'released'
            released_payload['released_at'] = authoritative_timestamp()
            key_release = f'{reservation_id}:released'
            env = self._append_payload(
                'reservation', released_payload, idempotency_key=key_release,
                task_id=str(reservation.get('task_id')),
                run_id=reservation.get('run_id'),
                department_id=reservation.get('department_id'),
            )
            self._reservations[reservation_id] = released_payload
            return env  # type: ignore[return-value]

    def _reservation(self, reservation_id: str) -> dict[str, object] | None:
        return self._reservations.get(reservation_id)

    def _last_reservation_entry(self, reservation_id: str) -> dict[str, object] | None:
        if self.ledger is None:
            return None
        found: dict[str, object] | None = None
        for entry in self.ledger.entries(event_type='reservation'):
            if (entry.get('payload') or {}).get('reservation_id') == reservation_id:
                found = entry
        return found

    def _append_payload(self, event_type: str, payload: dict[str, object], **kw: object) -> dict[str, object]:
        if self.ledger is None:
            # No ledger attached: still return an envelope-shaped record so
            # callers can reason about the result; nothing is persisted.
            payload_sha = sha256_text(canonical_json(payload))
            return {
                'schema_version': SCHEMA_ENVELOPE,
                'entry_id': _derive_entry_id(
                    str(kw.get('idempotency_key', '')), payload_sha
                ),
                'idempotency_key': kw.get('idempotency_key'),
                'event_type': event_type,
                'occurred_at': authoritative_timestamp(),
                'currency': 'CNY',
                'payload': payload,
                'payload_sha256': payload_sha,
            }
        return self.ledger.append(event_type, payload, **kw)  # type: ignore[arg-type]

    # -- status ------------------------------------------------------------ #
    def status(self) -> dict[str, object]:
        with self._lock:
            if self.unavailable:
                return {
                    'currency': 'CNY',
                    'available': False,
                    'code': ECON_BUDGET_TRUTH_UNAVAILABLE,
                    'policy': self.policy.to_dict(),
                }
            state = self._running_state()
            per_category = {}
            warning = False
            threshold = self.policy.warning_ratio_milli
            for cat, cap in self.policy.category_caps_fen.items():
                spent = state['category_spent'][cat]
                reserved = state['category_reserved'][cat]
                available = cap - spent - reserved
                if cap > 0 and (spent + reserved) * 1000 >= cap * threshold:
                    warning = True
                per_category[cat] = {
                    'cap_fen': cap,
                    'spent_fen': spent,
                    'reserved_fen': reserved,
                    'available_fen': available,
                }
            operating_cap = self.policy.operating_cap_fen()
            available_operating = operating_cap - state['spent_fen'] - state['reserved_fen']
            if operating_cap > 0 and (state['spent_fen'] + state['reserved_fen']) * 1000 >= operating_cap * threshold:
                warning = True
            return {
                'currency': 'CNY',
                'available': True,
                'code': 'economic-budget-warning' if warning else 'economic-budget-allowed',
                'warning': warning,
                'total_fen': self.policy.total_fen,
                'reserve_fen': self.policy.reserve_fen,
                'operating_cap_fen': operating_cap,
                'spent_fen': state['spent_fen'],
                'reserved_fen': state['reserved_fen'],
                'available_operating_fen': available_operating,
                'by_category': per_category,
                'policy': self.policy.to_dict(),
            }


# --------------------------------------------------------------------------- #
# Legacy budget ledger adapter (contract §7, §9.1)
# --------------------------------------------------------------------------- #
class LegacyBudgetLedgerAdapter:
    '''Preserves the existing BudgetLedger API for backward compatibility.'''

    def __init__(self, budget_ledger: Any) -> None:
        self._ledger = budget_ledger

    def records(self) -> list[dict[str, object]]:
        return self._ledger.records()

    def spent_fen(self, category: str | None = None) -> int:
        return self._ledger.spent_fen(category)

    def preflight(self, category: str, estimated_amount_fen: int) -> Any:
        return self._ledger.preflight(category, estimated_amount_fen)

    def append(self, record: Any) -> dict[str, object]:
        return self._ledger.append(record)

    def as_economic_policy(self) -> EconomicBudgetPolicy:
        policy = self._ledger.policy
        return EconomicBudgetPolicy(
            total_fen=int(policy.total_fen),
            reserve_fen=int(policy.reserve_fen),
            category_caps_fen={
                str(k): int(v) for k, v in policy.category_caps_fen.items()
            },
            warning_ratio_milli=int(policy.warning_ratio_milli),
            currency=str(policy.currency),
        )
