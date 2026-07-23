"""Launch the existing macro UI with experimental Roblox Background Mode."""

from __future__ import annotations

import sys
import threading
import time

# The installed mss package exposes mss.mss(). The existing vision module in
# the base project still calls mss.MSS(), so provide a compatibility alias.
import mss
if not hasattr(mss, "MSS"):
    mss.MSS = mss.mss

import main
from core import vision
from core.background import BackgroundModeController, BackgroundMouse, BackgroundKeyboard


class BackgroundDocker:
    """Keep Roblox as a separate window while satisfying the existing watchdog."""

    def __init__(self, original):
        self._original = original
        self.cutout = False
        self.docked = False

    def dock(self, hwnd, parent_hwnd, x=0, y=0):
        # Do not reparent Roblox. Mark it logically attached so the existing
        # watchdog does not repeatedly try to dock the same window.
        self.docked = True
        return True

    def undock(self, hwnd):
        self.docked = False
        return True

    def __getattr__(self, name):
        return getattr(self._original, name)


class BackgroundApi(main.Api):
    def __init__(self):
        super().__init__()
        self.docker = BackgroundDocker(self.docker)
        self.game_cutout = False
        vision._use_window_capture = False
        self._background_controller = None
        self._background_active_hwnd = None
        self._background_stop = threading.Event()
        threading.Thread(target=self._background_watchdog, daemon=True).start()
        self.push_log("[Background] Experimental Background Mode enabled.")
        self.push_log("[Background] Live Roblox-region capture enabled. Keep Roblox visible during this test.")
        self.push_log("[Background] Roblox remains a separate window. Normal mode remains unchanged.")

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
                    self.push_log("[Background] Vision is using the same visible Roblox-region capture path.")
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
