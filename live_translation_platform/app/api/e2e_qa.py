from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.db.repositories import E2EQATestRepository, LessonRepository
from app.e2e_qa import CHECKLIST_STATUSES, apply_capture, build_e2e_report, default_checklist, default_metrics, run_to_dict

router = APIRouter(prefix="/api/e2e-tests", tags=["e2e-qa"])


class E2EQACreateRequest(BaseModel):
    lesson_id: str | None = None
    title: str = Field(default="Stage 22 manual QA", min_length=1, max_length=255)
    stt_provider: str = Field(default="mock", pattern="^(mock|elevenlabs|azure|cartesia|faster_whisper)$")
    translation_provider: str = Field(default="mock", pattern="^(mock|azure|local)$")
    tts_provider: str = Field(default="mock", min_length=1, max_length=64)
    tts_language: str = Field(default="kk", min_length=1, max_length=32)
    tts_queue_mode: str = Field(default="sequential", min_length=1, max_length=32)
    chunk_ms: int = Field(default=100, ge=20, le=500)
    silence_commit_ms: int = Field(default=1000, ge=0, le=10000)
    max_segment_duration_ms: int = Field(default=6000, ge=0, le=60000)
    partials_enabled: bool = True


class E2EQAChecklistRequest(BaseModel):
    key: str
    status: str
    notes: str | None = None


@router.post("", status_code=201)
def create_e2e_test(payload: E2EQACreateRequest, request: Request) -> dict:
    if not request.app.state.settings.e2e_qa_enabled:
        raise HTTPException(status_code=404, detail="E2E QA is disabled.")
    with request.app.state.database.session_factory() as session:
        if payload.lesson_id and LessonRepository(session).get(payload.lesson_id) is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        run = E2EQATestRepository(session).create_run(
            lesson_id=payload.lesson_id,
            title=payload.title,
            stt_provider=payload.stt_provider,
            translation_provider=payload.translation_provider,
            tts_provider=payload.tts_provider,
            tts_language=payload.tts_language,
            tts_queue_mode=payload.tts_queue_mode,
            chunk_ms=payload.chunk_ms,
            silence_commit_ms=payload.silence_commit_ms,
            max_segment_duration_ms=payload.max_segment_duration_ms,
            partials_enabled=payload.partials_enabled,
            checklist=default_checklist(),
            metrics=default_metrics(),
        )
        return run_to_dict(run)


@router.get("")
def list_e2e_tests(request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        return {"items": [run_to_dict(run) for run in E2EQATestRepository(session).latest()]}


@router.get("/report")
def e2e_report(request: Request, lesson_id: str | None = Query(default=None)) -> dict:
    with request.app.state.database.session_factory() as session:
        return build_e2e_report(E2EQATestRepository(session).latest(limit=500), lesson_id=lesson_id)


@router.get("/{run_id}")
def get_e2e_test(run_id: str, request: Request) -> dict:
    run = _get_run_or_404(run_id, request)
    return run_to_dict(run)


@router.post("/{run_id}/checklist")
def update_e2e_checklist(run_id: str, payload: E2EQAChecklistRequest, request: Request) -> dict:
    if payload.status not in CHECKLIST_STATUSES:
        raise HTTPException(status_code=400, detail="Checklist status must be pending, pass, fail, or manual.")
    with request.app.state.database.session_factory() as session:
        run = E2EQATestRepository(session).update_checklist(run_id, payload.key, payload.status, payload.notes)
        if run is None:
            raise HTTPException(status_code=404, detail="E2E QA run or checklist item not found")
        return run_to_dict(run)


@router.post("/{run_id}/capture")
def capture_e2e_event(run_id: str, payload: dict, request: Request) -> dict:
    if request.app.state.settings.is_production or not request.app.state.settings.enable_debug_endpoints or not request.app.state.settings.e2e_qa_debug_capture_enabled:
        raise HTTPException(status_code=403, detail="E2E QA capture helper is disabled in production.")
    with request.app.state.database.session_factory() as session:
        repo = E2EQATestRepository(session)
        run = repo.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="E2E QA run not found")
        if payload.get("lesson_id") and run.lesson_id and payload.get("lesson_id") != run.lesson_id:
            raise HTTPException(status_code=400, detail="Capture lesson_id does not match E2E QA run.")
        try:
            checklist, metrics = apply_capture(run, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        updated = repo.update_state(run_id, checklist, metrics)
        return {"captured": True, "e2e_test": run_to_dict(updated)}


@router.post("/{run_id}/finish")
def finish_e2e_test(run_id: str, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        run = E2EQATestRepository(session).finish(run_id, completed_by="manual")
        if run is None:
            raise HTTPException(status_code=404, detail="E2E QA run not found")
        return run_to_dict(run)


def _get_run_or_404(run_id: str, request: Request):
    with request.app.state.database.session_factory() as session:
        run = E2EQATestRepository(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="E2E QA run not found")
        return run
