import ctypes
import logging
import sys
import tkinter as tk
import webbrowser

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from minisc.browser_token import find_firefox_oauth_token
from minisc.config import get_token, get_volume, save_token
from minisc.sc_api import AuthError, SoundCloudClient, SoundCloudError
from minisc.tray import TrayApp
from minisc.ui_style import (
    ACCENT,
    BG,
    BG_CARD,
    BORDER,
    FG,
    FG_MUTED,
    apply_dark_titlebar,
    center_geometry,
    flat_button,
)
from minisc.wave import WavePlayer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

TOKEN_URL = "https://soundcloud.com/signin"

INSTRUCTIONS = (
    "Токен из Firefox подставляется сам, если вы вошли в SoundCloud в нём.\n"
    "Если поле пустое — вставьте токен вручную:\n"
    "1. Войдите в аккаунт на soundcloud.com\n"
    "2. F12 → вкладка Application (Storage)\n"
    "3. Cookies → https://soundcloud.com → строка oauth_token\n"
    "4. Скопируйте её значение (вида 2-2-XXXXXX-…) сюда"
)


def _fatal(message: str) -> None:
    logger.error(message)
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "MiniSC", 0x10)
    except Exception:
        pass
    sys.exit(1)


def _prompt_token(error: str = "") -> str:
    """Окно ввода oauth_token. Блокирует запуск: без токена играть нечего.

    Почему руками, а не OAuth-редиректом, как у Яндекса: регистрация новых
    приложений у SoundCloud закрыта (одно приложение на человека, заявки
    рассматриваются вручную), а официальный API всё равно отдаёт только
    30-секундные сниппеты. Поэтому клиент ходит тем же путём, что и веб-плеер,
    и авторизуется той же сессионной кукой.
    """
    result = {"token": ""}

    root = tk.Tk()
    root.title("Авторизация в SoundCloud")
    root.configure(bg=BG)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    center_geometry(root, 460, 400)
    apply_dark_titlebar(root)

    tk.Label(
        root, text="☁ MiniSC", bg=BG, fg=ACCENT, font=("Segoe UI", 13, "bold"), anchor="w"
    ).pack(fill="x", padx=20, pady=(20, 4))

    tk.Label(
        root,
        text="Нужен oauth_token вашей сессии SoundCloud:",
        bg=BG, fg=FG_MUTED, anchor="w", wraplength=420, justify="left",
    ).pack(fill="x", padx=20, pady=(8, 0))

    tk.Label(
        root, text=INSTRUCTIONS, bg=BG, fg=FG, anchor="w", justify="left", wraplength=420,
    ).pack(fill="x", padx=20, pady=(8, 0))

    card = tk.Frame(root, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1)
    card.pack(fill="x", padx=20, pady=(16, 0))

    tk.Label(card, text="OAUTH_TOKEN", bg=BG_CARD, fg=FG_MUTED, font=("Segoe UI", 8), anchor="w").pack(
        fill="x", padx=14, pady=(10, 0)
    )

    entry = tk.Entry(
        card,
        font=("Consolas", 11),
        bg=BG_CARD,
        fg=ACCENT,
        insertbackground=ACCENT,
        relief="flat",
        highlightthickness=0,
    )
    entry.pack(fill="x", padx=14, pady=(0, 14))
    entry.focus_set()

    # Автоподстановка из Firefox: если сессия есть — токен окажется в поле сразу.
    # Не молча — само окно уже показано и в инструкции сверху сказано, откуда он
    # берётся, так что отдельная подпись про это не нужна.
    if not error:
        prefilled = find_firefox_oauth_token()
        if prefilled:
            entry.insert(0, prefilled)

    status_var = tk.StringVar(value=error or "Токен хранится локально в config.json.")
    tk.Label(
        root, textvariable=status_var, bg=BG, fg=FG_MUTED, anchor="w", wraplength=420, justify="left"
    ).pack(fill="x", padx=20, pady=(10, 0))

    def submit() -> None:
        token = entry.get().strip()
        if not token:
            status_var.set("Поле пустое — вставьте значение куки oauth_token.")
            return
        result["token"] = token
        root.destroy()

    def open_browser() -> None:
        try:
            webbrowser.open(TOKEN_URL)
        except Exception:
            logger.exception("Failed to open browser for SoundCloud login")

    entry.bind("<Return>", lambda _e: submit())

    btn_frame = tk.Frame(root, bg=BG)
    btn_frame.pack(fill="x", padx=20, pady=(16, 20))

    flat_button(btn_frame, "Открыть SoundCloud", open_browser).pack(
        side="left", expand=True, fill="x", padx=(0, 4)
    )
    flat_button(btn_frame, "Сохранить", submit, accent=True).pack(
        side="left", expand=True, fill="x", padx=(4, 4)
    )
    flat_button(btn_frame, "Отмена", root.destroy).pack(
        side="left", expand=True, fill="x", padx=(4, 0)
    )

    root.mainloop()

    if not result["token"]:
        _fatal("Без oauth_token MiniSC работать не может.")
    save_token(result["token"])
    logger.info("Токен сохранён")
    return result["token"]


def _connect() -> SoundCloudClient:
    """Возвращает клиента с заведомо рабочим токеном.

    Токен проверяется сразу запросом профиля: узнать о протухшей сессии на
    старте лучше, чем через минуту молчаливо пустой волной.
    """
    try:
        token = get_token()
    except RuntimeError:
        # Токена в конфиге нет — показываем окно. Если найдётся сессия в
        # Firefox, оно откроется с уже подставленным токеном (пользователь
        # видит это и подтверждает кнопкой), а не заберёт куки молча за спиной.
        token = _prompt_token()

    error = ""
    for _ in range(3):
        client = SoundCloudClient(token)
        try:
            client.me()
            logger.info("Авторизован как %s", client.username)
            return client
        except AuthError as exc:
            error = f"Токен не подошёл: {exc}"
            logger.warning(error)
        except SoundCloudError as exc:
            _fatal(f"SoundCloud недоступен: {exc}")
        token = _prompt_token(error)

    _fatal("Не удалось авторизоваться в SoundCloud.")
    raise SystemExit(1)


def main() -> None:
    client = _connect()
    player = WavePlayer(client)
    player.set_volume(get_volume())
    app = TrayApp(player)
    app.run()


if __name__ == "__main__":
    main()
