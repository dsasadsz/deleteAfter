import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import LiveMicTestRun
from app.db.repositories import LiveMicTestRepository


QUALITY_VALUES = {"good", "acceptable", "poor"}


class FinalCaptionCaptureService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def capture(self, payload: dict) -> LiveMicTestRun | None:
        lesson_id = payload.get("lesson_id")
        if not lesson_id:
            return None
        with self.session_factory() as session:
            return LiveMicTestRepository(session).capture_caption(str(lesson_id), payload)


def run_to_dict(run: LiveMicTestRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "live_test_id": run.id,
        "lesson_id": run.lesson_id,
        "status": run.status,
        "audio_source": run.audio_source,
        "stt_provider": run.stt_provider,
        "translation_provider": run.translation_provider,
        "chunk_ms": run.chunk_ms,
        "silence_commit_ms": run.silence_commit_ms,
        "max_segment_duration_ms": run.max_segment_duration_ms,
        "partials_enabled": run.partials_enabled,
        "test_phrase_label": run.test_phrase_label,
        "expected_text": run.expected_text,
        "tuning_snapshot": _loads(run.tuning_snapshot_json, {}),
        "provider_metrics": _loads(run.provider_metrics_json, {}),
        "last_caption": _loads(run.last_caption_json, {}),
        "transcript": run.transcript,
        "translations": _loads(run.translations_json, {}),
        "first_partial_latency_ms": run.first_partial_latency_ms,
        "final_latency_ms": run.final_latency_ms,
        "translation_latency_ms": run.translation_latency_ms,
        "total_latency_ms": run.total_latency_ms,
        "client_caption_latency_ms": run.client_caption_latency_ms,
        "chunks_sent": run.chunks_sent,
        "chunks_dropped": run.chunks_dropped,
        "commit_reason": run.commit_reason,
        "transcript_quality": run.transcript_quality,
        "translation_quality": run.translation_quality,
        "quality_notes": run.quality_notes,
        "quality_status": quality_status(run),
        "error": run.error,
        "completed_by": run.completed_by,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "updated_at": _iso(run.updated_at),
        "completed_at": _iso(run.completed_at),
    }


def quality_status(run: LiveMicTestRun) -> str:
    return "rated" if run.transcript_quality and run.translation_quality else "quality_unrated"


def build_live_test_report(
    runs: list[LiveMicTestRun],
    lesson_id: str | None = None,
    stt_provider: str | None = None,
    translation_provider: str | None = None,
    test_phrase_label: str | None = None,
) -> dict:
    filtered = []
    for run in runs:
        if lesson_id and run.lesson_id != lesson_id:
            continue
        if stt_provider and run.stt_provider != stt_provider:
            continue
        if translation_provider and run.translation_provider != translation_provider:
            continue
        if test_phrase_label and run.test_phrase_label != test_phrase_label:
            continue
        filtered.append(run)

    rows = [run_to_dict(run) for run in sorted(filtered, key=lambda item: _latency_sort(item.total_latency_ms))]
    completed = [run for run in filtered if run.status == "completed"]
    good = [run for run in completed if run.transcript_quality == "good" and run.translation_quality == "good" and run.total_latency_ms is not None]
    best = sorted(good, key=lambda item: (item.total_latency_ms or 999999999, item.chunks_dropped or 0, item.final_latency_ms or 999999999))[:1]
    recommended = None
    reason = "insufficient completed good-quality runs"
    if best:
        chosen = best[0]
        recommended = {
            "stt_provider": chosen.stt_provider,
            "translation_provider": chosen.translation_provider,
            "chunk_ms": chosen.chunk_ms,
            "silence_commit_ms": chosen.silence_commit_ms,
            "max_segment_duration_ms": chosen.max_segment_duration_ms,
            "partials_enabled": chosen.partials_enabled,
        }
        reason = "chosen because transcript_quality=good, translation_quality=good, lowest total_latency_ms"
    return {
        "total_runs": len(rows),
        "rows": rows,
        "fastest_first_partial": _run_min(rows, "first_partial_latency_ms"),
        "fastest_final": _run_min(rows, "final_latency_ms"),
        "fastest_total": _run_min(rows, "total_latency_ms"),
        "recommended_settings": recommended,
        "recommendation_reason": reason,
    }


def _run_min(rows: list[dict], key: str) -> dict | None:
    candidates = [row for row in rows if row.get(key) is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda row: row[key])


def _latency_sort(value: int | None) -> int:
    return value if value is not None else 999999999


def _loads(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
