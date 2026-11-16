"""Minimal isolated child entry point for attachment text extraction.

Started via ``python -m ewsmcp.extract_child --memory-limit-mb <MB>`` by
``ewsmcp.extract_worker``. Keeps module-load dependencies strictly to the Python
standard library so the child can apply its own OS memory ceiling as its very
first act before importing XML/document parsers or reading ``stdin``, and never
loads ``exchangelib``, ``mcp``, ``requests``, or server/gateway/tool modules.
"""

from __future__ import annotations

import ctypes
import io
import json
import logging
import mmap
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_MEMORY_LIMIT_MB: int = 1024
EXIT_MEMORY_EXCEEDED: int = 2
EXIT_MEMORY_LIMIT_FAILED: int = 3
EXTRACT_SOFT_DEADLINE_RATIO: float = 0.75

WORKER_LIMIT_ERROR_MESSAGE = (
    "the document could not be read within the time and memory limits"
)

_MAX_WORKER_HEADER_BYTES = 2 * 1024 * 1024
_PIPE_CHUNK_BYTES = 65_536
_PROBE_BYTES = 65_536

_SELF_JOB_HANDLE: Optional[int] = None


def memory_ceiling_supported(platform_name: Optional[str] = None) -> bool:
    """Return True on platforms where the child enforces an OS memory ceiling."""
    plat = (sys.platform if platform_name is None else platform_name).lower()
    return plat == "win32" or plat.startswith("linux")


if os.name == "nt":
    import ctypes.wintypes as _wt

    _JOB_OBJECT_EXTENDED_LIMIT_INFO_CLASS = 9
    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    _JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
    _MEM_COMMIT = 0x00001000
    _MEM_RESERVE = 0x00002000
    _MEM_RELEASE = 0x00008000
    _PAGE_READWRITE = 0x04

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

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", _wt.DWORD),
            ("PageFaultCount", _wt.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
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
    _k32.GetCurrentProcess.argtypes = []
    _k32.GetCurrentProcess.restype = _wt.HANDLE
    _k32.AssignProcessToJobObject.argtypes = [_wt.HANDLE, _wt.HANDLE]
    _k32.AssignProcessToJobObject.restype = _wt.BOOL
    _k32.K32GetProcessMemoryInfo.argtypes = [
        _wt.HANDLE,
        ctypes.c_void_p,
        _wt.DWORD,
    ]
    _k32.K32GetProcessMemoryInfo.restype = _wt.BOOL
    _k32.VirtualAlloc.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        _wt.DWORD,
        _wt.DWORD,
    ]
    _k32.VirtualAlloc.restype = ctypes.c_void_p
    _k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, _wt.DWORD]
    _k32.VirtualFree.restype = _wt.BOOL
    _k32.CloseHandle.argtypes = [_wt.HANDLE]
    _k32.CloseHandle.restype = _wt.BOOL


