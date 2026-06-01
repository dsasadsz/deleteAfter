import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.lessons import lesson_to_read
from app.db.repositories import (
    ComparisonRepository,
    DebugRepository,
    E2EQATestRepository,
    GlossaryRepository,
    LessonRepository,
    LiveMicTestRepository,
    MetricsRepository,
    RealTestRepository,
    SmokeTestRepository,
    TranscriptRepository,
)
from app.export.transcript_builder import TranscriptBuilder
from app.security.scopes import AUDIO_WRITE
from app.security.tokens import create_access_token
from app.smoke.provider_status import provider_status
from app.usage.cost_estimator import CostEstimator
from app.usage.repository import UsageRepository

router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    with request.app.state.database.session_factory() as db:
        lessons = [lesson_to_read(lesson).model_dump() for lesson in LessonRepository(db).list()]
    return templates.TemplateResponse("index.html", {"request": request, "lessons": lessons, "settings": request.app.state.settings})


@router.get("/teacher/{lesson_id}", response_class=HTMLResponse)
def teacher(request: Request, lesson_id: str) -> HTMLResponse:
    lesson = _get_lesson_or_404(request, lesson_id)
    with request.app.state.database.session_factory() as db:
        repo = GlossaryRepository(db)
        glossaries = [_glossary_to_view(item, len(repo.terms_for_glossary(item.id, enabled_only=False))) for item in repo.list_glossaries()]
    return templates.TemplateResponse(
        "teacher.html",
        {
            "request": request,
            "lesson": lesson,
            "glossaries": glossaries,
            "settings": request.app.state.settings,
            "teacher_audio_token": _teacher_audio_token(request, lesson),
        },
    )


@router.get("/student/{lesson_id}", response_class=HTMLResponse)
def student(request: Request, lesson_id: str) -> HTMLResponse:
    lesson = _get_lesson_or_404(request, lesson_id)
    return templates.TemplateResponse("student.html", {"request": request, "lesson": lesson, "settings": request.app.state.settings})


@router.get("/lessons/{lesson_id}/transcript", response_class=HTMLResponse)
def transcript_page(request: Request, lesson_id: str) -> HTMLResponse:
    try:
        transcript = TranscriptBuilder(request.app.state.database.session_factory).build(lesson_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    cost_summary = CostEstimator(request.app.state.database.session_factory, request.app.state.settings.default_currency).estimate_for_lesson(lesson_id)
    return templates.TemplateResponse(
        "transcript.html",
        {"request": request, "lesson_id": lesson_id, "transcript": transcript.model_dump(mode="json"), "cost_summary": cost_summary.model_dump(mode="json")},
    )


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    with request.app.state.database.session_factory() as db:
        lessons = [lesson_to_read(lesson).model_dump() for lesson in LessonRepository(db).list()]
        glossaries = [_glossary_to_view(item, len(GlossaryRepository(db).terms_for_glossary(item.id, enabled_only=False))) for item in GlossaryRepository(db).list_glossaries()]
        smoke_runs = [_smoke_run_to_view(run) for run in SmokeTestRepository(db).latest_runs()]
        comparisons = [_comparison_to_view(run) for run in ComparisonRepository(db).list_recent_comparisons()]
        real_tests = [_real_test_to_view(run) for run in RealTestRepository(db).latest()]
        usage_records = UsageRepository(db).list_records()
    metrics = request.app.state.metrics_repo.averages_by_lesson()
    usage_summary = CostEstimator(request.app.state.database.session_factory, request.app.state.settings.default_currency).estimate_from_usage_records(usage_records)
    transcripts = [
        {
            "lesson_id": item.lesson_id,
            "original_text": item.original_text,
            "translations": json.loads(item.translations_json),
            "created_at": item.created_at,
        }
        for item in request.app.state.transcript_repo.latest()
    ]
    errors = [
        {"lesson_id": item.lesson_id, "level": item.level, "message": item.message, "created_at": item.created_at}
        for item in request.app.state.debug_repo.latest()
        if item.level == "error"
    ]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "lessons": lessons,
            "metrics": metrics,
            "transcripts": transcripts,
            "errors": errors,
            "provider_status": provider_status(request.app.state.settings),
            "smoke_runs": smoke_runs,
            "comparisons": comparisons,
            "real_tests": real_tests,
            "usage_summary": usage_summary.model_dump(mode="json"),
            "settings": request.app.state.settings,
        },
    )


@router.get("/usage", response_class=HTMLResponse)
def usage_page(request: Request) -> HTMLResponse:
    with request.app.state.database.session_factory() as db:
        repo = UsageRepository(db)
        pricing = [_pricing_to_view(item) for item in repo.list_pricing()]
        records = repo.list_records()
    summary = CostEstimator(request.app.state.database.session_factory, request.app.state.settings.default_currency).estimate_from_usage_records(records)
    return templates.TemplateResponse(
        "usage.html",
        {
            "request": request,
            "pricing": pricing,
            "summary": summary.model_dump(mode="json"),
            "records": [_usage_record_to_view(item) for item in records[:100]],
        },
    )


