"""Клиент внутреннего API SoundCloud (api-v2) — то, чем ходит веб-плеер.

Почему не официальный api.soundcloud.com: он выдаёт только 30-секундные
сниппеты треков (это прямо написано в гайде SoundCloud для интеграторов,
см. `_refs/Agents.md` в SoundCloud-Desktop) — для плеера бесполезно.
api-v2 отдаёт полные транскодинги, ими же играет сам сайт.

Пути эндпоинтов не выдуманы: вытащены из таблицы роутов в JS-бандле
веб-плеера (`me/track_likes/ids`, `users/:userId/track_likes/:id`,
`mixed-selections`, `me/play-history/tracks`, `tracks/:id/related`).

Авторизация — заголовок `Authorization: OAuth <token>`, где токен берётся из
куки `oauth_token` браузерной сессии (config.get_token). Плюс к нему всегда
нужен публичный `client_id` веб-клиента: он не секрет и лежит открытым
текстом в JS-бандле, откуда мы его и достаём (resolve_client_id).
"""

import logging
import random
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from .config import get_client_id, save_client_id

logger = logging.getLogger(__name__)

API = "https://api-v2.soundcloud.com"
WEB = "https://soundcloud.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 20

# Порядок предпочтения транскодингов: AAC 160k — лучшее, что SoundCloud
# отдаёт бесплатно, дальше вниз по качеству. progressive (цельный mp3)
# оставлен последним запасным вариантом: он ниже качеством, но не требует
# от FFmpeg тянуть HLS-плейлист, поэтому надёжнее при кривой сети.
PRESET_PRIORITY = ("aac_160k", "abr_sq", "aac_96k")

# Протоколы, которые умеет открыть FFmpeg. Всё остальное у SoundCloud — это
# `cbc-encrypted-hls`/`ctr-encrypted-hls`: HLS под SAMPLE-AES с ключом Widevine,
# то есть DRM. Расшифровать его без Widevine-рукопожатия нельзя (в
# SoundCloud-Desktop ровно ради этого держится отдельный серверный модуль
# decrypt). Важно, что у таких треков рядом почти всегда лежит НЕзашифрованный
# mp3-транскодинг — если не фильтровать протокол, все попытки уходят в DRM-версии
# и играбельный трек ошибочно считается мёртвым.
PLAYABLE_PROTOCOLS = ("hls", "progressive")


class SoundCloudError(Exception):
    pass


class AuthError(SoundCloudError):
    """Токен протух или неверен — нужна повторная авторизация пользователем."""


@dataclass
class Track:
    """Плоское представление трека: в API-ответах полей десятки, плееру нужны эти."""

    id: str
    urn: str
    title: str
    artist: str
    duration_ms: int
    permalink_url: str
    transcodings: List[dict]
    track_authorization: Optional[str]
    artwork_url: Optional[str] = None

    @property
    def duration_seconds(self) -> float:
        return self.duration_ms / 1000 if self.duration_ms else 0.0

    def label(self) -> str:
        return f"{self.artist} — {self.title}" if self.artist else self.title


def parse_track(data: dict) -> Optional[Track]:
    """Делает Track из объекта api-v2, отсеивая всё, что играть нельзя.

    Отсеиваются: не-треки (плейлисты/юзеры в смешанных лентах), треки без
    media.transcodings (SoundCloud отдаёт такие «заглушки» для недоступных в
    регионе/удалённых), снипеты (policy SNIP — только 30 секунд для
    подписчиков SoundCloud Go) и DRM-треки.

    DRM определяется по наличию ХОТЬ ОДНОГО зашифрованного транскодинга, а не
    по отсутствию обычных: у таких треков рядом с `cbc/ctr-encrypted-hls`
    лежат и обычные на вид mp3-транскодинги, но их резолв отдаёт 404 —
    приманка. Проверено: 3 из 12 треков случайной подборки оказались такими,
    и без этой отбраковки каждый стоил трёх бесполезных запросов и заметной
    паузы перед скипом.
    """
    if not isinstance(data, dict) or data.get("kind") != "track":
        return None
    if data.get("policy") == "SNIP":
        return None

    all_transcodings = (data.get("media") or {}).get("transcodings") or []
    if any(
        "encrypted" in ((t.get("format") or {}).get("protocol") or "")
        for t in all_transcodings
    ):
        return None

    transcodings = [
        t
        for t in all_transcodings
        if not t.get("snipped")
        and t.get("url")
        and (t.get("format") or {}).get("protocol") in PLAYABLE_PROTOCOLS
    ]
    if not transcodings:
        return None

    user = data.get("user") or {}
    track_id = data.get("id")
    if track_id is None:
        return None

    return Track(
        id=str(track_id),
        urn=str(data.get("urn") or f"soundcloud:tracks:{track_id}"),
        title=str(data.get("title") or "?"),
        artist=str(user.get("username") or ""),
        duration_ms=int(data.get("full_duration") or data.get("duration") or 0),
        permalink_url=str(data.get("permalink_url") or ""),
        transcodings=transcodings,
        track_authorization=data.get("track_authorization"),
        artwork_url=data.get("artwork_url"),
    )


