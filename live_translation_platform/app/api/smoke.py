import json
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.db.repositories import LessonRepository, SmokeTestRepository
from app.smoke.audio_samples import chunk_wav_file

router = APIRouter(tags=["smoke"])


class SmokeRunRequest(BaseModel):
    lesson_id: str | None = None
    audio_mode: str = Field(pattern="^(mock_chunks|wav_upload|fake_rtms|direct_ws|real_rtms)$")
    streaming_mode: str = Field(default="realtime_stream", pattern="^(realtime_stream|fast_upload)$")
    stt_provider: str = Field(pattern="^(mock|elevenlabs|azure|cartesia|faster_whisper)$")
    translation_provider: str = Field(pattern="^(mock|azure|local)$")
    target_languages: list[str] = Field(default_factory=lambda: ["kk", "uz", "zh-Hans"])
    audio_sample_id: str | None = None
    glossary_id: str | None = None
    glossary_enabled: bool = False


@router.post("/api/smoke/run")
async def run_smoke_test(payload: SmokeRunRequest, request: Request, background_tasks: BackgroundTasks) -> dict:
    if payload.audio_mode == "real_rtms":
        raise HTTPException(status_code=400, detail="real_rtms smoke mode must be started from an active RTMS lesson.")
    with request.app.state.database.session_factory() as session:
        if payload.lesson_id and LessonRepository(session).get(payload.lesson_id) is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        run = SmokeTestRepository(session).create_run(
            lesson_id=payload.lesson_id,
            stt_provider=payload.stt_provider,
            translation_provider=payload.translation_provider,
            audio_mode=payload.audio_mode,
            target_languages=payload.target_languages,
            glossary_id=payload.glossary_id,
            glossary_enabled=payload.glossary_enabled,
        )
    background_tasks.add_task(request.app.state.smoke_runner.run, run.id, payload.audio_sample_id, streaming_mode=payload.streaming_mode)
    return {"smoke_test_id": run.id, "lesson_id": run.lesson_id, "status": "started"}


@router.get("/api/smoke/{smoke_test_id}")
def get_smoke_test(smoke_test_id: str, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        run = SmokeTestRepository(session).get_run(smoke_test_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Smoke test not found")
        return _smoke_run_response(run)


@router.post("/api/smoke/upload-audio")
async def upload_smoke_audio(request: Request, file: UploadFile = File(...)) -> dict:
    settings = request.app.state.settings
    max_bytes = settings.smoke_max_audio_file_mb * 1024 * 1024
    content_type = (file.content_type or "").lower()
    allowed_types = {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave", "audio/pcm", "application/octet-stream"}
    if content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Only WAV/PCM audio uploads are accepted.")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded audio file is empty.")
    if len(data) > max_bytes:
        raise HTTPException(status_code=400, detail=f"Audio file exceeds {settings.smoke_max_audio_file_mb} MB limit.")

    sample_id = f"sample_{Path(file.filename or 'sample.wav').stem[:24]}_{len(data)}"
    temp_dir = Path(settings.smoke_temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    path = temp_dir / f"{sample_id}.wav"
    path.write_bytes(data)
    try:
        sample = chunk_wav_file(path, settings.smoke_audio_chunk_ms)
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Invalid WAV file: {exc}") from exc
    return {
        "audio_sample_id": sample_id,
        "warning": sample.warning,
        "sample_rate": sample.sample_rate,
        "channels": sample.channels,
        "chunks": len(sample.chunks),
    }


@router.websocket("/ws/smoke/{smoke_test_id}")
async def smoke_websocket(smoke_test_id: str, websocket: WebSocket) -> None:
    await websocket.accept()
    app = websocket.app
    with app.state.database.session_factory() as session:
        repo = SmokeTestRepository(session)
        if repo.get_run(smoke_test_id) is None:
            await websocket.close(code=1008)
            return
        for event in repo.events_for_run(smoke_test_id):
            await websocket.send_json(json.loads(event.payload_json))
    await app.state.smoke_hub.connect(smoke_test_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        app.state.smoke_hub.disconnect(smoke_test_id, websocket)


def _smoke_run_response(run) -> dict:
    translations = json.loads(run.translations_json or "{}")
    provider_metrics = json.loads(run.provider_metrics_json or "{}")
    latency = {
        "first_partial": 0,
        "stt_final": 0,
        "translation": 0,
        "total_server": 0,
        "client_receive": 0,
        "first_partial_latency_ms": 0,
        "final_latency_ms": 0,
        "translation_latency_ms": 0,
        "total_server_latency_ms": 0,
        **json.loads(run.latency_json or "{}"),
    }
    return {
        "smoke_test_id": run.id,
        "lesson_id": run.lesson_id,
        "status": run.status,
        "providers": {"stt": run.stt_provider, "translator": run.translation_provider},
        "audio_source": "browser_ws" if run.audio_mode == "direct_ws" else run.audio_mode,
        "results": {
            "original_text": run.original_text or "",
            "original_text_normalized": run.original_text or "",
            "translations": translations,
        },
        "latency_ms": latency,
        "provider_metrics": provider_metrics,
        "audio_metrics": provider_metrics.get("audio_streaming", {}),
        "errors": [run.error] if run.error else [],
    }
