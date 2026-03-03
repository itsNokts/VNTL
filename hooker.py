"""
hooker.py — VNTL built-in text hooker

Injects vntl_hook_{x86|x64}.dll into the target VN process, reads text
extracted by the DLL from a named pipe, debounces letter-by-letter reveals,
and publishes stable lines to text_queue for consumption by input_loop.

Wire protocol (per message from DLL):
  [ UINT64 hook_id ][ DWORD charLen ][ wchar_t text[charLen] ]

hook_id is the caller's return address in the game, uniquely identifying
each call-site ("stream").  Multiple streams can be enabled simultaneously;
their latest stable texts are combined with a configurable separator before
being put into text_queue.

Windows-only; no new pip dependencies (uses ctypes + stdlib).
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import logging
import os
import struct
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Windows API wrappers (ctypes — no pywin32 required)
# ---------------------------------------------------------------------------

_k32 = ctypes.windll.kernel32 if sys.platform == "win32" else None

PROCESS_ALL_ACCESS        = 0x1F0FFF
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT                = 0x1000
MEM_RESERVE               = 0x2000
PAGE_READWRITE            = 0x04
PIPE_ACCESS_DUPLEX        = 0x00000003
PIPE_TYPE_BYTE            = 0x00000000
PIPE_READMODE_BYTE        = 0x00000000
PIPE_WAIT                 = 0x00000000
TH32CS_SNAPPROCESS        = 0x00000002
INVALID_HANDLE_VALUE      = wt.HANDLE(-1).value

NMPWAIT_USE_DEFAULT_WAIT = 0xFFFFFFFF


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize",              wt.DWORD),
        ("cntUsage",            wt.DWORD),
        ("th32ProcessID",       wt.DWORD),
        ("th32DefaultHeapID",   ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID",        wt.DWORD),
        ("cntThreads",          wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase",      ctypes.c_long),
        ("dwFlags",             wt.DWORD),
        ("szExeFile",           ctypes.c_char * 260),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dll_dir() -> Path:
    """Return the hook/ directory next to this file (works frozen and unfrozen)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / "hook"


def _is_target_64bit(pid: int) -> bool:
    """Return True if the process with *pid* is a native 64-bit process."""
    if sys.maxsize <= 2**32:
        return False  # VNTL itself is 32-bit → everything is 32-bit world
    handle = _k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        return False
    is_wow64 = ctypes.c_bool(False)
    _k32.IsWow64Process(handle, ctypes.byref(is_wow64))
    _k32.CloseHandle(handle)
    return not is_wow64.value


def list_processes() -> list[tuple[int, str]]:
    """Return [(pid, exe_name), ...] for all running processes."""
    if sys.platform != "win32":
        return []
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return []
    results: list[tuple[int, str]] = []
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    try:
        if _k32.Process32First(snap, ctypes.byref(entry)):
            while True:
                name = entry.szExeFile.decode("utf-8", errors="replace")
                results.append((entry.th32ProcessID, name))
                if not _k32.Process32Next(snap, ctypes.byref(entry)):
                    break
    finally:
        _k32.CloseHandle(snap)
    return results


def _inject_direct(pid: int, dll_path: str) -> bool:
    """
    Inject *dll_path* into process *pid* via the classic
    CreateRemoteThread + LoadLibraryW technique.
    Both VNTL and the target must share the same bitness for LoadLibraryW
    to resolve to the correct address.
    Returns True on success.
    """
    path_bytes = (dll_path + "\0").encode("utf-16-le")
    path_len   = len(path_bytes)

    handle = _k32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not handle:
        logger.error("OpenProcess(%d) failed: %d", pid, _k32.GetLastError())
        return False

    try:
        remote_buf = _k32.VirtualAllocEx(
            handle, None, path_len, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE,
        )
        if not remote_buf:
            logger.error("VirtualAllocEx failed: %d", _k32.GetLastError())
            return False

        written = ctypes.c_size_t(0)
        ok = _k32.WriteProcessMemory(
            handle, remote_buf, path_bytes, path_len, ctypes.byref(written),
        )
        if not ok:
            logger.error("WriteProcessMemory failed: %d", _k32.GetLastError())
            return False

        kernel32_handle = _k32.GetModuleHandleW("kernel32.dll")
        load_lib = _k32.GetProcAddress(kernel32_handle, b"LoadLibraryW")
        if not load_lib:
            logger.error("GetProcAddress(LoadLibraryW) failed.")
            return False

        thread = _k32.CreateRemoteThread(
            handle, None, 0, load_lib, remote_buf, 0, None,
        )
        if not thread:
            logger.error("CreateRemoteThread failed: %d", _k32.GetLastError())
            return False

        _k32.WaitForSingleObject(thread, 5000)
        _k32.CloseHandle(thread)
        return True

    finally:
        _k32.CloseHandle(handle)


