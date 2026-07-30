'''Provider-neutral append-only task events and atomic checkpoints.'''

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from threading import RLock
from time import sleep
from typing import Iterable

from .timebase import authoritative_timestamp


_PATH_LOCKS: dict[str, RLock] = {}
_PATH_LOCKS_GUARD = RLock()
_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32)


def _path_lock(path: Path) -> RLock:
    key = str(path.resolve(strict=False)).casefold()
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, RLock())


def _replace_with_windows_retry(source: str, target: Path) -> None:
    for attempt, delay in enumerate((*_REPLACE_RETRY_DELAYS, None)):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if delay is None:
                raise
            sleep(delay)


class TaskLedger:
    def __init__(self, tasks_dir: Path, checkpoints_dir: Path):
        self.tasks_dir = tasks_dir
        self.checkpoints_dir = checkpoints_dir

    def append(self, task_id: str, kind: str, payload: dict[str, object]) -> dict[str, object]:
        if not task_id or any(char in task_id for char in '/\\:'):
            raise ValueError('task_id must be a portable identifier.')
        if not kind.strip():
            raise ValueError('kind must be non-empty.')
        event = {
            'event_id': uuid.uuid4().hex, 'task_id': task_id, 'kind': kind,
            'timestamp': authoritative_timestamp(), 'payload': payload,
        }
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        path = self.tasks_dir / f'{task_id}.jsonl'
        with _path_lock(path):
            with path.open('a', encoding='utf-8', newline='\n') as handle:
                handle.write(
                    json.dumps(
                        event, ensure_ascii=False, separators=(',', ':'),
                    ) + '\n'
                )
                handle.flush()
                os.fsync(handle.fileno())
        return event

    def events(self, task_id: str) -> Iterable[dict[str, object]]:
        path = self.tasks_dir / f'{task_id}.jsonl'
        if not path.exists():
            return []
        rows = []
        with _path_lock(path):
            lines = path.read_text(encoding='utf-8').splitlines()
        for number, line in enumerate(lines, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f'Corrupt task event at line {number}: {exc}') from exc
            if not isinstance(value, dict):
                raise ValueError(f'Task event at line {number} is not an object.')
            rows.append(value)
        return rows

    def write_checkpoint(self, task_id: str, state: dict[str, object]) -> Path:
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        target = self.checkpoints_dir / f'{task_id}.checkpoint.json'
        document = {'task_id': task_id, 'timestamp': authoritative_timestamp(), 'state': state}
        with _path_lock(target):
            descriptor, temporary = tempfile.mkstemp(
                prefix=f'.{task_id}.', suffix='.tmp', dir=self.checkpoints_dir,
            )
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
                    json.dump(document, handle, ensure_ascii=False, indent=2)
                    handle.write('\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                _replace_with_windows_retry(temporary, target)
            except Exception:
                if os.path.exists(temporary):
                    os.unlink(temporary)
                raise
        return target

    def read_checkpoint(self, task_id: str) -> dict[str, object] | None:
        path = self.checkpoints_dir / f'{task_id}.checkpoint.json'
        with _path_lock(path):
            if not path.exists():
                return None
            value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Checkpoint must be a JSON object.')
        return value
