from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from app.db.repositories import LessonNotesRepository
from app.export.html_exporter import HTMLExporter
from app.export.markdown_exporter import MarkdownExporter
from app.export.notes_generator import NotesGenerator
from app.export.srt_exporter import SRTExporter
from app.export.transcript_builder import TranscriptBuilder
from app.export.vtt_exporter import VTTExporter

router = APIRouter(tags=["exports"])


class NotesGenerateRequest(BaseModel):
    language: str = "ru"
    mode: str = "simple"
    include_glossary_terms: bool = True


@router.get("/api/lessons/{lesson_id}/transcript")
def transcript_json(lesson_id: str, request: Request, include_partials: bool = False) -> dict:
    return _build(request, lesson_id, include_partials).model_dump(mode="json")


@router.get("/api/lessons/{lesson_id}/exports/json")
def export_json(lesson_id: str, request: Request, include_partials: bool = False) -> JSONResponse:
    transcript = _build(request, lesson_id, include_partials)
    return JSONResponse(transcript.model_dump(mode="json"))


@router.get("/api/lessons/{lesson_id}/exports/srt")
def export_srt(lesson_id: str, request: Request, lang: str = "ru", normalized: bool = True) -> PlainTextResponse:
    transcript = _build(request, lesson_id)
    return PlainTextResponse(
        SRTExporter().export(transcript, lang=lang, normalized=normalized),
        media_type="application/x-subrip; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{lesson_id}-{lang}.srt"'},
    )


@router.get("/api/lessons/{lesson_id}/exports/vtt")
def export_vtt(lesson_id: str, request: Request, lang: str = "ru", normalized: bool = True) -> PlainTextResponse:
    transcript = _build(request, lesson_id)
    return PlainTextResponse(
        VTTExporter().export(transcript, lang=lang, normalized=normalized),
        media_type="text/vtt; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{lesson_id}-{lang}.vtt"'},
    )


@router.get("/api/lessons/{lesson_id}/exports/markdown")
def export_markdown(lesson_id: str, request: Request, lang: str = "all", normalized: bool = True) -> PlainTextResponse:
    transcript = _build(request, lesson_id)
    return PlainTextResponse(
        MarkdownExporter().export(transcript, lang=lang, normalized=normalized),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{lesson_id}-{lang}.md"'},
    )


@router.get("/api/lessons/{lesson_id}/exports/html")
def export_html(lesson_id: str, request: Request, lang: str = "all", normalized: bool = True) -> HTMLResponse:
    transcript = _build(request, lesson_id)
    return HTMLResponse(
        HTMLExporter().export(transcript, lang=lang, normalized=normalized),
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{lesson_id}-{lang}.html"'},
    )


@router.post("/api/lessons/{lesson_id}/notes/generate")
def generate_notes(lesson_id: str, payload: NotesGenerateRequest, request: Request) -> dict:
    transcript = _build(request, lesson_id)
    try:
        notes = NotesGenerator().generate(
            transcript,
            language=payload.language,
            mode=payload.mode,
            include_glossary_terms=payload.include_glossary_terms,
        )
    except NotImplementedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with request.app.state.database.session_factory() as session:
        saved = LessonNotesRepository(session).save(
            lesson_id,
            payload.language,
            payload.mode,
            notes.content_markdown,
            notes.content_html,
            notes.metadata,
        )
    result = notes.model_dump(mode="json")
    result["notes_id"] = saved.id
    return result


def _build(request: Request, lesson_id: str, include_partials: bool = False):
    try:
        return TranscriptBuilder(request.app.state.database.session_factory).build(lesson_id, include_partials=include_partials)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
