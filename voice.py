"""голос: распознавание речи (faster-whisper) и озвучка (edge-tts)."""

import asyncio
import os
import re
import threading
from pathlib import Path

TTS_VOICE = os.getenv("TTS_VOICE", "ru-RU-SvetlanaNeural")  # женский русский голос
TTS_RATE = os.getenv("TTS_RATE", "+0%")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
MAX_TTS_CHARS = int(os.getenv("MAX_TTS_CHARS", "1500"))

_model = None
_model_lock = threading.Lock()


# ---------- распознавание ----------

def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
    return _model


def _decode(path: str):
    """декодирует любой аудио/видео файл в float32 16 кГц моно — без ffmpeg-бинарника."""
    import av
    import numpy as np

    container = av.open(path)
    try:
        if not container.streams.audio:
            return np.zeros(0, dtype=np.float32)
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
        chunks = []
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                chunks.append(resampled.to_ndarray().reshape(-1))
        for resampled in resampler.resample(None):
            chunks.append(resampled.to_ndarray().reshape(-1))
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks).astype(np.float32)
    finally:
        container.close()


def _transcribe_sync(path: str) -> str:
    audio = _decode(path)
    if audio.size < 1600:  # меньше 0.1 секунды
        return ""
    segments, _info = _get_model().transcribe(
        audio,
        language="ru",
        vad_filter=True,
        beam_size=5,
        condition_on_previous_text=False,
    )
    return " ".join(segment.text.strip() for segment in segments).strip()


async def transcribe(path) -> str:
    return await asyncio.to_thread(_transcribe_sync, str(path))


async def warmup():
    """заранее загрузить модель, чтобы первое голосовое не ждало скачивания."""
    try:
        await asyncio.to_thread(_get_model)
        print("whisper готов")
    except Exception as e:
        print("whisper warmup error", e)


# ---------- озвучка ----------

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿️‍⃣]+"
)
URL_RE = re.compile(r"https?://\S+|www\.\S+")
CODE_BLOCK_RE = re.compile(r"```.*?```", re.S)
INLINE_CODE_RE = re.compile(r"`([^`]*)`")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
MD_CHARS_RE = re.compile(r"[*_#>~|•·]+")
KAOMOJI = (":3", "^-^", ">_<", ">//<", "uwu", "uwp", ":p", ":d", "xd", "^^", "♡")


def clean_for_speech(text: str) -> str:
    """убирает всё, что плохо звучит вслух: код, разметку, смайлы, ссылки."""
    text = CODE_BLOCK_RE.sub(" Код пропущен. ", text)
    text = IMAGE_RE.sub(" ", text)
    text = MD_LINK_RE.sub(r"\1", text)
    text = INLINE_CODE_RE.sub(r"\1", text)
    text = URL_RE.sub(" ссылка ", text)
    text = EMOJI_RE.sub("", text)
    for token in KAOMOJI:
        text = re.sub(re.escape(token), "", text, flags=re.I)
    text = MD_CHARS_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", ". ", text)
    text = text.strip(" .\n\t")
    if len(text) > MAX_TTS_CHARS:
        text = text[:MAX_TTS_CHARS].rsplit(" ", 1)[0] + "…"
    return text


def _mp3_to_ogg(src: Path, dst: Path):
    """перекодирует mp3 в ogg/opus, который telegram принимает как голосовое."""
    import av

    container = av.open(str(src))
    try:
        output = av.open(str(dst), "w", format="ogg")
        try:
            stream = output.add_stream("libopus", rate=48000)
            stream.layout = "mono"
            stream.bit_rate = 48000
            resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
            for frame in container.decode(audio=0):
                for resampled in resampler.resample(frame):
                    for packet in stream.encode(resampled):
                        output.mux(packet)
            for resampled in resampler.resample(None):
                for packet in stream.encode(resampled):
                    output.mux(packet)
            for packet in stream.encode(None):
                output.mux(packet)
        finally:
            output.close()
    finally:
        container.close()


async def _edge_save(text: str, dst: Path):
    import edge_tts

    last_error = None
    for attempt in range(3):
        try:
            communicate = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE)
            await communicate.save(str(dst))
            if dst.exists() and dst.stat().st_size > 0:
                return
            raise RuntimeError("пустой аудиофайл")
        except Exception as e:
            last_error = e
            await asyncio.sleep(0.5 * (attempt + 1))
    raise last_error


async def synthesize(text: str, out_dir) -> Path | None:
    """озвучивает текст и возвращает путь к ogg/opus (или None, если озвучивать нечего)."""
    clean = clean_for_speech(text)
    if not clean:
        return None
    out_dir = Path(out_dir)
    mp3 = out_dir / "reply.mp3"
    ogg = out_dir / "reply.ogg"
    await _edge_save(clean, mp3)
    await asyncio.to_thread(_mp3_to_ogg, mp3, ogg)
    mp3.unlink(missing_ok=True)
    return ogg
