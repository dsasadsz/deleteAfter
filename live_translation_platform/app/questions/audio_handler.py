import asyncio
import json

from fastapi import WebSocket, WebSocketDisconnect

from app.config import Settings
from app.questions.service import QuestionService, question_to_read
from app.security.rate_limit import RATE_LIMIT_MESSAGE, check_rate_limit, rate_limit_key, subject_for_websocket
from app.stt.base import create_stt_provider


class VoiceQuestionError(Exception):
    def __init__(self, code: str, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or message


class StudentQuestionAudioHandler:
    def __init__(self, service: QuestionService, settings: Settings, rate_limiter=None) -> None:
        self.service = service
        self.settings = settings
        self.rate_limiter = rate_limiter

    async def handle(self, lesson_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        if await self._rate_limited(lesson_id, websocket):
            return
        if not self.settings.student_question_audio_enabled:
            await self._fail_and_close(
                lesson_id,
                websocket,
                VoiceQuestionError("audio_disabled", "Student question audio is disabled"),
                {},
                [],
                0,
            )
            return
        metadata: dict = {"source_language": "auto", "chunk_ms": 100}
        chunks: list[bytes] = []
        total_bytes = 0
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                if message.get("text") is not None:
                    payload = json.loads(message["text"])
                    event = payload.get("event") or payload.get("type")
                    if event == "question_audio_metadata":
                        metadata.update({key: value for key, value in payload.items() if key != "event"})
                        continue
                    if event == "finish_question":
                        await self._finalize(lesson_id, websocket, metadata, chunks, total_bytes)
                        return
                if message.get("bytes") is not None:
                    next_total_bytes = total_bytes + len(message["bytes"])
                    if next_total_bytes > self.settings.student_question_max_audio_bytes:
                        raise VoiceQuestionError(
                            "audio_too_large",
                            f"Student question audio bytes limit exceeded ({self.settings.student_question_max_audio_bytes} bytes).",
                        )
                    if len(chunks) >= self.settings.student_question_max_queue_size:
                        raise VoiceQuestionError("audio_queue_limit", "Student question audio queue limit exceeded")
                    chunks.append(message["bytes"])
                    total_bytes = next_total_bytes
                    if self._duration_ms(metadata, chunks) > self.settings.student_question_max_duration_seconds * 1000:
                        raise VoiceQuestionError("audio_too_long", "Student question audio duration limit exceeded")
        except WebSocketDisconnect:
            return
        except VoiceQuestionError as exc:
            await self._fail_and_close(lesson_id, websocket, exc, metadata, chunks, total_bytes)
        except Exception as exc:
            await self._fail_and_close(
                lesson_id,
                websocket,
                VoiceQuestionError("provider_error", "Student question STT provider disconnected.", detail=str(exc)),
                metadata,
                chunks,
                total_bytes,
            )

    async def _finalize(self, lesson_id: str, websocket: WebSocket, metadata: dict, chunks: list[bytes], total_bytes: int) -> None:
        provider_name = self.settings.student_question_stt_provider
        stt_kwargs = self._stt_kwargs(provider_name, metadata)
        if stt_kwargs.get("language") or stt_kwargs.get("source_language"):
            metadata["stt_language"] = stt_kwargs.get("language") or stt_kwargs.get("source_language")
        provider = create_stt_provider(provider_name, **stt_kwargs)
        metadata["stt_provider"] = provider.name
        try:
            recognized_text = await asyncio.wait_for(
                self._transcribe(provider, chunks, metadata),
                timeout=self.settings.student_question_stt_total_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            self._record_provider_error(metadata, provider)
            raise VoiceQuestionError("stt_total_timeout", "Student question transcription timed out.", detail=str(exc)) from exc
        except VoiceQuestionError as exc:
            raise self._with_provider_error(exc, metadata, provider) from exc
        except Exception as exc:
            self._record_provider_error(metadata, provider)
            raise VoiceQuestionError("provider_error", "Student question STT provider disconnected.", detail=str(exc)) from exc
        finally:
            await provider.close()
        question = await self.service.create_voice_question_from_transcript(
            lesson_id,
            recognized_text=recognized_text,
            source_language=metadata.get("source_language") or "auto",
            stt_provider=provider.name,
            audio_duration_ms=self._duration_ms(metadata, chunks),
            metadata={**metadata, "audio_bytes_received": total_bytes},
            student_id=metadata.get("student_id"),
            student_name=metadata.get("student_name"),
        )
        await websocket.send_json({"event": "question_created", "lesson_id": lesson_id, "question": question_to_read(question).model_dump(mode="json")})
        await websocket.close(code=1000)

    async def _transcribe(self, provider, chunks: list[bytes], metadata: dict) -> str:
        try:
            await asyncio.wait_for(provider.connect(), timeout=self.settings.student_question_stt_connect_timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise VoiceQuestionError("stt_connect_timeout", "Student question STT provider connection timed out.", detail=str(exc)) from exc
        for index, chunk in enumerate(chunks):
            chunk_metadata = {**metadata, "chunk_index": index, "chunks_total": len(chunks)}
            await provider.send_audio(chunk, metadata=chunk_metadata)
            if provider.name != "mock":
                await asyncio.sleep(0.01)
        if getattr(provider, "supports_commit", False):
            await provider.commit("student_question_finish")
        return await self._wait_for_final(provider)

    async def _wait_for_final(self, provider) -> str:
        timeout = self.settings.student_question_final_timeout_seconds
        deadline = asyncio.get_running_loop().time() + timeout
        events = provider.events().__aiter__()
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise VoiceQuestionError("stt_final_timeout", "Student question timed out waiting for final transcript.")
            try:
                event = await asyncio.wait_for(events.__anext__(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                detail = self._provider_timeout_detail(provider, str(exc))
                raise VoiceQuestionError("stt_final_timeout", "Student question timed out waiting for final transcript.", detail=detail) from exc
            except StopAsyncIteration as exc:
                detail = self._provider_timeout_detail(provider, "Student question ended without a final transcript.")
                raise VoiceQuestionError("stt_final_timeout", "Student question ended without a final transcript.", detail=detail) from exc
            if event.is_final and event.text:
                return event.text

    async def _fail_and_close(self, lesson_id: str, websocket: WebSocket, error: VoiceQuestionError, metadata: dict, chunks: list[bytes], total_bytes: int) -> None:
        error_metadata = {
            **metadata,
            "audio_bytes_received": total_bytes,
            "error_detail": error.detail,
        }
        question = await self.service.create_voice_question_error(
            lesson_id,
            error=error.message,
            code=error.code,
            source_language=metadata.get("source_language") or "auto",
            stt_provider=metadata.get("stt_provider") or self.settings.student_question_stt_provider,
            audio_duration_ms=self._duration_ms(metadata, chunks),
            metadata=error_metadata,
            student_id=metadata.get("student_id"),
            student_name=metadata.get("student_name"),
        )
        payload = {
            "event": "question_error",
            "lesson_id": lesson_id,
            "code": error.code,
            "error": error.message,
            "question": question_to_read(question).model_dump(mode="json"),
        }
        await self.service.broadcast_error(lesson_id, error.message, code=error.code, question=question)
        await websocket.send_json(payload)
        await websocket.close(code=1000)

    @staticmethod
    def _duration_ms(metadata: dict, chunks: list[bytes]) -> int:
        return int(len(chunks) * int(metadata.get("chunk_ms") or 100))

    def _stt_kwargs(self, provider_name: str, metadata: dict) -> dict:
        stt_language = self._stt_language(provider_name, metadata)
        if provider_name == "mock":
            return {"source_language": stt_language}
        if provider_name == "elevenlabs":
            return {
                "api_key": self.settings.elevenlabs_api_key,
                "model_id": self.settings.elevenlabs_stt_model,
                "language": stt_language,
                "audio_format": self.settings.elevenlabs_stt_audio_format,
                "sample_rate": int(metadata.get("sample_rate") or self.settings.elevenlabs_stt_sample_rate),
                "commit_strategy": "manual",
                "enable_partials": False,
                "connect_timeout_seconds": self.settings.elevenlabs_stt_connect_timeout_seconds,
                "receive_timeout_seconds": self.settings.elevenlabs_stt_receive_timeout_seconds,
                "max_reconnects": self.settings.elevenlabs_stt_max_reconnects,
            }
        if provider_name == "azure":
            return {
                "api_key": self.settings.azure_speech_key,
                "region": self.settings.azure_speech_region,
                "language": stt_language,
                "sample_rate": int(metadata.get("sample_rate") or self.settings.azure_speech_sample_rate),
                "bits_per_sample": self.settings.azure_speech_bits_per_sample,
                "channels": int(metadata.get("channels") or self.settings.azure_speech_channels),
                "enable_partials": False,
                "initial_silence_timeout_ms": self.settings.azure_speech_initial_silence_timeout_ms,
                "segmentation_silence_timeout_ms": self.settings.azure_speech_segmentation_silence_timeout_ms,
                "profanity": self.settings.azure_speech_profanity,
                "use_phrase_list": self.settings.azure_speech_use_phrase_list,
            }
        if provider_name == "cartesia":
            return {
                "api_key": self.settings.cartesia_api_key,
                "model": self.settings.cartesia_stt_model,
                "language": stt_language,
                "encoding": self.settings.cartesia_stt_encoding,
                "sample_rate": int(metadata.get("sample_rate") or self.settings.cartesia_stt_sample_rate),
                "enable_partials": False,
                "max_reconnects": self.settings.cartesia_stt_max_reconnects,
                "connect_timeout_seconds": self.settings.cartesia_stt_connect_timeout_seconds,
                "receive_timeout_seconds": self.settings.cartesia_stt_receive_timeout_seconds,
                "version": self.settings.cartesia_stt_version,
            }
        return {}

    def _stt_language(self, provider_name: str, metadata: dict) -> str:
        source_language = (metadata.get("source_language") or "auto").strip()
        if provider_name == "azure":
            azure_languages = {"ru": "ru-RU", "ru-RU": "ru-RU", "kk": "kk-KZ", "uz": "uz-UZ", "zh-Hans": "zh-CN"}
            return azure_languages.get(source_language, self.settings.azure_speech_language)
        if provider_name == "cartesia":
            cartesia_languages = {"ru-RU": "ru", "kk-KZ": "kk", "uz-UZ": "uz", "zh-CN": "zh"}
            return cartesia_languages.get(source_language, source_language if source_language != "auto" else self.settings.cartesia_stt_language)
        if provider_name == "elevenlabs":
            elevenlabs_languages = {"ru-RU": "ru", "kk-KZ": "kk", "uz-UZ": "uz", "zh-CN": "zh"}
            return elevenlabs_languages.get(source_language, source_language if source_language != "auto" else self.settings.elevenlabs_stt_language)
        return source_language

    def _with_provider_error(self, error: VoiceQuestionError, metadata: dict, provider) -> VoiceQuestionError:
        last_error = self._record_provider_error(metadata, provider)
        if not last_error or last_error in error.detail:
            return error
        return VoiceQuestionError(error.code, error.message, detail=f"{error.detail} Provider last error: {last_error}")

    @staticmethod
    def _record_provider_error(metadata: dict, provider) -> str | None:
        last_error = getattr(provider, "last_error", None)
        if last_error:
            metadata["provider_last_error"] = last_error
        return last_error

    def _provider_timeout_detail(self, provider, fallback: str) -> str:
        last_error = getattr(provider, "last_error", None)
        if last_error:
            return f"{fallback} Provider last error: {last_error}"
        return fallback

    async def _rate_limited(self, lesson_id: str, websocket: WebSocket) -> bool:
        if not self.settings.rate_limit_enabled or self.rate_limiter is None:
            return False
        subject = subject_for_websocket(websocket, lesson_id)
        key = rate_limit_key("question_voice", lesson_id, subject)
        result = await check_rate_limit(self.rate_limiter, key, self.settings.question_voice_rate_limit_per_minute)
        if result.allowed:
            return False
        await websocket.send_json(
            {
                "event": "question_error",
                "lesson_id": lesson_id,
                "code": "QUESTION_RATE_LIMITED",
                "error": RATE_LIMIT_MESSAGE,
                "retry_after_seconds": result.retry_after_seconds,
            }
        )
        await websocket.close(code=1000)
        return True
