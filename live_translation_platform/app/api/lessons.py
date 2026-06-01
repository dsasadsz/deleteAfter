import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models import Lesson
from app.db.repositories import LessonRepository
from app.schemas.lesson import LessonActionResponse, LessonCreate, LessonRead
from app.schemas.browser_audio import BrowserAudioStatus, BrowserAudioStatusResponse, BrowserAudioTuning
from app.schemas.rtms import RTMSStatus, RTMSStatusResponse
from app.zoom.meeting_sdk import ZoomMeetingSDKConfigurationError
from app.zoom.mock_zoom import MockZoomClient
from app.zoom.zoom_api_client import ZoomAPIError
from app.zoom.zoom_oauth import ZoomCredentialsError, ZoomOAuthError

router = APIRouter(prefix="/api/lessons", tags=["lessons"])


class RTMSAudioInject(BaseModel):
    chunks: int = Field(ge=1, le=1000)
    sample_rate: int = 16000
    channels: int = 1
    chunk_size: int = Field(default=3200, ge=1, le=64000)


class ZoomEmbedSignatureRequest(BaseModel):
    lesson_id: str
    role: int = 0
    user_name: str = "Student"


class SetAudioSourceRequest(BaseModel):
    audio_source: str = Field(pattern="^(mock|mock_audio|zoom_rtms|browser_ws)$")


def get_db(request: Request):
    yield from request.app.state.database.session()


def lesson_to_read(lesson: Lesson) -> LessonRead:
    return LessonRead(
        lesson_id=lesson.lesson_id,
        title=lesson.title,
        mode=lesson.mode,
        status=lesson.status,
        audio_source=lesson.audio_source,
        zoom_meeting_id=lesson.zoom_meeting_id,
        zoom_meeting_uuid=lesson.zoom_meeting_uuid,
        zoom_join_url=lesson.zoom_join_url,
        zoom_start_url=lesson.zoom_start_url,
        zoom_password=lesson.zoom_password,
        zoom_topic=lesson.zoom_topic,
        zoom_created_at=lesson.zoom_created_at,
        zoom={
            "meeting_id": lesson.zoom_meeting_id,
            "meeting_uuid": lesson.zoom_meeting_uuid,
            "join_url": lesson.zoom_join_url,
            "start_url": lesson.zoom_start_url,
            "password": lesson.zoom_password,
            "topic": lesson.zoom_topic,
            "created_at": lesson.zoom_created_at,
        },
        rtms_stream_id=lesson.rtms_stream_id,
        rtms_session_id=lesson.rtms_session_id,
        rtms_started_at=lesson.rtms_started_at,
        rtms_connected_at=lesson.rtms_connected_at,
        rtms_last_audio_at=lesson.rtms_last_audio_at,
        rtms_last_transcript_at=lesson.rtms_last_transcript_at,
        rtms_error=lesson.rtms_error,
        rtms_armed=lesson.rtms_armed,
        rtms_armed_at=lesson.rtms_armed_at,
        audio_chunks_received=lesson.audio_chunks_received,
        transcript_events_received=lesson.transcript_events_received,
        audio_chunks_dropped=lesson.audio_chunks_dropped,
        browser_audio_status=lesson.browser_audio_status,
        browser_audio_connected_at=lesson.browser_audio_connected_at,
        browser_audio_last_chunk_at=lesson.browser_audio_last_chunk_at,
        browser_audio_chunks_received=lesson.browser_audio_chunks_received,
        browser_audio_bytes_received=lesson.browser_audio_bytes_received,
        browser_audio_chunks_dropped=lesson.browser_audio_chunks_dropped,
        browser_audio_error=lesson.browser_audio_error,
        pipeline_status=lesson.pipeline_status,
        pipeline_audio_source=lesson.pipeline_audio_source,
        pipeline_chunks_processed=lesson.pipeline_chunks_processed,
        stt_events_generated=lesson.stt_events_generated,
        captions_sent=lesson.captions_sent,
        stt_provider_status=lesson.stt_provider_status,
        stt_provider_connected_at=lesson.stt_provider_connected_at,
        stt_provider_audio_chunks_sent=lesson.stt_provider_audio_chunks_sent,
        stt_provider_audio_bytes_sent=lesson.stt_provider_audio_bytes_sent,
        stt_provider_partial_events=lesson.stt_provider_partial_events,
        stt_provider_final_events=lesson.stt_provider_final_events,
        stt_provider_no_match_count=lesson.stt_provider_no_match_count,
        stt_provider_canceled_count=lesson.stt_provider_canceled_count,
        stt_provider_last_event_at=lesson.stt_provider_last_event_at,
        stt_provider_errors_count=lesson.stt_provider_errors_count,
        stt_provider_last_error=lesson.stt_provider_last_error,
        stt_provider_last_transcript=lesson.stt_provider_last_transcript,
        translation_requests_count=lesson.translation_requests_count,
        translation_errors_count=lesson.translation_errors_count,
        translation_last_error=lesson.translation_last_error,
        translation_last_success_at=lesson.translation_last_success_at,
        translation_avg_latency_ms=lesson.translation_avg_latency_ms,
        stt_provider=lesson.stt_provider,
        translation_provider=lesson.translation_provider,
        target_languages=[item for item in lesson.target_languages.split(",") if item],
        glossary_id=lesson.glossary_id,
        glossary_enabled=lesson.glossary_enabled,
        rtms_status=lesson.rtms_status,
        connected_students=lesson.connected_students,
        created_at=lesson.created_at,
    )


