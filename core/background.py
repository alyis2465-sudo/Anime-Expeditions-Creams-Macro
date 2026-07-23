"""Experimental Windows background-mode support for Roblox.

This module is intentionally isolated from the existing macro runner. Normal
mode remains unchanged. BackgroundModeController provides window-specific
capture and input primitives so the runner can be adapted incrementally after
real Roblox testing.

The first implementation uses Win32 PrintWindow for window capture and
PostMessage for client-relative mouse/keyboard events. Roblox may reject some
background messages depending on the input path used by the game, so callers
should run the health check before starting a long macro session.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

if sys.platform != "win32":
    raise RuntimeError("Background Mode is currently supported on Windows only")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12

class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

@dataclass(frozen=True)
class BackgroundHealth:
    hwnd_found: bool
    client_size: tuple[int, int]
    capture_ok: bool
    capture_nonempty: bool
    error: Optional[str] = None


def _make_lparam(x: int, y: int) -> int:
    return (int(y) << 16) | (int(x) & 0xFFFF)


def _client_size(hwnd: int) -> tuple[int, int]:
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    return rect.right - rect.left, rect.bottom - rect.top


def _capture_client(hwnd: int):
    """Capture the client area into a BGRA numpy array.

    PrintWindow asks the target window to render into a compatible bitmap,
    which allows capture without moving the user's cursor or changing the
    foreground window. A small BitBlt fallback handles windows that refuse
    PW_RENDERFULLCONTENT.
    """
    import numpy as np

    width, height = _client_size(hwnd)
    if width <= 0 or height <= 0:
        raise RuntimeError("Roblox client area has no size")

    hwnd_dc = user32.GetDC(hwnd)
    if not hwnd_dc:
        raise ctypes.WinError(ctypes.get_last_error())
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    old_bitmap = gdi32.SelectObject(mem_dc, bitmap)
    try:
        rendered = user32.PrintWindow(hwnd, mem_dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
        if not rendered:
            rendered = user32.PrintWindow(hwnd, mem_dc, PW_CLIENTONLY)
        if not rendered:
            return None

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        buffer = (ctypes.c_ubyte * (width * height * 4))()
        copied = gdi32.GetDIBits(
            mem_dc,
            bitmap,
            0,
            height,
            ctypes.byref(buffer),
            ctypes.byref(bmi),
            0,
        )
        if copied != height:
            return None
        return np.frombuffer(buffer, dtype=np.uint8).reshape((height, width, 4)).copy()
    finally:
        gdi32.SelectObject(mem_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)


class BackgroundModeController:
    """Window-specific Roblox capture and input controller.

    This class does not change the global mouse position and does not activate
    the Roblox window. It is intentionally separate from core.mouse and
    core.keyboard so the existing macro path remains untouched.
    """

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
            return BackgroundHealth(True, size, capture_ok, nonempty)
        except Exception as exc:
            return BackgroundHealth(True, (0, 0), False, False, str(exc))


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
