"""Launch the existing macro UI with experimental Roblox Background Mode.

Normal `python main.py` remains unchanged. This launcher reuses the existing
UI, task system, runner, vision code, and recovery logic. Background Mode keeps
Roblox as a separate visible window and uses the same live screen capture path
as the vision system. It does not force PrintWindow capture, because Roblox's
hardware-rendered surface can return stale content through that API.
"""

from __future__ import annotations

import sys
import threading
import time

import main
from core import vision
from core.background import BackgroundModeController, BackgroundMouse, BackgroundKeyboard


class BackgroundDocker:
    """Compatibility layer that prevents Roblox from being reparented."""

    def __init__(self, original):
        self._original = original
        self.cutout = False
        self.docked = False

    def dock(self, hwnd, parent_hwnd, x=0, y=0):
        # Background Mode must not reparent or cut out the Roblox window.
        self.docked = False
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
        self.game_cutout = False
        # Critical: do not force PrintWindow. The vision pipeline must use the
        # same live visible-region capture path as BackgroundModeController.
        # PrintWindow was the source of stale or mismatched Roblox frames.
        vision._use_window_capture = False
        self._background_controller = None
        self._background_active_hwnd = None
        self._background_stop = threading.Event()
        threading.Thread(target=self._background_watchdog, daemon=True).start()
        self.push_log("[Background] Experimental Background Mode enabled.")
        self.push_log("[Background] Live Roblox-region capture enabled. Keep Roblox visible during this test.")
        self.push_log("[Background] Roblox will remain a separate window. Normal mode remains unchanged.")

    def _background_watchdog(self):
        while not self.stopping.is_set() and not self._background_stop.is_set():
            hwnd = self.game_hwnd
            if hwnd and hwnd != self._background_active_hwnd:
                try:
                    controller = BackgroundModeController(hwnd)
                    health = controller.health_check()
                    if not health.capture_ok or not health.capture_nonempty:
                        raise RuntimeError(health.error or "Roblox live capture returned no usable pixels")
                    self._background_controller = controller
                    self.runner._mouse = BackgroundMouse(controller)
                    self.runner._keyboard = BackgroundKeyboard(controller)
                    self._background_active_hwnd = hwnd
                    self.push_log(
                        f"[Background] Live capture ready: {health.client_size[0]}x{health.client_size[1]} "
                        f"via {health.capture_source}."
                    )
                    self.push_log(
                        "[Background] Vision is using the same visible Roblox-region capture path."
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


main.Api = BackgroundApi

if __name__ == "__main__":
    if "--test" in sys.argv:
        print("Background Mode launcher does not support --test yet.")
        print("Run the normal diagnostics with: python main.py --test")
    else:
        main._launch_ui()
