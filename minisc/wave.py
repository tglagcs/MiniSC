import logging
import random
import threading
import time
from collections import Counter, deque
from typing import Callable, List, Optional, Tuple

import pygame

from . import pcm
from .config import (
    get_blocked_ids,
    get_likes_cache,
    get_max_track_seconds,
    get_speed,
    save_blocked_ids,
    save_likes_cache,
    save_speed,
)
from .sc_api import SoundCloudClient, SoundCloudError, Track, shuffled

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.5
PREFETCH_THRESHOLD = 2
# Сколько «семян» из недавно прослушанного держим для добора соседей.
SEED_LIMIT = 15
RELATED_LIMIT = 20
# Сколько транскодингов пробуем, прежде чем признать трек непроигрываемым.
MAX_STREAM_ATTEMPTS = 3

# --- разнообразие по артистам ---
# У SoundCloud «похожие» сильно тяготеют к тому же артисту, а если пользователь
# кого-то много слушал, ОТРАВЛЕНЫ им все источники разом: play_history состоит
# из него, related каждого его трека на треть снова он, и даже персональные
# подборки под него подстроены (замерено на живом аккаунте: до 79% истории и 31%
# пула добора — один артист). Поэтому мало «не ставить подряд» — нужно и питать
# волну из более широкого сигнала, и жёстко ограничивать частоту артиста.
SEED_RECENT = 2           # сидов от недавно игравшего (продолжает текущее настроение)
SEED_LIKES = 3            # + сидов от СЛУЧАЙНЫХ лайков (лайки разнообразны — это и лечит)
MAX_QUEUE_PER_ARTIST = 1  # не больше стольких треков одного артиста в очереди разом
ARTIST_COOLDOWN = 5       # не ставить артиста, если он среди стольких последних сыгранных
ARTIST_WINDOW = 30        # окно сессии, в котором действует лимит частоты
MAX_PLAYS_PER_WINDOW = 2  # ...не чаще стольких раз одного артиста в этом окне
SELECTION_MIX = 12        # сколько треков персональных подборок подмешивать в добор
LIKE_MIX = 4              # сколько лайков подмешивать прямо в пул добора

TrackChangeCallback = Callable[[Optional[Track]], None]


