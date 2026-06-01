from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from app.loadtest.audio_normalizer import AudioNormalizationError, normalize_lesson_audio
from app.loadtest.quality_metrics import stt_quality_report, translation_quality_report
from app.loadtest.report_builder import build_local_load_test_report, write_json_report
from app.loadtest.virtual_lesson_bots import StudentCaptionEvent
from app.monitoring.metrics import runtime_metrics_snapshot


REPORT_DIR = Path("reports/local_load_tests")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _audio_payload(audio: dict[str, Any]) -> dict[str, Any]:
    if audio and "normalized" not in audio and "sample_rate" in audio:
        return {"normalized": audio}
    return audio


class LocalLoadTestRequest(BaseModel):
    sessions: int = Field(default=3, ge=1, le=6)
    students_per_session: int = Field(default=90, ge=1, le=120)
    mode: Literal["light", "real_pipeline", "full"] = "light"
    audio_file_id: str | None = None
    reference_ru_text: str = ""
    reference_translations: dict[str, str] = Field(default_factory=dict)
    target_languages: list[str] = Field(default_factory=lambda: ["kk", "uz", "zh-Hans"])
    stt_provider: str = "faster_whisper"
    translation_provider: str = "local"
    tts_provider: str = "local"
    tts_enabled: bool = True
    tts_languages: list[str] = Field(default_factory=lambda: ["kk", "zh-Hans"])
    tts_request_ratio: float = Field(default=0.25, ge=0, le=1)
    student_join_ramp_seconds: int = Field(default=30, ge=0, le=300)
    audio_chunk_ms: int = Field(default=50, ge=20, le=500)
    force_commit_every_seconds: int = Field(default=3, ge=0, le=60)
    duration_limit_seconds: int = Field(default=240, ge=1, le=300)
    record_runtime_metrics: bool = True
    download_audio: bool = False

    @field_validator("sessions")
    @classmethod
    def validate_sessions(cls, value: int) -> int:
        if value not in {1, 3, 6}:
            raise ValueError("sessions must be one of 1, 3, or 6")
        return value

    @field_validator("target_languages", "tts_languages")
    @classmethod
    def validate_languages(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item and item.strip()]
        if not normalized:
            raise ValueError("at least one language is required")
        return list(dict.fromkeys(normalized))


@dataclass
class LocalLoadTestRun:
    run_id: str
    request: LocalLoadTestRequest
    status: str = "created"
    created_at: str = field(default_factory=_utc_now_iso)
    started_at: str | None = None
    completed_at: str | None = None
    sessions: list[dict[str, Any]] = field(default_factory=list)
    students: list[dict[str, Any]] = field(default_factory=list)
    caption_events: list[dict[str, Any]] = field(default_factory=list)
    tts_events: list[dict[str, Any]] = field(default_factory=list)
    metric_snapshots: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    provider_errors: list[dict[str, Any]] = field(default_factory=list)
    dropped_chunks: int = 0
    quality: dict[str, Any] = field(default_factory=dict)
    audio: dict[str, Any] = field(default_factory=dict)
    report_path: str | None = None

    def add_metric_snapshot(self, metrics: dict[str, Any], *, collected_at: str | None = None) -> None:
        self.metric_snapshots.append({"collected_at": collected_at or _utc_now_iso(), "metrics": metrics})

    def add_caption_event(self, event: StudentCaptionEvent | dict[str, Any]) -> None:
        if isinstance(event, StudentCaptionEvent):
            payload = event.model_dump(mode="json")
            payload["student_receive_latency_ms"] = event.student_receive_latency_ms
            payload["stt_latency_ms"] = event.provider_latency_ms.get("stt")
            payload["translation_latency_ms"] = event.provider_latency_ms.get("translation")
        else:
            payload = dict(event)
        self.caption_events.append(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "request": self.request.model_dump(mode="json"),
            "sessions": self.sessions,
            "students": self.students,
            "caption_events": self.caption_events,
            "tts_events": self.tts_events,
            "metric_snapshots": self.metric_snapshots,
            "logs": self.logs,
            "provider_errors": self.provider_errors,
            "dropped_chunks": self.dropped_chunks,
            "quality": self.quality,
            "audio": _audio_payload(self.audio),
            "report_path": self.report_path,
        }

    def build_report(self) -> dict[str, Any]:
        return build_local_load_test_report(self.to_dict())


