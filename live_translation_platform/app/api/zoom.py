import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.db.repositories import DebugRepository, LessonRepository
from app.zoom.meeting_sdk import ZoomMeetingSDKConfigurationError
from app.zoom.zoom_webhooks import (
    build_url_validation_response,
    extract_zoom_webhook_context,
    is_rtms_started_event,
    is_rtms_stopped_event,
    is_url_validation_event,
    validate_zoom_webhook_signature,
)

router = APIRouter(prefix="/api/zoom", tags=["zoom"])
logger = logging.getLogger(__name__)


class MeetingSDKSignatureRequest(BaseModel):
    lesson_id: str
    role: int = 0
    user_name: str = "Student"


@router.post("/meeting-sdk/signature")
def meeting_sdk_signature(payload: MeetingSDKSignatureRequest, request: Request) -> dict:
    with request.app.state.database.session_factory() as db:
        lesson = LessonRepository(db).get(payload.lesson_id)
        if lesson is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "LESSON_NOT_FOUND",
                    "message": "Lesson not found",
                    "details": {"lesson_id": payload.lesson_id, "has_zoom_meeting_id": False, "has_sdk_key": False, "missing": ["lesson"]},
                },
            )
        try:
            return request.app.state.zoom_meeting_sdk.build_embed_config(
                lesson,
                user_name=payload.user_name,
                role=payload.role,
            )
        except ZoomMeetingSDKConfigurationError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message, "details": exc.details}) from exc


@router.post("/webhook")
async def zoom_webhook(request: Request) -> dict:
    settings = request.app.state.settings
    raw_body = await request.body()
    if settings.zoom_webhook_signature_required:
        validation = validate_zoom_webhook_signature(
            request.headers,
            raw_body,
            settings.zoom_webhook_secret_token,
            settings.zoom_webhook_timestamp_tolerance_seconds,
        )
        if not validation.valid:
            if validation.reason == "missing_secret":
                raise HTTPException(status_code=500, detail="Zoom webhook signature validation is misconfigured.")
            raise HTTPException(status_code=401, detail="Invalid Zoom webhook signature.")
    elif not settings.is_production:
        logger.warning("Zoom webhook signature validation is disabled in development.")

    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc

    if is_url_validation_event(payload):
        plain_token = payload.get("payload", {}).get("plainToken") or payload.get("plainToken")
        return build_url_validation_response(plain_token or "", settings.zoom_webhook_secret_token)

    context = extract_zoom_webhook_context(payload)
    with request.app.state.database.session_factory() as db:
        repo = LessonRepository(db)
        lesson = repo.find_by_zoom(context.meeting_id, context.meeting_uuid)
        if lesson is None:
            request.app.state.debug_repo.save(
                "__unmatched__",
                f"Unmatched Zoom webhook event {context.event}",
                "warning",
                {
                    "event": context.event,
                    "meeting_id": context.meeting_id,
                    "meeting_uuid": context.meeting_uuid,
                    "rtms_stream_id": context.rtms_stream_id,
                },
            )
            return {"status": "unmatched", "event": context.event}
        lesson_id = lesson.lesson_id

    if is_rtms_started_event(context.event):
        if not lesson.rtms_armed and request.app.state.rtms_manager.enabled:
            with request.app.state.database.session_factory() as db:
                stored = LessonRepository(db).update_rtms(
                    lesson_id,
                    rtms_status="webhook_received",
                    rtms_stream_id=context.rtms_stream_id,
                    rtms_session_id=context.rtms_session_id,
                    rtms_started_at=datetime.utcnow(),
                    rtms_error=None,
                )
            await request.app.state.caption_hub.broadcast_debug(
                lesson_id,
                {
                    "event": "rtms_status",
                    "lesson_id": lesson_id,
                    "status": "webhook_received",
                    "level": "info",
                    "message": "RTMS webhook received before pipeline was armed",
                    "payload": {"meeting_id": context.meeting_id, "meeting_uuid": context.meeting_uuid, "rtms_stream_id": context.rtms_stream_id},
                },
            )
            return {"status": "stored", "event": context.event, "matched_lesson_id": lesson_id, "rtms_status": stored.rtms_status if stored else "webhook_received"}
        status_response = await request.app.state.rtms_manager.start_lesson(lesson, payload, context)
        if lesson.rtms_armed and request.app.state.settings.rtms_auto_start_pipeline_on_webhook:
            with request.app.state.database.session_factory() as db:
                current = LessonRepository(db).get(lesson_id)
            if current is not None:
                await request.app.state.session_manager.start(current)
        return {
            "status": "accepted",
            "event": context.event,
            "matched_lesson_id": lesson_id,
            "rtms": status_response.model_dump(),
        }
    if is_rtms_stopped_event(context.event):
        status_response = await request.app.state.rtms_manager.stop_lesson(lesson_id)
        return {
            "status": "accepted",
            "event": context.event,
            "matched_lesson_id": lesson_id,
            "rtms": status_response.model_dump() if status_response else None,
        }

    await request.app.state.caption_hub.broadcast_debug(
        lesson_id,
        {
            "event": "zoom_webhook",
            "lesson_id": lesson_id,
            "message": f"Zoom webhook received: {context.event}",
            "payload": {"meeting_id": context.meeting_id, "meeting_uuid": context.meeting_uuid},
        },
    )
    return {"status": "accepted", "event": context.event, "matched_lesson_id": lesson_id}
