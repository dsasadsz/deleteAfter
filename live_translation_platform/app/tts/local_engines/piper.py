from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Protocol

from app.tts.base import TTSConfigurationError
from app.tts.local_engines.base import (
    LocalTTSEngine,
    LocalTTSSynthesisResult,
    audio_content_type,
    local_voice,
    sanitize_tts_error,
    unique,
    voice_env_suffix,
    voice_id_suffix,
)


class PiperRunner(Protocol):
    async def synthesize(self, *, text: str, language: str, voice_path: str, output_format: str) -> bytes:
        ...


class PiperSubprocessRunner:
    def __init__(self, bin_path: str) -> None:
        self.bin_path = bin_path

    async def synthesize(self, *, text: str, language: str, voice_path: str, output_format: str) -> bytes:
        suffix = ".wav" if (output_format or "wav").lower() == "wav" else f".{output_format}"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as output:
            output_path = output.name
        try:
            if self.bin_path.lower().endswith(".py"):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    self.bin_path,
                    "--model",
                    voice_path,
                    "--output_file",
                    output_path,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            elif self.bin_path.lower().endswith((".bat", ".cmd")):
                command = subprocess.list2cmdline([self.bin_path, "--model", voice_path, "--output_file", output_path])
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    self.bin_path,
                    "--model",
                    voice_path,
                    "--output_file",
                    output_path,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            _stdout, stderr = await process.communicate(text.encode("utf-8"))
            if process.returncode != 0:
                message = stderr.decode("utf-8", errors="replace").strip() or f"piper exited with status {process.returncode}"
                raise TTSConfigurationError(message)
            return Path(output_path).read_bytes()
        finally:
            try:
                os.remove(output_path)
            except FileNotFoundError:
                pass


class PiperTTSEngine(LocalTTSEngine):
    name = "piper"

    def __init__(
        self,
        *,
        enabled: bool = True,
        bin_path: str = "",
        voices: dict[str, str] | None = None,
        default_voice: str = "",
        timeout_seconds: float = 5.0,
        output_format: str = "wav",
        runner: PiperRunner | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.bin_path = bin_path or ""
        self.voices = {language: path for language, path in (voices or {}).items() if path}
        self.default_voice = default_voice or ""
        self.timeout_seconds = float(timeout_seconds or 5.0)
        self.output_format = output_format or "wav"
        self._runner = runner
        self._last_error: str | None = None
        self.request_count = 0
        self.error_count = 0
        self.timeout_count = 0
        self._latencies_ms: list[float] = []
        self._last_success_at: datetime | None = None

    def supports(self, language: str) -> bool:
        return bool(self.status_for_language(language).get("ready"))

    def default_voice_for_language(self, language: str) -> str:
        return f"piper-{voice_id_suffix(language)}" if self._voice_path_for_language(language) else ""

    def voice_catalog(self) -> dict[str, list[dict]]:
        catalog: dict[str, list[dict]] = {}
        for language in sorted(set(self.voices) | ({"ru", "kk", "uz", "zh-Hans"} if self.default_voice else set())):
            voice_id = self.default_voice_for_language(language)
            if voice_id:
                catalog[language] = [local_voice(language, self.name, voice_id)]
        return catalog

    def status(self) -> dict:
        missing = []
        if self.enabled and not self.bin_path:
            missing.append("PIPER_BIN_PATH")
        if not self.enabled:
            status = "disabled"
        elif missing:
            status = "not_configured"
        elif self._last_error:
            status = "error"
        else:
            status = "ready"
        ready = self.enabled and not missing and not self._last_error
        return {
            "ready": ready,
            "status": status,
            "enabled": self.enabled,
            "missing": missing,
            "engine": self.name,
            "timeout_seconds": self.timeout_seconds,
            "output_format": self.output_format,
            "content_type": audio_content_type(self.output_format),
            "last_error": self._last_error,
            "request_count": self.request_count,
            "error_count": self.error_count,
            "timeout_count": self.timeout_count,
            "average_latency_ms": self.average_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "last_success_at": self._last_success_at.isoformat() if self._last_success_at else None,
            "configured_languages": sorted(self.voice_catalog()),
        }

    def status_for_language(self, language: str) -> dict:
        status = self.status()
        missing = list(status["missing"])
        if self.enabled and not self._voice_path_for_language(language):
            missing.append(f"PIPER_VOICE_{voice_env_suffix(language)}")
        missing = unique(missing)
        ready = self.enabled and not missing and not self._last_error
        status.update(
            {
                "ready": ready,
                "status": "ready" if ready else ("disabled" if not self.enabled else "not_configured"),
                "missing": missing,
                "language": language,
            }
        )
        return status

    @property
    def average_latency_ms(self) -> float | None:
        if not self._latencies_ms:
            return None
        return sum(self._latencies_ms) / len(self._latencies_ms)

    @property
    def p95_latency_ms(self) -> float | None:
        if not self._latencies_ms:
            return None
        values = sorted(self._latencies_ms)
        index = max(0, min(len(values) - 1, int(round(0.95 * (len(values) - 1)))))
        return values[index]

    async def synthesize(self, text: str, language: str, voice: str | None = None, audio_format: str | None = None) -> LocalTTSSynthesisResult:
        status = self.status_for_language(language)
        if not status["ready"]:
            raise TTSConfigurationError(f"Piper TTS is not configured: {', '.join(status['missing']) or status['status']}")
        started_at = perf_counter()
        output_format = (audio_format or self.output_format or "wav").lower()
        voice_path = self._voice_path_for_language(language)
        if not voice_path:
            raise TTSConfigurationError(f"Piper voice is not configured for {language}")
        runner = self._runner or PiperSubprocessRunner(self.bin_path)
        try:
            audio = await asyncio.wait_for(
                runner.synthesize(text=text, language=language, voice_path=voice_path, output_format=output_format),
                timeout=self.timeout_seconds,
            )
            self._last_error = None
            self._last_success_at = datetime.utcnow()
            return LocalTTSSynthesisResult(audio_bytes=audio, content_type=audio_content_type(output_format), duration_ms=None)
        except asyncio.TimeoutError as exc:
            self.timeout_count += 1
            self.error_count += 1
            self._last_error = self._sanitize(f"Piper TTS timeout after {self.timeout_seconds:g}s")
            raise TTSConfigurationError(self._last_error) from exc
        except Exception as exc:
            self.error_count += 1
            self._last_error = self._sanitize(str(exc) or exc.__class__.__name__)
            raise TTSConfigurationError(self._last_error) from exc
        finally:
            self.request_count += 1
            self._latencies_ms.append((perf_counter() - started_at) * 1000)

    def _voice_path_for_language(self, language: str) -> str:
        return self.voices.get(language) or self.default_voice

    def _sanitize(self, message: object) -> str:
        return sanitize_tts_error(message, self.bin_path, self.default_voice, *self.voices.values())
