import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from app.audio.base import AudioChunk, AudioSource


@dataclass(frozen=True)
class ChunkedWavSample:
    chunks: list[bytes]
    sample_rate: int
    channels: int
    sample_width: int
    warning: str | None

    @property
    def metadata(self) -> dict:
        return {"sample_rate": self.sample_rate, "channels": self.channels, "format": "L16"}


def chunk_wav_file(path: str | Path, chunk_ms: int) -> ChunkedWavSample:
    with wave.open(str(path), "rb") as reader:
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        sample_rate = reader.getframerate()
        frames = reader.readframes(reader.getnframes())

    bytes_per_second = max(1, sample_rate * channels * sample_width)
    chunk_size = max(1, int(bytes_per_second * chunk_ms / 1000))
    chunks = [frames[index : index + chunk_size] for index in range(0, len(frames), chunk_size) if frames[index : index + chunk_size]]
    warning = None
    if channels != 1 or sample_rate != 16000 or sample_width != 2:
        warning = (
            "Recommended WAV format is mono PCM 16kHz 16-bit. "
            f"Got channels={channels}, sample_rate={sample_rate}, sample_width={sample_width}."
        )
    return ChunkedWavSample(
        chunks=chunks,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        warning=warning,
    )


class StaticAudioSource(AudioSource):
    def __init__(self, name: str, chunks: list[AudioChunk]) -> None:
        self.name = name
        self._chunks = chunks
        self._closed = False

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        for chunk in self._chunks:
            if self._closed:
                break
            yield chunk

    async def close(self) -> None:
        self._closed = True