@router.get("/smoke", response_class=HTMLResponse)
def smoke(request: Request) -> HTMLResponse:
    with request.app.state.database.session_factory() as db:
        lessons = [lesson_to_read(lesson).model_dump() for lesson in LessonRepository(db).list()]
        smoke_runs = [_smoke_run_to_view(run) for run in SmokeTestRepository(db).latest_runs()]
        glossary_repo = GlossaryRepository(db)
        glossary_items = [_glossary_to_view(item, len(glossary_repo.terms_for_glossary(item.id, enabled_only=False))) for item in glossary_repo.list_glossaries()]
    return templates.TemplateResponse(
        "smoke.html",
        {
            "request": request,
            "lessons": lessons,
            "provider_status": provider_status(request.app.state.settings),
            "smoke_runs": smoke_runs,
            "glossaries": glossary_items,
        },
    )


@router.get("/compare", response_class=HTMLResponse)
def compare(request: Request) -> HTMLResponse:
    with request.app.state.database.session_factory() as db:
        comparisons = [_comparison_to_view(run) for run in ComparisonRepository(db).list_recent_comparisons()]
        glossaries = [_glossary_to_view(item, len(GlossaryRepository(db).terms_for_glossary(item.id, enabled_only=False))) for item in GlossaryRepository(db).list_glossaries()]
    return templates.TemplateResponse(
        "compare.html",
        {
            "request": request,
            "provider_status": provider_status(request.app.state.settings),
            "comparisons": comparisons,
            "glossaries": glossaries,
        },
    )


@router.get("/glossaries", response_class=HTMLResponse)
def glossaries(request: Request) -> HTMLResponse:
    with request.app.state.database.session_factory() as db:
        repo = GlossaryRepository(db)
        items = [_glossary_to_view(item, len(repo.terms_for_glossary(item.id, enabled_only=False))) for item in repo.list_glossaries()]
    return templates.TemplateResponse("glossaries.html", {"request": request, "glossaries": items})


@router.get("/glossaries/{glossary_id}", response_class=HTMLResponse)
def glossary_detail(request: Request, glossary_id: str) -> HTMLResponse:
    with request.app.state.database.session_factory() as db:
        repo = GlossaryRepository(db)
        glossary = repo.get_glossary(glossary_id)
        if glossary is None:
            raise HTTPException(status_code=404, detail="Glossary not found")
        terms = [_term_to_view(term) for term in repo.terms_for_glossary(glossary_id, enabled_only=False)]
    return templates.TemplateResponse(
        "glossary_detail.html",
        {"request": request, "glossary": _glossary_to_view(glossary, len(terms)), "terms": terms},
    )


@router.get("/real-test", response_class=HTMLResponse)
def real_test(request: Request) -> HTMLResponse:
    with request.app.state.database.session_factory() as db:
        real_tests = [_real_test_to_view(run) for run in RealTestRepository(db).latest()]
    return templates.TemplateResponse(
        "real_test.html",
        {
            "request": request,
            "readiness": provider_status(request.app.state.settings),
            "real_tests": real_tests,
            "settings": request.app.state.settings,
        },
    )


@router.get("/live-tests", response_class=HTMLResponse)
def live_tests_page(request: Request) -> HTMLResponse:
    with request.app.state.database.session_factory() as db:
        lessons = [lesson_to_read(lesson).model_dump() for lesson in LessonRepository(db).list()]
        runs = [request.app.state.live_test_to_view(run) if hasattr(request.app.state, "live_test_to_view") else _live_test_to_view(run) for run in LiveMicTestRepository(db).latest()]
    return templates.TemplateResponse("live_tests.html", {"request": request, "lessons": lessons, "runs": runs})


@router.get("/live-tests/report", response_class=HTMLResponse)
def live_tests_report_page(
    request: Request,
    lesson_id: str | None = None,
    stt_provider: str | None = None,
    translation_provider: str | None = None,
    test_phrase_label: str | None = None,
) -> HTMLResponse:
    with request.app.state.database.session_factory() as db:
        from app.live_tests import build_live_test_report

        report = build_live_test_report(LiveMicTestRepository(db).latest(limit=500), lesson_id, stt_provider, translation_provider, test_phrase_label)
    filters = {
        "lesson_id": lesson_id or "",
        "stt_provider": stt_provider or "",
        "translation_provider": translation_provider or "",
        "test_phrase_label": test_phrase_label or "",
    }
    return templates.TemplateResponse("live_tests_report.html", {"request": request, "report": report, "filters": filters})


@router.get("/load-tests/local", response_class=HTMLResponse)
def local_load_tests_page(request: Request) -> HTMLResponse:
    runner = getattr(request.app.state, "local_load_test_runner", None)
    runs = []
    if runner is not None:
        runs = [run.to_dict() for run in runner.list_runs()]
    return templates.TemplateResponse("local_load_tests.html", {"request": request, "runs": runs})


