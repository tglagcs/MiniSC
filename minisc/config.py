import json
import os
import sys
from pathlib import Path


def _data_dir() -> Path:
    """Где хранить config.json/likes_cache.json.

    Из исходников — корень проекта (удобно для разработки). Из собранного
    exe (PyInstaller ставит sys.frozen) — %APPDATA%/MiniSC: рядом с exe
    писать нельзя (он может лежать в Program Files или в write-protected
    папке), а Path(__file__) в onefile-сборке вообще указывает во временную
    распаковку, которая исчезает после выхода.
    """
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA") or Path.home()) / "MiniSC"
        base.mkdir(parents=True, exist_ok=True)
        return base
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _data_dir() / "config.json"
# Кэш лайкнутого — отдельно от config.json: он объёмнее (сотни id),
# перезаписывается целиком и его потеря безобидна (просто перекачаем список).
LIKES_CACHE_PATH = _data_dir() / "likes_cache.json"

DEFAULT_VOLUME = 1.0
DEFAULT_SPEED = 1.0
MIN_SPEED = 0.5
MAX_SPEED = 2.0

# Треки длиннее этого в волну не берём: плеер декодирует трек в сырой PCM
# целиком (см. pcm.py), а на SoundCloud полно диджей-сетов на 1-2 часа —
# такой трек это гигабайт+ в памяти и минуты ожидания перед стартом.
DEFAULT_MAX_TRACK_MINUTES = 20

DEFAULT_HOTKEYS = {
    "previous": {"ctrl": True, "shift": True, "alt": True, "key": "Q"},
    "next": {"ctrl": True, "shift": True, "alt": True, "key": "E"},
    "play_pause": {"ctrl": True, "shift": True, "alt": True, "key": "W"},
    "seek": {"ctrl": True, "shift": True, "alt": True, "key": "X"},
    "speed_up": {"ctrl": True, "shift": True, "alt": True, "key": "D"},
    "speed_down": {"ctrl": True, "shift": True, "alt": True, "key": "A"},
    "speed_reset": {"ctrl": True, "shift": True, "alt": True, "key": "S"},
}

TOKEN_HELP = (
    "Не найден oauth_token SoundCloud.\n\n"
    "При запуске main.py откроется окно авторизации с инструкцией.\n\n"
    "Либо задайте токен вручную:\n"
    '   config.json: {"oauth_token": "2-2-XXXXXX-..."}\n'
    "   либо переменная окружения SOUNDCLOUD_OAUTH_TOKEN.\n\n"
    "Где взять: soundcloud.com → войти → DevTools (F12) → Application →\n"
    "Cookies → https://soundcloud.com → значение куки oauth_token."
)


def _read_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_config(data: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_token() -> str:
    env_token = os.environ.get("SOUNDCLOUD_OAUTH_TOKEN")
    if env_token and env_token.strip():
        return env_token.strip()

    token = str(_read_config().get("oauth_token", "")).strip()
    if token:
        return token

    raise RuntimeError(TOKEN_HELP)


def save_token(token: str) -> None:
    data = _read_config()
    data["oauth_token"] = token.strip()
    _write_config(data)


def get_client_id() -> str:
    """Публичный client_id веб-плеера SoundCloud (см. sc_api.resolve_client_id).

    Кэшируется в конфиге, потому что вытаскивается скрейпом главной страницы —
    лишний раз дёргать не за чем. Протухает редко (SoundCloud меняет его
    вместе с релизом веб-клиента), на 401 sc_api перевыпускает сам.
    """
    return str(_read_config().get("client_id", "")).strip()


def save_client_id(client_id: str) -> None:
    data = _read_config()
    data["client_id"] = client_id.strip()
    _write_config(data)


def get_volume() -> float:
    try:
        value = float(_read_config().get("volume", DEFAULT_VOLUME))
    except (TypeError, ValueError):
        return DEFAULT_VOLUME
    return max(0.0, min(1.0, value))


def save_volume(value: float) -> None:
    data = _read_config()
    data["volume"] = round(max(0.0, min(1.0, value)), 3)
    _write_config(data)


def get_speed() -> float:
    try:
        value = float(_read_config().get("speed", DEFAULT_SPEED))
    except (TypeError, ValueError):
        return DEFAULT_SPEED
    return max(MIN_SPEED, min(MAX_SPEED, value))


def save_speed(value: float) -> None:
    data = _read_config()
    data["speed"] = round(max(MIN_SPEED, min(MAX_SPEED, value)), 3)
    _write_config(data)


def get_max_track_seconds() -> float:
    try:
        minutes = float(_read_config().get("max_track_minutes", DEFAULT_MAX_TRACK_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_MAX_TRACK_MINUTES
    return max(1.0, minutes) * 60


def get_blocked_ids() -> set:
    """Локальный «дизлайк»: у SoundCloud нет серверного дизлайка (см. wave.py),
    поэтому отвергнутые треки просто не попадают в волну на этой машине."""
    raw = _read_config().get("blocked", [])
    if not isinstance(raw, list):
        return set()
    return {str(i) for i in raw}


def save_blocked_ids(ids) -> None:
    data = _read_config()
    data["blocked"] = sorted(str(i) for i in ids)
    _write_config(data)


def get_likes_cache() -> dict:
    """Кэш списка лайков: `{"ids": [...], "fetched_at": <unix ts>}`.

    У SoundCloud нет ревизии списка (как у Яндекса), поэтому сверка не
    «дешёвый вопрос об изменениях», а обычная перекачка id в фоне при старте —
    кэш нужен, чтобы ❤ в меню было верным сразу, не дожидаясь сети.
    """
    if not LIKES_CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(LIKES_CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_likes_cache(cache: dict) -> None:
    try:
        LIKES_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # кэш — необязательная оптимизация, без него просто медленнее старт


def _normalize_hotkey(entry: dict, default: dict) -> dict:
    key = str(entry.get("key", default["key"])).strip().upper()[:1] or default["key"]
    return {
        "ctrl": bool(entry.get("ctrl", default["ctrl"])),
        "shift": bool(entry.get("shift", default["shift"])),
        "alt": bool(entry.get("alt", default["alt"])),
        "key": key,
    }


def get_hotkeys() -> dict:
    stored = _read_config().get("hotkeys", {})
    if not isinstance(stored, dict):
        stored = {}
    return {
        name: _normalize_hotkey(stored.get(name) or {}, default)
        for name, default in DEFAULT_HOTKEYS.items()
    }


def save_hotkeys(hotkeys: dict) -> None:
    data = _read_config()
    data["hotkeys"] = {
        name: _normalize_hotkey(hotkeys.get(name) or {}, default)
        for name, default in DEFAULT_HOTKEYS.items()
    }
    _write_config(data)