@router.post("", response_model=LessonRead, status_code=status.HTTP_201_CREATED)
async def create_lesson(payload: LessonCreate, request: Request, db: Session = Depends(get_db)) -> LessonRead:
    audio_source = _resolve_audio_source(payload.mode, payload.audio_source, request.app.state.settings)
    if payload.mode == "mock":
        meeting = await MockZoomClient().create_meeting(payload.title)
        rtms_status = RTMSStatus.NOT_CONFIGURED
    else:
        try:
            meeting = await request.app.state.zoom_api_client.create_meeting(payload.title)
            rtms_status = RTMSStatus.WAITING_FOR_MEETING
        except ZoomCredentialsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (ZoomOAuthError, ZoomAPIError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

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
        rtms_status=rtms_status,
        browser_audio_status=BrowserAudioStatus.WAITING_FOR_TEACHER if audio_source == "browser_ws" else BrowserAudioStatus.NOT_CONNECTED,
    )
    return lesson_to_read(LessonRepository(db).create(lesson))


@router.get("", response_model=list[LessonRead])
def list_lessons(db: Session = Depends(get_db)) -> list[LessonRead]:
    return [lesson_to_read(lesson) for lesson in LessonRepository(db).list()]


@router.get("/{lesson_id}", response_model=LessonRead)
def get_lesson(lesson_id: str, db: Session = Depends(get_db)) -> LessonRead:
    lesson = LessonRepository(db).get(lesson_id)
    if lesson is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    return lesson_to_read(lesson)


@router.post("/{lesson_id}/start", response_model=LessonActionResponse)
async def start_lesson(lesson_id: str, request: Request, db: Session = Depends(get_db)) -> LessonActionResponse:
    repo = LessonRepository(db)
    lesson = repo.get(lesson_id)
    if lesson is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    await request.app.state.session_manager.start(lesson)
    repo.update_status(lesson_id, "running")
    current = repo.get(lesson_id)
    return LessonActionResponse(lesson_id=lesson_id, status="running", rtms_status=current.rtms_status if current else lesson.rtms_status)


@router.post("/{lesson_id}/stop", response_model=LessonActionResponse)
async def stop_lesson(lesson_id: str, request: Request, db: Session = Depends(get_db)) -> LessonActionResponse:
    repo = LessonRepository(db)
    lesson = repo.get(lesson_id)
    if lesson is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    await request.app.state.session_manager.stop(lesson_id)
    repo.update_status(lesson_id, "stopped")
    current = repo.get(lesson_id)
    return LessonActionResponse(lesson_id=lesson_id, status="stopped", rtms_status=current.rtms_status if current else lesson.rtms_status)


@router.post("/{lesson_id}/start-rtms", response_model=RTMSStatusResponse)
async def start_rtms(lesson_id: str, request: Request, db: Session = Depends(get_db)) -> RTMSStatusResponse:
    _ensure_rtms_experimental_enabled(request)
    lesson = LessonRepository(db).get(lesson_id)
    if lesson is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    if lesson.mode == "mock":
        raise HTTPException(status_code=400, detail="RTMS not used in mock mode.")
    status_response = await request.app.state.rtms_manager.start_lesson(lesson)
    if status_response.rtms_status == RTMSStatus.NOT_CONFIGURED:
        raise HTTPException(status_code=400, detail=status_response.rtms_error or "RTMS disabled or SDK not installed")
    return status_response


