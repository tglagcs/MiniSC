"""Единая точка декодирования аудио в сырой PCM и применения скорости/питча.

Раньше `flac-mp4` декодировался в WAV отдельным путём (см. историю lossless.py),
а mp3/flac играл через `pygame.mixer.music` напрямую из сжатых байт. Для
скорости воспроизведения это не годится — у `pygame.mixer.music` в принципе
нет способа поменять скорость. Поэтому теперь ЛЮБОЙ источник (mp3/flac/wav/
flac-mp4 — что угодно, что понимает PyAV) сначала декодируется в сырой PCM
одного и того же формата, а играется через `pygame.mixer.Sound`/`Channel`.

Скорость реализована "в лоб", как на старой кассете/пластинке: питч меняется
вместе со скоростью, а не сохраняется независимо (это специально — тут не
нужен полноценный time-stretch с фазовым вокодером). Технически это просто
ресемплинг: говорим ресемплеру "этот кусок звучал на частоте RATE*speed",
приводим к настоящей RATE — те же сэмплы читаются быстрее/медленнее, а вместе
с ними и питч едет пропорционально.
"""

import io

import av

RATE = 44100
CHANNELS = 2
SAMPLE_WIDTH = 2  # 16 бит
FRAME_SIZE = CHANNELS * SAMPLE_WIDTH

MIN_SPEED = 0.5
MAX_SPEED = 2.0


def _frame_bytes(frame) -> bytes:
    """Полезные байты packed-фрейма БЕЗ хвостового паддинга.

    `bytes(frame.planes[0])` отдаёт буфер плейна целиком, а FFmpeg выравнивает
    буферы — сверх реальных `frame.samples` может лежать мусор. У ВЫХОДНЫХ
    фреймов ресемплера это стабильно 128 байт на ~4608-байтовый фрейм: если
    склеивать плейны целиком, мусор вшивается в звук каждые ~26мс и слышится
    как постоянное потрескивание (так трещали все mp3/aac-треки).
    """
    bytes_per_frame = len(frame.layout.channels) * frame.format.bytes
    return bytes(frame.planes[0])[: frame.samples * bytes_per_frame]


def _resample_once(frame, out_format: str = "s16", out_layout: str = "stereo", out_rate: int = RATE) -> bytes:
    resampler = av.AudioResampler(format=out_format, layout=out_layout, rate=out_rate)
    chunks = [_frame_bytes(f) for f in resampler.resample(frame)]
    chunks += [_frame_bytes(f) for f in resampler.resample(None)]
    return b"".join(chunks)


def decode_to_pcm(data: bytes) -> bytes:
    """Декодирует произвольный контейнер (mp3/flac/wav/flac-mp4/…) в сырой
    PCM: s16, стерео, `RATE` Гц, интерливинг — то, что понимает
    `pygame.mixer.Sound(buffer=...)` при инициализированном с теми же
    параметрами микшере.

    Кодек декодирует поток кусками (например, FLAC — блоками по 4608
    сэмплов, mp3 — по 1152), но конвертация в целевой формат (в т.ч. смена
    битности — у lossless-мастеров нередко родной формат s32/24-бит, а не
    s16) делается ОДНИМ вызовом ресемплера над уже склеенным в единый буфер
    потоком, а не по кускам отдельно. Работает и для packed-форматов (один
    плейн с интерливингом), и для planar (mp3/aac декодируются в fltp —
    отдельный плейн на каждый канал).

    При склейке каждый плейн режется ровно по `samples` (см. `_frame_bytes`
    про паддинг FFmpeg) — и у входных фреймов тоже, иначе `planes[p].update()`
    падает с несовпадением размера.
    """
    container = av.open(io.BytesIO(data), mode="r")
    try:
        return _decode_container(container)
    finally:
        container.close()


def decode_url_to_pcm(url: str) -> bytes:
    """То же самое, но источник — ссылка, а не байты в памяти.

    Нужно для SoundCloud: лучшее доступное качество там отдаётся HLS-плейлистом
    (`.m3u8` + сегменты), собирать который вручную незачем — FFmpeg внутри PyAV
    умеет открыть его сам и по той же ссылке скачать сегменты. Прямой mp3
    (progressive-транскодинг) открывается этой же функцией.
    """
    # timeout щедрый специально: он тут не «сколько ждать ответа», а предохранитель
    # от намертво зависшего соединения — трек качается и декодируется целиком,
    # и на длинном треке через медленный канал 30 секунд легко не хватает.
    container = av.open(url, mode="r", timeout=120)
    try:
        return _decode_container(container)
    finally:
        container.close()


def _decode_container(container) -> bytes:
    stream = container.streams.audio[0]
    frames = list(container.decode(stream))
    if not frames:
        return b""

    first = frames[0]
    if first.format.is_planar:
        n_planes = len(first.layout.channels)
        plane_sample_bytes = first.format.bytes
    else:
        n_planes = 1
        plane_sample_bytes = len(first.layout.channels) * first.format.bytes

    total_samples = sum(f.samples for f in frames)
    combined = av.AudioFrame(format=first.format.name, layout=first.layout.name, samples=total_samples)
    combined.sample_rate = first.sample_rate
    for p in range(n_planes):
        raw = b"".join(bytes(f.planes[p])[: f.samples * plane_sample_bytes] for f in frames)
        combined.planes[p].update(raw)

    return _resample_once(combined)


def apply_speed(pcm: bytes, speed: float) -> bytes:
    """Возвращает PCM, проигранный на `speed`x — с естественным изменением питча.

    "Враньё" про частоту (RATE*speed вместо настоящей RATE) даёт ресемплеру
    задание физически прочитать те же сэмплы быстрее/медленнее — вместе с
    этим меняется и питч. Важно: весь кусок собирается в ОДИН AudioFrame и
    ресемплируется одним вызовом, а не по частям — если скормить ресемплеру
    несколько отдельных фреймов подряд (например, декодируя WAV кусками),
    на границах между ними появляются едва заметные щелчки, которые на
    заметно нестандартной скорости складываются в слышимое потрескивание.
    """
    if abs(speed - 1.0) < 1e-6:
        return pcm

    speed = max(MIN_SPEED, min(MAX_SPEED, speed))

    n_frames = len(pcm) // FRAME_SIZE
    pcm = pcm[: n_frames * FRAME_SIZE]
    if n_frames == 0:
        return pcm

    fake_rate = int(round(RATE * speed))
    frame = av.AudioFrame(format="s16", layout="stereo", samples=n_frames)
    frame.sample_rate = fake_rate
    frame.planes[0].update(pcm)

    resampled = _resample_once(frame)

    # У ресемплера есть небольшой "хвост" на флаше фильтра (лишние доли
    # процента длины) — обрезаем до математически ожидаемой длины, чтобы
    # длительность совпадала точно.
    expected_len = seconds_to_byte_offset(bytes_to_seconds(len(pcm)) / speed)
    if len(resampled) > expected_len:
        resampled = resampled[:expected_len]
    return resampled


def seconds_to_byte_offset(seconds: float) -> int:
    offset = int(seconds * RATE * FRAME_SIZE)
    return offset - (offset % FRAME_SIZE)


def bytes_to_seconds(n_bytes: int) -> float:
    return n_bytes / (RATE * FRAME_SIZE)
