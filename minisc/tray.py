import ctypes
import logging
import threading
from typing import Optional

import pystray
from PIL import Image, ImageDraw

from . import pcm
from .config import get_hotkeys, save_speed, save_volume
from .hotkeys import KeyboardHotkeys
from .hotkeys_window import show_hotkeys_window
from .osd import show_event as show_event_osd
from .osd import show_speed as show_speed_osd
from .osd import show_volume as show_volume_osd
from .player_window import show_player_window, toggle_player_window
from .sc_api import Track
from .volume_hotkey import VolumeHotkey
from .wave import WavePlayer

logger = logging.getLogger(__name__)

APP_NAME = "minisc"
APP_TITLE = "Моя волна SoundCloud"

# Фирменный оранжевый SoundCloud вместо жёлтого Яндекса — единственное, чем
# иконка отличается от иконки MiniYaMu.
ACCENT = (255, 85, 0, 255)
ACCENT_DARK = (20, 20, 22, 255)

VOLUME_HOTKEY_STEP = 0.05
SPEED_HOTKEY_STEP = 0.05


def _enable_dark_menu_theme() -> None:
    """Просит проводник рисовать всплывающие меню процесса в тёмной теме.

    Недокументированные, но давно стабильные ординалы uxtheme.dll (135/136),
    используются так же в Windows Terminal и множестве других инструментов.
    Если Windows не даст их использовать (другой билд/версия) — просто
    промолчим и меню останется системным светлым.
    """
    try:
        uxtheme = ctypes.WinDLL("uxtheme", use_last_error=True)

        set_preferred_app_mode = uxtheme[135]
        set_preferred_app_mode.restype = ctypes.c_int
        set_preferred_app_mode.argtypes = [ctypes.c_int]
        set_preferred_app_mode(1)  # 1 = AllowDark

        flush_menu_themes = uxtheme[136]
        flush_menu_themes.argtypes = []
        flush_menu_themes()
    except Exception:
        logger.exception("Failed to enable dark tray menu theme")


def _make_icon_image() -> Image.Image:
    """Оранжевый круг с облаком — вместо треугольника «play» у MiniYaMu.

    Облако собирается из трёх кругов и прямоугольного основания: круги
    перекрываются, поэтому силуэт выходит слитным, а сглаживание даёт
    четырёхкратный суперсэмплинг (рисуем крупно, ужимаем LANCZOS-ом) —
    тот же приём, что во всех виджетах `ui_style.py`.
    """
    scale = 4
    size = 64 * scale
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 2 * scale
    draw.ellipse((pad, pad, size - pad, size - pad), fill=ACCENT)

    # Размеры подобраны по превью в четырёх масштабах: облако должно оставаться
    # облаком и в 16px трея, поэтому оно заметно уже круга — иначе на мелком
    # размере силуэт упирается в края и читается сплошным пятном.
    base_y = size * 0.62          # линия, по которой облако «стоит»
    left, right = size * 0.28, size * 0.74

    for cx, cy, r in (
        (size * 0.41, size * 0.48, size * 0.13),   # левый купол
        (size * 0.55, size * 0.43, size * 0.16),   # главный купол, чуть выше
        (size * 0.68, size * 0.52, size * 0.11),   # маленький правый
    ):
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ACCENT_DARK)

    # Основание: скругление только снизу, сверху его прячут купола.
    draw.rounded_rectangle(
        (left, size * 0.48, right, base_y), radius=size * 0.07, fill=ACCENT_DARK
    )

    return img.resize((64, 64), Image.LANCZOS)


def _track_label(track: Optional[Track]) -> str:
    if track is None:
        return "Ничего не играет"
    return track.label()


