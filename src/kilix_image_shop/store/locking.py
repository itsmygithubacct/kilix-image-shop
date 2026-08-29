"""Ownership-checked single-writer advisory lock."""

from __future__ import annotations

import fcntl
import os
import stat
import threading
from dataclasses import dataclass, field

from .layout import ProjectLayout, StoreError


class LockBusy(StoreError):
    """Another writer currently owns the project lock."""


@dataclass(slots=True)
class ProjectWriterLock:
    layout: ProjectLayout
    blocking: bool = False
    _descriptor: int | None = field(default=None, init=False, repr=False)
    _owner_thread: int | None = field(default=None, init=False, repr=False)
    _identity: tuple[int, int] | None = field(default=None, init=False, repr=False)

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def acquire(self) -> None:
        if self.held:
            raise StoreError("writer lock is already held by this instance")
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.layout.lock, flags)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise StoreError("writer lock carrier is not a regular file")
            operation = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
            fcntl.flock(descriptor, operation)
            current = self.layout.lock.lstat()
            identity = (metadata.st_dev, metadata.st_ino)
            if stat.S_ISLNK(current.st_mode) or identity != (current.st_dev, current.st_ino):
                raise StoreError("writer lock carrier changed during acquisition")
        except BlockingIOError as exc:
            if "descriptor" in locals():
                os.close(descriptor)
            raise LockBusy("project writer lock is already owned") from exc
        except OSError as exc:
            if "descriptor" in locals():
                os.close(descriptor)
            raise StoreError("project writer lock cannot be acquired safely") from exc
        except Exception:
            if "descriptor" in locals():
                os.close(descriptor)
            raise
        self._descriptor = descriptor
        self._owner_thread = threading.get_ident()
        self._identity = identity

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            raise StoreError("writer lock is not held")
        if self._owner_thread != threading.get_ident():
            raise StoreError("writer lock can only be released by its owning thread")
        try:
            metadata = os.fstat(descriptor)
            current = self.layout.lock.lstat()
            if self._identity != (metadata.st_dev, metadata.st_ino) or self._identity != (
                current.st_dev,
                current.st_ino,
            ):
                raise StoreError("writer lock ownership carrier changed before release")
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError as exc:
            raise StoreError("project writer lock cannot be released safely") from exc
        finally:
            os.close(descriptor)
            self._descriptor = None
            self._owner_thread = None
            self._identity = None

    def __enter__(self) -> ProjectWriterLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()
