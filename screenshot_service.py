from __future__ import annotations

import base64
import io
import logging
import os
import sys
from typing import Optional

logger = logging.getLogger("vntl.screenshot")

try:
    import mss
    from PIL import Image
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False
    logger.warning(
        "mss/Pillow not installed — screenshot context disabled. "
        "Run: uv pip install mss Pillow"
    )

_COMPARE_SIZE          = (160, 90)  # thumbnail size for change detection (16:9)
_SEND_WIDTH            = 800        # max width when sending to Claude (px)
_PIXEL_SENSITIVITY     = 10         # per-pixel noise floor for change counting (0–255)
_LAST_SCREENSHOT_PATH  = os.path.expanduser("~/.config/vntl/last_screenshot.jpg")

# ---------------------------------------------------------------------------
# Windows helpers: find the main window of a given PID
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wt

    _user32 = ctypes.windll.user32
    _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    def _find_game_window(pid: int) -> Optional[int]:
        """Return the HWND of the largest visible top-level window owned by pid."""
        found: list[tuple[int, int]] = []

        def cb(hwnd: int, _lparam: int) -> bool:
            lp = ctypes.c_ulong(0)
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(lp))
            if lp.value == pid and _user32.IsWindowVisible(hwnd):
                r = wt.RECT()
                _user32.GetClientRect(hwnd, ctypes.byref(r))
                if r.right > 0 and r.bottom > 0:
                    found.append((r.right * r.bottom, hwnd))
            return True

        _user32.EnumWindows(_WNDENUMPROC(cb), 0)
        return max(found)[1] if found else None

    def _client_screen_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
        """Return (left, top, width, height) of the client area in screen coords."""
        r = wt.RECT()
        if not _user32.GetClientRect(hwnd, ctypes.byref(r)):
            return None
        pt = _POINT(0, 0)
        _user32.ClientToScreen(hwnd, ctypes.byref(pt))
        w = r.right - r.left
        h = r.bottom - r.top
        return (pt.x, pt.y, w, h) if w > 0 and h > 0 else None

else:
    def _find_game_window(pid: int) -> Optional[int]:  # type: ignore[misc]
        return None

    def _client_screen_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:  # type: ignore[misc]
        return None


# ---------------------------------------------------------------------------
# ScreenshotService
# ---------------------------------------------------------------------------

class ScreenshotService:
    """
    Captures screenshots and detects scene changes via grayscale thumbnail diff.

    Call capture() each time a new dialogue line arrives.  Returns a
    base64-encoded JPEG when the scene has changed since the last sent
    screenshot, or None when the scene is unchanged (or libraries unavailable).

    Call set_pid(pid) to specify which process window to capture. The feature
    is inactive until a PID is set (is_attached == False).
    """

    def __init__(self) -> None:
        self._last_thumb: Optional[bytes] = None
        self._pid: Optional[int] = None
        self.threshold: float = 15.0
        self.last_pct: float = 0.0
        self.last_triggered: bool = False

    @property
    def is_attached(self) -> bool:
        return self._pid is not None

    def set_pid(self, pid: Optional[int]) -> None:
        self._pid = pid

    def capture(self, force: bool = False) -> Optional[str]:
        """
        Take a screenshot of the game window (if a PID is set) or the primary
        monitor.  Returns base64 JPEG string if the scene changed, else None.
        Returns None if mss/Pillow are not installed.
        Pass force=True to bypass the scene-change threshold (used to retry a
        failed describe_scene call on the next dialogue line).
        """
        if not _AVAILABLE:
            return None

        pid = self._pid
        try:
            with mss.mss() as sct:
                region = sct.monitors[1]  # fallback: primary monitor
                if pid is not None:
                    hwnd = _find_game_window(pid)
                    if hwnd is not None:
                        bounds = _client_screen_rect(hwnd)
                        if bounds is not None:
                            x, y, w, h = bounds
                            region = {"left": x, "top": y, "width": w, "height": h}
                raw = sct.grab(region)
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        except Exception:
            logger.exception("Screenshot capture failed")
            return None

        thumb = img.convert("L").resize(_COMPARE_SIZE).tobytes()
        pct = self._diff_pct(thumb)
        self._last_thumb = thumb  # always advance baseline — compare consecutive frames
        self.last_pct = pct
        self.last_triggered = pct > self.threshold
        if not self.last_triggered and not force:
            return None

        w, h = img.size
        if w > _SEND_WIDTH:
            img = img.resize((_SEND_WIDTH, int(h * _SEND_WIDTH / w)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)

        os.makedirs(os.path.dirname(_LAST_SCREENSHOT_PATH), exist_ok=True)
        with open(_LAST_SCREENSHOT_PATH, "wb") as f:
            f.write(buf.getvalue())
        logger.debug(
            "Screenshot saved to %s (%d bytes).",
            _LAST_SCREENSHOT_PATH,
            buf.tell(),
        )
        return base64.b64encode(buf.getvalue()).decode()

    def _diff_pct(self, new_thumb: bytes) -> float:
        """Return % of pixels that changed by more than _PIXEL_SENSITIVITY."""
        if self._last_thumb is None or len(new_thumb) != len(self._last_thumb):
            return 100.0  # first frame always triggers
        changed = sum(1 for a, b in zip(new_thumb, self._last_thumb)
                      if abs(a - b) > _PIXEL_SENSITIVITY)
        return changed * 100.0 / len(new_thumb)