def _inject_via_helper(pid: int, dll_path: str) -> bool:
    """
    Delegate injection to vntl_inject32.exe (a 32-bit helper exe) so that
    LoadLibraryW resolves to the correct 32-bit address when injecting into
    a WOW64 (32-bit) target from a 64-bit VNTL process.
    Returns True on success.
    """
    helper = _dll_dir() / "vntl_inject32.exe"
    if not helper.exists():
        logger.error(
            "vntl_inject32.exe not found — build it with `make -C hook`"
        )
        return False
    logger.info("Delegating to vntl_inject32.exe for PID %d", pid)
    result = subprocess.run([str(helper), str(pid), dll_path])
    return result.returncode == 0


def _inject_dll(pid: int, dll_path: str) -> bool:
    """
    Inject *dll_path* into process *pid*.
    Automatically dispatches to the 32-bit helper exe when the target is a
    32-bit (WOW64) process and VNTL itself is running as 64-bit Python,
    because LoadLibraryW must resolve from a 32-bit address space.
    Returns True on success.
    """
    if sys.platform != "win32":
        logger.error("DLL injection is Windows-only.")
        return False

    target_64 = _is_target_64bit(pid)
    vntl_64   = sys.maxsize > 2**32

    if not target_64 and vntl_64:
        return _inject_via_helper(pid, dll_path)
    else:
        return _inject_direct(pid, dll_path)


# ---------------------------------------------------------------------------
# Debouncer
# ---------------------------------------------------------------------------

class TextDebouncer:
    """
    Call *callback* only after text has been stable for *delay_s* seconds.
    Handles letter-by-letter reveal animations: intermediate partial strings
    are cancelled as new text arrives.
    """

    def __init__(
        self,
        callback: Callable[[str], Awaitable[None]],
        delay_s: float = 0.15,
    ) -> None:
        self._callback = callback
        self._delay    = delay_s
        self._pending: str | None = None
        self._task:    asyncio.Task | None = None

    async def feed(self, text: str) -> None:
        self._pending = text
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._emit_after_delay(text))

    async def _emit_after_delay(self, text: str) -> None:
        try:
            await asyncio.sleep(self._delay)
            if self._pending == text:
                await self._callback(text)
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# StreamState
# ---------------------------------------------------------------------------

@dataclass
class StreamState:
    """Per-call-site state for one text stream."""
    hook_id:   int
    debouncer: TextDebouncer
    latest:    str        = ""
    samples:   list[str]  = field(default_factory=list)  # last 3, for UI
    last_seen: float      = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# HookerService
# ---------------------------------------------------------------------------

