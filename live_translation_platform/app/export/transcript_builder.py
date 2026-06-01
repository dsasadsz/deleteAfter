import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import LatencyMetric, Lesson, TranscriptSegment
from app.export.schemas import TranscriptExport, TranscriptExportSegment


class TranscriptBuilder:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def build(self, lesson_id: str, include_partials: bool = False) -> TranscriptExport:
        with self.session_factory() as session:
            lesson = session.get(Lesson, lesson_id)
            if lesson is None:
                raise ValueError("Lesson not found")
            statement = select(TranscriptSegment).where(TranscriptSegment.lesson_id == lesson_id)
            if not include_partials:
                statement = statement.where(TranscriptSegment.is_final.is_(True))
            rows = list(session.scalars(statement.order_by(TranscriptSegment.created_at, TranscriptSegment.id)).all())
            metrics = list(session.scalars(select(LatencyMetric).where(LatencyMetric.lesson_id == lesson_id)).all())
            return self._build_payload(lesson, rows, metrics)

    def _build_payload(self, lesson: Lesson, rows: list[TranscriptSegment], metrics: list[LatencyMetric]) -> TranscriptExport:
        base = rows[0].created_at if rows else datetime.utcnow()
        segments = []
        for index, row in enumerate(rows):
            start = row.start_time or row.created_at or base + timedelta(seconds=index * 4)
            if start < base:
                start = base + timedelta(seconds=index * 4)
            end = row.end_time or start + timedelta(seconds=4)
            speaker = json.loads(row.speaker_json or "{}")
            latency = json.loads(row.latency_json or "{}") or _latency_for_index(metrics, index)
            segments.append(
                TranscriptExportSegment(
                    id=row.id,
                    start_time=_timestamp(start - base),
                    end_time=_timestamp(end - base),
                    speaker=speaker.get("name") or "Teacher",
                    original_text_raw=row.original_text_raw or row.original_text,
                    original_text_normalized=row.original_text_normalized or row.original_text,
                    translations=json.loads(row.translations_json or "{}"),
                    glossary={
                        "normalization_changes": json.loads(row.normalization_changes_json or "[]"),
                        "postprocess_changes": json.loads(row.translation_postprocess_changes_json or "[]"),
                    },
                    latency_ms=latency,
                    provider={"stt": row.provider_stt, "translator": row.provider_translator},
                    is_final=row.is_final,
                    created_at=row.created_at,
                )
            )
        glossary_corrections = sum(
            len(segment.glossary.get("normalization_changes", [])) + len(segment.glossary.get("postprocess_changes", []))
            for segment in segments
        )
        return TranscriptExport(
            lesson_id=lesson.lesson_id,
            title=lesson.title,
            mode=lesson.mode,
            audio_source=lesson.audio_source,
            providers={"stt": lesson.stt_provider, "translator": lesson.translation_provider},
            target_languages=[item for item in lesson.target_languages.split(",") if item],
            glossary={"enabled": lesson.glossary_enabled, "glossary_id": lesson.glossary_id},
            segments=segments,
            summary={"segments": len(segments)},
            metrics={
                "segments_count": len(segments),
                "glossary_corrections_count": glossary_corrections,
                "latency": _latency_summary(metrics),
            },
        )


def _timestamp(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds() * 1000))
    hours, remainder = divmod(total, 3600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"


def _latency_for_index(metrics: list[LatencyMetric], index: int) -> dict:
    if index >= len(metrics):
        return {}
    metric = metrics[index]
    return {"stt": metric.stt_ms, "translation": metric.translation_ms, "total": metric.total_ms}


def _latency_summary(metrics: list[LatencyMetric]) -> dict:
    if not metrics:
        return {"avg_stt": 0, "avg_translation": 0, "avg_total": 0}
    return {
        "avg_stt": round(sum(item.stt_ms for item in metrics) / len(metrics), 1),
        "avg_translation": round(sum(item.translation_ms for item in metrics) / len(metrics), 1),
        "avg_total": round(sum(item.total_ms for item in metrics) / len(metrics), 1),
    }
