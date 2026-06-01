from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from app.api.questions import _enforce_question_rate_limit, _ensure_question_belongs_to_lesson
from app.api.tts import get_tts_audio, synthesize_tts_for_lesson, tts_status
from app.db.models import Lesson
from app.db.repositories import GlossaryRepository, LessonQuestionRepository, LessonRepository
from app.api.lessons import _ensure_rtms_experimental_enabled, _resolve_audio_source
from app.export.html_exporter import HTMLExporter
from app.export.markdown_exporter import MarkdownExporter
from app.export.srt_exporter import SRTExporter
from app.export.transcript_builder import TranscriptBuilder
from app.export.vtt_exporter import VTTExporter
from app.glossary.schemas import GlossaryCreate
from app.integration.auth import authorize_websocket_access, require_integration_key
from app.integration.spec import integration_spec
from app.integration.schemas import (
    IntegrationLessonCreate,
    IntegrationLessonResponse,
    IntegrationQuestionListResponse,
    IntegrationQuestionResponse,
    IntegrationRTMSActionResponse,
    IntegrationStatusResponse,
    IntegrationTextQuestionRequest,
)
from app.questions.audio_handler import StudentQuestionAudioHandler
from app.questions.service import question_to_read
from app.providers.quotas import enrich_provider_status
from app.schemas.rtms import RTMSStatus
from app.security.scopes import (
    AUDIO_WRITE,
    CAPTIONS_READ,
    DIAGNOSTICS_READ,
    QUESTION_MODERATE,
    QUESTION_READ,
    QUESTION_WRITE,
    STUDENT_TOKEN_SCOPES,
    TEACHER_TOKEN_SCOPES,
    TTS_PLAY,
    ZOOM_EMBED,
    validate_requested_scopes,
)
from app.security.schemas import StudentTokenRequest, StudentTokenResponse, TeacherTokenRequest, TeacherTokenResponse
from app.security.tokens import create_access_token, verify_access_token
from app.smoke.provider_status import provider_status
from app.tts.schemas import TTSSynthesizeRequest, TTSStatusResponse
from app.usage.cost_estimator import CostEstimator
from app.zoom.mock_zoom import MockZoomClient
from app.zoom.meeting_sdk import ZoomMeetingSDKConfigurationError
from app.zoom.zoom_api_client import ZoomAPIError
from app.zoom.zoom_oauth import ZoomCredentialsError, ZoomOAuthError

router = APIRouter(tags=["Integration Lessons"])
api_router = APIRouter(prefix="/api/v1/integration", dependencies=[Depends(require_integration_key)])


@api_router.get("/providers/status", tags=["Integration Providers"])
def integration_provider_status(request: Request) -> dict:
    return enrich_provider_status(provider_status(request.app.state.settings), request.app.state.settings, request.app)


@api_router.get("/spec", tags=["Integration Providers"])
def integration_machine_readable_spec() -> dict:
    return integration_spec()


@api_router.post("/lessons", response_model=IntegrationLessonResponse, status_code=201, tags=["Integration Lessons"])
async def create_integration_lesson(payload: IntegrationLessonCreate, request: Request) -> IntegrationLessonResponse:
    meeting = await _create_meeting(payload, request)
    audio_source = _resolve_audio_source(payload.mode, payload.audio_source, request.app.state.settings)
    lesson = Lesson(
        lesson_id=f"lesson_{uuid4().hex[:12]}",
        title=payload.title,
        mode=payload.mode,
        status="created",
        audio_source=audio_source,
        zoom_meeting_id=meeting.meeting_id,
        zoom_meeting_uuid=meeting.meeting_uuid,
        zoom_join_url=meeting.join_url,
        zoom_start_url=meeting.start_url,
        zoom_password=meeting.password,
        zoom_topic=meeting.topic,
        zoom_created_at=meeting.created_at,
        stt_provider=payload.stt_provider,
        translation_provider=payload.translation_provider,
        target_languages=",".join(payload.target_languages),
        glossary_id=payload.glossary_id,
        glossary_enabled=payload.glossary_enabled,
        rtms_status=RTMSStatus.WAITING_FOR_MEETING if payload.mode == "zoom" else RTMSStatus.NOT_CONFIGURED,
        browser_audio_status="waiting_for_teacher" if audio_source == "browser_ws" else "not_connected",
        external_lesson_id=payload.external_lesson_id,
        external_course_id=payload.external_course_id,
        external_teacher_id=payload.external_teacher_id,
        external_tenant_id=payload.external_tenant_id,
        callback_url=str(payload.callback_url) if payload.callback_url else None,
        integration_metadata_json=json.dumps(payload.integration_metadata, ensure_ascii=False),
    )
    with request.app.state.database.session_factory() as session:
        created = LessonRepository(session).create(lesson)
        return _lesson_response(created, request)