class HookerService:
    """
    Manages the lifecycle of the DLL injection hooker:
    - Creates a named pipe server that the injected DLL connects to.
    - Injects the appropriate DLL into the target process.
    - Reads framed UTF-16 text from the pipe in an executor thread.
    - Groups text by hook_id (caller address) into streams.
    - Only enabled streams contribute; their latest stable texts are joined
      with a configurable separator before being put into text_queue.
    - No text is emitted until the user selects streams via the stream picker.
    """

    PIPE_NAME = r"\\.\pipe\vntl_hook"

    def __init__(self) -> None:
        self.text_queue: asyncio.Queue[str] = asyncio.Queue()
        self.is_attached: bool = False
        self._pipe: wt.HANDLE | None = None
        self._reader_task: asyncio.Task | None = None
        self._attached_pid: int | None = None

        # Stream registry: hook_id → StreamState
        self._streams: dict[int, StreamState] = {}

        # [...] = only these hook_ids contribute, in order; empty = nothing emitted
        self._enabled_streams: list[int] = []
        self._separator: str = "\n"
        self._last_emitted: str = ""

    # ------------------------------------------------------------------
    # Public API — stream management
    # ------------------------------------------------------------------

    def get_streams(self) -> list[StreamState]:
        """Return all known streams, ordered by first-seen (dict insertion)."""
        return list(self._streams.values())

    def set_enabled_streams(self, ids: list[int]) -> None:
        """Set which streams contribute to translated output."""
        self._enabled_streams = ids
        self._last_emitted = ""  # force re-emit on next stable text
        # Immediately translate whatever is already on screen
        parts = [
            self._streams[hid].latest
            for hid in self._enabled_streams
            if hid in self._streams and self._streams[hid].latest
        ]
        combined = self._separator.join(parts)
        if combined:
            self._last_emitted = combined
            self.text_queue.put_nowait(combined)

    def set_separator(self, sep: str) -> None:
        """Set the string used to join multiple stream texts."""
        self._separator = sep

    # ------------------------------------------------------------------
    # Public API — server / attach / detach
    # ------------------------------------------------------------------

    def start_server(self) -> None:
        """
        Create the named pipe so it exists before attach() injects the DLL.
        This call returns immediately; the actual connection wait happens
        inside _wait_and_read(), scheduled by attach().
        """
        if sys.platform != "win32":
            logger.debug("HookerService: not on Windows, skipping pipe setup.")
            return

        PIPE_BUFFER = 131072  # 128 KB

        self._pipe = _k32.CreateNamedPipeW(
            self.PIPE_NAME,
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            1,            # max instances
            PIPE_BUFFER,
            PIPE_BUFFER,
            0,            # default timeout
            None,         # default security
        )
        if self._pipe == INVALID_HANDLE_VALUE:
            logger.error("CreateNamedPipe failed: %d", _k32.GetLastError())
            self._pipe = None
            return

        logger.debug("HookerService: named pipe created.")

    def _reset_pipe(self) -> None:
        """
        Tear down any existing pipe state and create a fresh pipe server.
        Must be called before each attach() so that the new DLL client can
        connect even after a previous client disconnected.
        """
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._pipe is not None:
            # DisconnectNamedPipe returns the server to the "listening" state;
            # safe to call even if no client was ever connected.
            _k32.DisconnectNamedPipe(self._pipe)
            _k32.CloseHandle(self._pipe)
            self._pipe = None
        self.is_attached = False
        self._attached_pid = None
        self._streams.clear()
        self.start_server()

    def attach(self, pid: int) -> bool:
        """
        Inject the hook DLL into *pid*, then schedule an async task that
        waits for the DLL to connect and starts the pipe reader.
        Picks x86 or x64 DLL automatically.
        Returns True if injection succeeded, False on any error.
        """
        if sys.platform != "win32":
            logger.error("attach() is Windows-only.")
            return False

        # Always reset pipe state so re-attachment works after a disconnect.
        self._reset_pipe()

        if self._pipe is None:
            logger.error("Failed to create named pipe.")
            return False

        target_64 = _is_target_64bit(pid)
        dll_name  = "vntl_hook_x64.dll" if target_64 else "vntl_hook_x86.dll"
        dll_path  = str((_dll_dir() / dll_name).resolve())

        if not os.path.exists(dll_path):
            logger.error(
                "Hook DLL not found: %s — build it with `make -C hook`", dll_path
            )
            return False

        logger.info("Injecting %s into PID %d …", dll_name, pid)
        if _inject_dll(pid, dll_path):
            logger.info("DLL injected into PID %d; waiting for pipe connection.", pid)
            self._attached_pid = pid
            asyncio.create_task(self._wait_and_read())
            return True
        else:
            logger.error("Injection failed for PID %d.", pid)
            return False

    def detach(self) -> None:
        """
        Close the pipe handle.  The DLL detects the broken pipe in its
        write path and calls FreeLibraryAndExitThread to unload itself.
        """
        self.is_attached = False
        self._attached_pid = None
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._pipe:
            _k32.CloseHandle(self._pipe)
            self._pipe = None
        logger.info("HookerService: detached.")

    # ------------------------------------------------------------------
    # Internal pipe reader
    # ------------------------------------------------------------------

    def _connect_pipe(self) -> tuple[bool, int]:
        """Blocking ConnectNamedPipe; returns (ok, win32_error). Must run in executor."""
        ret = bool(_k32.ConnectNamedPipe(self._pipe, None))
        err = _k32.GetLastError()
        return ret, err

    async def _wait_and_read(self) -> None:
        """Wait (in executor) for the DLL to connect, then start reading."""
        loop = asyncio.get_running_loop()
        ok, err = await loop.run_in_executor(None, self._connect_pipe)
        # ERROR_PIPE_CONNECTED (535) — client connected before we called ConnectNamedPipe; OK
        if not ok and err != 535:
            logger.warning(
                "HookerService: ConnectNamedPipe failed (err=%d); aborting read.", err
            )
            return
        logger.info("HookerService: DLL connected to pipe.")
        self.is_attached = True
        self._reader_task = asyncio.create_task(self._pipe_reader_loop())

    async def _pipe_reader_loop(self) -> None:
        """Read framed messages from the pipe and dispatch to streams."""
        loop = asyncio.get_running_loop()
        logger.debug("HookerService: pipe reader started.")
        try:
            while True:
                # Read 8-byte hook_id
                hook_id_bytes = await loop.run_in_executor(
                    None, self._read_bytes, 8
                )
                if hook_id_bytes is None:
                    break
                hook_id = struct.unpack_from("<Q", hook_id_bytes)[0]

                # Read 4-byte char count
                header = await loop.run_in_executor(None, self._read_bytes, 4)
                if header is None:
                    break
                char_len = struct.unpack_from("<I", header)[0]
                if char_len == 0 or char_len > 65535:
                    continue

                # Read text body
                body = await loop.run_in_executor(
                    None, self._read_bytes, char_len * 2
                )
                if body is None:
                    break

                text = body.decode("utf-16-le")
                logger.debug("Hooker stream 0x%016X raw: %r", hook_id, text)
                await self._on_raw_text(hook_id, text)

        except Exception as exc:
            logger.warning("HookerService pipe reader error: %s", exc)
        finally:
            self.is_attached = False
            logger.info("HookerService: disconnected from pipe.")

    async def _on_raw_text(self, hook_id: int, text: str) -> None:
        """Route an incoming text event to the appropriate stream's debouncer."""
        if hook_id not in self._streams:
            self._streams[hook_id] = StreamState(
                hook_id=hook_id,
                debouncer=TextDebouncer(
                    lambda t, hid=hook_id: self._on_stable(hid, t)
                ),
            )
        s = self._streams[hook_id]
        s.last_seen = time.monotonic()
        if not s.samples or s.samples[-1] != text:
            s.samples.append(text)
            if len(s.samples) > 3:
                s.samples.pop(0)
        await s.debouncer.feed(text)

    async def _on_stable(self, hook_id: int, text: str) -> None:
        """Called when a stream's debouncer fires with a stable text."""
        if hook_id in self._streams:
            self._streams[hook_id].latest = text
        await self._try_emit()

    async def _try_emit(self) -> None:
        """Combine latest texts from enabled streams and queue if changed."""
        parts = [
            self._streams[hid].latest
            for hid in self._enabled_streams
            if hid in self._streams and self._streams[hid].latest
        ]
        combined = self._separator.join(parts)
        if combined and combined != self._last_emitted:
            self._last_emitted = combined
            await self.text_queue.put(combined)

    def _read_bytes(self, n: int) -> bytes | None:
        """Blocking read of exactly *n* bytes from the pipe. Returns None on error."""
        buf   = ctypes.create_string_buffer(n)
        total = 0
        while total < n:
            read = ctypes.c_ulong(0)
            dest = ctypes.cast(ctypes.addressof(buf) + total, ctypes.c_char_p)
            ok = _k32.ReadFile(
                self._pipe, dest, n - total, ctypes.byref(read), None,
            )
            if not ok or read.value == 0:
                err = _k32.GetLastError()
                logger.warning(
                    "HookerService: ReadFile failed (ok=%s, bytes=%d, err=%d)",
                    bool(ok), read.value, err,
                )
                return None
            total += read.value
        return bytes(buf.raw)
