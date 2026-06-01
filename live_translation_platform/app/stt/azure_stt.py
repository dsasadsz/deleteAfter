import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.realtime.metrics import milliseconds_between
from app.stt.base import STTEvent, STTProvider


class AzureSpeechConfigurationError(RuntimeError):
    pass


class AzureSpeechConnectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AzureProviderError:
    reason: str
    message: str
    raw: dict


class AzureSTTProvider(STTProvider):
    name = "azure"

    def __init__(
        self,
        api_key: str,
        region: str,
        language: str = "ru-RU",
        sample_rate: int = 16000,
        bits_per_sample: int = 16,
        channels: int = 1,
        enable_partials: bool = True,
        initial_silence_timeout_ms: int = 5000,
        segmentation_silence_timeout_ms: int = 800,
        profanity: str = "Masked",
        use_phrase_list: bool = True,
        speechsdk: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self.region = region
        self.language = language
        self.sample_rate = sample_rate
        self.bits_per_sample = bits_per_sample
        self.channels = channels
        self.enable_partials = enable_partials
        self.initial_silence_timeout_ms = initial_silence_timeout_ms
        self.segmentation_silence_timeout_ms = segmentation_silence_timeout_ms
        self.profanity = profanity
        self.use_phrase_list = use_phrase_list
        self._speechsdk = speechsdk
        self._events: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._push_stream: Any | None = None
        self._recognizer: Any | None = None
        self._closed = False
        self._latest_audio_received_at: datetime | None = None
        self.connected_at: datetime | None = None
        self.session_started_at: datetime | None = None
        self.session_stopped_at: datetime | None = None
        self.audio_chunks_sent = 0
        self.audio_bytes_sent = 0
        self.partial_events_received = 0
        self.final_events_received = 0
        self.no_match_count = 0
        self.canceled_count = 0
        self.errors_count = 0
        self.last_event_at: datetime | None = None
        self.last_error: str | None = None
        self.last_transcript: str | None = None
        self._latencies_ms: list[int] = []

    @property
    def azure_connected_at(self) -> datetime | None:
        return self.connected_at

    @property
    def azure_audio_chunks_sent(self) -> int:
        return self.audio_chunks_sent

    @property
    def azure_audio_bytes_sent(self) -> int:
        return self.audio_bytes_sent

    @property
    def azure_partial_events_received(self) -> int:
        return self.partial_events_received

    @property
    def azure_final_events_received(self) -> int:
        return self.final_events_received

    @property
    def azure_no_match_count(self) -> int:
        return self.no_match_count

    @property
    def azure_canceled_count(self) -> int:
        return self.canceled_count

    @property
    def azure_session_started_at(self) -> datetime | None:
        return self.session_started_at

    @property
    def azure_session_stopped_at(self) -> datetime | None:
        return self.session_stopped_at

    @property
    def azure_last_event_at(self) -> datetime | None:
        return self.last_event_at

    @property
    def azure_last_error(self) -> str | None:
        return self.last_error

    @property
    def stt_provider_latency_ms(self) -> float:
        return round(sum(self._latencies_ms) / len(self._latencies_ms), 1) if self._latencies_ms else 0.0

    async def connect(self) -> None:
        if not self.api_key:
            raise AzureSpeechConfigurationError("Missing AZURE_SPEECH_KEY for STT_PROVIDER=azure.")
        if not self.region:
            raise AzureSpeechConfigurationError("Missing AZURE_SPEECH_REGION for STT_PROVIDER=azure.")
        self._loop = asyncio.get_running_loop()
        speechsdk = self._speechsdk or _import_speechsdk()
        try:
            speech_config = speechsdk.SpeechConfig(subscription=self.api_key, region=self.region)
            speech_config.speech_recognition_language = self.language
            self._set_sdk_properties(speechsdk, speech_config)
            stream_format = speechsdk.audio.AudioStreamFormat(
                samples_per_second=self.sample_rate,
                bits_per_sample=self.bits_per_sample,
                channels=self.channels,
            )
            self._push_stream = speechsdk.audio.PushAudioInputStream(stream_format=stream_format)
            audio_config = speechsdk.audio.AudioConfig(stream=self._push_stream)
            self._recognizer = speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=audio_config)
            self._connect_callbacks()
            await asyncio.to_thread(self._recognizer.start_continuous_recognition_async().get)
        except Exception as exc:
            self.last_error = f"Azure Speech connection failed: {exc}"
            raise AzureSpeechConnectionError(self.last_error) from exc
        self.connected_at = datetime.utcnow()
        self._closed = False

    async def send_audio(self, audio_chunk: bytes, metadata: dict | None = None) -> None:
        if self._push_stream is None:
            raise AzureSpeechConnectionError("Azure Speech push stream is not connected.")
        metadata = metadata or {}
        self._latest_audio_received_at = metadata.get("audio_received_at") or datetime.utcnow()
        sample_rate = metadata.get("sample_rate") or self.sample_rate
        channels = metadata.get("channels") or self.channels
        audio_format = metadata.get("format") or "L16"
        if sample_rate not in {8000, 16000} or sample_rate != self.sample_rate or channels != self.channels or audio_format not in {"L16", "pcm_s16le", "pcm_16000"}:
            self.last_error = (
                "Audio format warning: Azure STT expects PCM signed int16 mono at "
                f"{self.sample_rate} Hz; got sample_rate={sample_rate}, channels={channels}, format={audio_format}. "
                "Stage 6A does not resample."
            )
        await asyncio.to_thread(self._push_stream.write, audio_chunk)
        self.audio_chunks_sent += 1
        self.audio_bytes_sent += len(audio_chunk)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                break
            yield event

    async def close(self) -> None:
        self._closed = True
        if self._recognizer is not None:
            try:
                await asyncio.to_thread(self._recognizer.stop_continuous_recognition_async().get)
            except Exception as exc:
                self._record_error(f"Azure Speech stop failed: {exc}")
        if self._push_stream is not None:
            try:
                self._push_stream.close()
            except Exception as exc:
                self._record_error(f"Azure Speech stream close failed: {exc}")
        await self._events.put(None)

    def _connect_callbacks(self) -> None:
        self._recognizer.recognizing.connect(self._on_recognizing)
        self._recognizer.recognized.connect(self._on_recognized)
        self._recognizer.canceled.connect(self._on_canceled)
        self._recognizer.session_started.connect(self._on_session_started)
        self._recognizer.session_stopped.connect(self._on_session_stopped)

    def _on_recognizing(self, evt: Any) -> None:
        if not self.enable_partials:
            return
        event = parse_azure_recognizing_event(evt, self._latest_audio_timestamp(), self.language)
        if event is None:
            return
        self.partial_events_received += 1
        self._record_event(event)

    def _on_recognized(self, evt: Any) -> None:
        event = parse_azure_recognized_event(evt, self._latest_audio_timestamp(), self.language)
        if event is None:
            self.no_match_count += 1
            return
        self.final_events_received += 1
        self._record_event(event)

    def _on_canceled(self, evt: Any) -> None:
        error = parse_azure_canceled_event(evt)
        self.canceled_count += 1
        self._record_error(error.message or error.reason)

    def _on_session_started(self, evt: Any) -> None:
        self.session_started_at = datetime.utcnow()

    def _on_session_stopped(self, evt: Any) -> None:
        self.session_stopped_at = datetime.utcnow()

    def _record_event(self, event: STTEvent) -> None:
        self.last_event_at = event.timestamp
        self.last_transcript = event.text
        if event.audio_received_at is not None:
            self._latencies_ms.append(milliseconds_between(event.audio_received_at, event.timestamp))
        if self._loop and not self._closed:
            self._loop.call_soon_threadsafe(self._events.put_nowait, event)

    def _record_error(self, message: str) -> None:
        self.errors_count += 1
        self.last_error = message

    def _latest_audio_timestamp(self) -> datetime:
        return self._latest_audio_received_at or datetime.utcnow()

    def _set_sdk_properties(self, speechsdk: Any, speech_config: Any) -> None:
        property_id = getattr(speechsdk, "PropertyId", None)
        if property_id is None:
            return
        _safe_set_property(
            speech_config,
            getattr(property_id, "SpeechServiceConnection_InitialSilenceTimeoutMs", None),
            str(self.initial_silence_timeout_ms),
        )
        _safe_set_property(
            speech_config,
            getattr(property_id, "Speech_SegmentationSilenceTimeoutMs", None),
            str(self.segmentation_silence_timeout_ms),
        )
        _safe_set_property(
            speech_config,
            getattr(property_id, "SpeechServiceResponse_ProfanityOption", None),
            self.profanity,
        )


