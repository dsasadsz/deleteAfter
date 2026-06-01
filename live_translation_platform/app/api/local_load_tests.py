from __future__ import annotations

import json
import secrets
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from app.loadtest.report_builder import render_html_report, render_markdown_report, sanitize_for_report
from app.loadtest.virtual_lesson_runner import LocalLoadTestRequest, LocalLoadTestRunner

router = APIRouter(tags=["local-load-tests"])


@router.post("/api/load-tests/local", status_code=201)
async def start_local_load_test(
    payload: LocalLoadTestRequest,
    request: Request,
    x_integration_key: str | None = Header(default=None),
) -> dict:
    _ensure_local_load_tests_allowed(request, x_integration_key)
    runner = _runner(request)
    run = await runner.start(payload, app=request.app)
    return _run_payload(run)


@router.post("/api/load-tests/local/audio", status_code=201)
async def upload_local_load_test_audio(
    request: Request,
    file: UploadFile,
    x_integration_key: str | None = Header(default=None),
) -> dict:
    _ensure_local_load_tests_allowed(request, x_integration_key)
    suffix = Path(file.filename or "lesson.wav").suffix.lower()
    if suffix not in {".wav", ".mp3"}:
        raise HTTPException(status_code=400, detail="Only WAV and MP3 lesson audio uploads are supported.")
    target_dir = Path("reports/local_load_tests/uploads")
    target_dir.mkdir(parents=True, exist_ok=True)
    audio_file_id = f"audio_{secrets.token_hex(8)}{suffix}"
    target = target_dir / audio_file_id
    content = await file.read()
    max_bytes = int(getattr(request.app.state.settings, "max_audio_upload_bytes", 2 * 1024 * 1024) or 2 * 1024 * 1024)
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail="Audio upload exceeds maximum size.")
    target.write_bytes(content)
    return {"audio_file_id": audio_file_id, "filename": file.filename, "bytes": len(content)}


@router.get("/api/load-tests/local")
def list_local_load_tests(request: Request) -> dict:
    runner = _runner(request)
    return {"items": [_run_payload(run) for run in runner.list_runs()]}


@router.get("/api/load-tests/local/{run_id}")
def get_local_load_test(run_id: str, request: Request) -> dict:
    run = _get_run_or_404(run_id, request)
    return _run_payload(run)


@router.post("/api/load-tests/local/{run_id}/stop")
async def stop_local_load_test(
    run_id: str,
    request: Request,
    x_integration_key: str | None = Header(default=None),
) -> dict:
    _ensure_local_load_tests_allowed(request, x_integration_key)
    run = await _runner(request).stop(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Local load-test run not found")
    return _run_payload(run)


@router.websocket("/ws/load-tests/local/{run_id}")
async def local_load_test_websocket(run_id: str, websocket: WebSocket) -> None:
    await websocket.accept()
    runner = getattr(websocket.app.state, "local_load_test_runner", None)
    try:
        while True:
            run = runner.get(run_id) if runner is not None else None
            if run is None:
                await websocket.send_json({"event": "error", "detail": "Local load-test run not found"})
                await websocket.close(code=1008)
                return
            await websocket.send_json({"event": "run_update", "run": _run_payload(run)})
            await websocket.receive_text()
    except WebSocketDisconnect:
        return


@router.get("/api/load-tests/local/{run_id}/report/json")
def local_load_test_report_json(run_id: str, request: Request) -> JSONResponse:
    report = _report_or_404(run_id, request)
    return JSONResponse(report)


@router.get("/api/load-tests/local/{run_id}/report/markdown")
def local_load_test_report_markdown(run_id: str, request: Request) -> PlainTextResponse:
    report = _report_or_404(run_id, request)
    return PlainTextResponse(render_markdown_report(report), media_type="text/markdown; charset=utf-8")


@router.get("/api/load-tests/local/{run_id}/report/html")
def local_load_test_report_html(run_id: str, request: Request) -> HTMLResponse:
    report = _report_or_404(run_id, request)
    return HTMLResponse(render_html_report(report), media_type="text/html; charset=utf-8")


def _runner(request: Request) -> LocalLoadTestRunner:
    runner = getattr(request.app.state, "local_load_test_runner", None)
    if runner is None:
        runner = LocalLoadTestRunner()
        request.app.state.local_load_test_runner = runner
    return runner


def _get_run_or_404(run_id: str, request: Request):
    run = _runner(request).get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Local load-test run not found")
    return run


def _report_or_404(run_id: str, request: Request) -> dict:
    report = _runner(request).report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Local load-test run not found")
    return report


def _run_payload(run) -> dict:
    payload = run.to_dict()
    report = run.build_report()
    payload["report"] = {
        "overall_verdict": report.get("overall_verdict"),
        "infrastructure_verdict": report.get("infrastructure_verdict"),
        "model_latency_verdict": report.get("model_latency_verdict"),
        "quality_verdict": report.get("quality_verdict"),
    }
    payload["report_links"] = {
        "json": f"/api/load-tests/local/{run.run_id}/report/json",
        "markdown": f"/api/load-tests/local/{run.run_id}/report/markdown",
        "html": f"/api/load-tests/local/{run.run_id}/report/html",
    }
    return sanitize_for_report(payload)


def _ensure_local_load_tests_allowed(request: Request, x_integration_key: str | None) -> None:
    settings = request.app.state.settings
    debug_allowed = bool(getattr(settings, "debug_endpoints_allowed", False))
    explicit_allowed = bool(getattr(settings, "allow_load_tests", False))
    if not (debug_allowed or explicit_allowed):
        raise HTTPException(status_code=403, detail="Local load tests require ENABLE_DEBUG_ENDPOINTS=true or ALLOW_LOAD_TESTS=true.")
    if getattr(settings, "is_production", False) and not (debug_allowed or explicit_allowed):
        raise HTTPException(status_code=403, detail="Production local load tests require ENABLE_DEBUG_ENDPOINTS=true or ALLOW_LOAD_TESTS=true.")
    if not getattr(settings, "integration_auth_enabled", True):
        return
    valid_keys = list(getattr(settings, "integration_api_keys", []) or [])
    if valid_keys and x_integration_key and any(secrets.compare_digest(x_integration_key, key) for key in valid_keys):
        return
    if valid_keys:
        raise HTTPException(status_code=401, detail="Missing or invalid integration API key.")
    if getattr(settings, "app_env", "").lower() in {"development", "test"}:
        return
    raise HTTPException(status_code=401, detail="Integration API key must be configured before starting local load tests.")