@api_router.get("/lessons/{lesson_id}", response_model=IntegrationLessonResponse, tags=["Integration Lessons"])
def get_integration_lesson(lesson_id: str, request: Request) -> IntegrationLessonResponse:
    return _lesson_response(_get_lesson(request, lesson_id), request)


@api_router.get("/lessons/by-external/{external_lesson_id}", response_model=IntegrationLessonResponse, tags=["Integration Lessons"])
def get_integration_lesson_by_external(external_lesson_id: str, request: Request) -> IntegrationLessonResponse:
    with request.app.state.database.session_factory() as session:
        lesson = LessonRepository(session).find_by_external_lesson_id(external_lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        return _lesson_response(lesson, request)


@api_router.post("/lessons/{lesson_id}/arm-rtms", response_model=IntegrationRTMSActionResponse, tags=["Integration Lessons"])
async def integration_arm_rtms(lesson_id: str, request: Request) -> IntegrationRTMSActionResponse:
    _ensure_rtms_experimental_enabled(request)
    with request.app.state.database.session_factory() as session:
        repo = LessonRepository(session)
        lesson = repo.get(lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        lesson = repo.set_rtms_armed(lesson_id, True, RTMSStatus.WAITING_FOR_MEETING) or lesson
    await request.app.state.caption_hub.broadcast_debug(
        lesson_id,
        {
            "event": "rtms_status",
            "version": "1.0",
            "lesson_id": lesson_id,
            "external_lesson_id": lesson.external_lesson_id,
            "status": lesson.rtms_status,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "payload": {"armed": True},
        },
    )
    return IntegrationRTMSActionResponse(lesson_id=lesson_id, rtms_status=lesson.rtms_status, armed=True)


@api_router.post("/lessons/{lesson_id}/disarm-rtms", response_model=IntegrationRTMSActionResponse, tags=["Integration Lessons"])
async def integration_disarm_rtms(lesson_id: str, request: Request) -> IntegrationRTMSActionResponse:
    _ensure_rtms_experimental_enabled(request)
    with request.app.state.database.session_factory() as session:
        repo = LessonRepository(session)
        lesson = repo.set_rtms_armed(lesson_id, False)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
    return IntegrationRTMSActionResponse(lesson_id=lesson_id, rtms_status=lesson.rtms_status, armed=False)


@api_router.post("/lessons/{lesson_id}/start", tags=["Integration Lessons"])
async def integration_start_lesson(lesson_id: str, request: Request) -> dict:
    lesson = _get_lesson(request, lesson_id)
    await request.app.state.session_manager.start(lesson)
    with request.app.state.database.session_factory() as session:
        LessonRepository(session).update_status(lesson_id, "running")
    return {"lesson_id": lesson_id, "status": "running"}


@api_router.post("/lessons/{lesson_id}/stop", tags=["Integration Lessons"])
async def integration_stop_lesson(lesson_id: str, request: Request) -> dict:
    _get_lesson(request, lesson_id)
    await request.app.state.session_manager.stop(lesson_id)
    with request.app.state.database.session_factory() as session:
        LessonRepository(session).update_status(lesson_id, "stopped")
    return {"lesson_id": lesson_id, "status": "stopped"}


@api_router.get("/lessons/{lesson_id}/status", response_model=IntegrationStatusResponse, tags=["Integration Lessons"])
def integration_lesson_status(lesson_id: str, request: Request) -> IntegrationStatusResponse:
    lesson = _get_lesson(request, lesson_id)
    metrics = request.app.state.metrics_repo.averages_by_lesson().get(lesson_id, {})
    errors = [
        {"message": item.message, "created_at": item.created_at.isoformat()}
        for item in request.app.state.debug_repo.latest_for_lesson(lesson_id)
        if item.level == "error"
    ]
    return IntegrationStatusResponse(
        lesson_id=lesson.lesson_id,
        external_lesson_id=lesson.external_lesson_id,
        lesson_status=lesson.status,
        rtms_status=lesson.rtms_status,
        pipeline_status=lesson.pipeline_status,
        audio_source=lesson.audio_source,
        stt={
            "provider": lesson.stt_provider,
            "status": lesson.stt_provider_status,
            "partial_events": lesson.stt_provider_partial_events,
            "final_events": lesson.stt_provider_final_events,
        },
        translation={
            "provider": lesson.translation_provider,
            "requests": lesson.translation_requests_count,
            "errors": lesson.translation_errors_count,
        },
        captions={
            "sent": lesson.captions_sent,
            "connected_clients": request.app.state.caption_hub.connected_count(lesson_id),
        },
        latency_ms={
            "avg_stt": metrics.get("stt", 0),
            "avg_translation": metrics.get("translation", lesson.translation_avg_latency_ms or 0),
            "avg_total": metrics.get("total", 0),
        },
        errors=errors,
    )


@api_router.get("/lessons/{lesson_id}/zoom/embed-config", tags=["Integration Lessons"])
def integration_zoom_embed_config(lesson_id: str, request: Request, user_name: str = "Student") -> dict:
    lesson = _get_lesson(request, lesson_id)
    try:
        payload = request.app.state.zoom_meeting_sdk.build_embed_config(
            lesson,
            user_name=user_name,
            role=request.app.state.settings.zoom_meeting_sdk_role_student,
        )
    except ZoomMeetingSDKConfigurationError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message, "details": exc.details}) from exc
    payload.pop("start_url", None)
    return payload


