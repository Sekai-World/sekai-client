"""Non-blocking in-process lock and cross-process ``fcntl.flock`` helpers.

Phase 4.2 uses these to make each update cycle mutually exclusive:

- :class:`ProcessCycleLock` is a non-blocking in-process mutex. Overlapping
  triggers (same process) *skip* rather than queue stale work.
- :func:`repo_file_locks` acquires an ``fcntl.flock`` exclusive lock on each
  repository-adjacent ``.lock`` file in a *deterministic, normalized, sorted*
  order, so enabling multiple repositories never deadlocks; all locks are
  released (in reverse order) on every exit path (normal, exception, early
  return).

Only the Python standard library is used, so this works on macOS/Linux.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from threading import Lock


class RepoLockUnavailable(RuntimeError):
    """Raised when a repository-adjacent ``flock`` cannot be acquired.

    This is a distinct classification from any ``RuntimeError`` raised by the
    cycle body: lock contention must be reported as a skip, never swallowed as
    a generation/commit failure.
    """


def sorted_lock_paths(paths: Iterable[str]) -> list[str]:
    """Return ``paths`` normalized, de-duplicated, and sorted for a fixed order.

    Each path is normalized via ``os.path.realpath`` so that different spellings
    of the same file (relative segments like ``a/./b.lock``, ``..`` traversal, or
    a symlinked spelling) resolve to one canonical lock. The normalized set is
    then sorted, giving every caller the same observable acquisition order.
    """
    return sorted({os.path.realpath(p) for p in paths})


class ProcessCycleLock:
    """Non-blocking in-process mutex guarding the single update cycle.

    ``acquire()`` returns ``True`` only if no other in-process cycle holds it
    and ``False`` otherwise, so callers can *skip* overlapping work. Holders
    must call ``release()``; the convenience context manager releases on every
    exit.
    """

    def __init__(self) -> None:
        self._lock = Lock()

    def acquire(self) -> bool:
        return self._lock.acquire(blocking=False)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> ProcessCycleLock:
        # The context manager form is only used when the caller already knows
        # it holds the lock (e.g. tests). Production code uses ``acquire()`` so
        # it can skip instead of raising.
        if not self.acquire():
            raise RuntimeError("update cycle already running in this process")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


@contextmanager
def repo_file_locks(paths: Iterable[str], non_blocking: bool = True) -> Iterator[None]:
    """Acquire exclusive ``flock`` locks on each path, in sorted order.

    On success the context body runs with every lock held. On any acquisition
    failure (including a lock already held by another process when
    ``non_blocking`` is ``True``) a :class:`RepoLockUnavailable` is raised after
    releasing whatever was acquired; the ``finally`` block always releases and
    closes every handle in reverse acquisition order, so no lock leaks. The
    original cause (an :class:`OSError` from ``fcntl.flock``) is preserved via
    ``__cause__``. Any exception raised by the context *body* is propagated
    unchanged (it is never wrapped as :class:`RepoLockUnavailable`).
    """
    ordered = sorted_lock_paths(paths)
    handles: list[int] = []
    try:
        for lock_path in ordered:
            parent = os.path.dirname(lock_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            handles.append(fd)
            flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if non_blocking else 0)
            try:
                fcntl.flock(fd, flags)
            except OSError as err:  # another process holds it (or other error)
                raise RepoLockUnavailable(
                    f"could not acquire repo lock {lock_path}: {err}"
                ) from err
        yield
    finally:
        for fd in reversed(handles):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
