from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.repositories import LessonRepository

router = APIRouter(prefix="/api/load-test", tags=["load-test"])


class LoadTestCaptionPayload(BaseModel):
    sequence: int = Field(default=0, ge=0)
    original_text: str = "Mock load-test caption"
    translations: dict[str, str] = Field(default_factory=lambda: {"kk": "Mock load-test caption"})
    source_language: str = "ru-RU"
    speaker_id: str = "load-test"
    speaker_name: str = "Load Test"
    is_partial: bool = False
    latency_ms: dict[str, float | int] = Field(default_factory=lambda: {"stt": 0, "translation": 0, "total": 0})
    load_test_client_published_at: str | None = None


def get_db(request: Request):
    yield from request.app.state.database.session()


@router.post("/lessons/{lesson_id}/publish-caption")
async def publish_load_test_caption(
    lesson_id: str,
    payload: LoadTestCaptionPayload,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    _ensure_load_test_endpoints_enabled(request)
    lesson = LessonRepository(db).get(lesson_id)
    if lesson is None:
        raise HTTPException(status_code=404, detail="Lesson not found")

    now = datetime.now(timezone.utc)
    caption = {
        "event": "caption",
        "event_id": f"loadtest_{uuid4().hex}",
        "lesson_id": lesson_id,
        "meeting_id": lesson.zoom_meeting_id or "",
        "sequence": payload.sequence,
        "provider": {"stt": "mock-load-test", "translator": "mock-load-test"},
        "source_language": payload.source_language,
        "original_text": payload.original_text,
        "translations": payload.translations,
        "is_partial": payload.is_partial,
        "is_final": not payload.is_partial,
        "speaker": {"id": payload.speaker_id, "name": payload.speaker_name},
        "timestamps": {
            "audio_received_at": now.isoformat(),
            "stt_result_at": now.isoformat(),
            "translation_done_at": now.isoformat(),
            "websocket_sent_at": now.isoformat(),
        },
        "latency_ms": payload.latency_ms,
        "load_test": True,
        "load_test_published_at": now.isoformat(),
        "load_test_client_published_at": payload.load_test_client_published_at,
    }
    await request.app.state.caption_hub.broadcast_caption(lesson_id, caption)
    return {
        "published": True,
        "lesson_id": lesson_id,
        "sequence": payload.sequence,
        "connected_clients": request.app.state.caption_hub.connected_count(lesson_id),
    }


def _ensure_load_test_endpoints_enabled(request: Request) -> None:
    settings = request.app.state.settings
    if settings.enable_load_test_endpoints and settings.app_env.lower() == "development":
        return
    raise HTTPException(
        status_code=403,
        detail="Load-test endpoints require ENABLE_LOAD_TEST_ENDPOINTS=true and APP_ENV=development.",
    )
