"""Глобальный хоткей Ctrl+Shift+Alt+колесо мыши для громкости.

У кастомных иконок в системном трее Windows нет события прокрутки колеса при
наведении курсора — Shell_NotifyIcon его в принципе не доставляет (проверено
эмпирически, включая NOTIFYICON_VERSION_4). Поэтому громкость крутится через
глобальный низкоуровневый хук мыши (WH_MOUSE_LL), который реагирует только пока
одновременно зажаты Ctrl+Shift+Alt — редкая комбинация, которая не должна
конфликтовать с чужими Ctrl+колесо (зум и т.п.) в других окнах.
"""

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable, Optional

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32

WH_MOUSE_LL = 14
WM_MOUSEWHEEL = 0x020A
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12
WHEEL_DELTA = 120


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


LOWLEVELMOUSEPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, LOWLEVELMOUSEPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype = ctypes.c_long
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]


def _modifiers_held() -> bool:
    def down(vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    return down(VK_CONTROL) and down(VK_SHIFT) and down(VK_MENU)


class VolumeHotkey:
    """Слушает Ctrl+Shift+Alt+колесо мыши в отдельном потоке и сообщает о щелчках."""

    def __init__(self, on_scroll: Callable[[int], None]):
        self._on_scroll = on_scroll
        self._hook: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._proc = LOWLEVELMOUSEPROC(self._hook_proc)

    def _hook_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code == 0 and w_param == WM_MOUSEWHEEL and _modifiers_held():
            info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            delta = ctypes.c_short(info.mouseData >> 16).value
            notches = delta // WHEEL_DELTA
            if notches:
                try:
                    self._on_scroll(notches)
                except Exception:
                    logger.exception("Volume hotkey callback failed")
                return 1  # проглатываем событие, чтобы не долетало до окна под курсором
        return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)

    def start(self) -> None:
        if self._thread is not None:
            return

        ready = threading.Event()

        def run() -> None:
            # hMod=None: хук в контексте своего же процесса, отдельного модуля не нужно
            # (передача хендла EXE здесь приводит к ERROR_MOD_NOT_FOUND).
            self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, None, 0)
            if not self._hook:
                logger.error("Failed to install low-level mouse hook for volume hotkey")
                ready.set()
                return
            ready.set()

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

        self._thread = threading.Thread(target=run, daemon=True, name="minisc-volume-hotkey")
        self._thread.start()
        ready.wait(timeout=2)

    def stop(self) -> None:
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
