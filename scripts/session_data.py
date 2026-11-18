"""Exclusive, versioned Android /data storage for local Linux sessions.

Never deletes data. A lost manifest or incompatible runtime requires operator
inspection, not an automatic reset. The caller must fence Docker users too.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import uuid
from pathlib import Path


class StorageError(RuntimeError):
    pass


class SessionData:
    def __init__(self, path: Path, contract: dict):
        self.path = Path(path).absolute()
        self.contract = contract
        self.lock = None

    def __enter__(self):
        try:
            self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_mode & 0o077 or ',' in str(self.path)):
                raise StorageError('session directory must be private, owned, and not a symlink or comma path')
            self.path = self.path.resolve()
            self.data = self.path / 'data'
            self.lock = os.open(self.path / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            if not stat.S_ISREG(os.fstat(self.lock).st_mode):
                raise StorageError('session lock is not a regular file')
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StorageError('session data is in use') from exc
            manifest = self.path / 'manifest.json'
            if manifest.exists() or manifest.is_symlink():
                fd = os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
                with os.fdopen(fd) as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise StorageError('session manifest is not a regular file')
                    document = json.loads(stream.read(16385))
                if (document.get('schema') != 1 or document.get('contract') != self.contract or
                        set(document) != {'schema', 'contract', 'identity'}):
                    raise StorageError('session runtime contract does not match')
                self.identity = str(uuid.UUID(document['identity']))
                if not self.data.is_dir() or self.data.is_symlink():
                    raise StorageError('session data is missing or unsafe; refusing reset')
            else:
                if self.data.exists() or self.data.is_symlink():
                    raise StorageError('unidentified session data; refusing reset')
                self.identity = str(uuid.uuid4())
                self.data.mkdir(mode=0o700)
                temporary = self.path / ('manifest-' + self.identity)
                with temporary.open('x', encoding='utf-8') as stream:
                    json.dump({'schema': 1, 'identity': self.identity, 'contract': self.contract}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(manifest)
                fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return self
        except BaseException as exc:
            self.close()
            if isinstance(exc, (OSError, ValueError, KeyError, TypeError, AttributeError)):
                raise StorageError('cannot safely acquire session data: ' + str(exc)) from exc
            raise

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def __exit__(self, *_):
        self.close()