@router.post("/{lesson_id}/arm-rtms")
async def arm_rtms(lesson_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    _ensure_rtms_experimental_enabled(request)
    repo = LessonRepository(db)
    lesson = repo.get(lesson_id)
    if lesson is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    if lesson.mode == "mock":
        raise HTTPException(status_code=400, detail="RTMS not used in mock mode.")
    lesson = repo.set_rtms_armed(lesson_id, True, RTMSStatus.WAITING_FOR_MEETING) or lesson
    await request.app.state.caption_hub.broadcast_debug(
        lesson_id,
        {
            "event": "rtms_status",
            "lesson_id": lesson_id,
            "status": lesson.rtms_status,
            "level": "info",
            "message": "RTMS pipeline armed",
            "created_at": lesson.updated_at.isoformat(),
        },
    )
    if lesson.rtms_stream_id and request.app.state.settings.rtms_auto_start_pipeline_on_webhook:
        await request.app.state.rtms_manager.start_lesson(lesson)
        current = repo.get(lesson_id)
        if current:
            await request.app.state.session_manager.start(current)
    return {"lesson_id": lesson_id, "rtms_armed": True, "rtms_status": lesson.rtms_status}


@router.post("/{lesson_id}/disarm-rtms")
async def disarm_rtms(lesson_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    _ensure_rtms_experimental_enabled(request)
    repo = LessonRepository(db)
    lesson = repo.get(lesson_id)
    if lesson is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    lesson = repo.set_rtms_armed(lesson_id, False) or lesson
    await request.app.state.caption_hub.broadcast_debug(
        lesson_id,
        {
            "event": "rtms_status",
            "lesson_id": lesson_id,
            "status": lesson.rtms_status,
            "level": "info",
            "message": "RTMS pipeline disarmed",
            "created_at": lesson.updated_at.isoformat(),
        },
    )
    return {"lesson_id": lesson_id, "rtms_armed": False, "rtms_status": lesson.rtms_status}


@router.get("/{lesson_id}/rtms", response_model=RTMSStatusResponse)
def get_rtms_status(lesson_id: str, request: Request) -> RTMSStatusResponse:
    status_response = request.app.state.rtms_manager.get_status(lesson_id)
    if status_response is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    return status_response


@router.get("/{lesson_id}/browser-audio", response_model=BrowserAudioStatusResponse)
def get_browser_audio_status(lesson_id: str, request: Request) -> BrowserAudioStatusResponse:
    return request.app.state.browser_audio_manager.get_status(lesson_id)


@router.get("/{lesson_id}/browser-audio/tuning", response_model=BrowserAudioTuning)
def get_browser_audio_tuning(lesson_id: str, request: Request, db: Session = Depends(get_db)) -> BrowserAudioTuning:
    if LessonRepository(db).get(lesson_id) is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    return request.app.state.browser_audio_manager.tuning_for_lesson(lesson_id)


@router.put("/{lesson_id}/browser-audio/tuning", response_model=BrowserAudioTuning)
async def update_browser_audio_tuning(
    lesson_id: str,
    payload: BrowserAudioTuning,
    request: Request,
    db: Session = Depends(get_db),
) -> BrowserAudioTuning:
    if LessonRepository(db).get(lesson_id) is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    updated = request.app.state.browser_audio_manager.update_tuning(lesson_id, payload)
    await request.app.state.caption_hub.broadcast_debug(
        lesson_id,
        {
            "event": "browser_audio_tuning_updated",
            "lesson_id": lesson_id,
            "level": "info",
            "message": "Browser audio tuning updated",
            "payload": updated.model_dump(mode="json"),
        },
    )
    return updated


@router.post("/{lesson_id}/set-audio-source", response_model=LessonRead)
def set_audio_source(lesson_id: str, payload: SetAudioSourceRequest, db: Session = Depends(get_db)) -> LessonRead:
    audio_source = _normalize_audio_source(payload.audio_source)
    lesson = LessonRepository(db).set_audio_source(lesson_id, audio_source)
    if lesson is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    return lesson_to_read(lesson)


@router.get("/{lesson_id}/diagnostics")
def lesson_diagnostics(lesson_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    lesson = LessonRepository(db).get(lesson_id)
    if lesson is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    lesson_payload = lesson_to_read(lesson).model_dump(mode="json")
    rtms = request.app.state.rtms_manager.get_status(lesson_id)
    browser_audio = request.app.state.browser_audio_manager.get_status(lesson_id)
    captions = [
        {
            "original_text": item.original_text,
            "translations": json.loads(item.translations_json or "{}"),
            "created_at": item.created_at.isoformat(),
        }
        for item in request.app.state.transcript_repo.latest_for_lesson(lesson_id)
    ]
    debug_events = [
        {
            "level": item.level,
            "message": item.message,
            "payload": json.loads(item.payload_json or "{}"),
            "created_at": item.created_at.isoformat(),
        }
        for item in request.app.state.debug_repo.latest_for_lesson(lesson_id)
    ]
    return {
        "lesson": lesson_payload,
        "rtms": rtms.model_dump() if rtms else None,
        "browser_audio": browser_audio.model_dump(mode="json"),
        "pipeline": {
            "status": lesson.pipeline_status,
            "source": lesson.pipeline_audio_source,
            "chunks_processed": lesson.pipeline_chunks_processed,
        },
        "stt": {
            "provider": lesson.stt_provider,
            "partial_events": lesson.stt_provider_partial_events,
            "final_events": lesson.stt_provider_final_events,
            "last_error": lesson.stt_provider_last_error,
        },
        "translation": {
            "provider": lesson.translation_provider,
            "requests": lesson.translation_requests_count,
            "errors": lesson.translation_errors_count,
            "last_error": lesson.translation_last_error,
        },
        "captions": {"sent": lesson.captions_sent},
        "latest_captions": captions,
        "latest_debug_events": debug_events,
        "latest_errors": [item for item in debug_events if item["level"] == "error"],
    }


@router.get("/{lesson_id}/zoom/embed-config")
def get_zoom_embed_config(lesson_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    lesson = LessonRepository(db).get(lesson_id)
    if lesson is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "LESSON_NOT_FOUND",
                "message": "Lesson not found",
                "details": {"lesson_id": lesson_id, "has_zoom_meeting_id": False, "has_sdk_key": False, "missing": ["lesson"]},
            },
        )
    try:
        return request.app.state.zoom_meeting_sdk.build_embed_config(lesson, user_name="Student", role=request.app.state.settings.zoom_meeting_sdk_role_student)
    except ZoomMeetingSDKConfigurationError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message, "details": exc.details}) from exc


