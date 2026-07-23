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

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]


@dataclass(frozen=True)
class BackgroundHealth:
    hwnd_found: bool
    client_size: tuple[int, int]
    capture_ok: bool
    capture_nonempty: bool
    capture_source: str = "none"
    error: Optional[str] = None


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
        return frame.copy() if frame.size else None


def _send_mouse(flags: int) -> None:
    extra = ctypes.c_ulong(0)
    inp = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, flags, 0, ctypes.pointer(extra)))
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        raise ctypes.WinError(ctypes.get_last_error())


def _send_key(vk: int, flags: int = 0) -> None:
    extra = ctypes.c_ulong(0)
    inp = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(int(vk), 0, flags, 0, ctypes.pointer(extra)))
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        raise ctypes.WinError(ctypes.get_last_error())


def _focus_window(hwnd: int) -> None:
    """Give Roblox real foreground focus before SendInput reaches it."""
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.BringWindowToTop(hwnd)
    if not user32.SetForegroundWindow(hwnd):
        raise ctypes.WinError(ctypes.get_last_error())
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if user32.GetForegroundWindow() == hwnd:
            return
        time.sleep(0.01)
    raise RuntimeError("Windows did not grant Roblox foreground focus")


class BackgroundModeController:
    def __init__(self, hwnd: int):
        if not hwnd or not user32.IsWindow(hwnd):
            raise ValueError("Invalid Roblox window handle")
        self.hwnd = int(hwnd)

    def client_size(self) -> tuple[int, int]:
        return _client_size(self.hwnd)

    def capture(self):
        return _capture_client(self.hwnd)

    def click(self, x: int, y: int, button: str = "left") -> None:
        """Click exact Roblox-client coordinates with real foreground input."""
        origin_x, origin_y = _client_origin_screen(self.hwnd)
        screen_x = origin_x + int(x)
        screen_y = origin_y + int(y)

        _focus_window(self.hwnd)
        time.sleep(0.12)
        if not user32.SetCursorPos(screen_x, screen_y):
            raise ctypes.WinError(ctypes.get_last_error())

        point = POINT()
        user32.GetCursorPos(ctypes.byref(point))
        if (point.x, point.y) != (screen_x, screen_y):
            raise RuntimeError(f"Cursor landed at ({point.x},{point.y}), expected ({screen_x},{screen_y})")

        flags = {
            "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        }[button]
        _send_mouse(flags[0])
        time.sleep(0.06)
        _send_mouse(flags[1])
        # Keep Roblox focused briefly so its UI receives and processes the event.
        time.sleep(0.35)

    def key_down(self, vk: int) -> None:
        _send_key(vk)

    def key_up(self, vk: int) -> None:
        _send_key(vk, KEYEVENTF_KEYUP)

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

    def move_to(self, x: int, y: int) -> None:
        return None

    def click(self, x: int = None, y: int = None, button: str = "left", hold: float = 0.05) -> None:
        if x is None or y is None:
            raise ValueError("Background clicks require coordinates")
        self.controller.click(int(x), int(y), button)
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
