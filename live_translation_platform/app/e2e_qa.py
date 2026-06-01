import json
from datetime import datetime
from typing import Any

from app.db.models import E2EQATestRun


CHECKLIST_ITEMS = [
    ("teacher.zoom_lesson_created", "Teacher: Zoom lesson created"),
    ("teacher.zoom_video_works", "Teacher: Zoom video works"),
    ("teacher.browser_mic_streams", "Teacher: browser mic streams audio"),
    ("teacher.pipeline_captions", "Teacher: pipeline produces captions"),
    ("student.translated_captions", "Student: sees translated captions"),
    ("student.tts_audio", "Student: hears TTS translated audio"),
    ("student.audio_ducking", "Student: Zoom audio ducking/fallback works"),
    ("student.text_question", "Student: can ask text question"),
    ("student.voice_question", "Student: can ask voice question"),
    ("teacher.translated_questions", "Teacher: sees translated student questions"),
    ("teacher.answer_dismiss", "Teacher: can mark answered/dismiss"),
]

CHECKLIST_STATUSES = {"pending", "pass", "fail", "manual"}


def default_checklist() -> dict[str, dict[str, str | None]]:
    return {key: {"label": label, "status": "pending", "notes": "", "updated_at": None} for key, label in CHECKLIST_ITEMS}


def default_metrics() -> dict[str, Any]:
    return {
        "captions": {"final_count": 0},
        "tts": {},
        "ducking": {},
        "questions": {"text_count": 0, "voice_count": 0, "translated_ru_count": 0, "answered_or_dismissed_count": 0},
    }


def run_to_dict(run: E2EQATestRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "e2e_test_id": run.id,
        "lesson_id": run.lesson_id,
        "title": run.title,
        "status": run.status,
        "stt_provider": run.stt_provider,
        "translation_provider": run.translation_provider,
        "tts_provider": run.tts_provider,
        "tts_language": run.tts_language,
        "tts_queue_mode": run.tts_queue_mode,
        "chunk_ms": run.chunk_ms,
        "silence_commit_ms": run.silence_commit_ms,
        "max_segment_duration_ms": run.max_segment_duration_ms,
        "partials_enabled": run.partials_enabled,
        "checklist": _loads(run.checklist_json, default_checklist()),
        "metrics": _loads(run.metrics_json, default_metrics()),
        "notes": run.notes,
        "completed_by": run.completed_by,
        "teacher_url": f"/teacher/{run.lesson_id}" if run.lesson_id else None,
        "student_url": f"/student/{run.lesson_id}" if run.lesson_id else None,
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
        "completed_at": _iso(run.completed_at),
    }


def apply_capture(run: E2EQATestRun, payload: dict) -> tuple[dict, dict]:
    checklist = _loads(run.checklist_json, default_checklist())
    metrics = _loads(run.metrics_json, default_metrics())
    event_type = payload.get("event_type")
    if event_type == "final_caption":
        _capture_caption(checklist, metrics, payload)
    elif event_type == "tts":
        _capture_tts(checklist, metrics, payload)
    elif event_type == "student_question":
        _capture_question(checklist, metrics, payload)
    else:
        raise ValueError("event_type must be final_caption, tts, or student_question")
    return checklist, metrics


def build_e2e_report(runs: list[E2EQATestRun], lesson_id: str | None = None) -> dict[str, Any]:
    filtered = [run for run in runs if not lesson_id or run.lesson_id == lesson_id]
    rows = [run_to_dict(run) for run in filtered]
    recommended = _recommended_defaults(filtered)
    markdown = _markdown_report(rows, recommended)
    return {
        "total_runs": len(rows),
        "rows": rows,
        "recommended_defaults": recommended,
        "markdown": markdown,
    }


def _capture_caption(checklist: dict, metrics: dict, payload: dict) -> None:
    latency = payload.get("latency_ms") or {}
    captions = metrics.setdefault("captions", {})
    captions["final_count"] = int(captions.get("final_count") or 0) + 1
    captions["last_original_text"] = payload.get("original_text") or ""
    captions["last_translations"] = payload.get("translations") or {}
    captions["total_latency_ms"] = _int_or_none(latency.get("total_latency_ms") or latency.get("total"))
    captions["final_latency_ms"] = _int_or_none(latency.get("final_latency_ms"))
    captions["translation_latency_ms"] = _int_or_none(latency.get("translation_latency_ms") or latency.get("translation"))
    _mark(checklist, "teacher.pipeline_captions", "pass", "Final caption captured.")
    if captions["last_translations"]:
        _mark(checklist, "student.translated_captions", "pass", "Translated caption observed.")


