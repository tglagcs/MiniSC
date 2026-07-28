import ctypes
import tkinter as tk
from typing import Callable, Optional

from .config import DEFAULT_HOTKEYS, get_hotkeys, save_hotkeys
from .hotkeys import VK_CONTROL, VK_MENU, VK_SHIFT, vk_to_char
from .ui_root import open_window
from .ui_style import ACCENT, BG, FG, FG_MUTED, PillLabel, apply_dark_titlebar, center_geometry_auto, flat_button

_WINDOW_TITLE = "Хоткеи — MiniSC"

_ACTION_LABELS = {
    "previous": "⏮ Предыдущий трек",
    "next": "⏭ Следующий трек",
    "play_pause": "⏯ Пауза / продолжить",
    "seek": "🎛 Окно плеера",
    "speed_up": "🐇 Ускорить на 5%",
    "speed_down": "🐢 Замедлить на 5%",
    "speed_reset": "▶ Сбросить скорость до 1.0x",
}

_MOD_KEYSYMS = {
    "Control_L": "ctrl", "Control_R": "ctrl",
    "Shift_L": "shift", "Shift_R": "shift",
    "Alt_L": "alt", "Alt_R": "alt",
}

# Биты event.state на Windows/Tk (замерено эмпирически: Shift=0x1, Control=0x4,
# Alt=0x20000) — снимок модификаторов на момент самого события клавиши.
_SHIFT_MASK = 0x0001
_CONTROL_MASK = 0x0004
_ALT_MASK = 0x20000

_user32 = ctypes.windll.user32

# Живой человек физически не может нажать и отпустить 4-клавишный аккорд
# быстрее этого окна — поэтому не полагаемся на состояние в один момент
# (у 4 пальцев нет гарантии "приземлиться" абсолютно одновременно: Windows
# кладёт keydown-сообщения в очередь в реальном порядке срабатывания, и если
# одна из модификаторов на пару миллисекунд "опаздывает" относительно
# обычной клавиши, она просто не попадёт в event.state этого события) —
# вместо этого копим состояние опросом за короткое окно.
_POLL_INTERVAL_MS = 15
_POLL_COUNT = 6


def _combo_text(combo: dict) -> str:
    parts = []
    if combo.get("ctrl"):
        parts.append("Ctrl")
    if combo.get("shift"):
        parts.append("Shift")
    if combo.get("alt"):
        parts.append("Alt")
    key = combo.get("key")
    if key:
        parts.append(key)
    return " + ".join(parts) if parts else "—"