@router.post("/{lesson_id}/debug/inject-rtms-audio")
async def inject_rtms_audio(lesson_id: str, payload: RTMSAudioInject, request: Request, db: Session = Depends(get_db)) -> dict:
    _ensure_rtms_experimental_enabled(request)
    if request.app.state.settings.app_env != "development":
        raise HTTPException(status_code=403, detail="RTMS audio injection is only available in development.")
    lesson = LessonRepository(db).get(lesson_id)
    if lesson is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    result = await request.app.state.rtms_manager.inject_audio(
        lesson_id,
        chunks=payload.chunks,
        sample_rate=payload.sample_rate,
        channels=payload.channels,
        chunk_size=payload.chunk_size,
    )
    return {"status": "accepted", **result}


def _resolve_audio_source(mode: str, requested: str | None, settings=None) -> str:
    if requested:
        return _normalize_audio_source(requested)
    if mode == "mock":
        return "mock"
    default_source = getattr(settings, "default_audio_source", "browser_ws")
    if (
        default_source == "browser_ws"
        and getattr(settings, "browser_audio_enabled", True)
        and getattr(settings, "browser_audio_primary", True)
    ):
        return "browser_ws"
    if default_source in {"mock", "zoom_rtms", "browser_ws"}:
        return default_source
    return "browser_ws"


def _normalize_audio_source(value: str) -> str:
    normalized = value.lower()
    if normalized == "mock_audio":
        return "mock"
    if normalized in {"mock", "zoom_rtms", "browser_ws"}:
        return normalized
    raise HTTPException(status_code=400, detail=f"Unknown audio_source: {value}")


def _ensure_rtms_experimental_enabled(request: Request) -> None:
    if request.app.state.settings.rtms_experimental_enabled:
        return
    raise HTTPException(
        status_code=400,
        detail={
            "code": "RTMS_DISABLED",
            "message": "Zoom RTMS is disabled and hidden from the current UX. Use Browser Mic WebSocket audio.",
        },
    )
