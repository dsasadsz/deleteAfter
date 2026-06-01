import json

from fastapi import APIRouter, HTTPException, Request

from app.db.repositories import GlossaryRepository, LessonRepository
from app.glossary.default_glossaries import PROGRAMMING_RU
from app.glossary.schemas import GlossaryCreate, GlossaryTermCreate, GlossaryTermUpdate, GlossaryUpdate

router = APIRouter(tags=["glossaries"])


@router.get("/api/glossaries")
def list_glossaries(request: Request) -> list[dict]:
    with request.app.state.database.session_factory() as session:
        repo = GlossaryRepository(session)
        return [_glossary_response(item, len(repo.terms_for_glossary(item.id, enabled_only=False))) for item in repo.list_glossaries()]


@router.post("/api/glossaries")
def create_glossary(payload: GlossaryCreate, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        repo = GlossaryRepository(session)
        glossary = repo.create_glossary(
            name=payload.name,
            description=payload.description,
            domain=payload.domain,
            source_language=payload.source_language,
            target_languages=payload.target_languages,
            is_default=payload.is_default,
        )
        return _glossary_response(glossary, 0)


@router.get("/api/glossaries/{glossary_id}")
def get_glossary(glossary_id: str, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        repo = GlossaryRepository(session)
        glossary = repo.get_glossary(glossary_id)
        if glossary is None:
            raise HTTPException(status_code=404, detail="Glossary not found")
        terms = repo.terms_for_glossary(glossary_id, enabled_only=False)
        payload = _glossary_response(glossary, len(terms))
        payload["terms"] = [_term_response(term) for term in terms]
        return payload


@router.put("/api/glossaries/{glossary_id}")
def update_glossary(glossary_id: str, payload: GlossaryUpdate, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        repo = GlossaryRepository(session)
        glossary = repo.update_glossary(glossary_id, **payload.model_dump(exclude_unset=True))
        if glossary is None:
            raise HTTPException(status_code=404, detail="Glossary not found")
        return _glossary_response(glossary, len(repo.terms_for_glossary(glossary_id, enabled_only=False)))


@router.delete("/api/glossaries/{glossary_id}")
def delete_glossary(glossary_id: str, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        if not GlossaryRepository(session).delete_glossary(glossary_id):
            raise HTTPException(status_code=404, detail="Glossary not found")
        return {"status": "deleted", "glossary_id": glossary_id}


@router.post("/api/glossaries/{glossary_id}/terms")
def create_term(glossary_id: str, payload: GlossaryTermCreate, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        repo = GlossaryRepository(session)
        if repo.get_glossary(glossary_id) is None:
            raise HTTPException(status_code=404, detail="Glossary not found")
        term = repo.create_term(glossary_id=glossary_id, **payload.model_dump())
        return _term_response(term)


@router.put("/api/glossaries/{glossary_id}/terms/{term_id}")
def update_term(glossary_id: str, term_id: str, payload: GlossaryTermUpdate, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        repo = GlossaryRepository(session)
        term = repo.get_term(term_id)
        if term is None or term.glossary_id != glossary_id:
            raise HTTPException(status_code=404, detail="Glossary term not found")
        updated = repo.update_term(term_id, **payload.model_dump(exclude_unset=True))
        return _term_response(updated)


@router.delete("/api/glossaries/{glossary_id}/terms/{term_id}")
def delete_term(glossary_id: str, term_id: str, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        repo = GlossaryRepository(session)
        term = repo.get_term(term_id)
        if term is None or term.glossary_id != glossary_id:
            raise HTTPException(status_code=404, detail="Glossary term not found")
        repo.delete_term(term_id)
        return {"status": "deleted", "term_id": term_id}


@router.post("/api/glossaries/defaults/programming-ru")
def load_programming_default(request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        repo = GlossaryRepository(session)
        glossary = repo.upsert_glossary_by_name(
            name=PROGRAMMING_RU["name"],
            description=PROGRAMMING_RU["description"],
            domain=PROGRAMMING_RU["domain"],
            source_language=PROGRAMMING_RU["source_language"],
            target_languages=PROGRAMMING_RU["target_languages"],
            is_default=PROGRAMMING_RU["is_default"],
        )
        existing_terms = {term.canonical: term for term in repo.terms_for_glossary(glossary.id, enabled_only=False)}
        for item in PROGRAMMING_RU["terms"]:
            current = existing_terms.get(item["canonical"])
            if current:
                repo.update_term(current.id, **item)
            else:
                repo.create_term(glossary.id, **item)
        terms_count = len(repo.terms_for_glossary(glossary.id, enabled_only=False))
        return _glossary_response(glossary, terms_count)


@router.post("/api/lessons/{lesson_id}/glossary")
def set_lesson_glossary(lesson_id: str, payload: dict, request: Request) -> dict:
    glossary_id = payload.get("glossary_id")
    enabled = bool(payload.get("enabled", True))
    with request.app.state.database.session_factory() as session:
        if glossary_id and GlossaryRepository(session).get_glossary(glossary_id) is None:
            raise HTTPException(status_code=404, detail="Glossary not found")
        lesson = LessonRepository(session).set_glossary(lesson_id, glossary_id, enabled)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        return {"lesson_id": lesson_id, "glossary_id": lesson.glossary_id, "enabled": lesson.glossary_enabled}


@router.get("/api/lessons/{lesson_id}/glossary")
def get_lesson_glossary(lesson_id: str, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        lesson = LessonRepository(session).get(lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        glossary = GlossaryRepository(session).get_glossary(lesson.glossary_id) if lesson.glossary_id else None
        return {
            "lesson_id": lesson_id,
            "glossary_id": lesson.glossary_id,
            "enabled": lesson.glossary_enabled,
            "glossary": _glossary_response(glossary, 0) if glossary else None,
        }


def _glossary_response(glossary, terms_count: int) -> dict:
    return {
        "id": glossary.id,
        "name": glossary.name,
        "description": glossary.description,
        "domain": glossary.domain,
        "source_language": glossary.source_language,
        "target_languages": json.loads(glossary.target_languages_json or "[]"),
        "is_default": glossary.is_default,
        "terms_count": terms_count,
        "created_at": glossary.created_at.isoformat(),
        "updated_at": glossary.updated_at.isoformat(),
    }


def _term_response(term) -> dict:
    return {
        "id": term.id,
        "glossary_id": term.glossary_id,
        "source": term.source,
        "canonical": term.canonical,
        "aliases": json.loads(term.aliases_json or "[]"),
        "translations": json.loads(term.translations_json or "{}"),
        "case_sensitive": term.case_sensitive,
        "match_type": term.match_type,
        "priority": term.priority,
        "enabled": term.enabled,
    }
