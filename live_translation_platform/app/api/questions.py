from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from app.db.repositories import LessonQuestionRepository, LessonRepository
from app.integration.auth import authorize_websocket_access
from app.questions.audio_handler import StudentQuestionAudioHandler
from app.questions.schemas import QuestionRead, TextQuestionRequest
from app.questions.service import question_to_read
from app.security.scopes import QUESTION_MODERATE, QUESTION_READ, QUESTION_WRITE
from app.security.rate_limit import check_rate_limit, rate_limit_http_exception, rate_limit_key, subject_for_request
from app.security.schemas import TokenErrorCode
from app.security.tokens import TokenError, require_lesson, require_scope, verify_access_token

router = APIRouter(tags=["questions"])


def get_db(request: Request):
    yield from request.app.state.database.session()


@router.post("/api/lessons/{lesson_id}/questions/text", response_model=QuestionRead, status_code=status.HTTP_201_CREATED)
async def create_text_question(lesson_id: str, payload: TextQuestionRequest, request: Request) -> QuestionRead:
    _authorize_question_http(request, lesson_id, QUESTION_WRITE)
    await _enforce_question_rate_limit(request, lesson_id, "question_text", request.app.state.settings.question_text_rate_limit_per_minute, payload.student_id)
    try:
        question = await request.app.state.question_service.create_text_question(
            lesson_id,
            text=payload.text,
            source_language=payload.source_language,
            student_id=payload.student_id,
            student_name=payload.student_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return question_to_read(question)


@router.get("/api/lessons/{lesson_id}/questions", response_model=list[QuestionRead])
def list_questions(lesson_id: str, request: Request, db: Session = Depends(get_db)) -> list[QuestionRead]:
    _authorize_question_http(request, lesson_id, QUESTION_READ)
    if LessonRepository(db).get(lesson_id) is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    return [question_to_read(question) for question in LessonQuestionRepository(db).list_questions_for_lesson(lesson_id)]


@router.post("/api/lessons/{lesson_id}/questions/{question_id}/answer", response_model=QuestionRead)
async def mark_answered(lesson_id: str, question_id: int, request: Request) -> QuestionRead:
    _authorize_question_http(request, lesson_id, QUESTION_MODERATE)
    await _enforce_question_rate_limit(request, lesson_id, "question_moderation", request.app.state.settings.question_moderation_rate_limit_per_minute)
    _ensure_question_belongs_to_lesson(request, lesson_id, question_id)
    question = await request.app.state.question_service.mark_answered(question_id)
    if question is None or question.lesson_id != lesson_id:
        raise HTTPException(status_code=404, detail="Question not found")
    return question_to_read(question)


@router.post("/api/lessons/{lesson_id}/questions/{question_id}/dismiss", response_model=QuestionRead)
async def dismiss_question(lesson_id: str, question_id: int, request: Request) -> QuestionRead:
    _authorize_question_http(request, lesson_id, QUESTION_MODERATE)
    await _enforce_question_rate_limit(request, lesson_id, "question_moderation", request.app.state.settings.question_moderation_rate_limit_per_minute)
    _ensure_question_belongs_to_lesson(request, lesson_id, question_id)
    question = await request.app.state.question_service.dismiss(question_id)
    if question is None or question.lesson_id != lesson_id:
        raise HTTPException(status_code=404, detail="Question not found")
    return question_to_read(question)


@router.websocket("/ws/lessons/{lesson_id}/questions")
async def questions_websocket(lesson_id: str, websocket: WebSocket) -> None:
    if not await authorize_websocket_access(websocket, lesson_id, QUESTION_READ, allow_dev_bypass=True):
        return
    await websocket.accept()
    hub = websocket.app.state.question_hub
    await hub.connect(lesson_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(lesson_id, websocket)


@router.websocket("/ws/lessons/{lesson_id}/student-question-audio")
async def student_question_audio_websocket(lesson_id: str, websocket: WebSocket) -> None:
    if not await authorize_websocket_access(websocket, lesson_id, QUESTION_WRITE, allow_dev_bypass=True):
        return
    await StudentQuestionAudioHandler(
        websocket.app.state.question_service,
        websocket.app.state.settings,
        websocket.app.state.rate_limiter,
    ).handle(lesson_id, websocket)


def _authorize_question_http(request: Request, lesson_id: str, scope: str) -> None:
    settings = request.app.state.settings
    if not settings.websocket_auth_required and not settings.is_production and settings.allow_dev_ws_without_token:
        return
    token = request.query_params.get("token") or _bearer_token(request.headers.get("authorization"))
    try:
        payload = verify_access_token(token)
        require_lesson(payload, lesson_id)
        require_scope(payload, scope)
    except TokenError as exc:
        status_code = 403 if exc.code in {TokenErrorCode.TOKEN_SCOPE_MISSING, TokenErrorCode.TOKEN_LESSON_MISMATCH} else 401
        raise HTTPException(status_code=status_code, detail="Missing or invalid question access token.") from exc


def _ensure_question_belongs_to_lesson(request: Request, lesson_id: str, question_id: int) -> None:
    with request.app.state.database.session_factory() as session:
        question = LessonQuestionRepository(session).get_question(question_id)
        if question is None or question.lesson_id != lesson_id:
            raise HTTPException(status_code=404, detail="Question not found")


async def _enforce_question_rate_limit(request: Request, lesson_id: str, scope: str, limit: int, student_id: str | None = None) -> None:
    settings = request.app.state.settings
    if not settings.rate_limit_enabled:
        return
    subject = subject_for_request(request, lesson_id, student_id=student_id)
    key = rate_limit_key(scope, lesson_id, subject)
    result = await check_rate_limit(request.app.state.rate_limiter, key, limit)
    if not result.allowed:
        raise rate_limit_http_exception("QUESTION_RATE_LIMITED", result)


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    prefix = "Bearer "
    if value.startswith(prefix):
        return value[len(prefix) :]
    return None
