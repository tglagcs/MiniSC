import ctypes
import logging
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable, Optional

from PIL import Image, ImageColor, ImageDraw, ImageTk

logger = logging.getLogger(__name__)

BG = "#1e1e1e"
BG_CARD = "#2a2a2a"
FG = "#f2f2f2"
FG_MUTED = "#9aa0a6"
ACCENT = "#ff5500"
ACCENT_FG = "#1e1e1e"
ACCENT_HOVER = "#ff7733"
BTN_BG = "#2f2f2f"
BTN_BG_HOVER = "#3d3d3d"
BORDER = "#3a3a3a"

# Tk-канвас не умеет антиалиасинг — скруглённые формы рендерятся через PIL
# с суперсэмплингом (рисуем в _SS раз крупнее и уменьшаем LANCZOS-ом),
# иначе углы выходят "лесенкой". Тот же приём, что в osd.py.
_SS = 4


def _rounded_image(width: int, height: int, radius: int, fill: str, bg: str) -> Image.Image:
    img = Image.new("RGB", (width * _SS, height * _SS), ImageColor.getrgb(bg))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [0, 0, width * _SS - 1, height * _SS - 1],
        radius=radius * _SS,
        fill=ImageColor.getrgb(fill),
    )
    return img.resize((width, height), Image.LANCZOS)


def apply_dark_titlebar(root: tk.Tk) -> None:
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        value = ctypes.c_int(1)
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:
        logger.exception("Failed to enable dark title bar")


