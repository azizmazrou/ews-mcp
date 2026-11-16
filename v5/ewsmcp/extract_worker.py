"""Parent supervisor for isolated attachment text extraction subprocesses.

Runs ``python -m ewsmcp.extract_child`` per document in a scrubbed environment
and an empty temporary working directory:
- Hard wall-clock deadline (``EXTRACT_TIMEOUT_SECONDS = 20``) with a soft
  75% deadline inside the child so long honest documents return partial text
  with a note instead of failing whole
- Memory ceiling (``EXTRACT_MEMORY_LIMIT_MB = 1024``) applied by the child to
  its own interpreter process before reading ``stdin``, plus a parent Win32
  Job Object (``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``) on Windows and a dedicated
  session / process group (``start_new_session=True`` + ``killpg``) on POSIX so
  no child or grandchild process can outlive the call
- Strict hostile-input validation of the child's JSON response, output byte
  ceiling (``EXTRACT_MAX_OUTPUT_BYTES``), and character ceiling
  (``MAX_EXTRACTED_CHARS = 5,000,000``)
- Immediate kill on ``asyncio`` cancellation with the concurrency slot
  (``EXTRACT_MAX_CONCURRENT = 2``) held until the supervisor thread finishes
- On Linux, ``prctl(PR_SET_DUMPABLE, 0)`` is applied before starting the first
  worker child (unless ``EWS_KEEP_DUMPABLE=true``) so same-UID processes cannot
  read ``/proc/<pid>/environ`` or server memory

Imports nothing from ``ewsmcp.tools``, ``ewsmcp.gateway``, or ``ewsmcp.errors``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .extract import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    MAX_ERROR_MESSAGE_CHARS,
    MAX_EXTRACTED_CHARS,
    MAX_NOTE_CHARS,
    SUPPORTED_TYPES,
    ExtractionResult,
    cap_message,
    fold_and_prioritize_notes,
    sanitize_surrogates,
)
from .extract_child import (
    EXIT_MEMORY_EXCEEDED,
    EXIT_MEMORY_LIMIT_FAILED,
    EXTRACT_SOFT_DEADLINE_RATIO,
    WORKER_LIMIT_ERROR_MESSAGE,
    apply_self_memory_limit,
    memory_ceiling_supported,
)
from .toolerror import ToolError

__all__ = [
    "EXIT_MEMORY_EXCEEDED",
    "EXIT_MEMORY_LIMIT_FAILED",
    "EXTRACT_MAX_CONCURRENT",
    "EXTRACT_MAX_OUTPUT_BYTES",
    "EXTRACT_MEMORY_LIMIT_MB",
    "EXTRACT_SOFT_DEADLINE_RATIO",
    "EXTRACT_TIMEOUT_SECONDS",
    "MAX_CHILD_LOADED_MODULES",
    "MAX_CHILD_NOTES",
    "MAX_CHILD_UNIT_COUNT",
    "PR_SET_DUMPABLE",
    "WORKER_LIMIT_ERROR_MESSAGE",
    "WORKER_UNAVAILABLE_ERROR_MESSAGE",
    "apply_self_memory_limit",
    "build_worker_env",
    "ensure_server_non_dumpable",
    "extract_bytes_in_worker",
    "extract_item_in_worker",
    "is_forbidden_env_name",
    "memory_ceiling_supported",
]

EXTRACT_TIMEOUT_SECONDS: float = 20.0
EXTRACT_MEMORY_LIMIT_MB: int = 1024
EXTRACT_MAX_OUTPUT_BYTES: int = 24 * 1024 * 1024
EXTRACT_MAX_CONCURRENT: int = 2

MAX_CHILD_NOTES: int = 50
MAX_CHILD_UNIT_COUNT: int = 1_000_000
MAX_CHILD_LOADED_MODULES: int = 5_000

PR_SET_DUMPABLE: int = 4

WORKER_UNAVAILABLE_ERROR_MESSAGE = (
    "Document reader is unavailable (temporary directory or worker process "
    "could not be created)."
)

_PIPE_CHUNK_BYTES = 65_536

_SUPPORTED_KINDS_SET = frozenset(SUPPORTED_TYPES)
_ALLOWED_OK_KEYS = frozenset({
    "ok",
    "text",
    "extracted_from",
    "pages",
    "slides",
    "sheets",
    "pages_read",
    "slides_read",
    "sheets_read",
    "partial",
    "notes",
    "memory_ceiling_enforced",
    "loaded_modules",
})
_ALLOWED_ERR_KEYS = frozenset({
    "ok",
    "code",
    "message",
    "hint",
    "limit_exceeded",
})

_FORBIDDEN_ENV_PREFIXES = ("EWS_", "MCP_", "SEND_")
_FORBIDDEN_ENV_SUBSTRINGS = ("PASSWORD", "SECRET", "TOKEN", "KEY")

_PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)

_EXECUTOR_LOCK = threading.Lock()
_EXECUTOR: Optional[concurrent.futures.ThreadPoolExecutor] = None
_LOOP_SEMAPHORES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Tuple[int, asyncio.Semaphore]]" = (
    weakref.WeakKeyDictionary()
)

_DUMPABLE_LOCK = threading.Lock()
_DUMPABLE_APPLIED = False


def _get_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=8, thread_name_prefix="ews-extract-sup"
            )
        return _EXECUTOR


def _get_semaphore(max_concurrent: int) -> asyncio.Semaphore:
    cap = max(1, int(max_concurrent))
    loop = asyncio.get_running_loop()
    cached = _LOOP_SEMAPHORES.get(loop)
    if cached is None or cached[0] != cap:
        sem = asyncio.Semaphore(cap)
        _LOOP_SEMAPHORES[loop] = (cap, sem)
        return sem
    return cached[1]


def _env_keep_dumpable_enabled() -> bool:
    raw = os.environ.get("EWS_KEEP_DUMPABLE", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def ensure_server_non_dumpable(
    *,
    keep_dumpable: Optional[bool] = None,
    platform_name: Optional[str] = None,
) -> bool:
    """Mark the server process non-dumpable on Linux before spawning a worker.

    Closes ``/proc/<pid>/environ``, ``/proc/<pid>/mem``, and ``ptrace`` to other
    processes running under the same OS user unless ``EWS_KEEP_DUMPABLE=true``.
    """
    global _DUMPABLE_APPLIED
    eff_keep = (
        _env_keep_dumpable_enabled() if keep_dumpable is None else bool(keep_dumpable)
    )
    if eff_keep:
        return False
    plat = (sys.platform if platform_name is None else platform_name).lower()
    if not plat.startswith("linux"):
        return False
    with _DUMPABLE_LOCK:
        if _DUMPABLE_APPLIED and platform_name is None:
            return True
        try:
            libc = ctypes.CDLL(None)
            prctl_fn = getattr(libc, "prctl", None)
            if prctl_fn is None:
                libc = ctypes.CDLL("libc.so.6")
                prctl_fn = getattr(libc, "prctl", None)
            if prctl_fn is None:
                return False
            prctl_fn.argtypes = [
                ctypes.c_int,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
            ]
            prctl_fn.restype = ctypes.c_int
            rc = int(prctl_fn(PR_SET_DUMPABLE, 0, 0, 0, 0))
            if rc == 0:
                if platform_name is None:
                    _DUMPABLE_APPLIED = True
                return True
            return False
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Scrubbed environment & command builder
# ---------------------------------------------------------------------------


def is_forbidden_env_name(name: str) -> bool:
    upper = name.upper()
    if upper.startswith(_FORBIDDEN_ENV_PREFIXES):
        return True
    return any(tok in upper for tok in _FORBIDDEN_ENV_SUBSTRINGS)


def build_worker_env(
    base_env: Optional[Mapping[str, str]] = None,
    *,
    temp_dir: Optional[str] = None,
) -> Dict[str, str]:
    """Build a scrubbed environment dictionary for the extraction subprocess."""
    src = os.environ if base_env is None else base_env
    env: Dict[str, str] = {}

    # Copy only safe system/locale variables (never USERNAME or credentials).
    safe_passthrough = {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "OS",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "SYSTEMDRIVE",
    }
    for k, v in src.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if is_forbidden_env_name(k):
            continue
        if k.upper() in safe_passthrough:
            env[k] = v

    # Reduce PATH strictly to the interpreter directory (+ Windows System32 / base DLL dir).
    py_dir = os.path.dirname(os.path.abspath(sys.executable))
    path_entries: List[str] = [py_dir]
    if os.name == "nt":
        base_prefix = os.path.abspath(getattr(sys, "base_prefix", ""))
        if base_prefix and base_prefix != py_dir and os.path.isdir(base_prefix):
            path_entries.append(base_prefix)
        sys_root = env.get("SystemRoot") or env.get("SYSTEMROOT") or r"C:\Windows"
        sys32 = os.path.join(sys_root, "System32")
        if sys32 not in path_entries:
            path_entries.append(sys32)
    env["PATH"] = os.pathsep.join(path_entries)

    # Pass PYTHONPATH through with our package root prepended so `ewsmcp` is importable.
    existing_pp = (
        src.get("PYTHONPATH", "") if isinstance(src.get("PYTHONPATH"), str) else ""
    )
    pp_parts = [_PACKAGE_PARENT]
    if existing_pp:
        pp_parts.extend(
            p for p in existing_pp.split(os.pathsep) if p and p != _PACKAGE_PARENT
        )
    env["PYTHONPATH"] = os.pathsep.join(pp_parts)

    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"

    if temp_dir:
        env["TMP"] = temp_dir
        env["TEMP"] = temp_dir
        env["TMPDIR"] = temp_dir

    # Final defensive sweep: guarantee no forbidden key can ever survive.
    for key in list(env.keys()):
        if is_forbidden_env_name(key):
            del env[key]
    return env


def _worker_command(memory_limit_mb: int) -> List[str]:
    return [
        sys.executable,
        "-m",
        "ewsmcp.extract_child",
        "--memory-limit-mb",
        str(int(memory_limit_mb)),
    ]


# ---------------------------------------------------------------------------
# Parent Windows Job Object & POSIX session-group termination
# ---------------------------------------------------------------------------

if os.name == "nt":
    import ctypes.wintypes as _wt

    _JOB_OBJECT_EXTENDED_LIMIT_INFO_CLASS = 9
    _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", _wt.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", _wt.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", _wt.DWORD),
            ("SchedulingClass", _wt.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, _wt.LPCWSTR]
    _k32.CreateJobObjectW.restype = _wt.HANDLE
    _k32.SetInformationJobObject.argtypes = [
        _wt.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        _wt.DWORD,
    ]
    _k32.SetInformationJobObject.restype = _wt.BOOL
    _k32.OpenProcess.argtypes = [_wt.DWORD, _wt.BOOL, _wt.DWORD]
    _k32.OpenProcess.restype = _wt.HANDLE
    _k32.AssignProcessToJobObject.argtypes = [_wt.HANDLE, _wt.HANDLE]
    _k32.AssignProcessToJobObject.restype = _wt.BOOL
    _k32.CloseHandle.argtypes = [_wt.HANDLE]
    _k32.CloseHandle.restype = _wt.BOOL


def _attach_windows_job_object(
    pid: int, proc_handle: Optional[int] = None
) -> Optional[int]:
    """Create a parent Win32 Job Object with kill-on-close and assign ``pid``."""
    if os.name != "nt":
        return None
    h_job = _k32.CreateJobObjectW(None, None)
    if not h_job:
        return None
    try:
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        )
        ok_set = _k32.SetInformationJobObject(
            h_job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok_set:
            _k32.CloseHandle(h_job)
            return None

        ok_assign = False
        h_proc = _k32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, int(pid)
        )
        if h_proc:
            try:
                ok_assign = bool(_k32.AssignProcessToJobObject(h_job, h_proc))
            finally:
                _k32.CloseHandle(h_proc)
        if not ok_assign and proc_handle:
            ok_assign = bool(_k32.AssignProcessToJobObject(h_job, int(proc_handle)))
        if not ok_assign:
            _k32.CloseHandle(h_job)
            return None
        return int(h_job)
    except Exception:
        _k32.CloseHandle(h_job)
        return None


def _close_windows_job_object(h_job: Optional[int]) -> None:
    if os.name == "nt" and h_job:
        try:
            _k32.CloseHandle(h_job)
        except Exception:
            pass


def _kill_worker_tree(
    proc: Optional[subprocess.Popen[bytes]], h_job: Optional[int]
) -> None:
    """Terminate the worker child and every descendant in its session / Job Object."""
    if proc is not None:
        if os.name != "nt":
            killpg_fn = getattr(os, "killpg", None)
            sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            if callable(killpg_fn):
                try:
                    killpg_fn(proc.pid, sigkill)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
    _close_windows_job_object(h_job)


def _remove_temp_dir_with_retry(temp_dir: Optional[str]) -> None:
    """Remove ``temp_dir`` after child exit, retrying briefly on handle-release lag."""
    if not temp_dir:
        return
    for delay in (0.0, 0.01, 0.02, 0.05, 0.1, 0.2):
        if delay > 0.0:
            time.sleep(delay)
        if not os.path.exists(temp_dir):
            return
        try:
            shutil.rmtree(temp_dir)
            return
        except OSError:
            continue
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Hostile child-response validator & parent-side subprocess supervisor
# ---------------------------------------------------------------------------


def _limit_error() -> ToolError:
    return ToolError("validation", WORKER_LIMIT_ERROR_MESSAGE)


def _unavailable_error() -> ToolError:
    return ToolError("internal", WORKER_UNAVAILABLE_ERROR_MESSAGE)


def _validate_child_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, int):
        raise _limit_error()
    if val < 0 or val > MAX_CHILD_UNIT_COUNT:
        raise _limit_error()
    return val


def _parse_and_validate_child_output(
    raw_out: bytes, *, max_chars: int = MAX_EXTRACTED_CHARS
) -> ExtractionResult:
    """Validate untrusted child stdout JSON strictly as hostile input."""
    if not raw_out:
        raise _limit_error()
    try:
        parsed = json.loads(raw_out.decode("utf-8"))
    except Exception:
        raise _limit_error() from None

    if not isinstance(parsed, dict) or not isinstance(parsed.get("ok"), bool):
        raise _limit_error()

    if parsed["ok"] is False:
        if any(k not in _ALLOWED_ERR_KEYS for k in parsed):
            raise _limit_error()
        raw_limit = parsed.get("limit_exceeded", False)
        if not isinstance(raw_limit, bool):
            raise _limit_error()
        if raw_limit:
            raise _limit_error()
        raw_code = parsed.get("code", "validation")
        if raw_code != "validation":
            raise _limit_error()
        raw_msg = parsed.get("message")
        if raw_msg is not None and not isinstance(raw_msg, str):
            raise _limit_error()
        raw_hint = parsed.get("hint")
        if raw_hint is not None and not isinstance(raw_hint, str):
            raise _limit_error()
        msg = cap_message(
            raw_msg or WORKER_LIMIT_ERROR_MESSAGE, MAX_ERROR_MESSAGE_CHARS
        )
        hint = (
            cap_message(raw_hint, MAX_ERROR_MESSAGE_CHARS)
            if raw_hint
            else None
        )
        raise ToolError("validation", msg, hint=hint)

    if any(k not in _ALLOWED_OK_KEYS for k in parsed):
        raise _limit_error()

    text_val = parsed.get("text")
    ext_from = parsed.get("extracted_from")
    if (
        not isinstance(text_val, str)
        or not isinstance(ext_from, str)
        or ext_from not in _SUPPORTED_KINDS_SET
    ):
        raise _limit_error()

    pages = _validate_child_int(parsed.get("pages"))
    slides = _validate_child_int(parsed.get("slides"))
    sheets = _validate_child_int(parsed.get("sheets"))
    pages_read = _validate_child_int(parsed.get("pages_read"))
    slides_read = _validate_child_int(parsed.get("slides_read"))
    sheets_read = _validate_child_int(parsed.get("sheets_read"))
    if pages_read is not None and pages is not None and pages_read > pages:
        raise _limit_error()
    if slides_read is not None and slides is not None and slides_read > slides:
        raise _limit_error()
    if sheets_read is not None and sheets is not None and sheets_read > sheets:
        raise _limit_error()

    raw_partial = parsed.get("partial", False)
    if not isinstance(raw_partial, bool):
        raise _limit_error()

    raw_notes = parsed.get("notes")
    if raw_notes is None:
        note_items: List[str] = []
    else:
        if not isinstance(raw_notes, list) or len(raw_notes) > MAX_CHILD_NOTES:
            raise _limit_error()
        note_items = []
        for item in raw_notes:
            if not isinstance(item, str):
                raise _limit_error()
            if item:
                note_items.append(cap_message(item, MAX_NOTE_CHARS))

    eff_max_chars = max(1, int(max_chars))
    was_char_cut = len(text_val) > eff_max_chars
    clean_text = sanitize_surrogates(
        text_val[:eff_max_chars] if was_char_cut else text_val
    )
    if was_char_cut:
        note_items.append(
            cap_message(
                f"Extracted text stopped at {eff_max_chars} characters.",
                MAX_NOTE_CHARS,
            )
        )
    is_partial = bool(raw_partial) or was_char_cut
    folded_notes = fold_and_prioritize_notes(note_items)

    # Diagnostic-only fields: strictly validated & sanitized, never used for decisions.
    raw_enforced = parsed.get("memory_ceiling_enforced")
    if raw_enforced is not None and not isinstance(raw_enforced, bool):
        raise _limit_error()
    mem_enforced: Optional[bool] = raw_enforced

    raw_mods = parsed.get("loaded_modules")
    if raw_mods is None:
        loaded_modules: Tuple[str, ...] = ()
    else:
        if (
            not isinstance(raw_mods, list)
            or len(raw_mods) > MAX_CHILD_LOADED_MODULES
        ):
            raise _limit_error()
        clean_mods: List[str] = []
        for mod_name in raw_mods:
            if not isinstance(mod_name, str) or len(mod_name) > 512:
                raise _limit_error()
            clean_mods.append(sanitize_surrogates(mod_name))
        loaded_modules = tuple(clean_mods)

    return ExtractionResult(
        text=clean_text,
        extracted_from=ext_from,
        pages=pages,
        slides=slides,
        sheets=sheets,
        pages_read=pages_read,
        slides_read=slides_read,
        sheets_read=sheets_read,
        partial=is_partial,
        notes=folded_notes,
        memory_ceiling_enforced=mem_enforced,
        loaded_modules=loaded_modules,
    )


def _run_worker_sync(
    header: Dict[str, Any],
    payload: bytes,
    *,
    timeout_seconds: float,
    memory_limit_mb: int,
    max_output_bytes: int,
    max_chars: int = MAX_EXTRACTED_CHARS,
    cancel_event: Optional[threading.Event] = None,
    wake_event: Optional[threading.Event] = None,
    keep_dumpable: Optional[bool] = None,
) -> ExtractionResult:
    temp_dir: Optional[str] = None
    proc: Optional[subprocess.Popen[bytes]] = None
    h_job: Optional[int] = None
    stop_io = threading.Event()
    wake = wake_event if wake_event is not None else threading.Event()
    reader_finished = [False]
    out_chunks: List[bytes] = []
    out_overflow = [False]

    try:
        if cancel_event is not None and cancel_event.is_set():
            raise _limit_error()

        try:
            temp_dir = tempfile.mkdtemp(prefix="ewsmcp-extract-")
        except OSError:
            raise _unavailable_error() from None

        ensure_server_non_dumpable(keep_dumpable=keep_dumpable)
        env = build_worker_env(temp_dir=temp_dir)
        cmd = _worker_command(memory_limit_mb)

        started_at = time.time()
        effective_timeout = max(0.05, float(timeout_seconds))
        req_header = {
            **header,
            "timeout_seconds": effective_timeout,
            "started_at": started_at,
        }
        frame = (
            json.dumps(req_header, ensure_ascii=True, separators=(",", ":")).encode(
                "ascii"
            )
            + b"\n"
        )

        popen_kwargs: Dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "cwd": temp_dir,
            "env": env,
        }
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except OSError:
            raise _unavailable_error() from None
        except Exception:
            raise _limit_error() from None

        h_job = _attach_windows_job_object(
            proc.pid,
            proc_handle=int(getattr(proc, "_handle", 0) or 0),
        )
        deadline = time.monotonic() + effective_timeout

        def _write_stdin() -> None:
            assert proc is not None and proc.stdin is not None
            try:
                proc.stdin.write(frame)
                mv = memoryview(payload)
                pos = 0
                total = len(payload)
                while pos < total and not stop_io.is_set():
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    end = min(pos + _PIPE_CHUNK_BYTES, total)
                    proc.stdin.write(mv[pos:end])
                    pos = end
                if not stop_io.is_set() and (
                    cancel_event is None or not cancel_event.is_set()
                ):
                    proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        def _read_stdout() -> None:
            assert proc is not None and proc.stdout is not None
            seen = 0
            try:
                while not stop_io.is_set():
                    chunk = proc.stdout.read(_PIPE_CHUNK_BYTES)
                    if not chunk:
                        break
                    seen += len(chunk)
                    if seen > max_output_bytes:
                        out_overflow[0] = True
                        stop_io.set()
                        _kill_worker_tree(proc, None)
                        break
                    out_chunks.append(chunk)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
                reader_finished[0] = True
                wake.set()

        writer = threading.Thread(target=_write_stdin, daemon=True)
        reader = threading.Thread(target=_read_stdout, daemon=True)
        writer.start()
        reader.start()

        timed_out = False
        cancelled = False
        while not reader_finished[0]:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            rem = deadline - time.monotonic()
            if rem <= 0:
                timed_out = True
                break
            wake.wait(timeout=rem)

        if not timed_out and not cancelled and not out_overflow[0]:
            while proc.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                rem = deadline - time.monotonic()
                if rem <= 0:
                    timed_out = True
                    break
                try:
                    proc.wait(timeout=min(0.05, rem))
                    break
                except subprocess.TimeoutExpired:
                    continue

        if cancelled or timed_out or out_overflow[0]:
            stop_io.set()
            _kill_worker_tree(proc, h_job)
            h_job = None
            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass
            writer.join(timeout=0.5)
            reader.join(timeout=0.5)
            raise _limit_error()

        stop_io.set()
        writer.join(timeout=0.5)
        reader.join(timeout=0.5)

        if proc.returncode != 0:
            raise _limit_error()

        raw_out = b"".join(out_chunks)
        return _parse_and_validate_child_output(raw_out, max_chars=max_chars)
    finally:
        stop_io.set()
        _kill_worker_tree(proc, h_job)
        if proc is not None:
            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass
        _remove_temp_dir_with_retry(temp_dir)


async def _execute_under_semaphore(
    max_concurrent: int,
    work_fn: Callable[[threading.Event, threading.Event], ExtractionResult],
) -> ExtractionResult:
    """Acquire concurrency slot, run ``work_fn``, and kill+await on cancellation."""
    sem = _get_semaphore(max_concurrent)
    loop = asyncio.get_running_loop()
    async with sem:
        cancel_event = threading.Event()
        wake_event = threading.Event()
        fut = loop.run_in_executor(
            _get_executor(), lambda: work_fn(cancel_event, wake_event)
        )
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            cancel_event.set()
            wake_event.set()
            while not fut.done():
                try:
                    await asyncio.shield(fut)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            raise


async def extract_bytes_in_worker(
    data: bytes,
    *,
    name: Optional[str] = None,
    content_type: Optional[str] = None,
    max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_chars: int = MAX_EXTRACTED_CHARS,
    timeout_seconds: Optional[float] = None,
    memory_limit_mb: Optional[int] = None,
    max_output_bytes: Optional[int] = None,
    max_concurrent: Optional[int] = None,
    keep_dumpable: Optional[bool] = None,
) -> ExtractionResult:
    """Run ``extract_bytes`` in an isolated worker process under semaphore & limits."""
    eff_timeout = (
        EXTRACT_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds)
    )
    eff_mem = (
        EXTRACT_MEMORY_LIMIT_MB if memory_limit_mb is None else int(memory_limit_mb)
    )
    eff_out = (
        EXTRACT_MAX_OUTPUT_BYTES if max_output_bytes is None else int(max_output_bytes)
    )
    eff_conc = (
        EXTRACT_MAX_CONCURRENT if max_concurrent is None else int(max_concurrent)
    )
    eff_chars = max(1, int(max_chars))
    header = {
        "op": "bytes",
        "name": name,
        "content_type": content_type,
        "max_bytes": int(max_bytes),
        "max_chars": eff_chars,
        "payload_len": len(data),
    }

    def _work(
        cancel_ev: threading.Event, wake_ev: threading.Event
    ) -> ExtractionResult:
        return _run_worker_sync(
            header,
            data,
            timeout_seconds=eff_timeout,
            memory_limit_mb=eff_mem,
            max_output_bytes=eff_out,
            max_chars=eff_chars,
            cancel_event=cancel_ev,
            wake_event=wake_ev,
            keep_dumpable=keep_dumpable,
        )

    return await _execute_under_semaphore(eff_conc, _work)


async def extract_item_in_worker(
    item_fields: Dict[str, Any],
    *,
    max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_chars: int = MAX_EXTRACTED_CHARS,
    timeout_seconds: Optional[float] = None,
    memory_limit_mb: Optional[int] = None,
    max_output_bytes: Optional[int] = None,
    max_concurrent: Optional[int] = None,
    keep_dumpable: Optional[bool] = None,
) -> ExtractionResult:
    """Render serialized ``ItemAttachment`` fields (including HTML/MIME) in the worker."""
    eff_timeout = (
        EXTRACT_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds)
    )
    eff_mem = (
        EXTRACT_MEMORY_LIMIT_MB if memory_limit_mb is None else int(memory_limit_mb)
    )
    eff_out = (
        EXTRACT_MAX_OUTPUT_BYTES if max_output_bytes is None else int(max_output_bytes)
    )
    eff_conc = (
        EXTRACT_MAX_CONCURRENT if max_concurrent is None else int(max_concurrent)
    )
    eff_chars = max(1, int(max_chars))
    raw_fields = dict(item_fields)

    def _work(
        cancel_ev: threading.Event, wake_ev: threading.Event
    ) -> ExtractionResult:
        fields_copy = dict(raw_fields)
        mime_bytes = fields_copy.pop("mime_bytes", None)
        if isinstance(mime_bytes, (bytes, bytearray)) and mime_bytes:
            payload = bytes(mime_bytes)
            payload_kind = "mime"
        else:
            # Serialize potentially multi-megabyte HTML/text bodies on the
            # supervisor executor thread so the event loop is never blocked.
            payload = json.dumps(
                {
                    "text_body": sanitize_surrogates(
                        str(fields_copy.pop("text_body", "") or "")
                    ),
                    "body_str": sanitize_surrogates(
                        str(fields_copy.pop("body_str", "") or "")
                    ),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            payload_kind = "bodies_json"

        header = {
            "op": "item",
            "fields": {
                "sender": sanitize_surrogates(str(fields_copy.get("sender") or "")),
                "to": sanitize_surrogates(str(fields_copy.get("to") or "")),
                "cc": sanitize_surrogates(str(fields_copy.get("cc") or "")),
                "date_str": sanitize_surrogates(str(fields_copy.get("date_str") or "")),
                "subject": sanitize_surrogates(str(fields_copy.get("subject") or "")),
                "att_names": [
                    sanitize_surrogates(str(x))
                    for x in (fields_copy.get("att_names") or [])
                ],
                "extra_atts": int(fields_copy.get("extra_atts") or 0),
            },
            "payload_kind": payload_kind,
            "max_bytes": int(max_bytes),
            "max_chars": eff_chars,
            "payload_len": len(payload),
        }
        return _run_worker_sync(
            header,
            payload,
            timeout_seconds=eff_timeout,
            memory_limit_mb=eff_mem,
            max_output_bytes=eff_out,
            max_chars=eff_chars,
            cancel_event=cancel_ev,
            wake_event=wake_ev,
            keep_dumpable=keep_dumpable,
        )

    return await _execute_under_semaphore(eff_conc, _work)
