from __future__ import annotations

import audioop
import shutil
import subprocess
import wave
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel


TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1
TARGET_SAMPLE_WIDTH = 2


class AudioNormalizationError(ValueError):
    pass


class NormalizedAudio(BaseModel):
    source_path: str
    pcm_path: Path
    wav_path: Path
    original_sample_rate: int | None = None
    original_channels: int | None = None
    original_sample_width: int | None = None
    sample_rate: int = TARGET_SAMPLE_RATE
    channels: int = TARGET_CHANNELS
    sample_width: int = TARGET_SAMPLE_WIDTH
    format: str = "pcm_s16le"
    duration_seconds: float
    decoder: str
    bytes: int


def normalize_lesson_audio(source_path: str | Path, *, output_dir: str | Path) -> NormalizedAudio:
    source = Path(source_path)
    if not source.exists():
        raise AudioNormalizationError(f"Audio file not found: {source}")
    if not source.is_file():
        raise AudioNormalizationError(f"Audio path is not a file: {source}")
    suffix = source.suffix.lower()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    stem = f"{source.stem}_{uuid4().hex[:8]}"
    pcm_path = output / f"{stem}.pcm"
    wav_path = output / f"{stem}.wav"
    if suffix == ".wav":
        return _normalize_wav_stdlib(source, pcm_path=pcm_path, wav_path=wav_path)
    if suffix == ".mp3":
        return _normalize_with_external_decoder(source, pcm_path=pcm_path, wav_path=wav_path)
    raise AudioNormalizationError("Only WAV and MP3 lesson audio files are supported.")


def decoder_status() -> dict:
    return {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "pydub": _pydub_available(),
    }


def _normalize_wav_stdlib(source: Path, *, pcm_path: Path, wav_path: Path) -> NormalizedAudio:
    try:
        with wave.open(str(source), "rb") as reader:
            original_channels = reader.getnchannels()
            original_width = reader.getsampwidth()
            original_rate = reader.getframerate()
            frames = reader.readframes(reader.getnframes())
    except wave.Error as exc:
        raise AudioNormalizationError(f"Invalid WAV audio: {exc}") from exc
    pcm = _to_16k_mono_pcm(
        frames,
        sample_rate=original_rate,
        channels=original_channels,
        sample_width=original_width,
    )
    _write_pcm_and_wav(pcm, pcm_path=pcm_path, wav_path=wav_path)
    return NormalizedAudio(
        source_path=str(source),
        pcm_path=pcm_path,
        wav_path=wav_path,
        original_sample_rate=original_rate,
        original_channels=original_channels,
        original_sample_width=original_width,
        duration_seconds=_duration_seconds(pcm),
        decoder="wave+audioop",
        bytes=len(pcm),
    )


def _normalize_with_external_decoder(source: Path, *, pcm_path: Path, wav_path: Path) -> NormalizedAudio:
    if shutil.which("ffmpeg"):
        return _normalize_with_ffmpeg(source, pcm_path=pcm_path, wav_path=wav_path)
    if _pydub_available():
        return _normalize_with_pydub(source, pcm_path=pcm_path, wav_path=wav_path)
    raise AudioNormalizationError("MP3 decoding requires ffmpeg or pydub. Install ffmpeg or add pydub with an available decoder.")


def _normalize_with_ffmpeg(source: Path, *, pcm_path: Path, wav_path: Path) -> NormalizedAudio:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-ac",
        str(TARGET_CHANNELS),
        "-ar",
        str(TARGET_SAMPLE_RATE),
        "-f",
        "s16le",
        str(pcm_path),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise AudioNormalizationError(f"ffmpeg failed to decode audio: {result.stderr.strip() or result.returncode}")
    pcm = pcm_path.read_bytes()
    _write_wav(pcm, wav_path)
    return NormalizedAudio(
        source_path=str(source),
        pcm_path=pcm_path,
        wav_path=wav_path,
        duration_seconds=_duration_seconds(pcm),
        decoder="ffmpeg",
        bytes=len(pcm),
    )


def _normalize_with_pydub(source: Path, *, pcm_path: Path, wav_path: Path) -> NormalizedAudio:
    try:
        from pydub import AudioSegment
    except Exception as exc:
        raise AudioNormalizationError("pydub is not available for MP3 decoding.") from exc
    try:
        audio = AudioSegment.from_file(str(source))
    except Exception as exc:
        raise AudioNormalizationError(f"pydub failed to decode audio: {exc}") from exc
    normalized = audio.set_frame_rate(TARGET_SAMPLE_RATE).set_channels(TARGET_CHANNELS).set_sample_width(TARGET_SAMPLE_WIDTH)
    pcm = bytes(normalized.raw_data)
    _write_pcm_and_wav(pcm, pcm_path=pcm_path, wav_path=wav_path)
    return NormalizedAudio(
        source_path=str(source),
        pcm_path=pcm_path,
        wav_path=wav_path,
        original_sample_rate=audio.frame_rate,
        original_channels=audio.channels,
        original_sample_width=audio.sample_width,
        duration_seconds=_duration_seconds(pcm),
        decoder="pydub",
        bytes=len(pcm),
    )


def _to_16k_mono_pcm(data: bytes, *, sample_rate: int, channels: int, sample_width: int) -> bytes:
    if sample_width <= 0:
        raise AudioNormalizationError("WAV sample width must be positive.")
    pcm = data
    if sample_width != TARGET_SAMPLE_WIDTH:
        pcm = audioop.lin2lin(pcm, sample_width, TARGET_SAMPLE_WIDTH)
        sample_width = TARGET_SAMPLE_WIDTH
    if channels == 2:
        pcm = audioop.tomono(pcm, sample_width, 0.5, 0.5)
        channels = 1
    elif channels != 1:
        raise AudioNormalizationError(f"Unsupported WAV channel count: {channels}")
    if sample_rate != TARGET_SAMPLE_RATE:
        pcm, _state = audioop.ratecv(pcm, sample_width, channels, sample_rate, TARGET_SAMPLE_RATE, None)
    return pcm


def _write_pcm_and_wav(pcm: bytes, *, pcm_path: Path, wav_path: Path) -> None:
    pcm_path.write_bytes(pcm)
    _write_wav(pcm, wav_path)


def _write_wav(pcm: bytes, wav_path: Path) -> None:
    with wave.open(str(wav_path), "wb") as writer:
        writer.setnchannels(TARGET_CHANNELS)
        writer.setsampwidth(TARGET_SAMPLE_WIDTH)
        writer.setframerate(TARGET_SAMPLE_RATE)
        writer.writeframes(pcm)


def _duration_seconds(pcm: bytes) -> float:
    bytes_per_second = TARGET_SAMPLE_RATE * TARGET_CHANNELS * TARGET_SAMPLE_WIDTH
    return round(len(pcm) / max(1, bytes_per_second), 3)


def _pydub_available() -> bool:
    try:
        import pydub  # noqa: F401
    except Exception:
        return False
    return True