class TrayApp:
    def __init__(self, player: WavePlayer):
        _enable_dark_menu_theme()

        self.player = player
        self.player.on_track_change = self._on_track_change
        self._current_label = "Запуск..."

        self.icon = pystray.Icon(
            APP_NAME,
            _make_icon_image(),
            APP_TITLE,
            menu=self._build_menu(),
        )
        self._volume_hotkey = VolumeHotkey(self._on_volume_scroll)
        self._keyboard_hotkeys = KeyboardHotkeys()
        self._reload_keyboard_hotkeys()

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(lambda item: f"🎵 {self._current_label}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._play_pause_label, self._toggle_play_pause),
            pystray.MenuItem("⏮ Предыдущий трек", self._previous),
            pystray.MenuItem("⏭ Следующий трек", self._next),
            pystray.MenuItem("🎛 Плеер", self._show_player),
            pystray.MenuItem("⌨ Хоткеи", self._show_hotkeys),
            pystray.MenuItem(self._like_label, self._like),
            pystray.MenuItem(self._dislike_label, self._dislike),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("🔙 Выход", self._quit),
        )

    def _play_pause_label(self, item) -> str:
        return "⏸ Пауза" if self.player.state == "playing" else "▶ Продолжить"

    def _show_player(self, icon, item) -> None:
        show_player_window(self.player)

    def _on_volume_scroll(self, notches: int) -> None:
        value = self.player.get_volume() + notches * VOLUME_HOTKEY_STEP
        value = max(0.0, min(1.0, value))
        self.player.set_volume(value)
        save_volume(value)
        show_volume_osd(round(value * 100))

    def _do_speed_up(self) -> None:
        self._change_speed(SPEED_HOTKEY_STEP)

    def _do_speed_down(self) -> None:
        self._change_speed(-SPEED_HOTKEY_STEP)

    def _do_speed_reset(self) -> None:
        self._set_speed(1.0)

    def _change_speed(self, delta: float) -> None:
        self._set_speed(self.player.get_speed() + delta)

    def _set_speed(self, value: float) -> None:
        value = max(pcm.MIN_SPEED, min(pcm.MAX_SPEED, value))
        self.player.set_speed(value)
        save_speed(value)
        show_speed_osd(value)

    def _show_hotkeys(self, icon, item) -> None:
        # Пока окно записи открыто, глобальный хук должен молчать — иначе он
        # перехватывает нажатия раньше, чем они доходят до окна (уже занятая
        # комбинация уйдёт в реальное действие плеера, а не в поле записи).
        self._keyboard_hotkeys.set_bindings({}, {})
        show_hotkeys_window(on_close=self._reload_keyboard_hotkeys)

    def _reload_keyboard_hotkeys(self) -> None:
        hotkeys = get_hotkeys()
        callbacks = {
            "previous": self._do_previous,
            "next": self._do_next,
            "play_pause": self._do_toggle_play_pause,
            "seek": self._do_toggle_seek,
            "speed_up": self._do_speed_up,
            "speed_down": self._do_speed_down,
            "speed_reset": self._do_speed_reset,
        }
        self._keyboard_hotkeys.set_bindings(hotkeys, callbacks)

    def _on_track_change(self, track: Optional[Track]) -> None:
        self._current_label = _track_label(track)
        try:
            self.icon.title = f"{APP_TITLE}: {self._current_label}"[:127]
            if track is not None:
                self.icon.notify(track.artist or APP_TITLE, track.title or "")
            self.icon.update_menu()
        except Exception:
            logger.exception("Failed to refresh tray icon state")

    def _toggle_play_pause(self, icon, item) -> None:
        self._do_toggle_play_pause()

    def _next(self, icon, item) -> None:
        self._do_next()

    def _previous(self, icon, item) -> None:
        self._do_previous()

    def _like_label(self, item) -> str:
        return "💔 Убрать лайк" if self.player.is_current_liked() else "❤ Нравится"

    def _like(self, icon, item) -> None:
        # toggle_like мгновенный (сеть — в фоне), так что OSD показываем сразу.
        liked = self.player.toggle_like()
        if liked is None:
            return
        show_event_osd("❤ Нравится" if liked else "💔 Лайк снят")
        try:
            icon.update_menu()
        except Exception:
            logger.exception("Failed to refresh tray menu after like toggle")

    def _dislike_label(self, item) -> str:
        return "↩ Вернуть в волну" if self.player.is_current_disliked() else "\U0001F44E Не нравится"

    def _dislike(self, icon, item) -> None:
        # toggle_dislike мгновенный (запись в конфиг и скип — в фоне).
        disliked = self.player.toggle_dislike()
        if disliked is None:
            return
        show_event_osd("👎 Больше не играть" if disliked else "↩ Возвращён в волну")
        try:
            icon.update_menu()
        except Exception:
            logger.exception("Failed to refresh tray menu after dislike toggle")

    def _do_toggle_play_pause(self) -> None:
        self.player.toggle_play_pause()
        try:
            self.icon.update_menu()
        except Exception:
            logger.exception("Failed to refresh tray menu after play/pause hotkey")

    def _do_next(self) -> None:
        # Загрузка следующего трека занимает пару секунд — показываем OSD сразу,
        # чтобы было видно, что нажатие сработало и жать повторно не нужно.
        show_event_osd("⏭ Следующий трек…")
        threading.Thread(target=self.player.next, daemon=True).start()

    def _do_previous(self) -> None:
        show_event_osd("⏮ Предыдущий трек…")

        def run() -> None:
            if not self.player.previous():
                show_event_osd("⏮ Предыдущего трека нет")

        threading.Thread(target=run, daemon=True).start()

    def _do_toggle_seek(self) -> None:
        toggle_player_window(self.player)

    def _quit(self, icon, item) -> None:
        self._volume_hotkey.stop()
        self._keyboard_hotkeys.stop()
        self.player.stop()
        icon.stop()

    def run(self) -> None:
        self._volume_hotkey.start()
        self._keyboard_hotkeys.start()
        threading.Thread(target=self.player.start, daemon=True).start()
        self.icon.run()