class WavePlayer:
    """Бесконечная персональная волна на SoundCloud.

    Играет через `pygame.mixer.Sound`/`Channel` из полностью декодированного
    в PCM трека (а не потоково через `pygame.mixer.music`) — это нужно для
    перемотки и регулировки скорости: у `mixer.music` нет способа поменять
    скорость воспроизведения, а у `Sound` можно просто подсунуть заранее
    пересчитанный (через `pcm.apply_speed`) буфер.

    Откуда берётся бесконечность: у SoundCloud нет единой «моей волны» одной
    ссылкой (станция там всегда привязана к конкретному треку-семени), поэтому
    поток собирается сам: стартовая порция — из персональных подборок главной
    (`mixed-selections`, они персонализируются по токену), а дальше очередь
    непрерывно дозаправляется соседями (`tracks/:id/related`) уже сыгранных
    треков. Прослушивания уходят в историю SoundCloud (`report_play`) — на ней
    строится персонализация, то есть волна со временем подстраивается под вас.
    """

    def __init__(self, client: SoundCloudClient, on_track_change: Optional[TrackChangeCallback] = None):
        self.client = client
        self.on_track_change: TrackChangeCallback = on_track_change or (lambda track: None)

        pygame.mixer.init(frequency=pcm.RATE, size=-16, channels=pcm.CHANNELS)
        self._channel = pygame.mixer.Channel(0)

        self._queue: List[Track] = []
        self._liked_ids: set = set()
        self._blocked_ids: set = get_blocked_ids()
        self._seen_ids: set = set()
        self._seeds: List[Tuple[str, str]] = []          # (track_id, artist)
        self._recent_artists: deque = deque(maxlen=ARTIST_WINDOW)
        self._selection_pool: List[Track] = []           # кэш подборок, чтобы подмешивать дёшево
        self._like_pool: List[Track] = []                # кэш лайков — разнообразный сид/подмешка
        self._like_pool_loaded = False
        self._played: List[Track] = []
        self._position = -1
        self._advance_token = 0
        self._current: Optional[Track] = None
        self._current_pcm: Optional[bytes] = None
        self._playing_since = 0.0
        self._played_seconds = 0.0
        self._speed = max(pcm.MIN_SPEED, min(pcm.MAX_SPEED, get_speed()))
        self._max_track_seconds = get_max_track_seconds()

        self._lock = threading.RLock()
        self._state = "stopped"  # stopped | playing | paused
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._prefetch_thread: Optional[threading.Thread] = None
        self._volume = 1.0

    # ---- public API ---------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True

        threading.Thread(target=self._load_likes, daemon=True).start()
        self._fetch_more()
        self._advance()

        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
        self._channel.stop()

    def toggle_play_pause(self) -> None:
        with self._lock:
            if self._state == "playing":
                self._played_seconds = self._elapsed_seconds()
                self._channel.pause()
                self._state = "paused"
            elif self._state == "paused":
                self._playing_since = time.time()
                self._channel.unpause()
                self._state = "playing"

    def next(self) -> None:
        self._advance()

    def previous(self) -> bool:
        """Возвращает False, если в истории нет предыдущего трека."""
        with self._lock:
            if self._position <= 0:
                return False
            self._advance_token += 1
            token = self._advance_token
            self._position -= 1
            track = self._played[self._position]
        if not self._play_track(track, record_history=False, token=token):
            logger.warning("Failed to replay previous track %s, skipping", track.id)
            with self._lock:
                del self._played[self._position]
                self._position -= 1
            self._advance()
        return True

    def seek(self, seconds: float) -> None:
        with self._lock:
            if self._current is None or self._current_pcm is None:
                return
            duration = self._duration_seconds()
            seconds = max(0.0, min(seconds, duration)) if duration else max(0.0, seconds)
        self._play_from(seconds)

    def get_position(self) -> float:
        with self._lock:
            return self._elapsed_seconds()

    def get_duration(self) -> float:
        with self._lock:
            return self._duration_seconds()

    def get_speed(self) -> float:
        with self._lock:
            return self._speed

    def set_speed(self, value: float) -> None:
        value = max(pcm.MIN_SPEED, min(pcm.MAX_SPEED, value))
        with self._lock:
            if abs(value - self._speed) < 1e-6:
                return
            current_position = self._elapsed_seconds()
            was_paused = self._state == "paused"
            self._speed = value
        save_speed(value)

        if self._current_pcm is None:
            return
        self._play_from(current_position)
        if was_paused:
            # _play_from всегда запускает воспроизведение — возвращаем на паузу.
            self.toggle_play_pause()

    def toggle_like(self) -> Optional[bool]:
        """Ставит лайк, а если он уже стоит — снимает.

        Возвращает новое состояние (True = лайк поставлен) сразу, не дожидаясь
        сети: список лайкнутого ведётся локально (загружается при старте),
        сам запрос уходит в фоновом потоке.
        """
        track = self._current
        if track is None:
            return None
        with self._lock:
            was_liked = track.id in self._liked_ids
            if was_liked:
                self._liked_ids.discard(track.id)
            else:
                self._liked_ids.add(track.id)

        action = self.client.unlike if was_liked else self.client.like
        threading.Thread(target=self._safe_call, args=(action, track.id), daemon=True).start()
        return not was_liked

    def is_current_liked(self) -> bool:
        track = self._current
        if track is None:
            return False
        with self._lock:
            return track.id in self._liked_ids

    def toggle_dislike(self) -> Optional[bool]:
        """Локальный «не нравится» с возможностью отмены.

        У SoundCloud, в отличие от Яндекса, серверного дизлайка нет вообще —
        обучать нечего. Поэтому дизлайк здесь честно локальный: id уходит в
        `blocked` в config.json, и такой трек больше не попадает в волну на
        этой машине; сам трек при этом скипается. Повторное нажатие (после
        возврата по истории ⏮) снимает блокировку и НЕ скипает.
        """
        track = self._current
        if track is None:
            return None
        with self._lock:
            was_blocked = track.id in self._blocked_ids
            if was_blocked:
                self._blocked_ids.discard(track.id)
            else:
                self._blocked_ids.add(track.id)
            blocked_snapshot = set(self._blocked_ids)

        threading.Thread(target=save_blocked_ids, args=(blocked_snapshot,), daemon=True).start()

        if was_blocked:
            return False

        # Лайк и блокировка взаимоисключающи — как в официальном клиенте с лайком.
        with self._lock:
            was_liked = track.id in self._liked_ids
            self._liked_ids.discard(track.id)
        if was_liked:
            threading.Thread(
                target=self._safe_call, args=(self.client.unlike, track.id), daemon=True
            ).start()

        def apply() -> None:
            self._drop_from_queue(track.id)
            self._advance()

        threading.Thread(target=apply, daemon=True).start()
        return True

    def is_current_disliked(self) -> bool:
        track = self._current
        if track is None:
            return False
        with self._lock:
            return track.id in self._blocked_ids

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def current_track(self) -> Optional[Track]:
        return self._current

    def get_volume(self) -> float:
        with self._lock:
            return self._volume

    def set_volume(self, value: float) -> None:
        value = max(0.0, min(1.0, value))
        with self._lock:
            self._volume = value
        self._channel.set_volume(value)

    # ---- internals ------------------------------------------------------

    def _load_likes(self) -> None:
        """Наполняет `_liked_ids` для toggle_like.

        Сначала мгновенно из локального кэша (likes_cache.json), потом
        перекачка с сервера в фоне. Ревизии списка, как у Яндекса, у
        SoundCloud нет — спросить «менялось ли» нечем, поэтому кэш нужен
        ровно затем, чтобы ❤ в меню было верным сразу после старта.
        """
        cache = get_likes_cache()
        with self._lock:
            self._liked_ids.update(str(i) for i in cache.get("ids", []))

        ids = self._safe_call(self.client.liked_track_ids)
        if ids is None:
            return  # сеть упала — живём на кэше
        with self._lock:
            self._liked_ids.clear()
            self._liked_ids.update(ids)
        save_likes_cache({"ids": list(ids), "fetched_at": time.time()})
        logger.info("Fetched likes: %d tracks", len(ids))

    def _safe_call(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except SoundCloudError:
            logger.exception("SoundCloud API call failed: %s", getattr(func, "__name__", func))
            return None
        except Exception:
            logger.exception("Unexpected failure in %s", getattr(func, "__name__", func))
            return None

    def _load_track(self, track: Track) -> Optional[bytes]:
        """Резолвит поток и декодирует трек в PCM целиком, откатываясь по
        транскодингам при сбоях.

        Откат обязателен: CDN SoundCloud периодически обрывает конкретную
        раздачу (FFmpeg падает с ExitError уже посреди трека), причём
        одинаково и на HLS, и на progressive — зато следующая ссылка почти
        всегда играет. Ссылки одноразовые и подписанные (живут минуты),
        поэтому берутся прямо здесь, а не заранее при наполнении очереди.
        """
        attempts = 0
        try:
            for url in self.client.iter_stream_urls(track):
                attempts += 1
                try:
                    data = pcm.decode_url_to_pcm(url)
                except Exception:
                    logger.info("Поток трека %s не декодировался, пробую следующий", track.id)
                else:
                    if data:
                        return data
                if attempts >= MAX_STREAM_ATTEMPTS:
                    break
        except SoundCloudError:
            logger.exception("Не удалось получить ссылку на поток для трека %s", track.id)
            return None

        logger.warning("Нет играбельного потока для трека %s", track.id)
        return None

    def _play_from(self, track_seconds: float) -> None:
        """Запускает воспроизведение уже загруженного `_current_pcm` с указанной
        позиции (в секундах исходного трека), с учётом текущей скорости."""
        with self._lock:
            data = self._current_pcm
            speed = self._speed
        if not data:
            return

        offset = pcm.seconds_to_byte_offset(track_seconds)
        offset = max(0, min(offset, len(data)))
        chunk = data[offset:]
        if abs(speed - 1.0) > 1e-6:
            chunk = pcm.apply_speed(chunk, speed)

        sound = pygame.mixer.Sound(buffer=chunk)
        self._channel.play(sound)
        self._channel.set_volume(self._volume)

        with self._lock:
            self._played_seconds = track_seconds
            self._playing_since = time.time()
            self._state = "playing"

    def _enqueue(self, tracks: List[Track], strict: bool = True) -> int:
        """Кладёт в очередь годное, с ограничениями на частоту артиста.

        Годное — не сыгранное, не заблокированное, не длиннее лимита. Длинные
        треки отсекаются не из вкусовщины: плеер декодирует трек в сырой PCM
        целиком, а на SoundCloud полно диджей-сетов на час-два — это гигабайты
        в памяти и минуты ожидания перед первым звуком.

        Два ограничения против «играет один и тот же чел»:
        - `MAX_QUEUE_PER_ARTIST` — сколько одного артиста лежит в очереди разом;
        - `MAX_PLAYS_PER_WINDOW` — сколько раз он уже звучал за последние
          `ARTIST_WINDOW` треков (скользящее окно сессии). Именно второе лечит
          «по чуть-чуть, но постоянно»: без него артист, которым отравлены все
          источники, добирает свою пару в КАЖДЫЙ добор бесконечно.

        `strict=False` снимает оконный лимит (оставляя очередной кап) — режим
        для фолбэка, когда со строгим фильтром в очередь не влезло ничего
        (у пользователя вся лента — один артист): играть что-то лучше, чем
        встать колом.
        """
        added = 0
        with self._lock:
            per_artist = Counter(t.artist for t in self._queue)
            recent = Counter(self._recent_artists)
            for track in tracks:
                if track.id in self._seen_ids or track.id in self._blocked_ids:
                    continue
                duration = track.duration_seconds
                if duration and duration > self._max_track_seconds:
                    continue
                artist = track.artist
                if artist:
                    if per_artist[artist] >= MAX_QUEUE_PER_ARTIST:
                        continue
                    if strict and recent[artist] >= MAX_PLAYS_PER_WINDOW:
                        continue
                self._seen_ids.add(track.id)
                self._queue.append(track)
                per_artist[artist] += 1
                added += 1
        return added

    def _drop_from_queue(self, track_id: str) -> None:
        with self._lock:
            self._queue = [t for t in self._queue if t.id != track_id]

    def _remember_seed(self, track: Track) -> None:
        with self._lock:
            if any(sid == track.id for sid, _ in self._seeds):
                return
            self._seeds.append((track.id, track.artist))
            del self._seeds[:-SEED_LIMIT]

    def _ensure_like_pool(self) -> None:
        """Лениво подгружает лайки как разнообразный источник сидов/подмешки.

        Лайки — самый широкий сигнал вкуса (замерено: у пользователя 59 разных
        артистов из 73, тогда как play_history — почти один артист). Грузим
        один раз в фоне первого добора и держим перемешанными."""
        if self._like_pool_loaded:
            return
        self._like_pool_loaded = True
        likes = self._safe_call(self.client.liked_tracks) or []
        with self._lock:
            self._like_pool = shuffled(likes)

    def _pick_seeds(self) -> List[str]:
        """Сиды для добора — от РАЗНЫХ артистов: часть от недавно игравшего,
        часть от случайных лайков.

        Ключ к разнообразию именно у «залипшего» пользователя: если сеять
        только от недавнего, все сиды окажутся одним артистом (его и слушали),
        и `related` вернёт снова его же. Случайные лайки подсовывают в сиды
        другой, широкий вкус — и `related` от них тянет свежих соседей.
        """
        seeds: List[str] = []
        used_artists: set = set()

        def take(source, limit):
            n = 0
            for track_id, artist in source:
                key = artist or track_id
                if key in used_artists:
                    continue
                used_artists.add(key)
                seeds.append(track_id)
                n += 1
                if n >= limit:
                    break

        with self._lock:
            recent = list(reversed(self._seeds))
            likes = [(t.id, t.artist) for t in self._like_pool]
        random.shuffle(likes)

        take(recent, SEED_RECENT)
        take(likes, SEED_LIKES)
        return seeds

    def _take_selections(self, count: int) -> List[Track]:
        """Немного треков из персональных подборок, чтобы подмешать в добор.

        Подборки тянутся с сети дорого (несколько плейлистов), поэтому
        держим пул и черпаем из него по чуть-чуть, перезапрашивая лишь когда
        он опустеет. Это и есть источник кросс-артистного разнообразия рядом
        с моно-артистным `related`.
        """
        if not self._selection_pool:
            fetched = self._safe_call(self.client.personal_selections) or []
            self._selection_pool = shuffled(fetched)
        take, self._selection_pool = self._selection_pool[:count], self._selection_pool[count:]
        return take

    def _take_likes(self, count: int) -> List[Track]:
        """Немного случайных лайков прямо в пул добора — гарантированная
        инъекция разнообразия рядом с моно-артистным `related`. Крутится по
        кэшу лайков по кругу (лайки не «расходуются»: их приятно встречать в
        волне не по разу)."""
        with self._lock:
            pool = self._like_pool
        if not pool:
            return []
        return random.sample(pool, min(count, len(pool)))

    def _fetch_more(self) -> None:
        """Дозаправляет очередь разнообразным пулом.

        Пул собирается из трёх источников с разной шириной вкуса: `related`
        нескольких сидов (недавнее + случайные лайки), персональные подборки и
        прямая подмешка лайков. Всё перемешивается и кладётся через `_enqueue`
        со строгим оконным лимитом на артиста; если строгим не влезло ничего
        (вся лента — один артист), повторяем без оконного лимита, чтобы волна
        не встала.
        """
        self._ensure_like_pool()

        pool: List[Track] = []
        for seed_id in self._pick_seeds():
            related = self._safe_call(self.client.related, seed_id, RELATED_LIMIT)
            if related:
                pool.extend(related)

        pool.extend(self._take_selections(SELECTION_MIX))
        pool.extend(self._take_likes(LIKE_MIX))

        if pool:
            random.shuffle(pool)
            if self._enqueue(pool, strict=True):
                return
            # Строгий фильтр всё отсёк (моно-артист) — пускаем тот же пул мягче.
            if self._enqueue(pool, strict=False):
                return

        # Холодный старт (ни сидов, ни лайков, related пуст): соседи из истории
        # прослушиваний SoundCloud.
        history = self._safe_call(self.client.play_history) or []
        cold_pool: List[Track] = []
        for track in history[:5]:
            self._remember_seed(track)
            related = self._safe_call(self.client.related, track.id, RELATED_LIMIT)
            if related:
                cold_pool.extend(related)
        if cold_pool:
            random.shuffle(cold_pool)
            if self._enqueue(cold_pool, strict=False):
                return

        likes = self._safe_call(self.client.liked_tracks)
        if likes:
            self._enqueue(shuffled(likes), strict=False)

    def _advance(self) -> None:
        with self._lock:
            self._advance_token += 1
            token = self._advance_token
            has_forward_history = self._position < len(self._played) - 1
        if has_forward_history:
            with self._lock:
                self._position += 1
                track = self._played[self._position]
            if self._play_track(track, record_history=False, token=token):
                return
            # Трек из истории больше не проигрывается (например, ссылка протухла) —
            # убираем его из истории (иначе зациклимся на нём) и берём свежий трек.
            with self._lock:
                del self._played[self._position]
                self._position -= 1

        if not self._queue:
            self._fetch_more()

        with self._lock:
            if not self._queue:
                logger.warning("Очередь волны пуста, играть нечего")
                self._state = "stopped"
                self._current = None
                self.on_track_change(None)
                return
            track = self._pop_next_diverse()

        if not self._play_track(track, record_history=True, token=token):
            logger.warning("Failed to load track %s, skipping", track.id)
            self._advance()

    def _pop_next_diverse(self) -> Track:
        """Достаёт из очереди трек, не повторяющий недавно игравших артистов.

        Кап в очереди уже не даёт одному артисту накопиться, но два его трека
        всё же могут встать рядом; здесь берём первый трек, чьего артиста не
        было среди последних `ARTIST_COOLDOWN`, и лишь если таких нет —
        обычный первый. Вызывается под `self._lock`.

        `_recent_artists` хранит целое окно `ARTIST_WINDOW` (для оконного
        лимита в `_enqueue`), поэтому для кулдауна берём только его хвост —
        иначе на длинной сессии отсеются почти все.
        """
        cooldown = set(list(self._recent_artists)[-ARTIST_COOLDOWN:])
        for i, track in enumerate(self._queue):
            if track.artist and track.artist in cooldown:
                continue
            return self._queue.pop(i)
        return self._queue.pop(0)

    def _play_track(self, track: Track, record_history: bool, token: int) -> bool:
        """Грузит и проигрывает трек. `token` — фиксирует "поколение" запроса:

        если за время (медленной, сетевой) загрузки next()/previous() успели
        вызвать ещё раз, `_advance_token` уже уйдёт вперёд, и этот результат
        тихо отбрасывается вместо перезаписи состояния более свежим запросом.
        """
        data = self._load_track(track)
        if not data:
            return False

        with self._lock:
            if token != self._advance_token:
                logger.info("Dropping stale playback result for track %s (superseded)", track.id)
                return True

            self._current = track
            self._current_pcm = data
            if record_history:
                self._played.append(track)
                self._position = len(self._played) - 1
                if track.artist:
                    self._recent_artists.append(track.artist)
            queue_low = len(self._queue) <= PREFETCH_THRESHOLD

        self._play_from(0.0)

        if record_history:
            self._remember_seed(track)
            threading.Thread(
                target=self._safe_call, args=(self.client.report_play, track), daemon=True
            ).start()
        self.on_track_change(track)

        if record_history and queue_low:
            self._prefetch_more_async()

        return True

    def _prefetch_more_async(self) -> None:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            return
        self._prefetch_thread = threading.Thread(target=self._fetch_more, daemon=True)
        self._prefetch_thread.start()

    def _elapsed_seconds(self) -> float:
        if self._state == "playing":
            return self._played_seconds + (time.time() - self._playing_since) * self._speed
        return self._played_seconds

    def _duration_seconds(self) -> float:
        track = self._current
        if track is None:
            return 0.0
        # Длительность считаем по фактически декодированному PCM: у SoundCloud
        # duration в метаданных нередко расходится с реальным транскодингом
        # (особенно у progressive-mp3), а перемотка должна попадать точно.
        with self._lock:
            data = self._current_pcm
        if data:
            return pcm.bytes_to_seconds(len(data))
        return track.duration_seconds

    def _monitor_loop(self) -> None:
        while self._running:
            time.sleep(POLL_INTERVAL)
            with self._lock:
                state = self._state
            if state != "playing":
                continue
            if self._channel.get_busy():
                continue
            self._advance()
