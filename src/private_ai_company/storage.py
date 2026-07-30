'''Content-addressed evidence and artifact storage inside a GroupPack.'''

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

PORTABLE_ID = re.compile(r'^[a-z0-9][a-z0-9._-]{2,191}$')
STORE_KINDS = {'evidence', 'artifact'}


class ContentAddressedStore:
    def __init__(self, group_root: Path, base_dir: Path):
        self.group_root = group_root.resolve()
        self.base_dir = base_dir.resolve()
        self.base_dir.relative_to(self.group_root)

    def put(
        self,
        kind: str,
        task_id: str,
        payload: dict[str, object],
        *,
        media_type: str = 'application/json',
    ) -> dict[str, object]:
        if kind not in STORE_KINDS:
            raise ValueError(f'Unsupported store kind {kind!r}.')
        if not PORTABLE_ID.fullmatch(task_id):
            raise ValueError('task_id must be a portable identifier.')
        stable_content = {
            'schema_version': 'content-record/v0',
            'kind': kind,
            'task_id': task_id,
            'media_type': media_type,
            'payload': payload,
        }
        canonical = json.dumps(
            stable_content, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
        ).encode('utf-8')
        digest = hashlib.sha256(canonical).hexdigest()
        reference = f'{kind}:{digest[:20]}'
        directory = self.base_dir / kind
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f'{digest}.json'
        if not target.exists():
            descriptor, temporary = tempfile.mkstemp(
                prefix=f'.{digest[:12]}.', suffix='.tmp', dir=directory,
            )
            try:
                with os.fdopen(descriptor, 'wb') as handle:
                    handle.write(canonical)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            except Exception:
                if os.path.exists(temporary):
                    os.unlink(temporary)
                raise
        return {
            'ref': reference,
            'kind': kind,
            'sha256': digest,
            'bytes': len(canonical),
            'path': target.relative_to(self.group_root).as_posix(),
            'media_type': media_type,
        }

    def read(self, record: dict[str, object]) -> dict[str, object]:
        relative = record.get('path')
        if not isinstance(relative, str):
            raise ValueError('Stored record path is missing.')
        target = (self.group_root / relative).resolve()
        target.relative_to(self.group_root)
        value = json.loads(target.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Stored content record must be an object.')
        return value