@router.get("/e2e-test", response_class=HTMLResponse)
def e2e_test_page(request: Request) -> HTMLResponse:
    with request.app.state.database.session_factory() as db:
        lessons = [lesson_to_read(lesson).model_dump() for lesson in LessonRepository(db).list()]
        runs = [_e2e_test_to_view(run) for run in E2EQATestRepository(db).latest()]
    return templates.TemplateResponse("e2e_test.html", {"request": request, "lessons": lessons, "runs": runs})


@router.get("/e2e-test/report", response_class=HTMLResponse)
def e2e_test_report_page(request: Request, lesson_id: str | None = None) -> HTMLResponse:
    from app.e2e_qa import build_e2e_report

    with request.app.state.database.session_factory() as db:
        report = build_e2e_report(E2EQATestRepository(db).latest(limit=500), lesson_id=lesson_id)
    return templates.TemplateResponse("e2e_test_report.html", {"request": request, "report": report, "filters": {"lesson_id": lesson_id or ""}})


def _get_lesson_or_404(request: Request, lesson_id: str) -> dict:
    with request.app.state.database.session_factory() as db:
        lesson = LessonRepository(db).get(lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        return lesson_to_read(lesson).model_dump()


def _teacher_audio_token(request: Request, lesson: dict) -> str:
    settings = request.app.state.settings
    if not getattr(settings, "websocket_auth_required", False):
        return ""
    if not getattr(settings, "security_signing_secret", ""):
        return ""
    return create_access_token(
        {
            "sub": "teacher-demo-page",
            "role": "teacher",
            "lesson_id": lesson["lesson_id"],
            "scopes": [AUDIO_WRITE],
        },
        ttl_seconds=getattr(settings, "teacher_audio_token_ttl_seconds", 7200),
    )


def _smoke_run_to_view(run) -> dict:
    return {
        "id": run.id,
        "lesson_id": run.lesson_id,
        "status": run.status,
        "audio_mode": run.audio_mode,
        "stt_provider": run.stt_provider,
        "translation_provider": run.translation_provider,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "error": run.error,
    }


def _comparison_to_view(run) -> dict:
    summary = json.loads(run.summary_json or "{}")
    return {
        "id": run.id,
        "status": run.status,
        "audio_mode": run.audio_mode,
        "translation_provider": run.translation_provider,
        "run_mode": run.run_mode,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "skipped": json.loads(run.skipped_json or "[]"),
        "summary": summary,
        "error": run.error,
    }


def _real_test_to_view(run) -> dict:
    return {
        "id": run.id,
        "lesson_id": run.lesson_id,
        "status": run.status,
        "stt_provider": run.selected_stt_provider,
        "translation_provider": run.selected_translation_provider,
        "started_at": run.started_at,
        "error": run.error,
    }


def _live_test_to_view(run) -> dict:
    return {
        "id": run.id,
        "lesson_id": run.lesson_id,
        "status": run.status,
        "stt_provider": run.stt_provider,
        "translation_provider": run.translation_provider,
        "test_phrase_label": run.test_phrase_label,
        "total_latency_ms": run.total_latency_ms,
        "transcript_quality": run.transcript_quality,
        "translation_quality": run.translation_quality,
        "created_at": run.created_at,
    }


def _e2e_test_to_view(run) -> dict:
    from app.e2e_qa import run_to_dict

    return run_to_dict(run)


def _glossary_to_view(glossary, terms_count: int) -> dict:
    return {
        "id": glossary.id,
        "name": glossary.name,
        "description": glossary.description,
        "domain": glossary.domain,
        "source_language": glossary.source_language,
        "target_languages": json.loads(glossary.target_languages_json or "[]"),
        "is_default": glossary.is_default,
        "terms_count": terms_count,
    }


def _term_to_view(term) -> dict:
    return {
        "id": term.id,
        "source": term.source,
        "canonical": term.canonical,
        "aliases": json.loads(term.aliases_json or "[]"),
        "translations": json.loads(term.translations_json or "{}"),
        "match_type": term.match_type,
        "priority": term.priority,
        "enabled": term.enabled,
    }


def _pricing_to_view(pricing) -> dict:
    return {
        "id": pricing.id,
        "provider_type": pricing.provider_type,
        "provider_name": pricing.provider_name,
        "unit": pricing.unit,
        "price_per_unit": pricing.price_per_unit,
        "currency": pricing.currency,
        "source_note": pricing.source_note,
        "enabled": pricing.enabled,
    }


def _usage_record_to_view(record) -> dict:
    return {
        "provider_type": record.provider_type,
        "provider_name": record.provider_name,
        "metric_name": record.metric_name,
        "quantity": record.quantity,
        "unit": record.unit,
        "lesson_id": record.lesson_id,
        "smoke_test_id": record.smoke_test_id,
        "comparison_id": record.comparison_id,
        "created_at": record.created_at,
    }
