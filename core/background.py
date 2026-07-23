"""Experimental Windows background-mode support for Roblox.

This module keeps normal mode untouched. It provides a safer experimental
adapter for the Background Mode branch. Roblox's rendered game surface does
not reliably respond to ordinary WM_* PostMessage input, and PrintWindow can
return a stale or blank frame for a hardware-accelerated Roblox surface. The
adapter therefore refuses to claim that a background session is ready unless
its capture is verified.

The current capture fallback uses the live desktop region occupied by Roblox.
This produces a real current frame, but the Roblox window must remain visible
for this fallback to be valid. True occluded-window capture requires a separate
Windows Graphics Capture implementation and is not silently faked here.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

from . import pacing

if sys.platform != "win32":
    raise RuntimeError("Background Mode is currently supported on Windows only")

user32 = ctypes.WinDLL("user32", use_last_error=True)

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


@dataclass(frozen=True)
class BackgroundHealth:
    hwnd_found: bool
    client_size: tuple[int, int]
    capture_ok: bool
    capture_nonempty: bool
    capture_source: str = "none"
    error: Optional[str] = None


def _make_lparam(x: int, y: int) -> int:
    return (int(y) << 16) | (int(x) & 0xFFFF)


def _client_size(hwnd: int) -> tuple[int, int]:
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    return rect.right - rect.left, rect.bottom - rect.top


def _client_origin_screen(hwnd: int) -> tuple[int, int]:
    point = POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return point.x, point.y


def _screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    point = POINT(int(x), int(y))
    if not user32.ScreenToClient(hwnd, ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return point.x, point.y


def _capture_client(hwnd: int):
    """Capture the current visible Roblox client region using MSS.

    This is deliberately a visible-region capture. It avoids the old
    PrintWindow path, which produced a successful-looking buffer that did not
    necessarily contain the current Roblox frame.
    """
    import mss
    import numpy as np

    width, height = _client_size(hwnd)
    left, top = _client_origin_screen(hwnd)
    if width <= 0 or height <= 0:
        raise RuntimeError("Roblox client area has no size")

    with mss.mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
        frame = np.asarray(shot, dtype=np.uint8)
        if frame.size == 0:
            return None
        return frame.copy()


class BackgroundModeController:
    """Roblox capture and experimental background input controller."""

    def __init__(self, hwnd: int):
        if not hwnd or not user32.IsWindow(hwnd):
            raise ValueError("Invalid Roblox window handle")
        self.hwnd = int(hwnd)

    def client_size(self) -> tuple[int, int]:
        return _client_size(self.hwnd)

    def capture(self):
        return _capture_client(self.hwnd)

    def move(self, x: int, y: int) -> None:
        user32.PostMessageW(self.hwnd, WM_MOUSEMOVE, 0, _make_lparam(x, y))

    def click(self, x: int, y: int, button: str = "left") -> None:
        messages = {
            "left": (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON),
            "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON),
            "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON),
        }
        try:
            down, up, mask = messages[button]
        except KeyError as exc:
            raise ValueError(f"Unsupported mouse button: {button}") from exc
        lp = _make_lparam(x, y)
        user32.PostMessageW(self.hwnd, WM_MOUSEMOVE, mask, lp)
        user32.PostMessageW(self.hwnd, down, mask, lp)
        user32.PostMessageW(self.hwnd, up, 0, lp)

    def key_down(self, vk: int) -> None:
        user32.PostMessageW(self.hwnd, WM_KEYDOWN, int(vk), 0)

    def key_up(self, vk: int) -> None:
        user32.PostMessageW(self.hwnd, WM_KEYUP, int(vk), 0)

    def tap(self, vk: int, hold: float = 0.03) -> None:
        self.key_down(vk)
        time.sleep(hold)
        self.key_up(vk)

    def health_check(self) -> BackgroundHealth:
        try:
            size = self.client_size()
            frame = self.capture()
            capture_ok = frame is not None
            nonempty = bool(capture_ok and frame.size and frame.max() > 0)
            return BackgroundHealth(
                True,
                size,
                capture_ok,
                nonempty,
                "mss-visible-region",
            )
        except Exception as exc:
            return BackgroundHealth(True, (0, 0), False, False, "none", str(exc))


class BackgroundMouse:
    """Screen-coordinate mouse API compatible with core.mouse.Mouse."""

    def __init__(self, controller: BackgroundModeController):
        self.controller = controller

    def _client(self, x: int, y: int) -> tuple[int, int]:
        return _screen_to_client(self.controller.hwnd, x, y)

    def move_to(self, x: int, y: int) -> None:
        cx, cy = self._client(x, y)
        self.controller.move(cx, cy)

    def down(self, button: str = "left") -> None:
        raise RuntimeError("BackgroundMouse.down() is not supported independently")

    def up(self, button: str = "left") -> None:
        raise RuntimeError("BackgroundMouse.up() is not supported independently")

    def nudge(self, dx: int = 1, dy: int = 0) -> None:
        return None

    def click(self, x: int = None, y: int = None, button: str = "left", hold: float = 0.05) -> None:
        if x is None or y is None:
            raise ValueError("Background clicks require coordinates")
        cx, cy = self._client(x, y)
        self.controller.move(cx, cy)
        time.sleep(0.01)
        self.controller.click(cx, cy, button)
        if hold > 0:
            time.sleep(hold)
        pacing.action_pause()

    def double_click(self, x: int = None, y: int = None, button: str = "left", gap: float = 0.08) -> None:
        self.click(x, y, button)
        time.sleep(gap)
        self.click(x, y, button)

    def shuffle_click(self, x: int, y: int, button: str = "left", hold: float = 0.05) -> None:
        self.click(x, y, button, hold)

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left", steps: int = 15, duration: float = 0.2) -> None:
        raise RuntimeError("Background drag is not supported yet")

    def scroll(self, amount: int) -> None:
        raise RuntimeError("Background scroll is not supported yet")

    def position(self):
        return None


class BackgroundKeyboard:
    """Keyboard API compatible with core.keyboard.Keyboard."""

    def __init__(self, controller: BackgroundModeController):
        self.controller = controller

    def key_down(self, vk: int) -> None:
        self.controller.key_down(vk)

    def key_up(self, vk: int) -> None:
        self.controller.key_up(vk)

    def tap(self, vk: int, hold: float = 0.03, pace: bool = True) -> None:
        self.controller.tap(vk, hold)
        if pace:
            pacing.action_pause()

    def type_text(self, text: str, delay: float = 0.02) -> None:
        for ch in text:
            self.tap(ord(ch.upper()), pace=False)
            time.sleep(delay)
        pacing.action_pause()

    def combo(self, *vks: int, hold: float = 0.05) -> None:
        for vk in vks:
            self.key_down(vk)
        time.sleep(hold)
        for vk in reversed(vks):
            self.key_up(vk)
        pacing.action_pause()


def find_roblox_window(title_substring: str = "Roblox") -> Optional[int]:
    matches: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        if title_substring.lower() in title.value.lower():
            matches.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(enum_proc, 0)
    return matches[0] if matches else None