def resolve_client_id(session: Optional[requests.Session] = None) -> str:
    """Достаёт публичный client_id веб-плеера: главная страница → её JS-бандлы →
    первое вхождение `client_id:"..."`.

    Скрипты перебираются с конца: client_id объявлен в одном из последних
    чанков, и так он находится с первой-второй попытки вместо девяти.
    """
    session = session or requests.Session()
    try:
        html = session.get(WEB, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT).text
    except requests.RequestException as exc:
        raise SoundCloudError(f"Не удалось открыть soundcloud.com: {exc}") from exc

    scripts = re.findall(r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+)"', html)
    for url in reversed(scripts):
        try:
            body = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT).text
        except requests.RequestException:
            continue
        match = re.search(r'client_id\s*:\s*"([a-zA-Z0-9]{20,})"', body)
        if match:
            client_id = match.group(1)
            logger.info("Resolved SoundCloud client_id from %s", url.rsplit("/", 1)[-1])
            return client_id

    raise SoundCloudError("Не удалось найти client_id в JS веб-плеера SoundCloud")


class SoundCloudClient:
    def __init__(self, oauth_token: str):
        self.token = oauth_token.strip()
        self._session = requests.Session()
        self._client_id = get_client_id()
        self._client_id_lock = threading.Lock()
        self._me: Optional[dict] = None

    # ---- низкий уровень ------------------------------------------------

    def _ensure_client_id(self, force: bool = False) -> str:
        with self._client_id_lock:
            if self._client_id and not force:
                return self._client_id
            self._client_id = resolve_client_id(self._session)
            save_client_id(self._client_id)
            return self._client_id

    def _headers(self, authed: bool) -> Dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if authed and self.token:
            headers["Authorization"] = f"OAuth {self.token}"
        return headers

    def request(
        self,
        method: str,
        url: str,
        params: Optional[dict] = None,
        authed: bool = True,
        expect_json: bool = True,
    ) -> Any:
        """Запрос к api-v2 с одной повторной попыткой на протухший client_id.

        401 неоднозначен: это либо истёкший client_id (лечится перевыпуском —
        он живёт до следующего релиза веб-клиента), либо мёртвый oauth_token
        (лечится только пользователем). Поэтому сначала пробуем свежий
        client_id, и лишь если и с ним 401 — считаем виноватым токен и просим
        переавторизацию.
        """
        if not url.startswith("http"):
            url = f"{API}/{url.lstrip('/')}"

        for attempt in (0, 1):
            query = dict(params or {})
            query["client_id"] = self._ensure_client_id(force=attempt == 1)
            try:
                resp = self._session.request(
                    method, url, params=query, headers=self._headers(authed), timeout=TIMEOUT
                )
            except requests.RequestException as exc:
                raise SoundCloudError(f"{method} {url}: {exc}") from exc

            if resp.status_code == 401 and attempt == 0:
                logger.info("401 from %s — перевыпускаю client_id", url)
                continue
            if resp.status_code == 401:
                raise AuthError("SoundCloud отверг авторизацию (401): токен недействителен")
            if resp.status_code == 404:
                raise SoundCloudError(f"404: {url}")
            if not resp.ok:
                raise SoundCloudError(f"{method} {url} → HTTP {resp.status_code}: {resp.text[:200]}")

            if not expect_json or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError as exc:
                raise SoundCloudError(f"Не JSON в ответе {url}: {exc}") from exc

        raise SoundCloudError("unreachable")

    def _get(self, path: str, params: Optional[dict] = None, authed: bool = True) -> Any:
        return self.request("GET", path, params=params, authed=authed)

    # ---- аккаунт --------------------------------------------------------

    def me(self) -> dict:
        if self._me is None:
            data = self._get("me")
            if not isinstance(data, dict) or "id" not in data:
                raise AuthError("SoundCloud не вернул профиль по этому токену")
            self._me = data
        return self._me

    @property
    def user_id(self) -> str:
        return str(self.me()["id"])

    @property
    def username(self) -> str:
        return str(self.me().get("username") or "")

    # ---- лента ----------------------------------------------------------

    def _paged(self, path: str, params: Optional[dict], pages: int) -> List[dict]:
        """Проходит по `next_href` (linked_partitioning) не более `pages` страниц."""
        query = dict(params or {})
        query["linked_partitioning"] = 1
        items: List[dict] = []
        url = path
        for _ in range(pages):
            data = self._get(url, params=query)
            if not isinstance(data, dict):
                break
            items.extend(data.get("collection") or [])
            url = data.get("next_href")
            if not url:
                break
            query = {}  # next_href уже содержит все параметры, кроме client_id
        return items

    def hydrate_tracks(self, track_ids: List[str]) -> List[Track]:
        """Достаёт полные объекты треков по id (батчами, как это делает сайт).

        Нужно потому, что в плейлистах SoundCloud полностью раскрыты только
        первые несколько треков, а остальные приходят «заглушками» без
        `media.transcodings` — играть по ним нечего.
        """
        tracks: List[Track] = []
        for start in range(0, len(track_ids), 40):
            batch = track_ids[start : start + 40]
            data = self._get("tracks", params={"ids": ",".join(batch)})
            for raw in data or []:
                track = parse_track(raw)
                if track is not None:
                    tracks.append(track)
        return tracks

    def playlist_tracks(self, playlist_id: str, limit: int = 30) -> List[Track]:
        data = self._get(f"playlists/{playlist_id}")
        raw_tracks = (data or {}).get("tracks") or []
        tracks: List[Track] = []
        stub_ids: List[str] = []
        for raw in raw_tracks[:limit]:
            if (raw.get("media") or {}).get("transcodings"):
                track = parse_track(raw)
                if track is not None:
                    tracks.append(track)
            elif raw.get("id") is not None:
                stub_ids.append(str(raw["id"]))
        if len(tracks) < limit and stub_ids:
            tracks.extend(self.hydrate_tracks(stub_ids[: limit - len(tracks)]))
        return tracks

    def personal_selections(self, playlists: int = 3) -> List[Track]:
        """Персональная лента: подборки с главной SoundCloud.

        `mixed-selections` — это то, что сайт показывает на главной. С
        авторизацией подборки персональные («More of what you like», «The
        Upload»), без неё — общередакционные. Внутри подборок лежат не треки,
        а плейлисты, поэтому за треками приходится сходить отдельно — берём
        несколько случайных плейлистов, а не все двадцать: одного захода
        хватает на десятки треков, а очередь всё равно дозаправляется
        соседями.
        """
        data = self._get("mixed-selections", params={"limit": 20})
        playlist_ids: List[str] = []
        for selection in (data or {}).get("collection") or []:
            for item in ((selection.get("items") or {}).get("collection")) or []:
                if item.get("kind") == "playlist" and item.get("id") is not None:
                    playlist_ids.append(str(item["id"]))

        random.shuffle(playlist_ids)
        tracks: List[Track] = []
        for playlist_id in playlist_ids[:playlists]:
            try:
                tracks.extend(self.playlist_tracks(playlist_id))
            except SoundCloudError:
                logger.info("Плейлист %s не открылся, пропускаю", playlist_id)
        return tracks

    def play_history(self, limit: int = 50) -> List[Track]:
        """Недавно прослушанное — вторая опора персонализации.

        Треки отсюда сами по себе в волну не идут (их только что слушали), но
        служат «семенами»: по ним запрашиваются related-соседи.
        """
        items = self._paged("me/play-history/tracks", {"limit": limit}, pages=1)
        tracks = []
        for item in items:
            track = parse_track(item.get("track") or {})
            if track is not None:
                tracks.append(track)
        return tracks

    def liked_tracks(self, limit: int = 200) -> List[Track]:
        items = self._paged(
            f"users/{self.user_id}/track_likes", {"limit": limit}, pages=2
        )
        tracks = []
        for item in items:
            track = parse_track(item.get("track") or item)
            if track is not None:
                tracks.append(track)
        return tracks

    def related(self, track_id: str, limit: int = 20) -> List[Track]:
        """Похожие на трек — этим волна и продолжается бесконечно."""
        data = self._get(f"tracks/{track_id}/related", params={"limit": limit})
        tracks = []
        for raw in (data or {}).get("collection") or []:
            track = parse_track(raw)
            if track is not None:
                tracks.append(track)
        return tracks

    # ---- лайки ----------------------------------------------------------

    def liked_track_ids(self) -> List[str]:
        """Только id лайкнутого — отдельный лёгкий эндпоинт самого веб-плеера
        (`me/track_likes/ids`), полные объекты треков для галочки ❤ не нужны."""
        data = self._get("me/track_likes/ids", params={"limit": 200})
        return [str(i) for i in (data or {}).get("collection") or []]

    def like(self, track_id: str) -> None:
        self.request(
            "PUT", f"users/{self.user_id}/track_likes/{track_id}", expect_json=False
        )

    def unlike(self, track_id: str) -> None:
        self.request(
            "DELETE", f"users/{self.user_id}/track_likes/{track_id}", expect_json=False
        )

    # ---- прослушивания --------------------------------------------------

    def report_play(self, track: Track) -> None:
        """Пишет трек в историю прослушиваний — на этом SoundCloud и строит
        персональные рекомендации. Аналог фидбека ротору у Яндекса."""
        try:
            self.request(
                "POST",
                "me/play-history",
                params={"track_urn": track.urn},
                expect_json=False,
            )
        except SoundCloudError as exc:
            logger.info("play-history не принял трек %s: %s", track.id, exc)

    # ---- поток ----------------------------------------------------------

    def _ordered_transcodings(self, track: Track) -> List[dict]:
        """Транскодинги от лучшего к худшему, с progressive в конце как запасным.

        Запасные варианты нужны не для красоты: CDN SoundCloud время от времени
        обрывает конкретную раздачу (FFmpeg падает на ней с ExitError), и
        вторая попытка по другому транскодингу почти всегда проходит.
        """
        ordered: List[dict] = []
        by_preset = {t.get("preset"): t for t in track.transcodings}
        for preset in PRESET_PRIORITY:
            if preset in by_preset:
                ordered.append(by_preset[preset])
        for transcoding in track.transcodings:
            if (transcoding.get("format") or {}).get("protocol") == "progressive":
                ordered.append(transcoding)
        for transcoding in track.transcodings:
            if transcoding not in ordered:
                ordered.append(transcoding)
        return ordered

    def iter_stream_urls(self, track: Track):
        """Выдаёт ссылки на поток по одной, от лучшего качества к запасному.

        Ссылки одноразовые и подписанные (живут минуты), поэтому резолвятся
        лениво — прямо перед попыткой воспроизведения, а не пачкой заранее.
        Каждая ссылка — то, что понимает FFmpeg/PyAV: HLS-плейлист или mp3.
        """
        params = {}
        if track.track_authorization:
            params["track_authorization"] = track.track_authorization

        for transcoding in self._ordered_transcodings(track):
            try:
                data = self._get(transcoding["url"], params=params)
            except SoundCloudError as exc:
                logger.info("Транскодинг %s недоступен: %s", transcoding.get("preset"), exc)
                continue
            url = (data or {}).get("url")
            if url:
                yield str(url)


def shuffled(tracks: List[Track]) -> List[Track]:
    tracks = list(tracks)
    random.shuffle(tracks)
    return tracks