def _apply_windows_self_memory_limit(memory_limit_mb: int) -> bool:
    """Create a Job Object with process/job commit limits and assign self."""
    global _SELF_JOB_HANDLE
    if os.name != "nt" or memory_limit_mb <= 0:
        return False
    limit_bytes = int(memory_limit_mb) * 1024 * 1024
    h_job = _k32.CreateJobObjectW(None, None)
    if not h_job:
        return False
    try:
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | _JOB_OBJECT_LIMIT_JOB_MEMORY
            | _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        )
        info.ProcessMemoryLimit = limit_bytes
        info.JobMemoryLimit = limit_bytes
        ok_set = _k32.SetInformationJobObject(
            h_job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok_set:
            _k32.CloseHandle(h_job)
            return False

        h_self = _k32.GetCurrentProcess()
        if not _k32.AssignProcessToJobObject(h_job, h_self):
            _k32.CloseHandle(h_job)
            return False

        # Keep the job handle open for the rest of the process lifetime.
        _SELF_JOB_HANDLE = int(h_job)

        # Verify the ceiling is not smaller than the interpreter's own commit.
        pmc = _PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        if not _k32.K32GetProcessMemoryInfo(
            h_self, ctypes.byref(pmc), ctypes.sizeof(pmc)
        ):
            return False
        if int(pmc.PagefileUsage) > limit_bytes:
            return False

        probe_ptr = _k32.VirtualAlloc(
            None, _PROBE_BYTES, _MEM_COMMIT | _MEM_RESERVE, _PAGE_READWRITE
        )
        if not probe_ptr:
            return False
        _k32.VirtualFree(ctypes.c_void_p(probe_ptr), 0, _MEM_RELEASE)
        return True
    except Exception:
        if _SELF_JOB_HANDLE is None:
            try:
                _k32.CloseHandle(h_job)
            except Exception:
                pass
        return False


def _apply_linux_self_memory_limit(memory_limit_mb: int) -> bool:
    """Apply RLIMIT_AS to self on Linux and verify current address space fits."""
    if memory_limit_mb <= 0:
        return False
    try:
        import resource

        limit_bytes = int(memory_limit_mb) * 1024 * 1024
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        target = (
            min(limit_bytes, hard)
            if hard != resource.RLIM_INFINITY
            else limit_bytes
        )
        if target <= 0:
            return False
        resource.setrlimit(resource.RLIMIT_AS, (target, target))

        with open("/proc/self/statm", "rb") as fp:
            first_tok = fp.read(64).split(None, 1)[0]
        vm_bytes = int(first_tok) * int(os.sysconf("SC_PAGE_SIZE"))
        if vm_bytes > target:
            return False

        probe = mmap.mmap(-1, _PROBE_BYTES)
        probe.close()
        return True
    except Exception:
        return False


def apply_self_memory_limit(
    memory_limit_mb: int, *, platform_name: Optional[str] = None
) -> Tuple[bool, bool]:
    """Apply the per-document memory ceiling to the current child process.

    Returns ``(ok, enforced)``:
    - Windows / Linux: ``(True, True)`` when applied and verified; ``(False, False)``
      when the ceiling cannot be applied or is smaller than the interpreter itself.
    - Other platforms: ``(True, False)`` so extraction continues while reporting
      that the document memory ceiling is not enforced on this platform.
    """
    plat = (sys.platform if platform_name is None else platform_name).lower()
    if plat == "win32":
        ok = _apply_windows_self_memory_limit(memory_limit_mb)
        return ok, ok
    if plat.startswith("linux"):
        ok = _apply_linux_self_memory_limit(memory_limit_mb)
        return ok, ok
    return True, False


def _parse_child_args(argv: Sequence[str]) -> int:
    mem_mb = DEFAULT_MEMORY_LIMIT_MB
    i = 0
    n = len(argv)
    while i < n:
        if argv[i] == "--memory-limit-mb" and i + 1 < n:
            try:
                mem_mb = int(argv[i + 1])
            except ValueError:
                return -1
            i += 2
        else:
            i += 1
    return mem_mb


def _compute_soft_deadline(req: Dict[str, Any]) -> Optional[float]:
    raw_timeout = req.get("timeout_seconds")
    if not isinstance(raw_timeout, (int, float)) or isinstance(raw_timeout, bool):
        return None
    timeout_s = float(raw_timeout)
    if timeout_s <= 0:
        return None
    soft_budget = EXTRACT_SOFT_DEADLINE_RATIO * timeout_s
    raw_started = req.get("started_at")
    elapsed = 0.0
    if isinstance(raw_started, (int, float)) and not isinstance(raw_started, bool):
        delta = time.time() - float(raw_started)
        if 0.0 <= delta <= timeout_s:
            elapsed = delta
    return time.monotonic() + max(0.0, soft_budget - elapsed)


def _read_exact_bounded(
    stream: Any, expected: int, ceiling: int, tool_error_cls: Any
) -> bytes:
    if expected < 0 or expected > ceiling:
        raise tool_error_cls(
            "validation",
            f"Attachment size ({expected} bytes) exceeds the server ceiling "
            f"({ceiling} bytes; EWS_MAX_ATTACHMENT_MB).",
        )
    chunks: List[bytes] = []
    remaining = expected
    while remaining > 0:
        block = stream.read(min(_PIPE_CHUNK_BYTES, remaining))
        if not block:
            break
        chunks.append(block)
        remaining -= len(block)
    data = b"".join(chunks)
    if len(data) != expected:
        raise tool_error_cls("validation", WORKER_LIMIT_ERROR_MESSAGE)
    return data


def _child_main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mem_mb = _parse_child_args(args)

    # First act before importing parsers or reading a single byte from stdin:
    # apply the OS memory ceiling directly to this interpreter process.
    ok_mem, mem_enforced = apply_self_memory_limit(mem_mb)
    if not ok_mem:
        return EXIT_MEMORY_LIMIT_FAILED

    logging.disable(logging.CRITICAL)
    stdin_bin = sys.stdin.buffer
    stdout_bin = sys.stdout.buffer
    # Redirect Python-level sys.stdout to stderr during parsing so stray prints
    # from third-party parsers never corrupt the JSON response frame.
    sys.stdout = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    def _emit(payload_obj: Dict[str, Any]) -> int:
        encoded = json.dumps(
            payload_obj, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        stdout_bin.write(encoded)
        stdout_bin.flush()
        return 0

    try:
        from . import extract as _extract
        from .toolerror import ToolError

        header_line = stdin_bin.readline(_MAX_WORKER_HEADER_BYTES + 1)
        if not header_line or len(header_line) > _MAX_WORKER_HEADER_BYTES:
            return _emit({"ok": False, "limit_exceeded": True})
        req = json.loads(header_line.decode("utf-8"))
        if not isinstance(req, dict):
            return _emit({"ok": False, "limit_exceeded": True})

        op = req.get("op")
        max_bytes = int(req.get("max_bytes") or _extract.DEFAULT_MAX_ATTACHMENT_BYTES)
        max_chars = int(req.get("max_chars") or _extract.MAX_EXTRACTED_CHARS)
        payload_len = int(req.get("payload_len") or 0)
        soft_deadline = _compute_soft_deadline(req)

        if op == "bytes":
            data = _read_exact_bounded(stdin_bin, payload_len, max_bytes, ToolError)
            res = _extract.extract_bytes(
                data,
                name=req.get("name"),
                content_type=req.get("content_type"),
                max_bytes=max_bytes,
                max_chars=max_chars,
                soft_deadline=soft_deadline,
            )
        elif op == "item":
            item_cap = max(max_bytes * 2, 4 * 1024 * 1024)
            raw_payload = _read_exact_bounded(
                stdin_bin, payload_len, item_cap, ToolError
            )
            fields = dict(req.get("fields") or {})
            pkind = req.get("payload_kind")
            if pkind == "mime":
                res = _extract.extract_serialized_message_item(
                    fields,
                    mime_bytes=raw_payload,
                    max_bytes=max_bytes,
                    max_chars=max_chars,
                    soft_deadline=soft_deadline,
                )
            else:
                bodies = json.loads(raw_payload.decode("utf-8"))
                fields["text_body"] = bodies.get("text_body") or ""
                fields["body_str"] = bodies.get("body_str") or ""
                res = _extract.extract_serialized_message_item(
                    fields,
                    max_bytes=max_bytes,
                    max_chars=max_chars,
                    soft_deadline=soft_deadline,
                )
        else:
            return _emit({"ok": False, "limit_exceeded": True})

        return _emit({
            "ok": True,
            "text": _extract.sanitize_surrogates(res.text),
            "extracted_from": res.extracted_from,
            "pages": res.pages,
            "slides": res.slides,
            "sheets": res.sheets,
            "pages_read": res.pages_read,
            "slides_read": res.slides_read,
            "sheets_read": res.sheets_read,
            "partial": bool(res.partial),
            "notes": [
                _extract.cap_message(n, _extract.MAX_NOTE_CHARS)
                for n in res.notes
                if n
            ],
            "memory_ceiling_enforced": bool(mem_enforced),
            "loaded_modules": sorted(sys.modules.keys()),
        })
    except MemoryError:
        return EXIT_MEMORY_EXCEEDED
    except ToolError as err:
        return _emit({
            "ok": False,
            "code": "validation",
            "message": _extract.cap_message(
                err.message, _extract.MAX_ERROR_MESSAGE_CHARS
            ),
            "hint": (
                _extract.cap_message(err.hint, _extract.MAX_ERROR_MESSAGE_CHARS)
                if err.hint
                else None
            ),
        })
    except Exception as exc:
        try:
            from . import extract as _ext_fb

            msg = _ext_fb.cap_message(
                f"Document could not be parsed ({type(exc).__name__}).",
                _ext_fb.MAX_ERROR_MESSAGE_CHARS,
            )
        except Exception:
            msg = f"Document could not be parsed ({type(exc).__name__})."[:300]
        return _emit({
            "ok": False,
            "code": "validation",
            "message": msg,
            "hint": None,
        })


if __name__ == "__main__":
    raise SystemExit(_child_main())
