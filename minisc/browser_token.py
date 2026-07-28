"""Достаёт oauth_token SoundCloud из куки Firefox — чтобы не вводить руками.

Почему именно Firefox и почему это просто: Firefox держит куки открытым
текстом в обычной SQLite-базе (`cookies.sqlite`, таблица `moz_cookies`).
У Chrome/Edge значение куки зашифровано (DPAPI + AES-GCM, а в свежих версиях
ещё и app-bound), и надёжно расшифровать его стало тяжело — поэтому здесь
поддержан только Firefox.

База читается БЕЗ копирования и без блокировок, через SQLite-URI
`immutable=1&mode=ro`: даже с запущенным Firefox (который держит WAL и лочит
файл) это отдаёт консистентный снимок только на чтение и ничего не пишет
рядом. У SoundCloud нет device-flow (как OAuth у Яндекса) — регистрация
приложений закрыта, — поэтому «пусть сервер сам отдаст токен» невозможно, и
взять уже существующую сессию из браузера — самый простой доступный путь.
"""

import configparser
import logging
import os
import sqlite3
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def _firefox_base() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    base = Path(appdata) / "Mozilla" / "Firefox"
    return base if base.exists() else None


def _profiles() -> List[Path]:
    """Профили Firefox в порядке предпочтения: сначала дефолтный (по
    profiles.ini), затем остальные — свежие по времени изменения куки первыми.

    Порядок важен: у человека может быть несколько профилей, а залогинен в
    SoundCloud он, скорее всего, в основном; если там токена нет — есть смысл
    заглянуть и в прочие, но начиная с недавно активных.
    """
    base = _firefox_base()
    if base is None:
        return []

    profiles_root = base / "Profiles"
    all_profiles = [p for p in profiles_root.glob("*") if (p / "cookies.sqlite").exists()] \
        if profiles_root.exists() else []

    preferred: List[Path] = []
    ini_path = base / "profiles.ini"
    if ini_path.exists():
        parser = configparser.ConfigParser()
        try:
            parser.read(ini_path, encoding="utf-8")
        except (configparser.Error, OSError):
            parser = None
        if parser is not None:
            # [Install*].Default — абсолютный/относительный путь активного профиля.
            # [Profile*].Default=1 — исторический дефолт. И то, и другое — путь
            # относительно каталога Firefox (или абсолютный, если IsRelative=0).
            candidates: List[str] = []
            for section in parser.sections():
                if section.startswith("Install"):
                    candidates.append(parser[section].get("Default", ""))
            for section in parser.sections():
                if section.startswith("Profile") and parser[section].get("Default") == "1":
                    candidates.append(parser[section].get("Path", ""))
            for rel in candidates:
                if not rel:
                    continue
                path = Path(rel) if Path(rel).is_absolute() else base / rel
                if (path / "cookies.sqlite").exists():
                    preferred.append(path)

    def cookies_mtime(profile: Path) -> float:
        try:
            return (profile / "cookies.sqlite").stat().st_mtime
        except OSError:
            return 0.0

    rest = sorted(
        (p for p in all_profiles if p not in preferred),
        key=cookies_mtime,
        reverse=True,
    )

    ordered: List[Path] = []
    for profile in preferred + rest:
        if profile not in ordered:
            ordered.append(profile)
    return ordered


def _read_oauth_token(cookies_db: Path) -> Optional[str]:
    uri = f"file:{cookies_db.as_posix()}?immutable=1&mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT value FROM moz_cookies "
            "WHERE name = 'oauth_token' AND host LIKE '%soundcloud.com'"
        ).fetchall()
    finally:
        con.close()
    for (value,) in rows:
        if value and str(value).strip():
            return str(value).strip()
    return None


def find_firefox_oauth_token() -> Optional[str]:
    """oauth_token SoundCloud из первого профиля Firefox, где он найдётся.

    None означает «не нашли» (нет Firefox, нет профиля, не залогинен в SC) —
    вызывающий откатывается на ручной ввод. Не бросает: любая ошибка чтения
    базы гасится в лог, потому что это лишь удобная попытка, а не обязательный
    шаг.
    """
    for profile in _profiles():
        db = profile / "cookies.sqlite"
        try:
            token = _read_oauth_token(db)
        except sqlite3.Error as exc:
            logger.info("Не удалось прочитать куки Firefox (%s): %s", profile.name, exc)
            continue
        if token:
            logger.info("oauth_token найден в профиле Firefox «%s»", profile.name)
            return token
    return None