class LocalLoadTestRunner:
    def __init__(self, report_dir: Path | str = REPORT_DIR) -> None:
        self.report_dir = Path(report_dir)
        self.runs: dict[str, LocalLoadTestRun] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_events: dict[str, asyncio.Event] = {}

    async def start(self, request: LocalLoadTestRequest, *, app: Any | None = None) -> LocalLoadTestRun:
        run = LocalLoadTestRun(run_id=f"local_{uuid4().hex[:12]}", request=request, status="running", started_at=_utc_now_iso())
        self._initialize_run_shape(run)
        self.runs[run.run_id] = run
        stop_event = asyncio.Event()
        self._stop_events[run.run_id] = stop_event
        self._tasks[run.run_id] = asyncio.create_task(self._run(run, stop_event, app=app))
        self._write_report(run)
        return run

    def list_runs(self) -> list[LocalLoadTestRun]:
        return sorted(self.runs.values(), key=lambda item: item.created_at, reverse=True)

    def get(self, run_id: str) -> LocalLoadTestRun | None:
        return self.runs.get(run_id)

    async def stop(self, run_id: str) -> LocalLoadTestRun | None:
        run = self.runs.get(run_id)
        if run is None:
            return None
        run.status = "stopping"
        event = self._stop_events.get(run_id)
        if event is not None:
            event.set()
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if run.status == "stopping":
            run.status = "cancelled"
        run.completed_at = run.completed_at or _utc_now_iso()
        self._mark_open_sessions(run, run.status)
        self._write_report(run)
        return run

    def report(self, run_id: str) -> dict[str, Any] | None:
        run = self.runs.get(run_id)
        return run.build_report() if run is not None else None

    def _initialize_run_shape(self, run: LocalLoadTestRun) -> None:
        expected = run.request.sessions * run.request.students_per_session
        run.sessions = [
            {"session_index": index, "lesson_id": f"{run.run_id}_lesson_{index + 1}", "status": "running", "started_at": _utc_now_iso()}
            for index in range(run.request.sessions)
        ]
        run.students = [
            {
                "student_id": f"student_{index + 1}",
                "session_index": index // run.request.students_per_session,
                "connected": False,
                "captions_received": 0,
            }
            for index in range(expected)
        ]
        run.logs.append({"ts": _utc_now_iso(), "level": "info", "message": "local virtual lesson load test started"})

    async def _run(self, run: LocalLoadTestRun, stop_event: asyncio.Event, *, app: Any | None) -> None:
        try:
            self._prepare_audio(run)
            if run.request.record_runtime_metrics:
                self._record_runtime_snapshot(run, app)
            await self._simulate_light_mode(run, stop_event, app=app)
            if stop_event.is_set():
                run.status = "cancelled"
            else:
                run.status = "completed"
            run.completed_at = _utc_now_iso()
            self._mark_open_sessions(run, run.status)
            if run.request.record_runtime_metrics:
                self._record_runtime_snapshot(run, app)
        except asyncio.CancelledError:
            run.status = "cancelled"
            run.completed_at = _utc_now_iso()
            self._mark_open_sessions(run, run.status)
            raise
        except Exception as exc:
            run.status = "failed"
            run.completed_at = _utc_now_iso()
            run.provider_errors.append({"error": str(exc.__class__.__name__), "message": str(exc)})
            self._mark_open_sessions(run, run.status)
        finally:
            self._write_report(run)

    async def _simulate_light_mode(self, run: LocalLoadTestRun, stop_event: asyncio.Event, *, app: Any | None) -> None:
        ramp = min(float(run.request.student_join_ramp_seconds), float(run.request.duration_limit_seconds), 1.0)
        await _sleep_or_stop(stop_event, ramp)
        if stop_event.is_set():
            return
        for student in run.students:
            student["connected"] = True
        now = datetime.now(timezone.utc)
        for student in run.students[: min(50, len(run.students))]:
            student["captions_received"] = 1
            run.caption_events.append(
                {
                    "student_id": student["student_id"],
                    "lesson_id": run.sessions[student["session_index"]]["lesson_id"],
                    "caption_sequence": 1,
                    "is_final": True,
                    "source_text": run.request.reference_ru_text or "Mock local load-test caption",
                    "translations": {language: f"Mock {language}" for language in run.request.target_languages},
                    "student_receive_latency_ms": 25,
                    "stt_latency_ms": 0 if run.request.mode == "light" else 250,
                    "translation_latency_ms": 0 if run.request.mode == "light" else 120,
                    "received_at": now.isoformat(),
                }
            )
        if run.request.tts_enabled and run.request.mode in {"full", "light"}:
            selected_count = int(len(run.students) * run.request.tts_request_ratio)
            for index in range(max(0, selected_count)):
                run.tts_events.append(
                    {
                        "student_id": run.students[index]["student_id"],
                        "lesson_id": run.sessions[run.students[index]["session_index"]]["lesson_id"],
                        "caption_id": "caption_1",
                        "language": run.request.tts_languages[index % len(run.request.tts_languages)],
                        "status_code": 200,
                        "latency_ms": 25 if index else 120,
                        "cache_status": "hit" if index else "miss",
                        "cached": bool(index),
                    }
                )
        recognized_text = run.request.reference_ru_text or "Mock local load-test caption"
        run.quality = {
            "stt": stt_quality_report(run.request.reference_ru_text or recognized_text, recognized_text, segments=[{"text": recognized_text}]),
            "translations": {
                language: translation_quality_report(
                    language,
                    next((event["translations"].get(language, "") for event in run.caption_events if event.get("translations")), ""),
                    run.request.reference_translations.get(language),
                )
                for language in run.request.target_languages
            },
        }
        remaining = max(0.0, min(float(run.request.duration_limit_seconds), 2.0) - ramp)
        await _sleep_or_stop(stop_event, remaining)

    def _record_runtime_snapshot(self, run: LocalLoadTestRun, app: Any | None) -> None:
        metrics = runtime_metrics_snapshot(app) if app is not None else {}
        run.add_metric_snapshot(metrics)

    def _prepare_audio(self, run: LocalLoadTestRun) -> None:
        if not run.request.audio_file_id:
            return
        source = self._resolve_audio_file(run.request.audio_file_id)
        try:
            normalized = normalize_lesson_audio(source, output_dir=self.report_dir / "normalized" / run.run_id)
        except AudioNormalizationError:
            raise
        run.audio = {
            "source": str(source),
            "normalized": normalized.model_dump(mode="json"),
        }
        run.logs.append(
            {
                "ts": _utc_now_iso(),
                "level": "info",
                "message": "audio normalized to 16kHz mono PCM",
                "audio": {
                    "duration_seconds": normalized.duration_seconds,
                    "sample_rate": normalized.sample_rate,
                    "channels": normalized.channels,
                    "decoder": normalized.decoder,
                },
            }
        )

    def _resolve_audio_file(self, audio_file_id: str) -> Path:
        candidate = Path(audio_file_id)
        if candidate.exists():
            return candidate
        upload = self.report_dir / "uploads" / Path(audio_file_id).name
        if upload.exists():
            return upload
        return candidate

    def _mark_open_sessions(self, run: LocalLoadTestRun, status: str) -> None:
        for session in run.sessions:
            if session.get("status") == "running":
                session["status"] = status
                session["completed_at"] = _utc_now_iso()

    def _write_report(self, run: LocalLoadTestRun) -> None:
        report = run.build_report()
        path = self.report_dir / f"{run.run_id}.json"
        write_json_report(report, path)
        run.report_path = str(path)


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return
