'''Restart-safe local state spine.

 portable local state spine required before the novel MVP vertical loop:

    governed IDs
    -> append-only task events
    -> atomic checkpoint/snapshot
    -> derived SQLite index
    -> content-addressed artifact/evidence registration
    -> process restart
    -> checkpoint validation and DAG resume
    -> export, restore, index rebuild, archive

 A restarted process must not execute completed DAG nodes again. Incomplete
 nodes may continue. SQLite is a disposable query index; it is never the sole
 truth and must be rebuildable from the authoritative files.

 This module composes the existing durable authorities ``TaskLedger`` and
 ``ContentAddressedStore``. It intentionally does not import the company event
 bus or any dashboard/network dependency.
'''

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Iterable, Mapping, Protocol

from .storage import ContentAddressedStore
from .task_ledger import TaskLedger
from .timebase import authoritative_timestamp


# --------------------------------------------------------------------------- #
# Frozen schema versions
# --------------------------------------------------------------------------- #
RUN_SCHEMA = 'state-spine-run/v1'
TASK_SCHEMA = 'state-spine-task/v1'
DEPT_SCHEMA = 'state-spine-department/v1'
SNAPSHOT_SCHEMA = 'state-spine-snapshot/v1'
ARTIFACT_SCHEMA = 'state-spine-artifact/v1'
EXPORT_SCHEMA = 'state-spine-export/v1'
MEMORY_QUERY_SCHEMA = 'canonical-memory-query/v1'
MEMORY_RESULT_SCHEMA = 'canonical-memory-result/v1'
MIGRATION_SCHEMA = 'state-spine-migration/v1'

DB_USER_VERSION = 1
SUPPORTED_DB_VERSIONS = frozenset({1})


# --------------------------------------------------------------------------- #
# Stable blocking codes
# --------------------------------------------------------------------------- #
CODE_INVALID_ID = 'state-spine-invalid-id'
CODE_SCHEMA_UNSUPPORTED = 'state-spine-schema-unsupported'
CODE_DB_NEWER = 'state-spine-database-newer-than-runtime'
CODE_BINDING_CONFLICT = 'state-spine-binding-conflict'
CODE_CHECKPOINT_CORRUPT = 'state-spine-checkpoint-corrupt'
CODE_HASH_MISMATCH = 'state-spine-hash-mismatch'
CODE_ARTIFACT_MISSING = 'state-spine-artifact-missing'
CODE_EXPORT_INCOMPLETE = 'state-spine-export-incomplete'
CODE_RESTORE_CONFLICT = 'state-spine-restore-conflict'
CODE_ARCHIVE_WRITE_BLOCKED = 'state-spine-archive-write-blocked'
CODE_MEMORY_UNAVAILABLE = 'state-spine-memory-unavailable'
CODE_INDEX_REBUILD_FAILED = 'state-spine-index-rebuild-failed'


class StateSpineError(Exception):
    '''Stable-code error raised by the state spine.'''

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Governed IDs
# --------------------------------------------------------------------------- #
_ID_RE = re.compile(r'^(run|task|dept|artifact)-[a-z0-9][a-z0-9._-]{2,63}$')
_ID_PREFIXES = ('run', 'task', 'dept', 'artifact')


class UnifiedIds:
    '''Create and validate the portable governed IDs.'''

    @classmethod
    def validate(cls, id_str: str) -> str:
        if not isinstance(id_str, str) or not _ID_RE.fullmatch(id_str):
            raise StateSpineError(
                CODE_INVALID_ID, f'Invalid governed ID: {id_str!r}'
            )
        return id_str

    @classmethod
    def kind(cls, id_str: str) -> str:
        cls.validate(id_str)
        return id_str.split('-', 1)[0]

    @classmethod
    def create(cls, prefix: str, *parts: str) -> str:
        if prefix not in _ID_PREFIXES:
            raise StateSpineError(
                CODE_INVALID_ID, f'Unknown ID prefix: {prefix!r}'
            )
        slug = cls._slugify('_'.join(parts)) if parts else 'gen'
        suffix = f'{slug}-{uuid.uuid4().hex[:8]}'
        id_str = f'{prefix}-{suffix}'
        cls.validate(id_str)
        return id_str

    @staticmethod
    def _slugify(text: str) -> str:
        out: list[str] = []
        for ch in text.lower():
            out.append(ch if ch.isalnum() else '-')
        slug = '-'.join(chunk for chunk in ''.join(out).split('-') if chunk)
        slug = slug[:40]
        return slug or 'x'