def _capture_tts(checklist: dict, metrics: dict, payload: dict) -> None:
    tts = metrics.setdefault("tts", {})
    tts.update(
        {
            "enabled": bool(payload.get("enabled")),
            "provider": payload.get("provider"),
            "language": payload.get("language"),
            "queue_mode": payload.get("queue_mode"),
            "latency_ms": _int_or_none(payload.get("latency_ms")),
        }
    )
    ducking_status = payload.get("ducking_status") or "unavailable"
    metrics.setdefault("ducking", {})["status"] = ducking_status
    if payload.get("enabled"):
        _mark(checklist, "student.tts_audio", "pass", "TTS playback captured.")
    if ducking_status in {"controllable", "ducked", "restored", "ducked_restored", "manual_fallback"}:
        _mark(checklist, "student.audio_ducking", "pass", f"Ducking status: {ducking_status}.")


def _capture_question(checklist: dict, metrics: dict, payload: dict) -> None:
    questions = metrics.setdefault("questions", {})
    input_type = payload.get("input_type")
    if input_type == "text":
        questions["text_count"] = int(questions.get("text_count") or 0) + 1
        _mark(checklist, "student.text_question", "pass", "Text question captured.")
    if input_type == "voice":
        questions["voice_count"] = int(questions.get("voice_count") or 0) + 1
        _mark(checklist, "student.voice_question", "pass", "Voice question captured.")
    if payload.get("translated_text_ru"):
        questions["translated_ru_count"] = int(questions.get("translated_ru_count") or 0) + 1
        _mark(checklist, "teacher.translated_questions", "pass", "Russian translation observed.")
    if payload.get("status") in {"answered", "dismissed"}:
        questions["answered_or_dismissed_count"] = int(questions.get("answered_or_dismissed_count") or 0) + 1
        _mark(checklist, "teacher.answer_dismiss", "pass", "Question moderation observed.")


def _recommended_defaults(runs: list[E2EQATestRun]) -> dict[str, Any] | None:
    candidates = []
    for run in runs:
        checklist = _loads(run.checklist_json, default_checklist())
        metrics = _loads(run.metrics_json, default_metrics())
        if run.status != "completed":
            continue
        if any(item.get("status") != "pass" for item in checklist.values()):
            continue
        latency = (metrics.get("captions") or {}).get("total_latency_ms")
        candidates.append((latency if latency is not None else 999999999, run))
    if not candidates:
        return None
    _, chosen = sorted(candidates, key=lambda item: item[0])[0]
    return {
        "stt_provider": chosen.stt_provider,
        "translation_provider": chosen.translation_provider,
        "tts_provider": chosen.tts_provider,
        "tts_language": chosen.tts_language,
        "tts_queue_mode": chosen.tts_queue_mode,
        "chunk_ms": chosen.chunk_ms,
        "silence_commit_ms": chosen.silence_commit_ms,
        "max_segment_duration_ms": chosen.max_segment_duration_ms,
        "partials_enabled": chosen.partials_enabled,
    }


def _markdown_report(rows: list[dict], recommended: dict | None) -> str:
    lines = ["# Stage 22 E2E QA Report", ""]
    if recommended:
        lines.extend(["## Recommended Defaults", "", "```json", json.dumps(recommended, ensure_ascii=False, indent=2), "```", ""])
    else:
        lines.extend(["## Recommended Defaults", "", "No completed all-pass E2E QA runs yet.", ""])
    lines.extend(["## Runs", ""])
    for row in rows:
        passed = sum(1 for item in row["checklist"].values() if item.get("status") == "pass")
        total = len(row["checklist"])
        latency = (row["metrics"].get("captions") or {}).get("total_latency_ms")
        lines.append(f"- {row['e2e_test_id']}: {row['status']}, checklist {passed}/{total}, total latency {latency or 'n/a'} ms")
    return "\n".join(lines) + "\n"


def _mark(checklist: dict, key: str, status: str, notes: str) -> None:
    if key not in checklist:
        return
    checklist[key]["status"] = status
    checklist[key]["notes"] = notes
    checklist[key]["updated_at"] = datetime.utcnow().isoformat()


def _loads(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