def parse_azure_recognizing_event(evt: Any, audio_received_at: datetime, language: str) -> STTEvent | None:
    text = _result_text(evt)
    if not text:
        return None
    return STTEvent(
        text=text,
        is_partial=True,
        is_final=False,
        language=language,
        confidence=None,
        provider="azure",
        timestamp=datetime.utcnow(),
        speaker_id="teacher",
        raw=_recognition_raw(evt),
        audio_received_at=audio_received_at,
    )


def parse_azure_recognized_event(evt: Any, audio_received_at: datetime, language: str) -> STTEvent | None:
    text = _result_text(evt)
    reason = _enum_name(getattr(getattr(evt, "result", None), "reason", None))
    if not text or "NoMatch" in reason:
        return None
    return STTEvent(
        text=text,
        is_partial=False,
        is_final=True,
        language=language,
        confidence=None,
        provider="azure",
        timestamp=datetime.utcnow(),
        speaker_id="teacher",
        raw=_recognition_raw(evt),
        audio_received_at=audio_received_at,
    )


def parse_azure_canceled_event(evt: Any) -> AzureProviderError:
    reason = _enum_name(getattr(evt, "reason", ""))
    details = getattr(evt, "error_details", "") or getattr(getattr(evt, "result", None), "error_details", "")
    return AzureProviderError(
        reason=reason,
        message=str(details or reason or "Azure Speech recognition canceled."),
        raw={"reason": reason, "error_details": str(details or ""), "session_id": getattr(evt, "session_id", None)},
    )


def _recognition_raw(evt: Any) -> dict:
    result = getattr(evt, "result", None)
    return {
        "reason": _enum_name(getattr(result, "reason", None)),
        "duration": getattr(result, "duration", None),
        "offset": getattr(result, "offset", None),
        "json": getattr(result, "json", None),
        "session_id": getattr(evt, "session_id", None),
    }


def _result_text(evt: Any) -> str:
    result = getattr(evt, "result", None)
    return str(getattr(result, "text", "") or "").strip()


def _enum_name(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "name", value)).split(".")[-1]


def _safe_set_property(speech_config: Any, property_id: Any, value: str) -> None:
    if property_id is None:
        return
    try:
        speech_config.set_property(property_id, value)
    except Exception:
        return


def _import_speechsdk():
    try:
        import azure.cognitiveservices.speech as speechsdk
    except ImportError as exc:
        raise AzureSpeechConfigurationError(
            "azure-cognitiveservices-speech is required for STT_PROVIDER=azure. Install requirements.txt."
        ) from exc
    return speechsdk


AzureSTT = AzureSTTProvider