@api_router.post("/lessons/{lesson_id}/student-token", response_model=StudentTokenResponse, tags=["Integration Tokens"])
def integration_student_token(lesson_id: str, payload: StudentTokenRequest, request: Request) -> StudentTokenResponse:
    lesson = _get_lesson(request, lesson_id)
    try:
        scopes = validate_requested_scopes(payload.scopes, allowed=STUDENT_TOKEN_SCOPES, defaults=[CAPTIONS_READ, ZOOM_EMBED, TTS_PLAY, QUESTION_WRITE, QUESTION_READ])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    settings = request.app.state.settings
    ttl_seconds = _bounded_ttl(payload.ttl_seconds, settings.student_ws_token_ttl_seconds, settings.access_token_ttl_seconds)
    token = create_access_token(
        {
            "sub": payload.external_student_id,
            "role": "student",
            "lesson_id": lesson.lesson_id,
            "external_lesson_id": lesson.external_lesson_id,
            "display_name": payload.display_name,
            "scopes": scopes,
        },
        ttl_seconds=ttl_seconds,
    )
    base_url = _public_base_url(request)
    ws_base_url = _public_ws_base_url(request, base_url)
    quoted = quote(token, safe="")
    return StudentTokenResponse(
        token=token,
        expires_at=_token_expires_at(token),
        lesson_id=lesson.lesson_id,
        captions_websocket_url=f"{ws_base_url}/ws/v1/lessons/{lesson.lesson_id}/captions?token={quoted}",
        embed_config_url=f"{base_url}/api/v1/integration/lessons/{lesson.lesson_id}/zoom/embed-config?token={quoted}",
        tts_status_url=f"{base_url}/api/v1/integration/lessons/{lesson.lesson_id}/tts/status?token={quoted}",
        tts_synthesize_url=f"{base_url}/api/v1/integration/lessons/{lesson.lesson_id}/tts/synthesize?token={quoted}",
        questions_websocket_url=f"{ws_base_url}/ws/v1/lessons/{lesson.lesson_id}/questions?token={quoted}",
        text_question_url=f"{base_url}/api/v1/integration/lessons/{lesson.lesson_id}/questions/text?token={quoted}",
        voice_question_audio_websocket_url=f"{ws_base_url}/ws/v1/lessons/{lesson.lesson_id}/student-question-audio?token={quoted}",
    )


