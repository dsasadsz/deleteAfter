import json
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import LessonQuestion
from app.db.repositories import LessonQuestionRepository, LessonRepository
from app.questions.schemas import QuestionRead
from app.realtime.question_hub import QuestionHub
from app.translation.base import create_translation_provider


class QuestionService:
    def __init__(self, session_factory: sessionmaker[Session], hub: QuestionHub, settings: Settings) -> None:
        self.session_factory = session_factory
        self.hub = hub
        self.settings = settings

    async def create_text_question(
        self,
        lesson_id: str,
        *,
        text: str,
        source_language: str,
        student_id: str | None = None,
        student_name: str | None = None,
    ) -> LessonQuestion:
        started_at = datetime.utcnow()
        with self.session_factory() as session:
            lesson = LessonRepository(session).get(lesson_id)
            if lesson is None:
                raise ValueError("Lesson not found")
            translation_provider = lesson.translation_provider or self.settings.translation_provider
        translated = await self.translate_to_teacher_language(text, source_language, self.settings.student_question_translation_target, translation_provider)
        latency_ms = int((datetime.utcnow() - started_at).total_seconds() * 1000)
        with self.session_factory() as session:
            question = LessonQuestionRepository(session).create_question(
                LessonQuestion(
                    lesson_id=lesson_id,
                    student_id=student_id,
                    student_name=student_name,
                    input_type="text",
                    source_language=source_language,
                    original_text=text,
                    translated_text_ru=translated,
                    translation_provider=translation_provider,
                    latency_ms=latency_ms,
                    metadata_json="{}",
                )
            )
        await self.broadcast("question_created", question)
        return question

    async def create_voice_question_from_transcript(
        self,
        lesson_id: str,
        *,
        recognized_text: str,
        source_language: str,
        stt_provider: str,
        audio_duration_ms: int | None,
        metadata: dict | None = None,
        student_id: str | None = None,
        student_name: str | None = None,
    ) -> LessonQuestion:
        started_at = datetime.utcnow()
        with self.session_factory() as session:
            lesson = LessonRepository(session).get(lesson_id)
            if lesson is None:
                raise ValueError("Lesson not found")
            translation_provider = lesson.translation_provider or self.settings.translation_provider
        translated = await self.translate_to_teacher_language(recognized_text, source_language, self.settings.student_question_translation_target, translation_provider)
        latency_ms = int((datetime.utcnow() - started_at).total_seconds() * 1000)
        with self.session_factory() as session:
            question = LessonQuestionRepository(session).create_question(
                LessonQuestion(
                    lesson_id=lesson_id,
                    student_id=student_id,
                    student_name=student_name,
                    input_type="voice",
                    source_language=source_language,
                    original_text=recognized_text,
                    translated_text_ru=translated,
                    recognized_text=recognized_text,
                    stt_provider=stt_provider,
                    translation_provider=translation_provider,
                    audio_duration_ms=audio_duration_ms,
                    latency_ms=latency_ms,
                    metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
                )
            )
        await self.broadcast("question_created", question)
        return question

    async def create_voice_question_error(
        self,
        lesson_id: str,
        *,
        error: str,
        code: str,
        source_language: str,
        stt_provider: str | None,
        audio_duration_ms: int | None,
        metadata: dict | None = None,
        student_id: str | None = None,
        student_name: str | None = None,
    ) -> LessonQuestion:
        with self.session_factory() as session:
            lesson = LessonRepository(session).get(lesson_id)
            if lesson is None:
                raise ValueError("Lesson not found")
            translation_provider = lesson.translation_provider or self.settings.translation_provider
            error_metadata = dict(metadata or {})
            error_metadata["stt_provider"] = stt_provider
            error_metadata["error"] = {
                "code": code,
                "message": error,
                "detail": (metadata or {}).get("error_detail") or error,
            }
            question = LessonQuestionRepository(session).create_question(
                LessonQuestion(
                    lesson_id=lesson_id,
                    student_id=student_id,
                    student_name=student_name,
                    input_type="voice",
                    source_language=source_language,
                    original_text="",
                    translated_text_ru="",
                    recognized_text=None,
                    status="error",
                    stt_provider=stt_provider,
                    translation_provider=translation_provider,
                    audio_duration_ms=audio_duration_ms,
                    latency_ms=None,
                    error=error,
                    metadata_json=json.dumps(error_metadata, ensure_ascii=False),
                )
            )
        return question

    async def translate_to_teacher_language(self, text: str, source_language: str, teacher_language: str = "ru", provider_name: str | None = None) -> str:
        if source_language in {teacher_language, "ru", "ru-RU"}:
            return text
        provider_name = provider_name or self.settings.translation_provider
        translator = create_translation_provider(
            provider_name,
            api_key=self.settings.azure_translator_key,
            region=self.settings.azure_translator_region,
            endpoint=self.settings.azure_translator_endpoint,
            api_version=self.settings.azure_translator_api_version,
        )
        translations = await translator.translate_many(text, source_language, [teacher_language])
        return translations.get(teacher_language, text)

    async def mark_answered(self, question_id: int) -> LessonQuestion | None:
        with self.session_factory() as session:
            question = LessonQuestionRepository(session).mark_answered(question_id)
        if question is not None:
            await self.broadcast("question_answered", question)
        return question

    async def dismiss(self, question_id: int) -> LessonQuestion | None:
        with self.session_factory() as session:
            question = LessonQuestionRepository(session).dismiss(question_id)
        if question is not None:
            await self.broadcast("question_dismissed", question)
        return question

    async def broadcast(self, event: str, question: LessonQuestion) -> None:
        await self.hub.broadcast(question.lesson_id, {"event": event, "lesson_id": question.lesson_id, "question": question_to_read(question).model_dump(mode="json")})

    async def broadcast_error(self, lesson_id: str, error: str, code: str = "question_error", question: LessonQuestion | None = None) -> None:
        payload = {"event": "question_error", "lesson_id": lesson_id, "code": code, "error": error}
        if question is not None:
            payload["question"] = question_to_read(question).model_dump(mode="json")
        await self.hub.broadcast(lesson_id, payload)


def question_to_read(question: LessonQuestion) -> QuestionRead:
    return QuestionRead(
        id=question.id,
        lesson_id=question.lesson_id,
        student_id=question.student_id,
        student_name=question.student_name,
        input_type=question.input_type,
        source_language=question.source_language,
        original_text=question.original_text,
        translated_text_ru=question.translated_text_ru,
        recognized_text=question.recognized_text,
        status=question.status,
        stt_provider=question.stt_provider,
        translation_provider=question.translation_provider,
        audio_duration_ms=question.audio_duration_ms,
        latency_ms=question.latency_ms,
        error=question.error,
        metadata_json=question.metadata_json,
        created_at=question.created_at,
        answered_at=question.answered_at,
        dismissed_at=question.dismissed_at,
    )
