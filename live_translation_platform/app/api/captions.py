import logging
from uuid import uuid4

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from app.db.repositories import LessonRepository
from app.integration.auth import authorize_websocket_access
from app.security.scopes import AUDIO_WRITE, CAPTIONS_READ, DIAGNOSTICS_READ

router = APIRouter(tags=["captions"])
logger = logging.getLogger("app.audio_ingest")


@router.websocket("/ws/lessons/{lesson_id}/captions")
async def captions_websocket(lesson_id: str, websocket: WebSocket) -> None:
    if not await authorize_websocket_access(websocket, lesson_id, CAPTIONS_READ, allow_dev_bypass=True):
        return
    await websocket.accept()
    app = websocket.app
    hub = app.state.caption_hub
    await hub.connect(lesson_id, websocket)
    _set_connected_count(app, lesson_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(lesson_id, websocket)
        _set_connected_count(app, lesson_id)


@router.websocket("/ws/lessons/{lesson_id}/debug")
async def debug_websocket(lesson_id: str, websocket: WebSocket) -> None:
    if not await authorize_websocket_access(websocket, lesson_id, DIAGNOSTICS_READ, allow_integration_key=True, allow_dev_bypass=True):
        return
    await websocket.accept()
    app = websocket.app
    hub = app.state.caption_hub
    await hub.connect(lesson_id, websocket, debug=True)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(lesson_id, websocket, debug=True)


@router.websocket("/ws/lessons/{lesson_id}/audio-ingest")
async def audio_ingest_websocket(lesson_id: str, websocket: WebSocket) -> None:
    if not await authorize_websocket_access(websocket, lesson_id, AUDIO_WRITE, allow_dev_bypass=True):
        return
    if _audio_ingest_requires_existing_lesson(websocket) and not _lesson_exists(websocket.app, lesson_id):
        await _close_audio_ingest(websocket, lesson_id, 4404, "LESSON_NOT_FOUND")
        return
    await _audio_ingest_loop(lesson_id, websocket)


@router.websocket("/ws/v1/lessons/{lesson_id}/audio-ingest")
async def audio_ingest_websocket_v1(lesson_id: str, websocket: WebSocket) -> None:
    if not await authorize_websocket_access(websocket, lesson_id, AUDIO_WRITE):
        return
    if not _lesson_exists(websocket.app, lesson_id):
        await _close_audio_ingest(websocket, lesson_id, 4404, "LESSON_NOT_FOUND")
        return
    await _audio_ingest_loop(lesson_id, websocket)


async def _audio_ingest_loop(lesson_id: str, websocket: WebSocket) -> None:
    request_id = websocket.headers.get("x-request-id") or f"ws_{uuid4().hex}"
    manager = None
    accepted = False
    connected = False
    _log_audio_ingest("audio_ingest_handler_enter", lesson_id, request_id)
    try:
        await websocket.accept()
        accepted = True
        _log_audio_ingest("websocket_accepted", lesson_id, request_id)
        app = websocket.app
        manager = getattr(app.state, "browser_audio_manager", None)
        if manager is None:
            await _audio_ingest_debug_unavailable(websocket, lesson_id, request_id, "Audio ingest endpoint unavailable")
            await websocket.close(code=1011, reason="AUDIO_INGEST_NOT_AVAILABLE")
            return
        await _audio_ingest_debug(manager, lesson_id, "websocket_accepted", "Browser audio ingest WebSocket accepted", request_id)

        connected = await manager.connect(lesson_id, websocket)
        _log_audio_ingest("manager_connected", lesson_id, request_id, connected=connected)
        if not connected:
            await websocket.close(code=1008, reason="AUDIO_INGEST_NOT_AVAILABLE")
            return
        await _audio_ingest_debug(manager, lesson_id, "manager_connected", "Browser audio manager connected", request_id)

        while True:
            _log_audio_ingest("waiting_for_message", lesson_id, request_id)
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                _log_audio_ingest("received_binary", lesson_id, request_id, bytes=len(message["bytes"]))
                await manager.handle_binary(lesson_id, message["bytes"])
            elif message.get("text") is not None:
                _log_audio_ingest("received_text", lesson_id, request_id)
                await manager.handle_text(lesson_id, message["text"])
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        _log_audio_ingest("handler_exception", lesson_id, request_id, level="error", error=str(exc), exc_info=True)
        if manager is not None:
            await _audio_ingest_debug(
                manager,
                lesson_id,
                "handler_exception",
                "Browser audio ingest handler failed",
                request_id,
                payload={"error": str(exc)},
                level="error",
            )
        if accepted:
            try:
                await websocket.close(code=1011, reason="internal_error")
            except RuntimeError:
                pass
    finally:
        if connected and manager is not None:
            await manager.disconnect(lesson_id, websocket)
        _log_audio_ingest("handler_exit", lesson_id, request_id)


def _log_audio_ingest(event_type: str, lesson_id: str, request_id: str, level: str = "info", exc_info: bool = False, **payload) -> None:
    log = logger.error if level == "error" else logger.info
    log(
        event_type,
        extra={"event": {"type": event_type, "lesson_id": lesson_id, "request_id": request_id, **payload}},
        exc_info=exc_info,
    )


async def _audio_ingest_debug(manager, lesson_id: str, event: str, message: str, request_id: str, payload: dict | None = None, level: str = "info") -> None:
    debug_payload = {"request_id": request_id, **(payload or {})}
    await manager._debug(lesson_id, event, message, debug_payload, level=level)


def _audio_ingest_requires_existing_lesson(websocket: WebSocket) -> bool:
    settings = websocket.app.state.settings
    return bool(
        websocket.query_params.get("token")
        or getattr(settings, "websocket_auth_required", False)
        or websocket.url.path.startswith("/ws/v1/")
    )


def _lesson_exists(app, lesson_id: str) -> bool:
    with app.state.database.session_factory() as db:
        return LessonRepository(db).get(lesson_id) is not None


async def _close_audio_ingest(websocket: WebSocket, lesson_id: str, code: int, reason: str) -> None:
    _log_audio_ingest("audio_ingest_rejected", lesson_id, websocket.headers.get("x-request-id") or f"ws_{uuid4().hex}", reason=reason, code=code)
    await websocket.close(code=code, reason=reason)


async def _audio_ingest_debug_unavailable(websocket: WebSocket, lesson_id: str, request_id: str, message: str) -> None:
    debug_repo = getattr(websocket.app.state, "debug_repo", None)
    if debug_repo is not None:
        debug_repo.save(lesson_id, message, "error", {"request_id": request_id, "code": "AUDIO_INGEST_NOT_AVAILABLE"})


@router.websocket("/ws/lessons/{lesson_id}/diagnostics")
async def diagnostics_websocket(lesson_id: str, websocket: WebSocket) -> None:
    if not await authorize_websocket_access(websocket, lesson_id, DIAGNOSTICS_READ, allow_integration_key=True, allow_dev_bypass=True):
        return
    await websocket.accept()
    app = websocket.app
    with app.state.database.session_factory() as db:
        lesson = LessonRepository(db).get(lesson_id)
        if lesson is None:
            await websocket.close(code=1008)
            return
    await websocket.send_json({"event": "readiness_update", "lesson_id": lesson_id, "status": "connected"})
    hub = app.state.caption_hub
    await hub.connect(lesson_id, websocket, debug=True)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(lesson_id, websocket, debug=True)


def _set_connected_count(app, lesson_id: str) -> None:
    with app.state.database.session_factory() as db:
        count = app.state.caption_hub.connected_count(lesson_id)
        LessonRepository(db).set_connected_students(lesson_id, count)
