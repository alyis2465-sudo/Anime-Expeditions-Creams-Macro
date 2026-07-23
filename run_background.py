"""Launch the existing macro UI with experimental Roblox Background Mode.

Normal `python main.py` remains unchanged. This launcher reuses the existing
UI, task system, runner, vision code, and recovery logic, but keeps Roblox as a
separate top-level window and swaps the runner to window-specific input once a
Roblox window passes the background health check.
"""

from __future__ import annotations

import sys
import threading
import time

import main
from core import vision
from core.background import BackgroundModeController, BackgroundMouse, BackgroundKeyboard
from core.mouse import Mouse
from core.keyboard import Keyboard


class BackgroundDocker:
    """Compatibility layer that prevents Roblox from being reparented.

    The existing watchdog still sees a docked game and keeps its normal state
    machine, but Roblox remains a separate top-level window. Window-content
    capture is used so the game can sit behind other applications.
    """

    def __init__(self, original):
        self._original = original
        self.cutout = True
        self.docked = False

    def dock(self, hwnd, parent_hwnd, x=0, y=0):
        self.docked = True
        return True

    def undock(self, hwnd):
        self.docked = False
        return True

    def __getattr__(self, name):
        return getattr(self._original, name)


class BackgroundApi(main.Api):
    """Existing API with a narrow, reversible background-mode adapter."""

    def __init__(self):
        super().__init__()
        self.docker = BackgroundDocker(self.docker)
        self.game_cutout = True
        vision.force_window_capture()
        self._background_controller = None
        self._background_active_hwnd = None
        self._background_stop = threading.Event()
        threading.Thread(target=self._background_watchdog, daemon=True).start()
        self.push_log("[Background] Experimental Background Mode enabled.")
        self.push_log("[Background] Roblox stays in a separate window. Normal mode is unchanged.")

    def _background_watchdog(self):
        while not self.stopping.is_set() and not self._background_stop.is_set():
            hwnd = self.game_hwnd
            if hwnd and hwnd != self._background_active_hwnd:
                try:
                    controller = BackgroundModeController(hwnd)
                    health = controller.health_check()
                    if not health.capture_ok or not health.capture_nonempty:
                        raise RuntimeError(health.error or "Roblox window capture returned no usable pixels")
                    self._background_controller = controller
                    self.runner._mouse = BackgroundMouse(controller)
                    self.runner._keyboard = BackgroundKeyboard(controller)
                    self._background_active_hwnd = hwnd
                    self.push_log(
                        f"[Background] Ready. Roblox capture: {health.client_size[0]}x{health.client_size[1]}."
                    )
                except Exception as exc:
                    self._background_controller = None
                    self._background_active_hwnd = None
                    self.runner._mouse = self.mouse
                    self.runner._keyboard = self.keyboard
                    self.push_log(f"[Background] Health check failed, using normal input: {exc}")
            elif not hwnd and self._background_active_hwnd:
                self._background_controller = None
                self._background_active_hwnd = None
                self.runner._mouse = self.mouse
                self.runner._keyboard = self.keyboard
                self.push_log("[Background] Roblox window lost. Normal input adapter restored.")
            time.sleep(0.5)

    def close_window(self):
        self._background_stop.set()
        super().close_window()


# _launch_ui creates the API through main.Api. Replacing the class before
# entering the existing launcher keeps every other UI and runner path intact.
main.Api = BackgroundApi

if __name__ == "__main__":
    if "--test" in sys.argv:
        print("Background Mode launcher does not support --test yet.")
        print("Run the normal diagnostics with: python main.py --test")
    else:
        main._launch_ui()
