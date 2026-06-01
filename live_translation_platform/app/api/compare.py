import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.db.repositories import ComparisonRepository, SmokeTestRepository
from app.smoke.provider_status import missing_for_selection

router = APIRouter(tags=["compare"])


class CompareRunRequest(BaseModel):
    audio_mode: str = Field(pattern="^(mock_chunks|wav_upload|fake_rtms|direct_ws)$")
    audio_sample_id: str | None = None
    stt_providers: list[str] = Field(default_factory=lambda: ["mock"])
    translation_provider: str = Field(pattern="^(mock|azure|local)$")
    target_languages: list[str] = Field(default_factory=lambda: ["kk", "uz", "zh-Hans"])
    run_mode: str = Field(default="sequential", pattern="^(sequential|parallel)$")
    glossary_id: str | None = None
    glossary_enabled: bool = False


@router.post("/api/compare/run")
async def run_comparison(payload: CompareRunRequest, request: Request, background_tasks: BackgroundTasks) -> dict:
    valid_stt = {"mock", "elevenlabs", "azure", "cartesia"}
    requested = []
    for provider in payload.stt_providers:
        if provider not in valid_stt:
            raise HTTPException(status_code=400, detail=f"Unknown STT provider: {provider}")
        if provider not in requested:
            requested.append(provider)

    skipped = []
    runnable = []
    for provider in requested:
        missing = missing_for_selection(request.app.state.settings, provider, payload.translation_provider)
        if missing:
            skipped.append({"stt_provider": provider, "reason": f"missing {', '.join(missing)}"})
        else:
            runnable.append(provider)

    with request.app.state.database.session_factory() as session:
        comparison_repo = ComparisonRepository(session)
        smoke_repo = SmokeTestRepository(session)
        comparison = comparison_repo.create_comparison(
            audio_mode=payload.audio_mode,
            audio_sample_id=payload.audio_sample_id,
            stt_providers=requested,
            translation_provider=payload.translation_provider,
            target_languages=payload.target_languages,
            run_mode=payload.run_mode,
            skipped=skipped,
            glossary_id=payload.glossary_id,
            glossary_enabled=payload.glossary_enabled,
        )
        runs = []
        for provider in runnable:
            smoke = smoke_repo.create_run(
                lesson_id=None,
                stt_provider=provider,
                translation_provider=payload.translation_provider,
                audio_mode=payload.audio_mode,
                target_languages=payload.target_languages,
                glossary_id=payload.glossary_id,
                glossary_enabled=payload.glossary_enabled,
            )
            comparison_repo.add_item(
                comparison.id,
                stt_provider=provider,
                translation_provider=payload.translation_provider,
                smoke_test_id=smoke.id,
                status="pending",
            )
            runs.append({"stt_provider": provider, "smoke_test_id": smoke.id})
        if not runs:
            comparison_repo.complete_comparison(comparison.id, {"completed": 0, "errors": 0}, status="completed")
    if runs:
        background_tasks.add_task(request.app.state.comparison_runner.run, comparison.id)
    return {"comparison_id": comparison.id, "status": "started" if runs else "completed", "runs": runs, "skipped": skipped}


@router.get("/api/compare/{comparison_id}")
def get_comparison(comparison_id: str, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        repo = ComparisonRepository(session)
        comparison = repo.get_comparison(comparison_id)
        if comparison is None:
            raise HTTPException(status_code=404, detail="Comparison not found")
        return _comparison_response(comparison, repo.items_for_comparison(comparison_id))


@router.websocket("/ws/compare/{comparison_id}")
async def compare_websocket(comparison_id: str, websocket: WebSocket) -> None:
    await websocket.accept()
    app = websocket.app
    with app.state.database.session_factory() as session:
        repo = ComparisonRepository(session)
        comparison = repo.get_comparison(comparison_id)
        if comparison is None:
            await websocket.close(code=1008)
            return
        items = repo.items_for_comparison(comparison_id)
        await websocket.send_json({"event": "comparison_started", "comparison_id": comparison_id})
        for skipped in json.loads(comparison.skipped_json or "[]"):
            await websocket.send_json({"event": "provider_skipped", "comparison_id": comparison_id, **skipped})
        for item in items:
            if item.status in {"running", "completed", "error"}:
                await websocket.send_json(
                    {
                        "event": "provider_started",
                        "comparison_id": comparison_id,
                        "stt_provider": item.stt_provider,
                        "smoke_test_id": item.smoke_test_id,
                    }
                )
            if item.status == "completed":
                await websocket.send_json({"event": "provider_completed", "comparison_id": comparison_id, **json.loads(item.result_json or "{}")})
            elif item.status == "error":
                await websocket.send_json({"event": "provider_error", "comparison_id": comparison_id, **json.loads(item.result_json or "{}")})
        if comparison.status == "completed":
            await websocket.send_json({"event": "comparison_completed", "comparison_id": comparison_id, "summary": json.loads(comparison.summary_json or "{}")})
    await app.state.comparison_hub.connect(comparison_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        app.state.comparison_hub.disconnect(comparison_id, websocket)


def _comparison_response(comparison, items) -> dict:
    return {
        "comparison_id": comparison.id,
        "status": comparison.status,
        "audio_mode": comparison.audio_mode,
        "audio_sample_id": comparison.audio_sample_id,
        "translation_provider": comparison.translation_provider,
        "run_mode": comparison.run_mode,
        "results": [_item_response(item) for item in items],
        "skipped": json.loads(comparison.skipped_json or "[]"),
        "summary": json.loads(comparison.summary_json or "{}"),
        "error": comparison.error,
    }


def _item_response(item) -> dict:
    result = json.loads(item.result_json or "{}")
    return {
        "stt_provider": item.stt_provider,
        "translation_provider": item.translation_provider,
        "smoke_test_id": item.smoke_test_id,
        "status": item.status,
        "audio_source": result.get("audio_source", "browser_ws" if result.get("audio_mode") == "direct_ws" else None) or result.get("audio_source") or "",
        "original_text": result.get("original_text", ""),
        "translations": result.get("translations", {}),
        "latency_ms": {
            "first_partial": 0,
            "stt_final": 0,
            "translation": 0,
            "total_server": 0,
            "client_receive": 0,
            **result.get("latency_ms", {}),
        },
        "dropped_chunks": result.get("dropped_chunks", 0),
        "sample_rate": result.get("sample_rate", 16000 if result.get("audio_source") == "browser_ws" else None),
        "error": item.error or result.get("error"),
        "glossary": result.get("glossary", {"enabled": False, "glossary_id": None, "normalization_changes": [], "postprocess_changes": []}),
    }