# --------------------------------------------------------------------------- #
# Canonical memory reader (injected, provider-neutral)
# --------------------------------------------------------------------------- #
class CanonicalMemoryReader(Protocol):
    '''Read-only, injected, provider-neutral canonical memory query boundary.'''

    def query(self, query: Mapping[str, object]) -> Mapping[str, object]: ...


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _canonical(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return authoritative_timestamp()


def _write_json_atomic(path: Path, obj: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f'.{path.stem}.', suffix='.tmp', dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(_canonical(obj) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _safe_rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _reject_traversal(root: Path, rel_path: str) -> Path:
    candidate = (root / rel_path).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise StateSpineError(
            CODE_RESTORE_CONFLICT, f'Path escapes root: {rel_path!r}'
        )
    return candidate


# --------------------------------------------------------------------------- #
# File layout
# --------------------------------------------------------------------------- #
def _run_path(root: Path, run_id: str) -> Path:
    return root / 'runs' / f'{run_id}.json'


def _dept_path(root: Path, dept_id: str) -> Path:
    return root / 'departments' / f'{dept_id}.json'


def _task_path(root: Path, task_id: str) -> Path:
    return root / 'tasks' / f'{task_id}.json'


def _ledger_path(root: Path, task_id: str) -> Path:
    return root / 'ledger' / f'{task_id}.jsonl'


def _checkpoint_path(root: Path, task_id: str) -> Path:
    return root / 'checkpoints' / f'{task_id}.checkpoint.json'


def _snapshot_path(root: Path, snapshot_id: str) -> Path:
    return root / 'snapshots' / f'{snapshot_id}.json'


def _artifact_record_path(root: Path, artifact_id: str) -> Path:
    return root / 'artifacts' / 'records' / f'{artifact_id}.json'


_SCHEMA_V1 = '''
CREATE TABLE IF NOT EXISTS migrations (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    root_rel TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS departments (
    dept_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    dept_id TEXT,
    schema_version TEXT NOT NULL,
    status TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT,
    schema_version TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT,
    dept_id TEXT,
    kind TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS events (
    task_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    event_ts TEXT NOT NULL,
    PRIMARY KEY(task_id, event_id)
);
'''


# --------------------------------------------------------------------------- #
# SQLite state index (derived)
# --------------------------------------------------------------------------- #
class SQLiteStateIndex:
    '''Transactional, rebuildable SQLite metadata index. Never the sole truth.'''

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._conn.execute('PRAGMA foreign_keys = ON')
        try:
            self._migrate()
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass
            raise

    # -- migration -------------------------------------------------------- #
    def _migrate(self) -> None:
        with self._lock:
            row = self._conn.execute('PRAGMA user_version').fetchone()
            version = int(row[0]) if row else 0
            if version == 0:
                self._conn.executescript(_SCHEMA_V1)
                self._conn.execute('PRAGMA user_version = 1')
                self._conn.execute(
                    'INSERT INTO migrations(id, version, applied_at, note) '
                    'VALUES (1, 1, ?, ?)',
                    (authoritative_timestamp(), 'baseline v0->v1'),
                )
            elif version in SUPPORTED_DB_VERSIONS:
                # Idempotent reopen: ensure tables exist.
                self._conn.executescript(_SCHEMA_V1)
            else:
                raise StateSpineError(
                    CODE_DB_NEWER,
                    f'Database version {version} is newer than supported '
                    f'{sorted(SUPPORTED_DB_VERSIONS)}.',
                )

    @property
    def user_version(self) -> int:
        with self._lock:
            row = self._conn.execute('PRAGMA user_version').fetchone()
            return int(row[0]) if row else 0

    # -- indexing --------------------------------------------------------- #
    def index_run(self, record: Mapping[str, object]) -> None:
        with self._lock:
            self._conn.execute(
                'INSERT OR REPLACE INTO runs '
                '(run_id, schema_version, status, created_at, updated_at, '
                'archived, root_rel) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    str(record['run_id']), str(record['schema_version']),
                    str(record['status']), str(record['created_at']),
                    str(record.get('updated_at') or record['created_at']),
                    1 if record.get('archived') else 0,
                    str(record.get('root_rel', '')),
                ),
            )

    def index_department(self, record: Mapping[str, object]) -> None:
        with self._lock:
            self._conn.execute(
                'INSERT OR REPLACE INTO departments '
                '(dept_id, run_id, schema_version, created_at) '
                'VALUES (?, ?, ?, ?)',
                (
                    str(record['dept_id']), str(record['run_id']),
                    str(record['schema_version']), str(record['created_at']),
                ),
            )

    def index_task(self, record: Mapping[str, object]) -> None:
        with self._lock:
            self._conn.execute(
                'INSERT OR REPLACE INTO tasks '
                '(task_id, run_id, dept_id, schema_version, status, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (
                    str(record['task_id']), str(record['run_id']),
                    record.get('dept_id') and str(record['dept_id']),
                    str(record['schema_version']),
                    record.get('status') and str(record['status']),
                    str(record['created_at']),
                ),
            )

    def index_snapshot(self, record: Mapping[str, object]) -> None:
        with self._lock:
            self._conn.execute(
                'INSERT OR REPLACE INTO snapshots '
                '(snapshot_id, run_id, task_id, schema_version, sha256, bytes, '
                'created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    str(record['snapshot_id']), str(record['run_id']),
                    record.get('task_id') and str(record['task_id']),
                    str(record['schema_version']),
                    str(record['checkpoint_sha256']),
                    int(record['checkpoint_bytes']),
                    str(record['created_at']),
                ),
            )

    def index_artifact(self, record: Mapping[str, object]) -> None:
        with self._lock:
            self._conn.execute(
                'INSERT OR REPLACE INTO artifacts '
                '(artifact_id, run_id, task_id, dept_id, kind, schema_version, '
                'sha256, bytes, media_type, rel_path, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    str(record['artifact_id']), str(record['run_id']),
                    record.get('task_id') and str(record['task_id']),
                    record.get('dept_id') and str(record['dept_id']),
                    str(record['kind']), str(record['schema_version']),
                    str(record['sha256']), int(record['bytes']),
                    str(record['media_type']), str(record['portable_path']),
                    str(record['created_at']),
                ),
            )

    def index_event(
        self, task_id: str, event_id: str, kind: str, timestamp: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                'INSERT OR REPLACE INTO events '
                '(task_id, event_id, event_kind, event_ts) VALUES (?, ?, ?, ?)',
                (task_id, event_id, kind, timestamp),
            )

    # -- queries ---------------------------------------------------------- #
    def get_run(self, run_id: str) -> dict[str, object] | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT run_id, schema_version, status, created_at, '
                'updated_at, archived FROM runs WHERE run_id = ?',
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            'run_id': row[0], 'schema_version': row[1], 'status': row[2],
            'created_at': row[3], 'updated_at': row[4], 'archived': bool(row[5]),
        }

    def get_task(self, task_id: str) -> dict[str, object] | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT task_id, run_id, dept_id, schema_version, status '
                'FROM tasks WHERE task_id = ?',
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            'task_id': row[0], 'run_id': row[1], 'dept_id': row[2],
            'schema_version': row[3], 'status': row[4],
        }

    def get_artifact(self, artifact_id: str) -> dict[str, object] | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT artifact_id, run_id, task_id, dept_id, kind, '
                'schema_version, sha256, bytes, media_type, rel_path, created_at '
                'FROM artifacts WHERE artifact_id = ?',
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            'artifact_id': row[0], 'run_id': row[1], 'task_id': row[2],
            'dept_id': row[3], 'kind': row[4], 'schema_version': row[5],
            'sha256': row[6], 'bytes': row[7], 'media_type': row[8],
            'rel_path': row[9], 'created_at': row[10],
        }

    def list_runs(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT run_id, status, archived FROM runs ORDER BY run_id'
            ).fetchall()
        return [
            {'run_id': r[0], 'status': r[1], 'archived': bool(r[2])}
            for r in rows
        ]

    def event_count(self, task_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                'SELECT COUNT(*) FROM events WHERE task_id = ?', (task_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def artifacts_for_run(self, run_id: str) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT artifact_id, run_id, task_id, dept_id, kind, '
                'schema_version, sha256, bytes, media_type, rel_path, created_at '
                'FROM artifacts WHERE run_id = ? ORDER BY artifact_id',
                (run_id,),
            ).fetchall()
        return [
            {
                'artifact_id': r[0], 'run_id': r[1], 'task_id': r[2],
                'dept_id': r[3], 'kind': r[4], 'schema_version': r[5],
                'sha256': r[6], 'bytes': r[7], 'media_type': r[8],
                'rel_path': r[9], 'created_at': r[10],
            }
            for r in rows
        ]

    def task_count(self, run_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                'SELECT COUNT(*) FROM tasks WHERE run_id = ?', (run_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    # -- rebuild ---------------------------------------------------------- #
    def rebuild(self, root: Path) -> None:
        root = Path(root).resolve()
        try:
            with self._lock:
                self._conn.execute('BEGIN')
                for table in (
                    'events', 'snapshots', 'artifacts',
                    'tasks', 'departments', 'runs', 'migrations',
                ):
                    self._conn.execute(f'DELETE FROM {table}')
                for path in sorted((root / 'runs').glob('*.json')):
                    rec = self._read_json(path)
                    if isinstance(rec, dict) and rec.get('schema_version') == RUN_SCHEMA:
                        self.index_run(rec)
                for path in sorted((root / 'departments').glob('*.json')):
                    rec = self._read_json(path)
                    if isinstance(rec, dict) and rec.get('schema_version') == DEPT_SCHEMA:
                        self.index_department(rec)
                for path in sorted((root / 'tasks').glob('*.json')):
                    rec = self._read_json(path)
                    if isinstance(rec, dict) and rec.get('schema_version') == TASK_SCHEMA:
                        self.index_task(rec)
                for path in sorted((root / 'snapshots').glob('*.json')):
                    rec = self._read_json(path)
                    if isinstance(rec, dict) and rec.get('schema_version') == SNAPSHOT_SCHEMA:
                        self.index_snapshot(rec)
                for path in sorted((root / 'ledger').glob('*.jsonl')):
                    task_id = path.stem
                    for line in path.read_text(encoding='utf-8').splitlines():
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise StateSpineError(
                                CODE_INDEX_REBUILD_FAILED,
                                f'Corrupt ledger line in {path.name}: {exc}',
                            )
                        self.index_event(
                            task_id, str(event.get('event_id', '')),
                            str(event.get('kind', '')),
                            str(event.get('timestamp', '')),
                        )
                records_dir = root / 'artifacts' / 'records'
                if records_dir.is_dir():
                    for path in sorted(records_dir.glob('*.json')):
                        rec = self._read_json(path)
                        if isinstance(rec, dict) and rec.get('schema_version') == ARTIFACT_SCHEMA:
                            self.index_artifact(rec)
                self._conn.execute('COMMIT')
        except StateSpineError:
            self._conn.execute('ROLLBACK')
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._conn.execute('ROLLBACK')
            raise StateSpineError(
                CODE_INDEX_REBUILD_FAILED,
                f'Failed to rebuild SQLite index: {exc}',
            ) from exc

    @staticmethod
    def _read_json(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError) as exc:
            raise StateSpineError(
                CODE_INDEX_REBUILD_FAILED, f'Cannot read {path}: {exc}'
            ) from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __del__(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# State-spine task ledger (TaskLedger-compatible, indexes after authority)
# --------------------------------------------------------------------------- #
class StateSpineTaskLedger:
    '''A ``TaskLedger``-compatible adapter that writes authoritative files
    first and then indexes them into the derived SQLite index.'''

    def __init__(self, spine: 'RestartSafeStateSpine', run_id: str) -> None:
        UnifiedIds.validate(run_id)
        if not spine.run_exists(run_id):
            raise StateSpineError(
                CODE_BINDING_CONFLICT, f'Unknown run: {run_id}'
            )
        self.spine = spine
        self.run_id = run_id
        self._ledger = TaskLedger(spine.root / 'ledger', spine.root / 'checkpoints')

    def _assert_writable(self) -> None:
        if self.spine.is_archived(self.run_id):
            raise StateSpineError(
                CODE_ARCHIVE_WRITE_BLOCKED,
                f'Run {self.run_id} is archived; new writes are blocked.',
            )

    def append(
        self, task_id: str, kind: str, payload: Mapping[str, object],
    ) -> dict[str, object]:
        UnifiedIds.validate(task_id)
        self._assert_writable()
        event = self._ledger.append(task_id, kind, dict(payload))
        self.spine._index.index_event(
            task_id, str(event.get('event_id', '')),
            str(event.get('kind', '')), str(event.get('timestamp', '')),
        )
        return event

    def events(self, task_id: str) -> Iterable[dict[str, object]]:
        UnifiedIds.validate(task_id)
        return self._ledger.events(task_id)

    def write_checkpoint(
        self, task_id: str, state: Mapping[str, object],
    ) -> Path:
        UnifiedIds.validate(task_id)
        self._assert_writable()
        path = self._ledger.write_checkpoint(task_id, dict(state))
        sha = _sha256_file(path)
        nbytes = path.stat().st_size
        snapshot_id = f'snapshot-{task_id}'
        record = {
            'schema_version': SNAPSHOT_SCHEMA,
            'snapshot_id': snapshot_id,
            'run_id': self.run_id,
            'task_id': task_id,
            'checkpoint_sha256': sha,
            'checkpoint_bytes': nbytes,
            'created_at': _now(),
        }
        _write_json_atomic(_snapshot_path(self.spine.root, snapshot_id), record)
        self.spine._index.index_snapshot(record)
        return path

    def read_checkpoint(self, task_id: str) -> dict[str, object] | None:
        UnifiedIds.validate(task_id)
        return self._ledger.read_checkpoint(task_id)


# --------------------------------------------------------------------------- #
# Restart-safe state spine
# --------------------------------------------------------------------------- #
class RestartSafeStateSpine:
    '''Create/open runs and register departments, tasks, artifacts, and
    evidence; recover, export, restore, and archive restart-safe state.'''

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._content_store = ContentAddressedStore(
            self.root, self.root / 'artifacts' / 'store',
        )
        self._index = SQLiteStateIndex(self.root / 'index.sqlite')

    # -- run lifecycle ---------------------------------------------------- #
    def run_exists(self, run_id: str) -> bool:
        return _run_path(self.root, run_id).exists()

    def is_archived(self, run_id: str) -> bool:
        record = self._index.get_run(run_id)
        return bool(record and record.get('archived'))

    def create_run(
        self, run_id: str | None = None, *, department_ids: Iterable[str] = (),
    ) -> str:
        run_id = run_id or UnifiedIds.create('run', 'spine')
        UnifiedIds.validate(run_id)
        if self.run_exists(run_id):
            raise StateSpineError(
                CODE_BINDING_CONFLICT, f'Run already exists: {run_id}'
            )
        now = _now()
        record = {
            'schema_version': RUN_SCHEMA,
            'run_id': run_id,
            'status': 'open',
            'created_at': now,
            'updated_at': now,
            'archived': False,
            'root_rel': _safe_rel(self.root, self.root),
        }
        _write_json_atomic(_run_path(self.root, run_id), record)
        self._index.index_run(record)
        for dept_id in department_ids:
            self.register_department(run_id, dept_id)
        return run_id

    def open_run(self, run_id: str) -> dict[str, object]:
        UnifiedIds.validate(run_id)
        path = _run_path(self.root, run_id)
        if not path.exists():
            raise StateSpineError(
                CODE_BINDING_CONFLICT, f'Run not found: {run_id}'
            )
        record = self._read_json_record(path, RUN_SCHEMA, 'run')
        self._index.index_run(record)
        return record

    def register_department(self, run_id: str, dept_id: str) -> dict[str, object]:
        UnifiedIds.validate(run_id)
        UnifiedIds.validate(dept_id)
        if not self.run_exists(run_id):
            raise StateSpineError(
                CODE_BINDING_CONFLICT, f'Unknown run: {run_id}'
            )
        if self.is_archived(run_id):
            raise StateSpineError(
                CODE_ARCHIVE_WRITE_BLOCKED,
                f'Run {run_id} is archived; new writes are blocked.',
            )
        if _dept_path(self.root, dept_id).exists():
            existing = self._read_json_record(
                _dept_path(self.root, dept_id), DEPT_SCHEMA, 'department',
            )
            if existing.get('run_id') != run_id:
                raise StateSpineError(
                    CODE_BINDING_CONFLICT,
                    f'Department {dept_id} already bound to another run.',
                )
        now = _now()
        record = {
            'schema_version': DEPT_SCHEMA,
            'dept_id': dept_id,
            'run_id': run_id,
            'created_at': now,
        }
        _write_json_atomic(_dept_path(self.root, dept_id), record)
        self._index.index_department(record)
        return record

    def register_task(
        self, run_id: str, task_id: str, *, dept_id: str | None = None,
    ) -> dict[str, object]:
        UnifiedIds.validate(run_id)
        UnifiedIds.validate(task_id)
        if dept_id is not None:
            UnifiedIds.validate(dept_id)
        if not self.run_exists(run_id):
            raise StateSpineError(
                CODE_BINDING_CONFLICT, f'Unknown run: {run_id}'
            )
        if self.is_archived(run_id):
            raise StateSpineError(
                CODE_ARCHIVE_WRITE_BLOCKED,
                f'Run {run_id} is archived; new writes are blocked.',
            )
        if _task_path(self.root, task_id).exists():
            existing = self._read_json_record(
                _task_path(self.root, task_id), TASK_SCHEMA, 'task',
            )
            if existing.get('run_id') != run_id:
                raise StateSpineError(
                    CODE_BINDING_CONFLICT,
                    f'Task {task_id} already bound to another run.',
                )
        now = _now()
        record = {
            'schema_version': TASK_SCHEMA,
            'task_id': task_id,
            'run_id': run_id,
            'dept_id': dept_id,
            'status': 'registered',
            'created_at': now,
        }
        _write_json_atomic(_task_path(self.root, task_id), record)
        self._index.index_task(record)
        return record

    def ledger_for_run(self, run_id: str) -> StateSpineTaskLedger:
        return StateSpineTaskLedger(self, run_id)

    # -- artifact / evidence ---------------------------------------------- #
    def _register_content(
        self, run_id: str, *, task_id: str | None, dept_id: str | None,
        kind: str, media_type: str, content: object,
    ) -> dict[str, object]:
        UnifiedIds.validate(run_id)
        if self.is_archived(run_id):
            raise StateSpineError(
                CODE_ARCHIVE_WRITE_BLOCKED,
                f'Run {run_id} is archived; new writes are blocked.',
            )
        if task_id is not None:
            UnifiedIds.validate(task_id)
        if dept_id is not None:
            UnifiedIds.validate(dept_id)
        if content is None:
            raise StateSpineError(
                CODE_INVALID_ID, 'Artifact content must not be None.'
            )
        raw = content.encode('utf-8') if isinstance(content, str) else json.dumps(
            content, ensure_ascii=False, sort_keys=True,
        ).encode('utf-8')
        content_sha = _sha256_bytes(raw)
        content_bytes = len(raw)
        artifact_id = UnifiedIds.create('artifact', kind)
        store_kind = 'evidence' if kind == 'evidence' else 'artifact'
        store_record = self._content_store.put(
            store_kind, task_id or run_id,
            {
                'schema_version': ARTIFACT_SCHEMA,
                'artifact_id': artifact_id,
                'run_id': run_id,
                'task_id': task_id,
                'dept_id': dept_id,
                'kind': kind,
                'media_type': media_type,
                'content_sha256': content_sha,
                'content_bytes': content_bytes,
                'content': content if isinstance(content, (str, dict, list)) else None,
                'created_at': _now(),
            },
        )
        record = {
            'schema_version': ARTIFACT_SCHEMA,
            'artifact_id': artifact_id,
            'run_id': run_id,
            'task_id': task_id,
            'dept_id': dept_id,
            'kind': kind,
            'media_type': media_type,
            'content_sha256': content_sha,
            'content_bytes': content_bytes,
            'sha256': store_record['sha256'],
            'bytes': store_record['bytes'],
            'portable_path': store_record['path'],
            'created_at': _now(),
        }
        _write_json_atomic(_artifact_record_path(self.root, artifact_id), record)
        self._index.index_artifact(record)
        return record

    def register_artifact(
        self, run_id: str, *, task_id: str | None = None,
        dept_id: str | None = None, media_type: str = 'application/json',
        content: object,
    ) -> dict[str, object]:
        return self._register_content(
            run_id, task_id=task_id, dept_id=dept_id, kind='artifact',
            media_type=media_type, content=content,
        )

    def register_evidence(
        self, run_id: str, *, task_id: str | None = None,
        dept_id: str | None = None, media_type: str = 'application/json',
        content: object,
    ) -> dict[str, object]:
        return self._register_content(
            run_id, task_id=task_id, dept_id=dept_id, kind='evidence',
            media_type=media_type, content=content,
        )

    # -- canonical memory (injected) -------------------------------------- #
    def query_memory(
        self, reader: CanonicalMemoryReader | None, run_id: str, *,
        task_id: str | None = None, dept_id: str | None = None,
        query: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        UnifiedIds.validate(run_id)
        if reader is None:
            raise StateSpineError(
                CODE_MEMORY_UNAVAILABLE, 'No canonical memory reader injected.'
            )
        query_doc = {
            'schema_version': MEMORY_QUERY_SCHEMA,
            'query': query if query is not None else {},
        }
        query_hash = _sha256_bytes(_canonical(query_doc).encode('utf-8'))
        try:
            result = reader.query(query_doc)
        except Exception as exc:
            raise StateSpineError(
                CODE_MEMORY_UNAVAILABLE,
                f'Canonical memory query failed: {exc}',
            ) from exc
        if not isinstance(result, dict) or result.get('schema_version') != MEMORY_RESULT_SCHEMA:
            raise StateSpineError(
                CODE_MEMORY_UNAVAILABLE,
                'Canonical memory result has unsupported schema.',
            )
        artifact = self.register_artifact(
            run_id, task_id=task_id, dept_id=dept_id,
            media_type='application/json', content=result,
        )
        return {
            'query': query_doc,
            'query_hash': query_hash,
            'result': result,
            'artifact': artifact,
        }

    # -- recovery --------------------------------------------------------- #
    def recover(self, run_id: str) -> dict[str, object]:
        UnifiedIds.validate(run_id)
        run_record = self.open_run(run_id)
        if run_record.get('schema_version') != RUN_SCHEMA:
            raise StateSpineError(
                CODE_SCHEMA_UNSUPPORTED, 'Run record schema is unsupported.'
            )
        if self._index.user_version not in SUPPORTED_DB_VERSIONS:
            raise StateSpineError(
                CODE_DB_NEWER,
                f'Database version {self._index.user_version} unsupported.',
            )
        problems: list[str] = []
        for snap_path in sorted((self.root / 'snapshots').glob('*.json')):
            rec = self._read_json_record(snap_path, SNAPSHOT_SCHEMA, 'snapshot')
            if rec.get('run_id') != run_id:
                continue
            checkpoint_path = _checkpoint_path(self.root, str(rec.get('task_id')))
            if not checkpoint_path.exists():
                problems.append(
                    f'snapshot {rec.get("snapshot_id")} checkpoint missing'
                )
                continue
            actual = _sha256_file(checkpoint_path)
            if actual != rec.get('checkpoint_sha256'):
                problems.append(
                    f'snapshot {rec.get("snapshot_id")} checkpoint hash mismatch'
                )
        artifacts = self._artifacts_for_run(run_id)
        for art in artifacts:
            file_path = (self.root / str(art['rel_path']))
            if not file_path.exists():
                problems.append(f'artifact {art["artifact_id"]} missing')
                continue
            if _sha256_file(file_path) != art['sha256']:
                problems.append(f'artifact {art["artifact_id"]} hash mismatch')
        if problems:
            return {
                'run_id': run_id,
                'status': 'recover-failed',
                'archived': self.is_archived(run_id),
                'problems': problems,
            }
        return {
            'run_id': run_id,
            'status': 'recover-ok',
            'archived': self.is_archived(run_id),
            'tasks': self._index.task_count(run_id),
            'artifacts': len(artifacts),
            'db_version': self._index.user_version,
        }

    def _artifacts_for_run(self, run_id: str) -> list[dict[str, object]]:
        return self._index.artifacts_for_run(run_id)

    # -- export / restore / archive --------------------------------------- #
    def _collect_run_files(self, run_id: str) -> list[tuple[Path, str]]:
        files: list[tuple[Path, str]] = []
        run_path = _run_path(self.root, run_id)
        if not run_path.exists():
            raise StateSpineError(
                CODE_BINDING_CONFLICT, f'Run not found: {run_id}'
            )
        files.append((run_path, _safe_rel(self.root, run_path)))
        for dept_path in sorted((self.root / 'departments').glob('*.json')):
            rec = self._read_json_record(dept_path, DEPT_SCHEMA, 'department')
            if rec.get('run_id') == run_id:
                files.append((dept_path, _safe_rel(self.root, dept_path)))
        for task_path in sorted((self.root / 'tasks').glob('*.json')):
            rec = self._read_json_record(task_path, TASK_SCHEMA, 'task')
            if rec.get('run_id') == run_id:
                files.append((task_path, _safe_rel(self.root, task_path)))
                ledger = _ledger_path(self.root, str(rec['task_id']))
                if ledger.exists():
                    files.append((ledger, _safe_rel(self.root, ledger)))
                checkpoint = _checkpoint_path(self.root, str(rec['task_id']))
                if checkpoint.exists():
                    files.append((checkpoint, _safe_rel(self.root, checkpoint)))
        for snap_path in sorted((self.root / 'snapshots').glob('*.json')):
            rec = self._read_json_record(snap_path, SNAPSHOT_SCHEMA, 'snapshot')
            if rec.get('run_id') == run_id:
                files.append((snap_path, _safe_rel(self.root, snap_path)))
        records_dir = self.root / 'artifacts' / 'records'
        if records_dir.is_dir():
            for rec_path in sorted(records_dir.glob('*.json')):
                rec = self._read_json_record(rec_path, ARTIFACT_SCHEMA, 'artifact')
                if rec.get('run_id') == run_id:
                    files.append((rec_path, _safe_rel(self.root, rec_path)))
        for art in self._artifacts_for_run(run_id):
            art_path = self.root / str(art['rel_path'])
            if art_path.exists():
                files.append((art_path, str(art['rel_path'])))
        return files

    def export_run(self, run_id: str, target_dir: Path) -> dict[str, object]:
        UnifiedIds.validate(run_id)
        target_dir = Path(target_dir).resolve()
        files = self._collect_run_files(run_id)
        if not files:
            raise StateSpineError(
                CODE_EXPORT_INCOMPLETE, 'No authoritative files to export.'
            )
        manifest_files: list[dict[str, object]] = []
        for abs_path, rel_path in files:
            digest = _sha256_file(abs_path)
            nbytes = abs_path.stat().st_size
            dest = target_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(abs_path, dest)
            manifest_files.append(
                {'rel_path': rel_path, 'sha256': digest, 'bytes': nbytes}
            )
        manifest = {
            'schema_version': EXPORT_SCHEMA,
            'run_id': run_id,
            'exported_at': _now(),
            'file_count': len(manifest_files),
            'files': manifest_files,
        }
        manifest_path = target_dir / 'manifest.json'
        _write_json_atomic(manifest_path, manifest)
        return manifest

    def restore_run(self, source_dir: Path, target_root: Path) -> str:
        source_dir = Path(source_dir).resolve()
        target_root = Path(target_root).resolve()
        target_root.mkdir(parents=True, exist_ok=True)
        manifest_path = source_dir / 'manifest.json'
        if not manifest_path.exists():
            raise StateSpineError(
                CODE_EXPORT_INCOMPLETE, 'Export manifest is missing.'
            )
        manifest = self._read_json_record(manifest_path, EXPORT_SCHEMA, 'export')
        run_id = str(manifest['run_id'])
        UnifiedIds.validate(run_id)
        existing = SQLiteStateIndex(target_root / 'index.sqlite').get_run(run_id)
        if existing is not None:
            raise StateSpineError(
                CODE_RESTORE_CONFLICT,
                f'Target root already contains run {run_id}.',
            )
        written: list[Path] = []
        try:
            for entry in manifest['files']:
                rel_path = str(entry['rel_path'])
                candidate = _reject_traversal(target_root, rel_path)
                source = source_dir / rel_path
                if not source.exists():
                    raise StateSpineError(
                        CODE_EXPORT_INCOMPLETE,
                        f'Exported file missing: {rel_path!r}',
                    )
                actual_sha = _sha256_file(source)
                if actual_sha != entry['sha256']:
                    raise StateSpineError(
                        CODE_HASH_MISMATCH,
                        f'Hash mismatch for {rel_path!r}.',
                    )
                candidate.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(
                    prefix='.restore.', suffix='.tmp', dir=str(candidate.parent),
                )
                try:
                    with os.fdopen(descriptor, 'wb') as handle:
                        with source.open('rb') as src:
                            shutil.copyfileobj(src, handle)
                    os.replace(temporary, candidate)
                except Exception:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                    raise
                written.append(candidate)
            index = SQLiteStateIndex(target_root / 'index.sqlite')
            index.rebuild(target_root)
            return run_id
        except Exception:
            for path in written:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise

    def archive_run(self, run_id: str) -> dict[str, object]:
        UnifiedIds.validate(run_id)
        if not self.run_exists(run_id):
            raise StateSpineError(
                CODE_BINDING_CONFLICT, f'Run not found: {run_id}'
            )
        if self.is_archived(run_id):
            existing = self._read_json_record(
                _run_path(self.root, run_id), RUN_SCHEMA, 'run'
            )
            return existing
        export_dir = self.root / 'archives' / run_id
        self.export_run(run_id, export_dir)
        now = _now()
        record = {
            'schema_version': RUN_SCHEMA,
            'run_id': run_id,
            'status': 'archived',
            'created_at': self._read_json_record(
                _run_path(self.root, run_id), RUN_SCHEMA, 'run'
            ).get('created_at', now),
            'updated_at': now,
            'archived': True,
            'root_rel': _safe_rel(self.root, self.root),
            'archive_rel': _safe_rel(self.root, export_dir),
        }
        _write_json_atomic(_run_path(self.root, run_id), record)
        self._index.index_run(record)
        return record

    def rebuild_index(self) -> None:
        self._index.rebuild(self.root)

    # -- internal --------------------------------------------------------- #
    def _read_json_record(
        self, path: Path, expected_schema: str, label: str,
    ) -> dict[str, object]:
        if not path.exists():
            raise StateSpineError(
                CODE_ARTIFACT_MISSING, f'{label} file missing: {path.name}'
            )
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError as exc:
            raise StateSpineError(
                CODE_CHECKPOINT_CORRUPT, f'Corrupt {label} {path.name}: {exc}'
            ) from exc
        if not isinstance(value, dict):
            raise StateSpineError(
                CODE_CHECKPOINT_CORRUPT, f'{label} {path.name} is not an object.'
            )
        if value.get('schema_version') != expected_schema:
            raise StateSpineError(
                CODE_SCHEMA_UNSUPPORTED,
                f'{label} {path.name} has unsupported schema '
                f'{value.get("schema_version")!r}.',
            )
        return value

    def close(self) -> None:
        self._index.close()

    def __del__(self) -> None:
        try:
            self._index.close()
        except Exception:
            pass


__all__ = [
    'RUN_SCHEMA',
    'TASK_SCHEMA',
    'DEPT_SCHEMA',
    'SNAPSHOT_SCHEMA',
    'ARTIFACT_SCHEMA',
    'EXPORT_SCHEMA',
    'MEMORY_QUERY_SCHEMA',
    'MEMORY_RESULT_SCHEMA',
    'CODE_INVALID_ID',
    'CODE_SCHEMA_UNSUPPORTED',
    'CODE_DB_NEWER',
    'CODE_BINDING_CONFLICT',
    'CODE_CHECKPOINT_CORRUPT',
    'CODE_HASH_MISMATCH',
    'CODE_ARTIFACT_MISSING',
    'CODE_EXPORT_INCOMPLETE',
    'CODE_RESTORE_CONFLICT',
    'CODE_ARCHIVE_WRITE_BLOCKED',
    'CODE_MEMORY_UNAVAILABLE',
    'CODE_INDEX_REBUILD_FAILED',
    'StateSpineError',
    'UnifiedIds',
    'CanonicalMemoryReader',
    'SQLiteStateIndex',
    'StateSpineTaskLedger',
    'RestartSafeStateSpine',
]
