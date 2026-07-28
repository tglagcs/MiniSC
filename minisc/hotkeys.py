"""Глобальные хоткеи Ctrl/Shift/Alt+клавиша для управления плеером.

Как и колесо мыши для громкости (см. `volume_hotkey.py`), работает через
низкоуровневый хук (`WH_KEYBOARD_LL`), а не `RegisterHotKey` — комбинации
настраиваются пользователем в рантайме, а `RegisterHotKey` не даёт слушать
несколько динамически меняющихся сочетаний так же гибко и предсказуемо.
"""

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_KEYUP = 0x0101
WM_SYSKEYUP = 0x0105
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


LOWLEVELKEYBOARDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, LOWLEVELKEYBOARDPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype = ctypes.c_long
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]


def vk_to_char(vk_code: int) -> str:
    """Переводит виртуальный код клавиши в одну букву/цифру (A-Z, 0-9), иначе ''."""
    if 0x41 <= vk_code <= 0x5A or 0x30 <= vk_code <= 0x39:
        return chr(vk_code)
    return ""


def modifiers_held() -> Dict[str, bool]:
    def down(vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    return {"ctrl": down(VK_CONTROL), "shift": down(VK_SHIFT), "alt": down(VK_MENU)}


def combo_key(ctrl: bool, shift: bool, alt: bool, key: str) -> str:
    return f"{int(bool(ctrl))}{int(bool(shift))}{int(bool(alt))}{key.strip().upper()[:1]}"


class KeyboardHotkeys:
    """Слушает нажатия клавиш и вызывает callback при совпадении с зарегистрированной комбинацией."""

    def __init__(self) -> None:
        self._bindings: Dict[str, Callable[[], None]] = {}
        self._pressed: set = set()
        self._lock = threading.Lock()
        self._hook: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._proc = LOWLEVELKEYBOARDPROC(self._hook_proc)

    def set_bindings(self, hotkeys: Dict[str, dict], callbacks: Dict[str, Callable[[], None]]) -> None:
        """hotkeys: {action: {"ctrl":.., "shift":.., "alt":.., "key":..}}, callbacks: {action: func}."""
        bindings = {}
        for name, combo in hotkeys.items():
            cb = callbacks.get(name)
            key = combo.get("key")
            if not cb or not key:
                continue
            bindings[combo_key(combo.get("ctrl"), combo.get("shift"), combo.get("alt"), key)] = cb
        with self._lock:
            self._bindings = bindings

    def _hook_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code == 0:
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = info.vkCode
            if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                is_repeat = vk in self._pressed
                self._pressed.add(vk)
                if not is_repeat:
                    char = vk_to_char(vk)
                    if char:
                        mods = modifiers_held()
                        key = combo_key(mods["ctrl"], mods["shift"], mods["alt"], char)
                        with self._lock:
                            cb = self._bindings.get(key)
                        if cb:
                            try:
                                cb()
                            except Exception:
                                logger.exception("Keyboard hotkey callback failed")
                            return 1
            elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                self._pressed.discard(vk)
        return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)

    def start(self) -> None:
        if self._thread is not None:
            return

        ready = threading.Event()

        def run() -> None:
            # hMod=None: хук в контексте своего же процесса (см. volume_hotkey.py).
            self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
            if not self._hook:
                logger.error("Failed to install low-level keyboard hook")
                ready.set()
                return
            ready.set()

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

        self._thread = threading.Thread(target=run, daemon=True, name="minisc-keyboard-hotkeys")
        self._thread.start()
        ready.wait(timeout=2)

    def stop(self) -> None:
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