def show_hotkeys_window(on_close: Optional[Callable[[], None]] = None) -> None:
    """Открывает окно настройки хоткеев.

    `on_close` вызывается, когда окно закрывается (любым способом — кнопкой,
    крестиком, Alt+F4). Пока окно открыто, вызывающая сторона должна отключить
    глобальный хук: иначе он перехватывает те же нажатия раньше, чем они
    доходят до этого окна, — уже занятая комбинация уйдёт в реальное действие
    плеера, а не в поле записи.
    """
    hwnd = ctypes.windll.user32.FindWindowW(None, _WINDOW_TITLE)
    if hwnd:
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        return

    def build(master: tk.Tk) -> None:
        win = tk.Toplevel(master)
        win.title(_WINDOW_TITLE)
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        apply_dark_titlebar(win)

        tk.Label(
            win, text="⌨ Хоткеи", bg=BG, fg=ACCENT, font=("Segoe UI", 12, "bold"), anchor="w"
        ).pack(fill="x", padx=20, pady=(18, 2))
        tk.Label(
            win, text="Работают глобально, даже когда MiniSC не в фокусе.",
            bg=BG, fg=FG_MUTED, anchor="w", font=("Segoe UI", 9),
        ).pack(fill="x", padx=20, pady=(0, 10))

        pending = get_hotkeys()
        recording = {"action": None}
        combo_vars: dict = {}

        rows = tk.Frame(win, bg=BG)
        rows.pack(fill="x", padx=20)

        def stop_recording() -> None:
            recording["action"] = None

        def start_recording(action: str) -> None:
            if recording["action"] and recording["action"] != action:
                combo_vars[recording["action"]].set(_combo_text(pending[recording["action"]]))
            recording["action"] = action
            combo_vars[action].set("Нажмите комбинацию…")

        for action, default in DEFAULT_HOTKEYS.items():
            row = tk.Frame(rows, bg=BG)
            row.pack(fill="x", pady=4)

            tk.Label(
                row, text=_ACTION_LABELS.get(action, action), bg=BG, fg=FG,
                font=("Segoe UI", 10), anchor="w", width=26,
            ).pack(side="left")

            flat_button(row, "Записать", lambda a=action: start_recording(a)).pack(side="right")

            combo_var = tk.StringVar(value=_combo_text(pending[action]))
            combo_vars[action] = combo_var
            PillLabel(row, combo_var).pack(side="left", fill="x", expand=True, padx=12)

        tk.Label(
            win, text="🔊 Громкость всегда на Ctrl+Shift+Alt+колесо мыши — здесь не настраивается.",
            bg=BG, fg=FG_MUTED, anchor="w", font=("Segoe UI", 9),
        ).pack(fill="x", padx=20, pady=(8, 0))

        status_var = tk.StringVar(value="")
        tk.Label(win, textvariable=status_var, bg=BG, fg=FG_MUTED, font=("Segoe UI", 9), anchor="w").pack(
            fill="x", padx=20, pady=(4, 0)
        )

        def on_key_press(event: tk.Event) -> str | None:
            keysym = event.keysym
            if keysym in _MOD_KEYSYMS:
                # Сами по себе Ctrl/Shift/Alt не записываем — ждём обычную клавишу.
                return None

            action = recording["action"]
            if action is None:
                return None

            if keysym == "Escape":
                combo_vars[action].set(_combo_text(pending[action]))
                stop_recording()
                return "break"

            # По vkCode (== event.keycode на Windows), а не по keysym: зажатый
            # Ctrl+Alt на многих раскладках трактуется как AltGr и подменяет
            # keysym на другой символ, а сам хук (hotkeys.py) matчит именно
            # по vkCode — эти два способа должны быть согласованы.
            key = vk_to_char(event.keycode)
            if not key:
                status_var.set("Эта клавиша не поддерживается, нужна буква или цифра.")
                return "break"

            # Стартуем накопление сразу с того, что уже видно в event.state —
            # дальше опрос только добавляет модификаторы, которые "подъехали"
            # чуть позже.
            seen = {
                "ctrl": bool(event.state & _CONTROL_MASK),
                "shift": bool(event.state & _SHIFT_MASK),
                "alt": bool(event.state & _ALT_MASK),
            }

            def poll(remaining: int) -> None:
                seen["ctrl"] = seen["ctrl"] or bool(_user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
                seen["shift"] = seen["shift"] or bool(_user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
                seen["alt"] = seen["alt"] or bool(_user32.GetAsyncKeyState(VK_MENU) & 0x8000)
                if remaining > 0:
                    win.after(_POLL_INTERVAL_MS, poll, remaining - 1)
                else:
                    finalize()

            def finalize() -> None:
                if recording["action"] != action:
                    return  # пока копили, запись успели отменить/переключить на другое действие
                if not (seen["ctrl"] or seen["shift"] or seen["alt"]):
                    status_var.set("Нужен хотя бы один модификатор (Ctrl/Shift/Alt).")
                    return
                combo = {"ctrl": seen["ctrl"], "shift": seen["shift"], "alt": seen["alt"], "key": key}
                pending[action] = combo
                combo_vars[action].set(_combo_text(combo))
                status_var.set("")
                stop_recording()

            poll(_POLL_COUNT)
            return "break"

        win.bind("<KeyPress>", on_key_press)

        def do_reset() -> None:
            for action, default in DEFAULT_HOTKEYS.items():
                pending[action] = dict(default)
                combo_vars[action].set(_combo_text(default))
            stop_recording()
            status_var.set("Сброшено к значениям по умолчанию (не забудьте сохранить).")

        def do_save() -> None:
            stop_recording()
            save_hotkeys(pending)
            status_var.set("Сохранено!")

        def close_window() -> None:
            win.destroy()
            if on_close:
                try:
                    on_close()
                except Exception:
                    pass

        win.protocol("WM_DELETE_WINDOW", close_window)

        btn_frame = tk.Frame(win, bg=BG)
        btn_frame.pack(fill="x", padx=20, pady=(16, 18))
        flat_button(btn_frame, "Сбросить", do_reset).pack(side="left", expand=True, fill="x", padx=(0, 4))
        flat_button(btn_frame, "Сохранить", do_save, accent=True).pack(side="left", expand=True, fill="x", padx=(4, 4))
        flat_button(btn_frame, "Закрыть", close_window).pack(side="left", expand=True, fill="x", padx=(4, 0))

        # Высота — ровно по содержимому (вызов после сборки всех виджетов).
        center_geometry_auto(win, 560, y_ratio=2.0)

        win.focus_force()

    open_window(build)
