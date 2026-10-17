"""Cross-process shared/exclusive lifecycle locks for one database path."""

from contextlib import contextmanager
import os
from pathlib import Path
import threading


class ProcessLockError(RuntimeError):
    """The OS lock could not be acquired without waiting."""

    def __init__(self, code="DATABASE_LIFECYCLE_LOCK_BUSY"):
        super().__init__(code)
        self.code = code


class ProcessLockStateError(RuntimeError):
    """An unsupported in-process lock upgrade or mode mismatch was requested."""


def canonical_database_path(database: Path) -> Path:
    path = Path(database).resolve(strict=False)
    return Path(os.path.normcase(str(path))) if os.name == "nt" else path


def lock_path(database: Path) -> Path:
    """Return the permanent sidecar path for a canonical database path."""
    return Path(str(canonical_database_path(database)) + ".lock")


_state = threading.local()


def _held():
    held = getattr(_state, "held", None)
    if held is None:
        held = {}
        _state.held = held
    return held


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_ALWAYS = 4
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_SHARING_VIOLATION = 32

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.LockFileEx.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(OVERLAPPED),
    )
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.UnlockFileEx.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(OVERLAPPED),
    )
    _kernel32.UnlockFileEx.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    def _open_lock(path):
        handle = _kernel32.CreateFileW(
            str(path), _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None, _OPEN_ALWAYS, _FILE_ATTRIBUTE_NORMAL, None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            error = ctypes.get_last_error()
            raise OSError(error, "CreateFileW failed", str(path))
        return handle

    def _acquire(handle, mode):
        flags = _LOCKFILE_FAIL_IMMEDIATELY
        if mode == "exclusive":
            flags |= _LOCKFILE_EXCLUSIVE_LOCK
        overlapped = OVERLAPPED()
        if not _kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            if error in (_ERROR_LOCK_VIOLATION, _ERROR_SHARING_VIOLATION):
                raise ProcessLockError()
            raise OSError(error, "LockFileEx failed")
        return overlapped

    def _release(handle, overlapped):
        try:
            if not _kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
                error = ctypes.get_last_error()
                raise OSError(error, "UnlockFileEx failed")
        finally:
            # Closing the owned handle is mandatory even if unlock raises/fails.
            if not _kernel32.CloseHandle(handle):
                error = ctypes.get_last_error()
                raise OSError(error, "CloseHandle failed")

else:
    import fcntl

    def _open_lock(path):
        return os.open(path, os.O_CREAT | os.O_RDWR, 0o600)

    def _acquire(handle, mode):
        flags = fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX
        try:
            fcntl.flock(handle, flags | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProcessLockError() from exc
        return None

    def _release(handle, _overlapped):
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


@contextmanager
def database_process_lock(database: Path, mode: str):
    """Hold one non-blocking OS lock for the database's complete lifecycle."""
    if mode not in {"shared", "exclusive"}:
        raise ValueError("mode must be shared or exclusive")
    database = canonical_database_path(database)
    if not database.is_file():
        from 主程序.repositories.database import DatabaseError
        raise DatabaseError("DATABASE_NOT_INITIALIZED")
    key = str(database)
    held = _held()
    current = held.get(key)
    if current is not None:
        held_mode, depth, handle, overlapped = current
        if mode == "shared" and held_mode in {"shared", "exclusive"}:
            held[key] = (held_mode, depth + 1, handle, overlapped)
            try:
                yield
            finally:
                held_mode, depth, handle, overlapped = held[key]
                if depth == 1:
                    del held[key]
                else:
                    held[key] = (held_mode, depth - 1, handle, overlapped)
            return
        if mode == "exclusive" and held_mode == "exclusive":
            held[key] = (held_mode, depth + 1, handle, overlapped)
            try:
                yield
            finally:
                held_mode, depth, handle, overlapped = held[key]
                if depth == 1:
                    del held[key]
                else:
                    held[key] = (held_mode, depth - 1, handle, overlapped)
            return
        raise ProcessLockStateError("exclusive lock cannot upgrade an existing shared lock")

    sidecar = lock_path(database)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    handle = _open_lock(sidecar)
    try:
        overlapped = _acquire(handle, mode)
    except BaseException:
        if os.name == "nt":
            _kernel32.CloseHandle(handle)
        else:
            os.close(handle)
        raise
    held[key] = (mode, 1, handle, overlapped)
    try:
        yield
    finally:
        current = held.pop(key, None)
        if current is not None:
            _release(current[2], current[3])
