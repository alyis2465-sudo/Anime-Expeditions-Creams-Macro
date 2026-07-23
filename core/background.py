"""Experimental Windows Background Mode support for Roblox."""

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


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


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
        down, up, mask = messages[button]
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
            return BackgroundHealth(True, size, capture_ok, nonempty, "mss-visible-region")
        except Exception as exc:
            return BackgroundHealth(True, (0, 0), False, False, "none", str(exc))


class BackgroundMouse:
    def __init__(self, controller: BackgroundModeController):
        self.controller = controller

    def _client(self, x: int, y: int) -> tuple[int, int]:
        return _screen_to_client(self.controller.hwnd, x, y)

    def move_to(self, x: int, y: int) -> None:
        cx, cy = self._client(x, y)
        self.controller.move(cx, cy)

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

    def nudge(self, dx: int = 1, dy: int = 0) -> None:
        return None

    def position(self):
        return None

    def down(self, button: str = "left") -> None:
        raise RuntimeError("Background mouse button hold is not supported yet")

    def up(self, button: str = "left") -> None:
        raise RuntimeError("Background mouse button hold is not supported yet")

    def drag(self, *args, **kwargs) -> None:
        raise RuntimeError("Background drag is not supported yet")

    def scroll(self, amount: int) -> None:
        raise RuntimeError("Background scroll is not supported yet")


class BackgroundKeyboard:
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
