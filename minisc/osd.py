"""Мини-оверлей внизу экрана (в духе floating-индикатора Wispr Flow).

Общий для громкости и скорости — обновляется вживую на каждый щелчок
хоткея, без спама нативными тостами. Сам прячется через паузу после
последнего изменения.
"""

import tkinter as tk

from PIL import Image, ImageColor, ImageDraw, ImageTk

from . import pcm
from .ui_root import open_window
from .ui_style import ACCENT, BG_CARD, FG

_WIDTH = 220
_HEIGHT = 64
_BOTTOM_MARGIN = 90
_RADIUS = 18
_BAR_PAD = 20
_BAR_HEIGHT = 4

_HIDE_DELAY_MS = 1100
_FADE_STEPS = 10
_FADE_INTERVAL_MS = 22
_MAX_ALPHA = 0.95

_TRANSPARENT_KEY = "#0a0a0a"  # ключ-цвет окна; не должен совпадать с BG_CARD/ACCENT

_ov: dict = {}


def _make_background() -> ImageTk.PhotoImage:
    key_rgb = ImageColor.getrgb(_TRANSPARENT_KEY)
    fill_rgb = ImageColor.getrgb(BG_CARD)
    img = Image.new("RGB", (_WIDTH, _HEIGHT), key_rgb)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, _WIDTH - 1, _HEIGHT - 1], radius=_RADIUS, fill=fill_rgb)
    return ImageTk.PhotoImage(img)


def _set_alpha(win: tk.Tk, value: float) -> None:
    try:
        win.attributes("-alpha", value)
    except tk.TclError:
        pass


def _build(master: tk.Tk) -> None:
    win = tk.Toplevel(master)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg=_TRANSPARENT_KEY)
    try:
        win.attributes("-transparentcolor", _TRANSPARENT_KEY)
    except tk.TclError:
        pass

    x = (win.winfo_screenwidth() - _WIDTH) // 2
    y = win.winfo_screenheight() - _HEIGHT - _BOTTOM_MARGIN
    win.geometry(f"{_WIDTH}x{_HEIGHT}+{x}+{y}")

    bg_photo = _make_background()
    bg_label = tk.Label(win, image=bg_photo, bg=_TRANSPARENT_KEY, bd=0, highlightthickness=0)
    bg_label.image = bg_photo
    bg_label.place(x=0, y=0, width=_WIDTH, height=_HEIGHT)

    text_var = tk.StringVar()
    text_label = tk.Label(
        win, textvariable=text_var, bg=BG_CARD, fg=FG, font=("Segoe UI", 11, "bold"), bd=0
    )
    text_label.place(x=_BAR_PAD, y=14, width=_WIDTH - 2 * _BAR_PAD, height=22)

    track = tk.Frame(win, bg="#3a3a3a", bd=0, highlightthickness=0)
    track.place(x=_BAR_PAD, y=_HEIGHT - 18, width=_WIDTH - 2 * _BAR_PAD, height=_BAR_HEIGHT)

    bar = tk.Frame(win, bg=ACCENT, bd=0, highlightthickness=0)
    bar.place(x=_BAR_PAD, y=_HEIGHT - 18, width=0, height=_BAR_HEIGHT)

    win.withdraw()
    _set_alpha(win, 0.0)

    _ov.update(win=win, text_var=text_var, text_label=text_label, track=track, bar=bar,
               hide_job=None, fade_job=None)


def _cancel_jobs() -> None:
    win = _ov["win"]
    if _ov.get("hide_job"):
        win.after_cancel(_ov["hide_job"])
        _ov["hide_job"] = None
    if _ov.get("fade_job"):
        win.after_cancel(_ov["fade_job"])
        _ov["fade_job"] = None


def _fade_out(step: int) -> None:
    win = _ov.get("win")
    if not win or not win.winfo_exists():
        return
    if step <= 0:
        win.withdraw()
        return
    _set_alpha(win, _MAX_ALPHA * step / _FADE_STEPS)
    _ov["fade_job"] = win.after(_FADE_INTERVAL_MS, _fade_out, step - 1)


def _do_show(master: tk.Tk, text: str, fraction: 'float | None') -> None:
    if not _ov or not _ov["win"].winfo_exists():
        _build(master)

    win = _ov["win"]
    _ov["text_var"].set(text)

    if fraction is None:
        # Событие без шкалы (лайк, скип) — прячем полоску, текст по центру.
        _ov["track"].place_forget()
        _ov["bar"].place_forget()
        _ov["text_label"].place_configure(y=(_HEIGHT - 22) // 2)
    else:
        fraction = max(0.0, min(1.0, fraction))
        _ov["text_label"].place_configure(y=14)
        _ov["track"].place(x=_BAR_PAD, y=_HEIGHT - 18, width=_WIDTH - 2 * _BAR_PAD, height=_BAR_HEIGHT)
        _ov["bar"].place(x=_BAR_PAD, y=_HEIGHT - 18,
                         width=int((_WIDTH - 2 * _BAR_PAD) * fraction), height=_BAR_HEIGHT)

    _cancel_jobs()
    win.deiconify()
    win.lift()
    _set_alpha(win, _MAX_ALPHA)

    _ov["hide_job"] = win.after(_HIDE_DELAY_MS, lambda: _fade_out(_FADE_STEPS))


def show(text: str, fraction: 'float | None') -> None:
    """Показывает/обновляет мини-оверлей; сам скрывается через паузу."""
    open_window(lambda master: _do_show(master, text, fraction))


def show_event(text: str) -> None:
    """Мини-оверлей без шкалы — мгновенный отклик на действие (лайк, скип)."""
    show(text, None)


def show_volume(percent: int) -> None:
    percent = max(0, min(100, percent))
    icon = "🔇" if percent == 0 else "🔉" if percent < 50 else "🔊"
    show(f"{icon} {percent}%", percent / 100)


def show_speed(speed: float) -> None:
    speed = max(pcm.MIN_SPEED, min(pcm.MAX_SPEED, speed))
    icon = "🐢" if speed < 1.0 else "🐇" if speed > 1.0 else "▶"
    fraction = (speed - pcm.MIN_SPEED) / (pcm.MAX_SPEED - pcm.MIN_SPEED)
    show(f"{icon} {speed:.2f}x", fraction)
