"""Cross-process sync lease backed by an OS file lock (DESIGN.md §Cache).

At most one ``ewsmcp`` process per ``DATA_DIR`` runs ``SyncEngine``. The
leader holds an exclusive non-blocking OS lock on ``cache/sync.lock`` for
the lifetime of the process (``msvcrt.locking`` on Windows, ``fcntl.flock``
on POSIX). Because the kernel owns the lock, abnormal termination releases
it automatically without heartbeats, timestamps, or PID-liveness checks.
"""

from __future__ import annotations

import errno
import os
import threading
from pathlib import Path
from typing import Optional

try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

LOCK_FILENAME = "sync.lock"
DEFAULT_RETRY_SECONDS = 30.0

# Windows byte-range locks (LockFile via msvcrt.locking) are mandatory:
# locking byte 0 would block diagnostic readers opening the file to inspect
# the holder PID written at offset 0. Locking 1 byte past the short PID text
# (Win32 explicitly allows locking beyond EOF) enforces mutual exclusion
# across handles while keeping offset 0 readable.
_WIN_LOCK_OFFSET = 1024


def _lock_fd(fd: int) -> bool:
    """Acquire a non-blocking exclusive lock on ``fd``.

    Returns ``False`` when another open file handle holds the lock, and
    raises ``OSError`` for any other failure (unsupported filesystem, etc.).
    """
    if os.name == "nt" and msvcrt is not None:
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        return True
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            raise
        return True
    raise OSError(errno.ENOSYS, "no OS file-locking primitive available")


def _unlock_fd(fd: int) -> None:
    if os.name == "nt" and msvcrt is not None:
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    elif fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class SyncLease:
    """Non-blocking OS file lock held for the life of the leader process."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.retry_seconds = float(retry_seconds)
        self._fd: Optional[int] = None
        self._guard = threading.Lock()

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Attempt to acquire the lease without blocking.

        Idempotent when already held. Returns ``False`` when another handle
        holds the lock; raises ``OSError`` when the lock file cannot be
        created, opened, or locked for any other reason.
        """
        with self._guard:
            if self._fd is not None:
                return True
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
            # Never pass O_TRUNC: truncating before acquiring would wipe the
            # current holder's diagnostic PID on POSIX and fail open() on
            # Windows if byte 0 were locked. Non-inheritable flags prevent
            # child processes from keeping the open file description alive.
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOINHERIT", 0)
            )
            fd = os.open(self.path, flags, 0o600)
            try:
                acquired = _lock_fd(fd)
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            if not acquired:
                try:
                    os.close(fd)
                except OSError:
                    pass
                return False
            try:
                payload = f"{os.getpid()}\n".encode("ascii")
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, payload)
                os.ftruncate(fd, len(payload))
            except OSError:
                pass
            self._fd = fd
            return True

    def release(self) -> None:
        """Unlock and close the descriptor without unlinking the lock file."""
        with self._guard:
            fd = self._fd
            if fd is None:
                return
            self._fd = None
            try:
                _unlock_fd(fd)
            except OSError:
                pass
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