@api_router.post("/lessons/{lesson_id}/teacher-token", response_model=TeacherTokenResponse, tags=["Integration Tokens"])
def integration_teacher_token(lesson_id: str, payload: TeacherTokenRequest, request: Request) -> TeacherTokenResponse:
    lesson = _get_lesson(request, lesson_id)
    try:
        scopes = validate_requested_scopes(payload.scopes, allowed=TEACHER_TOKEN_SCOPES, defaults=[AUDIO_WRITE, DIAGNOSTICS_READ, CAPTIONS_READ, QUESTION_READ, QUESTION_MODERATE])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    settings = request.app.state.settings
    ttl_seconds = _bounded_ttl(payload.ttl_seconds, settings.teacher_audio_token_ttl_seconds, settings.access_token_ttl_seconds)
    token = create_access_token(
        {
            "sub": payload.external_teacher_id,
            "role": "teacher",
            "lesson_id": lesson.lesson_id,
            "external_lesson_id": lesson.external_lesson_id,
            "display_name": payload.display_name,
            "scopes": scopes,
        },
        ttl_seconds=ttl_seconds,
    )
    base_url = _public_base_url(request)
    ws_base_url = _public_ws_base_url(request, base_url)
    quoted = quote(token, safe="")
    return TeacherTokenResponse(
        token=token,
        expires_at=_token_expires_at(token),
        lesson_id=lesson.lesson_id,
        audio_ingest_websocket_url=f"{ws_base_url}/ws/v1/lessons/{lesson.lesson_id}/audio-ingest?token={quoted}",
        diagnostics_websocket_url=f"{ws_base_url}/ws/v1/lessons/{lesson.lesson_id}/diagnostics?token={quoted}",
        questions_websocket_url=f"{ws_base_url}/ws/v1/lessons/{lesson.lesson_id}/questions?token={quoted}",
        questions_list_url=f"{base_url}/api/v1/integration/lessons/{lesson.lesson_id}/questions?token={quoted}",
        question_answer_url_template=f"{base_url}/api/v1/integration/lessons/{lesson.lesson_id}/questions/{{question_id}}/answer?token={quoted}",
        question_dismiss_url_template=f"{base_url}/api/v1/integration/lessons/{lesson.lesson_id}/questions/{{question_id}}/dismiss?token={quoted}",
    )


@api_router.get("/lessons/{lesson_id}/tts/status", response_model=TTSStatusResponse, response_model_exclude_none=True, tags=["Integration TTS"])
def integration_tts_status(lesson_id: str, request: Request) -> TTSStatusResponse:
    _get_lesson(request, lesson_id)
    return tts_status(request)


@api_router.post("/lessons/{lesson_id}/tts/synthesize", tags=["Integration TTS"])
async def integration_tts_synthesize(lesson_id: str, payload: TTSSynthesizeRequest, request: Request):
    with request.app.state.database.session_factory() as session:
        return await synthesize_tts_for_lesson(lesson_id, payload, request, session)


@api_router.get("/lessons/{lesson_id}/tts/audio/{audio_id}", tags=["Integration TTS"])
async def integration_tts_audio(lesson_id: str, audio_id: str, request: Request):
    return await get_tts_audio(lesson_id, audio_id, request, already_authorized=True)


