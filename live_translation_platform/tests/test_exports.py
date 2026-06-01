from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.db.models import Lesson, TranscriptSegment
from app.export.html_exporter import HTMLExporter
from app.export.markdown_exporter import MarkdownExporter
from app.export.notes_generator import NotesGenerator
from app.export.srt_exporter import SRTExporter
from app.export.transcript_builder import TranscriptBuilder
from app.export.vtt_exporter import VTTExporter
from app.main import create_app


def test_transcript_builder_returns_ordered_final_segments(tmp_path, monkeypatch):
    app = _app_with_transcript(tmp_path, monkeypatch)

    transcript = TranscriptBuilder(app.state.database.session_factory).build("lesson_export")

    assert [segment.original_text_normalized for segment in transcript.segments] == ["Сегодня мы изучим C#", "Entity Framework помогает с базой данных"]
    assert all(segment.is_final for segment in transcript.segments)
    assert transcript.metrics["segments_count"] == 2
    assert transcript.metrics["glossary_corrections_count"] == 1


def test_srt_exporter_formats_timestamps_and_missing_translations(tmp_path, monkeypatch):
    app = _app_with_transcript(tmp_path, monkeypatch)
    transcript = TranscriptBuilder(app.state.database.session_factory).build("lesson_export")

    srt = SRTExporter().export(transcript, lang="kk")

    assert "1\n00:00:00,000 --> 00:00:04,000" in srt
    assert "C# туралы" in srt
    assert "Translation unavailable" in srt


def test_vtt_exporter_formats_timestamps(tmp_path, monkeypatch):
    app = _app_with_transcript(tmp_path, monkeypatch)
    transcript = TranscriptBuilder(app.state.database.session_factory).build("lesson_export")

    vtt = VTTExporter().export(transcript, lang="ru", normalized=True)

    assert vtt.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:04.000" in vtt
    assert "Сегодня мы изучим C#" in vtt


def test_json_markdown_html_and_notes_exports(tmp_path, monkeypatch):
    app = _app_with_transcript(tmp_path, monkeypatch)
    transcript = TranscriptBuilder(app.state.database.session_factory).build("lesson_export")

    payload = transcript.model_dump()
    markdown = MarkdownExporter().export(transcript, lang="all")
    html = HTMLExporter().export(transcript, lang="all")
    notes = NotesGenerator().generate(transcript, language="ru", mode="simple", include_glossary_terms=True)

    assert payload["segments"][0]["original_text_raw"] == "Сегодня мы изучим си шарп"
    assert payload["segments"][0]["glossary"]["normalization_changes"][0]["to"] == "C#"
    assert "# Export Lesson" in markdown
    assert "Сегодня мы изучим C#" in markdown
    assert html.startswith("<!doctype html>")
    assert "<html" in html
    assert "## Key Points" in notes.content_markdown
    assert "C#" in notes.content_markdown


def test_transcript_page_and_export_endpoints(tmp_path, monkeypatch):
    app = _app_with_transcript(tmp_path, monkeypatch)

    with TestClient(app) as client:
        page = client.get("/lessons/lesson_export/transcript")
        json_export = client.get("/api/lessons/lesson_export/exports/json")
        srt = client.get("/api/lessons/lesson_export/exports/srt?lang=ru&normalized=true")
        vtt = client.get("/api/lessons/lesson_export/exports/vtt?lang=kk")
        markdown = client.get("/api/lessons/lesson_export/exports/markdown?lang=all")
        html = client.get("/api/lessons/lesson_export/exports/html?lang=all")
        notes = client.post(
            "/api/lessons/lesson_export/notes/generate",
            json={"language": "ru", "mode": "simple", "include_glossary_terms": True},
        )

    assert page.status_code == 200
    assert "Transcript / Exports" in page.text
    assert "/api/lessons/lesson_export/exports/json" in page.text
    assert "/api/lessons/lesson_export/exports/srt" in page.text
    assert "/api/lessons/lesson_export/exports/vtt" in page.text
    assert "/api/lessons/lesson_export/exports/markdown" in page.text
    assert "/api/lessons/lesson_export/exports/html" in page.text
    assert json_export.headers["content-type"].startswith("application/json")
    assert json_export.json()["segments"][0]["original_text_normalized"] == "Сегодня мы изучим C#"
    assert srt.headers["content-type"].startswith("application/x-subrip")
    assert "00:00:00,000 --> 00:00:04,000" in srt.text
    assert vtt.headers["content-type"].startswith("text/vtt")
    assert vtt.text.startswith("WEBVTT")
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "# Export Lesson" in markdown.text
    assert html.headers["content-type"].startswith("text/html")
    assert "<!doctype html>" in html.text
    assert notes.status_code == 200
    assert "## Key Points" in notes.json()["content_markdown"]


def _app_with_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'exports.db').as_posix()}")
    app = create_app()
    base = datetime(2026, 5, 8, 10, 0, 0)
    with app.state.database.session_factory() as session:
        lesson = Lesson(
            lesson_id="lesson_export",
            title="Export Lesson",
            mode="mock",
            status="stopped",
            zoom_meeting_id="mock_1",
            zoom_meeting_uuid="uuid_1",
            zoom_join_url="https://example.test/join",
            zoom_start_url="https://example.test/start",
            stt_provider="mock",
            translation_provider="mock",
            target_languages="kk,uz,zh-Hans",
            glossary_id="glossary_1",
            glossary_enabled=True,
        )
        session.add(lesson)
        session.add(
            TranscriptSegment(
                lesson_id="lesson_export",
                original_text="partial should be ignored",
                translations_json="{}",
                is_final=False,
                provider_stt="mock",
                provider_translator="mock",
                created_at=base - timedelta(seconds=2),
            )
        )
        session.add(
            TranscriptSegment(
                lesson_id="lesson_export",
                original_text="Сегодня мы изучим C#",
                original_text_raw="Сегодня мы изучим си шарп",
                original_text_normalized="Сегодня мы изучим C#",
                translations_json='{"kk":"C# туралы","uz":"C# haqida","zh-Hans":"C#"}',
                normalization_changes_json='[{"from":"си шарп","to":"C#","term_id":"term_csharp","match_type":"phrase"}]',
                translation_postprocess_changes_json="[]",
                normalization_applied=True,
                translation_postprocess_applied=False,
                is_final=True,
                provider_stt="mock",
                provider_translator="mock",
                created_at=base,
            )
        )
        session.add(
            TranscriptSegment(
                lesson_id="lesson_export",
                original_text="Entity Framework помогает с базой данных",
                original_text_raw="энтити фреймворк помогает с базой данных",
                original_text_normalized="Entity Framework помогает с базой данных",
                translations_json='{"uz":"Entity Framework yordam beradi"}',
                normalization_changes_json="[]",
                translation_postprocess_changes_json="[]",
                is_final=True,
                provider_stt="mock",
                provider_translator="mock",
                created_at=base + timedelta(seconds=4),
            )
        )
        session.commit()
    return app
