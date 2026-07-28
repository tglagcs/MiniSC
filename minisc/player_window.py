"""Единое окно плеера: трек + перемотка + громкость + скорость.

Заменяет три отдельных окна (volume/seek/speed) — открывается из трея
(«🎛 Плеер») или хоткеем (действие "seek", по умолчанию Ctrl+Shift+Alt+X,
работает как переключатель открыть/закрыть).
"""

import ctypes
import tkinter as tk

from . import pcm
from .config import save_speed, save_volume
from .ui_root import open_window
from .ui_style import ACCENT, BG, FG, ModernScale, apply_dark_titlebar, center_geometry_auto, flat_button
from .wave import WavePlayer

_WINDOW_TITLE = "Плеер — MiniSC"
_WIDTH = 380
REFRESH_MS = 500

_MIN_PERCENT = round(pcm.MIN_SPEED * 100)
_MAX_PERCENT = round(pcm.MAX_SPEED * 100)


def _format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"


def toggle_player_window(player: WavePlayer) -> None:
    """Открывает окно плеера, либо закрывает его, если оно уже открыто."""
    hwnd = ctypes.windll.user32.FindWindowW(None, _WINDOW_TITLE)
    if hwnd:
        def close(master: tk.Tk) -> None:
            for child in master.winfo_children():
                if isinstance(child, tk.Toplevel) and child.title() == _WINDOW_TITLE:
                    child.destroy()

        open_window(close)
    else:
        show_player_window(player)


def show_player_window(player: WavePlayer) -> None:
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

        # --- трек + перемотка ---
        title_var = tk.StringVar()
        tk.Label(
            win, textvariable=title_var, bg=BG, fg=ACCENT, font=("Segoe UI", 12, "bold"),
            anchor="w", wraplength=340, justify="left",
        ).pack(fill="x", padx=20, pady=(18, 2))

        time_var = tk.StringVar()
        tk.Label(win, textvariable=time_var, bg=BG, fg=FG, font=("Segoe UI", 10)).pack(anchor="w", padx=20)

        seek_dragging = {"active": False}

        def on_seek_press() -> None:
            seek_dragging["active"] = True

        def on_seek_release() -> None:
            seek_dragging["active"] = False
            player.seek(seek_scale.get())

        seek_scale = ModernScale(win, from_=0, to=1, length=340,
                                 on_press=on_seek_press, on_release=on_seek_release)
        seek_scale.pack(padx=20, pady=(8, 6))

        # --- громкость ---
        volume_var = tk.StringVar()
        tk.Label(win, textvariable=volume_var, bg=BG, fg=FG, font=("Segoe UI", 10)).pack(
            anchor="w", padx=20, pady=(10, 0)
        )

        volume_dragging = {"active": False}

        def on_volume_change(raw: int) -> None:
            value = int(raw) / 100
            player.set_volume(value)
            save_volume(value)
            volume_var.set(f"🔊 Громкость — {int(raw)}%")

        volume_scale = ModernScale(
            win, from_=0, to=100, command=on_volume_change, length=340,
            on_press=lambda: volume_dragging.update(active=True),
            on_release=lambda: volume_dragging.update(active=False),
        )
        volume_scale.set(round(player.get_volume() * 100))
        volume_var.set(f"🔊 Громкость — {round(player.get_volume() * 100)}%")
        volume_scale.pack(padx=20, pady=(8, 6))

        # --- скорость ---
        speed_var = tk.StringVar()
        tk.Label(win, textvariable=speed_var, bg=BG, fg=FG, font=("Segoe UI", 10)).pack(
            anchor="w", padx=20, pady=(10, 0)
        )

        speed_dragging = {"active": False}

        def on_speed_change(raw: int) -> None:
            value = int(raw) / 100
            player.set_speed(value)
            save_speed(value)
            speed_var.set(f"⏩ Скорость — {value:.2f}x")

        speed_scale = ModernScale(
            win, from_=_MIN_PERCENT, to=_MAX_PERCENT, resolution=5,
            command=on_speed_change, length=340,
            on_press=lambda: speed_dragging.update(active=True),
            on_release=lambda: speed_dragging.update(active=False),
        )
        speed_scale.set(round(player.get_speed() * 100))
        speed_var.set(f"⏩ Скорость — {player.get_speed():.2f}x")
        speed_scale.pack(padx=20, pady=(8, 6))

        flat_button(win, "Закрыть", win.destroy).pack(padx=20, pady=(14, 18), fill="x")

        # Высота — ровно по содержимому (вызов после сборки всех виджетов).
        center_geometry_auto(win, _WIDTH, y_ratio=2.0)

        def fit_height() -> None:
            # Название трека может переноситься на 1 или 2 строки — подгоняем
            # высоту окна на лету, не трогая позицию.
            win.update_idletasks()
            req = win.winfo_reqheight()
            if win.winfo_height() != req:
                win.geometry(f"{_WIDTH}x{req}")

        def refresh() -> None:
            if not win.winfo_exists():
                return

            track = player.current_track
            title_var.set(f"🎵 {track.title}" if track else "Ничего не играет")
            fit_height()

            duration = player.get_duration()
            position = player.get_position()
            if int(seek_scale["to"]) != max(1, int(duration)):
                seek_scale.configure(to=max(1, int(duration)))
            if not seek_dragging["active"]:
                seek_scale.set(round(min(position, duration) if duration else position))
            time_var.set(f"{_format_time(position)} / {_format_time(duration)}")

            # Громкость/скорость могут меняться хоткеями, пока окно открыто —
            # подтягиваем ползунки (программный set() команду не дёргает).
            if not volume_dragging["active"]:
                percent = round(player.get_volume() * 100)
                if volume_scale.get() != percent:
                    volume_scale.set(percent)
                    volume_var.set(f"🔊 Громкость — {percent}%")
            if not speed_dragging["active"]:
                speed_percent = round(player.get_speed() * 100)
                if speed_scale.get() != speed_percent:
                    speed_scale.set(speed_percent)
                    speed_var.set(f"⏩ Скорость — {speed_percent / 100:.2f}x")

            win.after(REFRESH_MS, refresh)

        refresh()

    open_window(build)