@api_router.post(
    "/lessons/{lesson_id}/questions/text",
    response_model=IntegrationQuestionResponse,
    status_code=201,
    tags=["Integration Questions"],
)
async def integration_create_text_question(lesson_id: str, payload: IntegrationTextQuestionRequest, request: Request) -> IntegrationQuestionResponse:
    lesson = _get_lesson(request, lesson_id)
    await _enforce_question_rate_limit(request, lesson_id, "question_text", request.app.state.settings.question_text_rate_limit_per_minute, payload.student_id)
    try:
        question = await request.app.state.question_service.create_text_question(
            lesson_id,
            text=payload.text,
            source_language=payload.source_language,
            student_id=payload.student_id,
            student_name=payload.student_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _integration_question_response(question_to_read(question).model_dump(mode="json"), lesson)


@api_router.get("/lessons/{lesson_id}/questions", response_model=IntegrationQuestionListResponse, tags=["Integration Questions"])
def integration_list_questions(lesson_id: str, request: Request) -> IntegrationQuestionListResponse:
    lesson = _get_lesson(request, lesson_id)
    with request.app.state.database.session_factory() as session:
        questions = [
            _integration_question_response(question_to_read(question).model_dump(mode="json"), lesson)
            for question in LessonQuestionRepository(session).list_questions_for_lesson(lesson_id)
        ]
    return IntegrationQuestionListResponse(lesson_id=lesson.lesson_id, external_lesson_id=lesson.external_lesson_id, questions=questions)


@api_router.post("/lessons/{lesson_id}/questions/{question_id}/answer", response_model=IntegrationQuestionResponse, tags=["Integration Questions"])
async def integration_mark_question_answered(lesson_id: str, question_id: int, request: Request) -> IntegrationQuestionResponse:
    lesson = _get_lesson(request, lesson_id)
    await _enforce_question_rate_limit(request, lesson_id, "question_moderation", request.app.state.settings.question_moderation_rate_limit_per_minute)
    _ensure_question_belongs_to_lesson(request, lesson_id, question_id)
    question = await request.app.state.question_service.mark_answered(question_id)
    if question is None or question.lesson_id != lesson_id:
        raise HTTPException(status_code=404, detail="Question not found")
    return _integration_question_response(question_to_read(question).model_dump(mode="json"), lesson)


@api_router.post("/lessons/{lesson_id}/questions/{question_id}/dismiss", response_model=IntegrationQuestionResponse, tags=["Integration Questions"])
async def integration_dismiss_question(lesson_id: str, question_id: int, request: Request) -> IntegrationQuestionResponse:
    lesson = _get_lesson(request, lesson_id)
    await _enforce_question_rate_limit(request, lesson_id, "question_moderation", request.app.state.settings.question_moderation_rate_limit_per_minute)
    _ensure_question_belongs_to_lesson(request, lesson_id, question_id)
    question = await request.app.state.question_service.dismiss(question_id)
    if question is None or question.lesson_id != lesson_id:
        raise HTTPException(status_code=404, detail="Question not found")
    return _integration_question_response(question_to_read(question).model_dump(mode="json"), lesson)


@api_router.get("/lessons/{lesson_id}/transcript", tags=["Integration Exports"])
def integration_transcript(lesson_id: str, request: Request, include_partials: bool = False) -> dict:
    return _transcript(request, lesson_id, include_partials).model_dump(mode="json")


@api_router.get("/lessons/{lesson_id}/exports/json", tags=["Integration Exports"])
def integration_export_json(lesson_id: str, request: Request, include_partials: bool = False) -> JSONResponse:
    return JSONResponse(_transcript(request, lesson_id, include_partials).model_dump(mode="json"))


@api_router.get("/lessons/{lesson_id}/exports/srt", tags=["Integration Exports"])
def integration_export_srt(lesson_id: str, request: Request, lang: str = "ru", normalized: bool = True) -> PlainTextResponse:
    return PlainTextResponse(SRTExporter().export(_transcript(request, lesson_id), lang=lang, normalized=normalized), media_type="application/x-subrip; charset=utf-8")


@api_router.get("/lessons/{lesson_id}/exports/vtt", tags=["Integration Exports"])
def integration_export_vtt(lesson_id: str, request: Request, lang: str = "ru", normalized: bool = True) -> PlainTextResponse:
    return PlainTextResponse(VTTExporter().export(_transcript(request, lesson_id), lang=lang, normalized=normalized), media_type="text/vtt; charset=utf-8")


@api_router.get("/lessons/{lesson_id}/exports/markdown", tags=["Integration Exports"])
def integration_export_markdown(lesson_id: str, request: Request, lang: str = "all", normalized: bool = True) -> PlainTextResponse:
    return PlainTextResponse(MarkdownExporter().export(_transcript(request, lesson_id), lang=lang, normalized=normalized), media_type="text/markdown; charset=utf-8")


@api_router.get("/lessons/{lesson_id}/exports/html", tags=["Integration Exports"])
def integration_export_html(lesson_id: str, request: Request, lang: str = "all", normalized: bool = True) -> HTMLResponse:
    return HTMLResponse(HTMLExporter().export(_transcript(request, lesson_id), lang=lang, normalized=normalized), media_type="text/html; charset=utf-8")


@api_router.get("/lessons/{lesson_id}/usage", tags=["Integration Usage"])
def integration_lesson_usage(lesson_id: str, request: Request) -> dict:
    _get_lesson(request, lesson_id)
    return _estimator(request).estimate_for_lesson(lesson_id).model_dump(mode="json")


@api_router.get("/lessons/{lesson_id}/cost", tags=["Integration Usage"])
def integration_lesson_cost(lesson_id: str, request: Request) -> dict:
    _get_lesson(request, lesson_id)
    return _estimator(request).estimate_for_lesson(lesson_id).model_dump(mode="json")


@api_router.get("/glossaries", tags=["Integration Glossary"])
def integration_glossaries(request: Request) -> list[dict]:
    with request.app.state.database.session_factory() as session:
        repo = GlossaryRepository(session)
        return [_glossary_response(item, len(repo.terms_for_glossary(item.id, enabled_only=False))) for item in repo.list_glossaries()]


@api_router.post("/glossaries", status_code=201, tags=["Integration Glossary"])
def integration_create_glossary(payload: GlossaryCreate, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        glossary = GlossaryRepository(session).create_glossary(**payload.model_dump())
        return _glossary_response(glossary, 0)


@api_router.get("/glossaries/{glossary_id}", tags=["Integration Glossary"])
def integration_get_glossary(glossary_id: str, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        repo = GlossaryRepository(session)
        glossary = repo.get_glossary(glossary_id)
        if glossary is None:
            raise HTTPException(status_code=404, detail="Glossary not found")
        payload = _glossary_response(glossary, len(repo.terms_for_glossary(glossary_id, enabled_only=False)))
        payload["terms"] = [_term_response(term) for term in repo.terms_for_glossary(glossary_id, enabled_only=False)]
        return payload


@api_router.post("/lessons/{lesson_id}/glossary", tags=["Integration Glossary"])
def integration_set_lesson_glossary(lesson_id: str, payload: dict, request: Request) -> dict:
    glossary_id = payload.get("glossary_id")
    enabled = bool(payload.get("enabled", True))
    with request.app.state.database.session_factory() as session:
        if glossary_id and GlossaryRepository(session).get_glossary(glossary_id) is None:
            raise HTTPException(status_code=404, detail="Glossary not found")
        lesson = LessonRepository(session).set_glossary(lesson_id, glossary_id, enabled)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        return {"lesson_id": lesson_id, "glossary_id": lesson.glossary_id, "enabled": lesson.glossary_enabled}


router.include_router(api_router)


@router.websocket("/ws/v1/lessons/{lesson_id}/captions")
async def integration_captions_websocket(lesson_id: str, websocket: WebSocket) -> None:
    if not await authorize_websocket_access(websocket, lesson_id, CAPTIONS_READ, allow_integration_key=True):
        return
    await websocket.accept()
    lesson = _get_lesson_for_websocket(websocket, lesson_id)
    if lesson is None:
        await websocket.close(code=1008)
        return
    proxy = IntegrationWebSocketProxy(websocket, lesson)
    hub = websocket.app.state.caption_hub
    await hub.connect(lesson_id, proxy)
    _set_connected_count(websocket.app, lesson_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(lesson_id, proxy)
        _set_connected_count(websocket.app, lesson_id)


@router.websocket("/ws/v1/lessons/{lesson_id}/diagnostics")
async def integration_diagnostics_websocket(lesson_id: str, websocket: WebSocket) -> None:
    if not await authorize_websocket_access(websocket, lesson_id, DIAGNOSTICS_READ, allow_integration_key=True):
        return
    await websocket.accept()
    lesson = _get_lesson_for_websocket(websocket, lesson_id)
    if lesson is None:
        await websocket.close(code=1008)
        return
    proxy = IntegrationWebSocketProxy(websocket, lesson, diagnostic=True)
    await websocket.send_json(proxy.enrich({"event": "readiness_update", "status": "connected"}))
    hub = websocket.app.state.caption_hub
    await hub.connect(lesson_id, proxy, debug=True)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(lesson_id, proxy, debug=True)


@router.websocket("/ws/v1/lessons/{lesson_id}/questions")
async def integration_questions_websocket(lesson_id: str, websocket: WebSocket) -> None:
    if not await authorize_websocket_access(websocket, lesson_id, QUESTION_READ, allow_integration_key=True):
        return
    await websocket.accept()
    lesson = _get_lesson_for_websocket(websocket, lesson_id)
    if lesson is None:
        await websocket.close(code=1008)
        return
    proxy = IntegrationQuestionWebSocketProxy(websocket, lesson)
    hub = websocket.app.state.question_hub
    await hub.connect(lesson_id, proxy)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(lesson_id, proxy)


@router.websocket("/ws/v1/lessons/{lesson_id}/student-question-audio")
async def integration_student_question_audio_websocket(lesson_id: str, websocket: WebSocket) -> None:
    if not await authorize_websocket_access(websocket, lesson_id, QUESTION_WRITE, allow_integration_key=False):
        return
    lesson = _get_lesson_for_websocket(websocket, lesson_id)
    if lesson is None:
        await websocket.close(code=1008)
        return
    proxy = IntegrationQuestionAudioWebSocketProxy(websocket, lesson)
    await StudentQuestionAudioHandler(
        websocket.app.state.question_service,
        websocket.app.state.settings,
        websocket.app.state.rate_limiter,
    ).handle(lesson_id, proxy)


class IntegrationWebSocketProxy:
    def __init__(self, websocket: WebSocket, lesson: Lesson, diagnostic: bool = False) -> None:
        self.websocket = websocket
        self.lesson = lesson
        self.diagnostic = diagnostic
        self.sequence = 0

    async def send_json(self, payload: dict) -> None:
        await self.websocket.send_json(self.enrich(payload))

    def enrich(self, payload: dict) -> dict:
        self.sequence += 1
        if self.diagnostic:
            return {
                "event": payload.get("event", "diagnostic"),
                "version": "1.0",
                "lesson_id": self.lesson.lesson_id,
                "external_lesson_id": self.lesson.external_lesson_id,
                "timestamp": payload.get("created_at") or payload.get("timestamp") or datetime.utcnow().isoformat() + "Z",
                "payload": payload.get("payload", payload),
            }
        enriched = dict(payload)
        enriched.setdefault("event", "caption")
        enriched.setdefault("version", "1.0")
        enriched.setdefault("lesson_id", self.lesson.lesson_id)
        enriched.setdefault("external_lesson_id", self.lesson.external_lesson_id)
        enriched.setdefault("sequence", self.sequence)
        enriched.setdefault("source_language", "ru-RU")
        enriched.setdefault("target_languages", [item for item in self.lesson.target_languages.split(",") if item])
        enriched.setdefault("selected_language_hint", None)
        enriched.setdefault("speaker", {"id": "teacher", "name": "Teacher"})
        enriched.setdefault("provider", {"stt": self.lesson.stt_provider, "translator": self.lesson.translation_provider})
        enriched.setdefault("glossary", {"enabled": self.lesson.glossary_enabled, "normalization_changes": [], "postprocess_changes": []})
        enriched.setdefault("latency_ms", {})
        enriched.setdefault("timestamps", {"websocket_sent_at": datetime.utcnow().isoformat() + "Z"})
        return enriched


class IntegrationQuestionWebSocketProxy:
    def __init__(self, websocket: WebSocket, lesson: Lesson) -> None:
        self.websocket = websocket
        self.lesson = lesson

    async def send_json(self, payload: dict) -> None:
        await self.websocket.send_json(_integration_question_event(payload, self.lesson))


class IntegrationQuestionAudioWebSocketProxy:
    def __init__(self, websocket: WebSocket, lesson: Lesson) -> None:
        self.websocket = websocket
        self.lesson = lesson

    @property
    def app(self):
        return self.websocket.app

    @property
    def client(self):
        return self.websocket.client

    @property
    def headers(self):
        return self.websocket.headers

    @property
    def query_params(self):
        return self.websocket.query_params

    async def accept(self) -> None:
        await self.websocket.accept()

    async def receive(self) -> dict:
        return await self.websocket.receive()

    async def send_json(self, payload: dict) -> None:
        await self.websocket.send_json(_integration_question_event(payload, self.lesson))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        await self.websocket.close(code=code, reason=reason or "")


async def _create_meeting(payload: IntegrationLessonCreate, request: Request):
    if payload.mode == "mock" or not payload.create_zoom_meeting:
        return await MockZoomClient().create_meeting(payload.title)
    try:
        return await request.app.state.zoom_api_client.create_meeting(payload.title)
    except ZoomCredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ZoomOAuthError, ZoomAPIError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _lesson_response(lesson: Lesson, request: Request) -> IntegrationLessonResponse:
    base_url = _public_base_url(request)
    ws_base_url = _public_ws_base_url(request, base_url)
    return IntegrationLessonResponse(
        lesson_id=lesson.lesson_id,
        external_lesson_id=lesson.external_lesson_id,
        external_course_id=lesson.external_course_id,
        external_teacher_id=lesson.external_teacher_id,
        external_tenant_id=lesson.external_tenant_id,
        title=lesson.title,
        mode=lesson.mode,
        audio_source=lesson.audio_source,
        status=lesson.status,
        stt_provider=lesson.stt_provider,
        translation_provider=lesson.translation_provider,
        target_languages=[item for item in lesson.target_languages.split(",") if item],
        glossary_id=lesson.glossary_id,
        glossary_enabled=lesson.glossary_enabled,
        zoom={
            "meeting_id": lesson.zoom_meeting_id,
            "meeting_uuid": lesson.zoom_meeting_uuid,
            "join_url": lesson.zoom_join_url,
            "start_url": lesson.zoom_start_url,
            "password": lesson.zoom_password or "",
        },
        student={
            "captions_websocket_url": f"{ws_base_url}/ws/v1/lessons/{lesson.lesson_id}/captions",
            "diagnostics_websocket_url": f"{ws_base_url}/ws/v1/lessons/{lesson.lesson_id}/diagnostics",
            "embed_config_url": f"{base_url}/api/v1/integration/lessons/{lesson.lesson_id}/zoom/embed-config",
        },
    )


def _get_lesson(request: Request, lesson_id: str) -> Lesson:
    with request.app.state.database.session_factory() as session:
        lesson = LessonRepository(session).get(lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        return lesson


def _get_lesson_for_websocket(websocket: WebSocket, lesson_id: str) -> Lesson | None:
    with websocket.app.state.database.session_factory() as session:
        return LessonRepository(session).get(lesson_id)


def _integration_question_response(question: dict, lesson: Lesson) -> IntegrationQuestionResponse:
    return IntegrationQuestionResponse(
        id=question["id"],
        lesson_id=question["lesson_id"],
        external_lesson_id=lesson.external_lesson_id,
        student_id=question.get("student_id"),
        student_name=question.get("student_name"),
        input_type=question["input_type"],
        source_language=question["source_language"],
        original_text=question.get("original_text") or "",
        recognized_text=question.get("recognized_text"),
        translated_text_ru=question.get("translated_text_ru") or "",
        status=question["status"],
        stt_provider=question.get("stt_provider"),
        translation_provider=question.get("translation_provider"),
        audio_duration_ms=question.get("audio_duration_ms"),
        latency_ms=question.get("latency_ms"),
        error=question.get("error"),
        created_at=_iso_timestamp(question.get("created_at")),
        answered_at=_iso_timestamp(question.get("answered_at")),
        dismissed_at=_iso_timestamp(question.get("dismissed_at")),
    )


def _integration_question_event(payload: dict, lesson: Lesson) -> dict:
    event = {
        "event": payload.get("event", "question_updated"),
        "version": "1.0",
        "lesson_id": lesson.lesson_id,
        "external_lesson_id": lesson.external_lesson_id,
        "timestamp": payload.get("timestamp") or datetime.utcnow().isoformat() + "Z",
    }
    if payload.get("code") is not None:
        event["code"] = payload.get("code")
    if payload.get("error") is not None:
        event["error"] = payload.get("error")
    if payload.get("retry_after_seconds") is not None:
        event["retry_after_seconds"] = payload.get("retry_after_seconds")
    if isinstance(payload.get("question"), dict):
        event["question"] = _integration_question_response(payload["question"], lesson).model_dump(mode="json")
    return event


def _iso_timestamp(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def _transcript(request: Request, lesson_id: str, include_partials: bool = False):
    try:
        return TranscriptBuilder(request.app.state.database.session_factory).build(lesson_id, include_partials=include_partials)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _estimator(request: Request) -> CostEstimator:
    settings = request.app.state.settings
    return CostEstimator(request.app.state.database.session_factory, default_currency=settings.default_currency, enabled=settings.cost_estimation_enabled)


def _public_base_url(request: Request) -> str:
    return request.app.state.settings.public_base_url.rstrip("/") or str(request.base_url).rstrip("/")


def _public_ws_base_url(request: Request, base_url: str) -> str:
    explicit = request.app.state.settings.public_ws_base_url.rstrip("/")
    return explicit or _ws_base_url(base_url)


def _ws_base_url(base_url: str) -> str:
    if base_url.startswith("https://"):
        return "wss://" + base_url.removeprefix("https://")
    if base_url.startswith("http://"):
        return "ws://" + base_url.removeprefix("http://")
    return base_url


def _bounded_ttl(requested: int | None, default_ttl: int, fallback_ttl: int) -> int:
    effective_default = default_ttl or fallback_ttl
    if requested is None:
        return effective_default
    return min(requested, effective_default)


def _token_expires_at(token: str) -> str:
    payload = verify_access_token(token)
    return datetime.fromtimestamp(payload.exp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _set_connected_count(app, lesson_id: str) -> None:
    with app.state.database.session_factory() as session:
        LessonRepository(session).set_connected_students(lesson_id, app.state.caption_hub.connected_count(lesson_id))


def _glossary_response(glossary, terms_count: int) -> dict:
    return {
        "id": glossary.id,
        "name": glossary.name,
        "description": glossary.description,
        "domain": glossary.domain,
        "source_language": glossary.source_language,
        "target_languages": json.loads(glossary.target_languages_json or "[]"),
        "is_default": glossary.is_default,
        "terms_count": terms_count,
        "created_at": glossary.created_at.isoformat(),
        "updated_at": glossary.updated_at.isoformat(),
    }


def _term_response(term) -> dict:
    return {
        "id": term.id,
        "glossary_id": term.glossary_id,
        "source": term.source,
        "canonical": term.canonical,
        "aliases": json.loads(term.aliases_json or "[]"),
        "translations": json.loads(term.translations_json or "{}"),
        "case_sensitive": term.case_sensitive,
        "match_type": term.match_type,
        "priority": term.priority,
        "enabled": term.enabled,
    }