class RoundButton(tk.Canvas):
    """Кнопка со скруглёнными углами (PIL-рендер фона + текст поверх)."""

    def __init__(
        self,
        parent: tk.Widget,
        text: str,
        command: Callable[[], None],
        accent: bool = False,
        radius: int = 9,
        padx: int = 16,
        pady: int = 8,
        font=("Segoe UI", 10),
    ) -> None:
        self._font = tkfont.Font(font=font)
        width = self._font.measure(text) + 2 * padx
        height = self._font.metrics("linespace") + 2 * pady
        super().__init__(
            parent, width=width, height=height,
            bg=parent.cget("bg"), bd=0, highlightthickness=0, cursor="hand2",
        )
        self._text = text
        self._command = command
        self._radius = radius
        self._fill = ACCENT if accent else BTN_BG
        self._fill_hover = ACCENT_HOVER if accent else BTN_BG_HOVER
        self._fg = ACCENT_FG if accent else FG
        self._hovering = False
        self._photo: Optional[ImageTk.PhotoImage] = None

        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", lambda _e: self._command())

    def _set_hover(self, hovering: bool) -> None:
        self._hovering = hovering
        self._redraw()

    def _redraw(self) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        fill = self._fill_hover if self._hovering else self._fill
        img = _rounded_image(w, h, self._radius, fill, bg=self.cget("bg"))
        self._photo = ImageTk.PhotoImage(img)
        self.delete("all")
        self.create_image(0, 0, image=self._photo, anchor="nw")
        self.create_text(w // 2, h // 2, text=self._text, fill=self._fg, font=self._font)


def flat_button(parent: tk.Widget, text: str, command, accent: bool = False) -> RoundButton:
    return RoundButton(parent, text, command, accent=accent)


class ModernScale(tk.Canvas):
    """Слайдер в современном стиле: тонкий скруглённый трек, залитая часть
    акцентным цветом, круглая ручка (как системный слайдер Windows 11).

    Частичная замена tk.Scale: set/get/configure(to=...)/["to"] совместимы,
    `command` получает целое значение на каждое изменение ПОЛЬЗОВАТЕЛЕМ
    (программный set() команду не дёргает — окно перемотки обновляет позицию
    таймером, и эхо от собственных обновлений там не нужно). Для перемотки
    есть on_press/on_release вместо внешних bind по ButtonPress/Release —
    внешний bind() затёр бы внутренние обработчики драга.
    """

    _KNOB_R = 8
    _TRACK_H = 5

    def __init__(
        self,
        parent: tk.Widget,
        from_: int = 0,
        to: int = 100,
        command: Optional[Callable[[int], None]] = None,
        length: int = 260,
        resolution: int = 1,
        on_press: Optional[Callable[[], None]] = None,
        on_release: Optional[Callable[[], None]] = None,
    ) -> None:
        height = 2 * self._KNOB_R + 6
        super().__init__(
            parent, width=length, height=height,
            bg=parent.cget("bg"), bd=0, highlightthickness=0, cursor="hand2",
        )
        self._from = from_
        self._to = max(to, from_ + 1)
        self._command = command
        self._resolution = max(1, resolution)
        self._on_press = on_press
        self._on_release = on_release
        self._value = from_
        self._hovering = False
        self._photo: Optional[ImageTk.PhotoImage] = None

        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)

    # --- совместимость с использованием tk.Scale в окнах ---

    def configure(self, cnf=None, **kw):
        if "to" in kw:
            self._to = max(int(kw.pop("to")), self._from + 1)
            self._redraw()
        if cnf or kw:
            return super().configure(cnf, **kw)

    config = configure

    def __getitem__(self, key):
        if key == "to":
            return self._to
        return super().__getitem__(key)

    def set(self, value) -> None:
        self._value = self._quantize(value)
        self._redraw()

    def get(self) -> int:
        return self._value

    # --- внутреннее ---

    def _quantize(self, value) -> int:
        value = max(self._from, min(self._to, value))
        return self._from + round((value - self._from) / self._resolution) * self._resolution

    def _x_to_value(self, x: int) -> int:
        usable = max(1, self.winfo_width() - 2 * self._KNOB_R)
        fraction = (x - self._KNOB_R) / usable
        return self._quantize(self._from + fraction * (self._to - self._from))

    def _apply_user_value(self, x: int) -> None:
        value = self._x_to_value(x)
        if value != self._value:
            self._value = value
            self._redraw()
            if self._command:
                self._command(value)

    def _press(self, event: tk.Event) -> None:
        if self._on_press:
            self._on_press()
        self._apply_user_value(event.x)

    def _drag(self, event: tk.Event) -> None:
        self._apply_user_value(event.x)

    def _release(self, _event: tk.Event) -> None:
        if self._on_release:
            self._on_release()

    def _set_hover(self, hovering: bool) -> None:
        self._hovering = hovering
        self._redraw()

    def _redraw(self) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return

        img = Image.new("RGB", (w * _SS, h * _SS), ImageColor.getrgb(self.cget("bg")))
        draw = ImageDraw.Draw(img)

        knob_r = self._KNOB_R * _SS
        track_h = self._TRACK_H * _SS
        cy = h * _SS // 2
        x0, x1 = knob_r, w * _SS - knob_r
        fraction = (self._value - self._from) / (self._to - self._from)
        knob_x = x0 + int((x1 - x0) * fraction)

        draw.rounded_rectangle([x0, cy - track_h // 2, x1, cy + track_h // 2],
                               radius=track_h // 2, fill=ImageColor.getrgb(BORDER))
        if knob_x > x0:
            draw.rounded_rectangle([x0, cy - track_h // 2, knob_x, cy + track_h // 2],
                                   radius=track_h // 2, fill=ImageColor.getrgb(ACCENT))
        knob_fill = ACCENT_HOVER if self._hovering else ACCENT
        draw.ellipse([knob_x - knob_r, cy - knob_r, knob_x + knob_r, cy + knob_r],
                     fill=ImageColor.getrgb(knob_fill))

        photo = ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))
        self._photo = photo
        self.delete("all")
        self.create_image(0, 0, image=photo, anchor="nw")


class PillLabel(tk.Canvas):
    """Надпись на скруглённой "пилюле" (для отображения комбинаций хоткеев)."""

    def __init__(
        self,
        parent: tk.Widget,
        textvariable: tk.StringVar,
        fg: str = ACCENT,
        fill: str = BG_CARD,
        font=("Consolas", 9),
        height: int = 28,
        radius: int = 9,
    ) -> None:
        super().__init__(
            parent, height=height,
            bg=parent.cget("bg"), bd=0, highlightthickness=0,
        )
        self._var = textvariable
        self._fg = fg
        self._fill = fill
        self._font = tkfont.Font(font=font)
        self._radius = radius
        self._photo: Optional[ImageTk.PhotoImage] = None

        self._trace = textvariable.trace_add("write", lambda *_: self._redraw())
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Destroy>", self._on_destroy)

    def _on_destroy(self, _event) -> None:
        try:
            self._var.trace_remove("write", self._trace)
        except Exception:
            pass

    def _redraw(self) -> None:
        if not self.winfo_exists():
            return
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        img = _rounded_image(w, h, self._radius, self._fill, bg=self.cget("bg"))
        self._photo = ImageTk.PhotoImage(img)
        self.delete("all")
        self.create_image(0, 0, image=self._photo, anchor="nw")
        self.create_text(w // 2, h // 2, text=self._var.get(), fill=self._fg, font=self._font)


def center_geometry(root: tk.Tk, width: int, height: int, y_ratio: float = 3.0) -> None:
    x = (root.winfo_screenwidth() - width) // 2
    # y_ratio=2.0 — точный центр; больше — выше центра. Деление именно float:
    # int(y_ratio) на дробных значениях (1.9 → 1) отправлял окно за низ экрана.
    y = max(0, int((root.winfo_screenheight() - height) / y_ratio))
    root.geometry(f"{width}x{height}+{x}+{y}")


def center_geometry_auto(root: tk.Widget, width: int, y_ratio: float = 2.0) -> None:
    """Центрирует окно, подгоняя высоту ровно под содержимое.

    Вызывать ПОСЛЕ создания всех виджетов (высота берётся из winfo_reqheight) —
    так не приходится вручную подбирать высоту и не остаётся пустоты снизу.
    """
    root.update_idletasks()
    height = root.winfo_reqheight()
    x = (root.winfo_screenwidth() - width) // 2
    y = max(0, int((root.winfo_screenheight() - height) / y_ratio))
    root.geometry(f"{width}x{height}+{x}+{y}")
