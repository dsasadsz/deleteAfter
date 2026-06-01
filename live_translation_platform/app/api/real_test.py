import json
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.lessons import lesson_to_read
from app.api.lessons import _resolve_audio_source
from app.db.models import Lesson
from app.db.repositories import LessonRepository, RealTestRepository
from app.schemas.rtms import RTMSStatus
from app.smoke.provider_status import provider_status
from app.zoom.zoom_api_client import ZoomAPIError
from app.zoom.zoom_oauth import ZoomCredentialsError, ZoomOAuthError

router = APIRouter(prefix="/api/real-test", tags=["real-test"])


class RealTestCreateLesson(BaseModel):
    title: str = Field(default="Real local Zoom + Browser Mic test", min_length=1, max_length=255)
    audio_source: str = Field(default="browser_ws", pattern="^(mock|mock_audio|zoom_rtms|browser_ws)$")
    stt_provider: str = "mock"
    translation_provider: str = "mock"
    target_languages: list[str] = Field(default_factory=lambda: ["kk", "uz", "zh-Hans"])


@router.get("/readiness")
def readiness(request: Request) -> dict:
    return provider_status(request.app.state.settings)


@router.post("/create-lesson")
async def create_real_test_lesson(payload: RealTestCreateLesson, request: Request) -> dict:
    readiness_snapshot = provider_status(request.app.state.settings)
    audio_source = _resolve_audio_source("zoom", payload.audio_source, request.app.state.settings)
    try:
        meeting = await request.app.state.zoom_api_client.create_meeting(payload.title)
    except ZoomCredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ZoomOAuthError, ZoomAPIError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    with request.app.state.database.session_factory() as session:
        lesson = Lesson(
            lesson_id=f"lesson_{uuid4().hex[:12]}",
            title=payload.title,
            mode="zoom",
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
            rtms_status=RTMSStatus.WAITING_FOR_MEETING,
            browser_audio_status="waiting_for_teacher" if audio_source == "browser_ws" else "not_connected",
        )
        lesson = LessonRepository(session).create(lesson)
        real_run = RealTestRepository(session).create_run(
            lesson_id=lesson.lesson_id,
            selected_stt_provider=payload.stt_provider,
            selected_translation_provider=payload.translation_provider,
            readiness_snapshot=readiness_snapshot,
        )
    return {"real_test_id": real_run.id, "lesson": lesson_to_read(lesson).model_dump(mode="json"), "readiness": readiness_snapshot}


def diagnostics_payload(request: Request, lesson_id: str) -> dict:
    with request.app.state.database.session_factory() as session:
        lesson = LessonRepository(session).get(lesson_id)
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
            "status": lesson_payload["pipeline_status"],
            "source": lesson_payload["pipeline_audio_source"],
            "chunks_processed": lesson_payload["pipeline_chunks_processed"],
        },
        "stt": {
            "provider": lesson_payload["stt_provider"],
            "partial_events": lesson_payload["stt_provider_partial_events"],
            "final_events": lesson_payload["stt_provider_final_events"],
            "last_error": lesson_payload["stt_provider_last_error"],
        },
        "translation": {
            "provider": lesson_payload["translation_provider"],
            "requests": lesson_payload["translation_requests_count"],
            "errors": lesson_payload["translation_errors_count"],
            "last_error": lesson_payload["translation_last_error"],
        },
        "captions": {"sent": lesson_payload["captions_sent"]},
        "latest_captions": captions,
        "latest_debug_events": debug_events,
        "latest_errors": [item for item in debug_events if item["level"] == "error"],
    }
