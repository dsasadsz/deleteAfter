from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.db.repositories import LessonRepository, LiveMicTestRepository
from app.live_tests import QUALITY_VALUES, build_live_test_report, run_to_dict
from app.schemas.browser_audio import BrowserAudioTuning

router = APIRouter(prefix="/api/live-tests", tags=["live-tests"])


class LiveTestCreateRequest(BaseModel):
    lesson_id: str
    stt_provider: str = Field(pattern="^(mock|elevenlabs|azure|cartesia|faster_whisper)$")
    translation_provider: str = Field(pattern="^(mock|azure|local)$")
    chunk_ms: int = Field(ge=20, le=500)
    silence_commit_ms: int = Field(ge=0, le=10000)
    max_segment_duration_ms: int = Field(ge=0, le=60000)
    partials_enabled: bool = True
    test_phrase_label: str = Field(min_length=1, max_length=128)
    expected_text: str | None = None


class LiveTestNotesRequest(BaseModel):
    transcript_quality: str | None = None
    translation_quality: str | None = None
    quality_notes: str | None = None


@router.post("", status_code=201)
async def create_live_test(payload: LiveTestCreateRequest, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        lesson_repo = LessonRepository(session)
        lesson = lesson_repo.get(payload.lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        lesson.audio_source = "browser_ws"
        lesson.stt_provider = payload.stt_provider
        lesson.translation_provider = payload.translation_provider
        lesson.browser_audio_status = "waiting_for_teacher"
        tuning = BrowserAudioTuning(
            chunk_ms=payload.chunk_ms,
            commit_strategy="manual",
            silence_commit_ms=payload.silence_commit_ms,
            partials_enabled=payload.partials_enabled,
            max_segment_duration_ms=payload.max_segment_duration_ms,
            periodic_commit_enabled=True,
            updated_by="live_test",
        )
        updated_tuning = request.app.state.browser_audio_manager.update_tuning(payload.lesson_id, tuning)
        run = LiveMicTestRepository(session).create_run(
            lesson_id=payload.lesson_id,
            stt_provider=payload.stt_provider,
            translation_provider=payload.translation_provider,
            chunk_ms=payload.chunk_ms,
            silence_commit_ms=payload.silence_commit_ms,
            max_segment_duration_ms=payload.max_segment_duration_ms,
            partials_enabled=payload.partials_enabled,
            test_phrase_label=payload.test_phrase_label,
            expected_text=payload.expected_text,
            tuning_snapshot=updated_tuning.model_dump(mode="json"),
        )
        session.commit()
    await request.app.state.caption_hub.broadcast_debug(
        payload.lesson_id,
        {"event": "live_test_started", "lesson_id": payload.lesson_id, "live_test_id": run.id, "payload": run_to_dict(run)},
    )
    return {
        "live_test_id": run.id,
        "lesson_id": run.lesson_id,
        "status": run.status,
        "tuning_applied": True,
        "teacher_url": f"/teacher/{run.lesson_id}",
        "student_url": f"/student/{run.lesson_id}",
    }


@router.get("")
def list_live_tests(request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        runs = LiveMicTestRepository(session).latest()
        return {"items": [run_to_dict(run) for run in runs]}


@router.get("/report")
def live_test_report(
    request: Request,
    lesson_id: str | None = Query(default=None),
    stt_provider: str | None = Query(default=None),
    translation_provider: str | None = Query(default=None),
    test_phrase_label: str | None = Query(default=None),
) -> dict:
    with request.app.state.database.session_factory() as session:
        runs = LiveMicTestRepository(session).latest(limit=500)
        return build_live_test_report(runs, lesson_id, stt_provider, translation_provider, test_phrase_label)


@router.get("/{run_id}")
def get_live_test(run_id: str, request: Request) -> dict:
    run = _get_run_or_404(run_id, request)
    return run_to_dict(run)


@router.post("/{run_id}/notes")
def update_live_test_notes(run_id: str, payload: LiveTestNotesRequest, request: Request) -> dict:
    _validate_quality(payload.transcript_quality)
    _validate_quality(payload.translation_quality)
    with request.app.state.database.session_factory() as session:
        run = LiveMicTestRepository(session).update_notes(run_id, payload.transcript_quality, payload.translation_quality, payload.quality_notes)
        if run is None:
            raise HTTPException(status_code=404, detail="Live test run not found")
        return run_to_dict(run)


@router.post("/{run_id}/finish")
def finish_live_test(run_id: str, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        run = LiveMicTestRepository(session).finish(run_id, completed_by="manual")
        if run is None:
            raise HTTPException(status_code=404, detail="Live test run not found")
        return run_to_dict(run)


@router.post("/{run_id}/capture")
def capture_live_test(run_id: str, payload: dict, request: Request) -> dict:
    if request.app.state.settings.is_production or not request.app.state.settings.enable_debug_endpoints:
        raise HTTPException(status_code=403, detail="Live test capture helper is disabled in production.")
    run = _get_run_or_404(run_id, request)
    if payload.get("lesson_id") != run.lesson_id:
        raise HTTPException(status_code=400, detail="Caption lesson_id does not match live test run.")
    captured = request.app.state.final_caption_capture.capture(payload)
    return {"captured": captured is not None, "live_test": run_to_dict(captured or run)}


def _get_run_or_404(run_id: str, request: Request):
    with request.app.state.database.session_factory() as session:
        run = LiveMicTestRepository(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Live test run not found")
        return run


def _validate_quality(value: str | None) -> None:
    if value is not None and value not in QUALITY_VALUES:
        raise HTTPException(status_code=400, detail="Quality must be good, acceptable, or poor.")
