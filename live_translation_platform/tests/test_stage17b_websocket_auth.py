import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.zoom.models import ZoomMeeting


def test_access_token_create_verify_round_trip(monkeypatch):
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage17b-secret")
    from app.security.scopes import CAPTIONS_READ
    from app.security.tokens import create_access_token, verify_access_token

    token = create_access_token(
        {
            "sub": "student-123",
            "role": "student",
            "lesson_id": "lesson_token",
            "external_lesson_id": "external-1",
            "scopes": [CAPTIONS_READ],
        },
        ttl_seconds=3600,
    )

    payload = verify_access_token(token)

    assert payload.sub == "student-123"
    assert payload.role == "student"
    assert payload.lesson_id == "lesson_token"
    assert payload.external_lesson_id == "external-1"
    assert CAPTIONS_READ in payload.scopes
    assert payload.exp > payload.iat
    assert payload.jti


def test_expired_token_is_rejected(monkeypatch):
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage17b-secret")
    from app.security.schemas import TokenErrorCode
    from app.security.tokens import TokenError, create_access_token, verify_access_token

    token = create_access_token(
        {"sub": "student-123", "role": "student", "lesson_id": "lesson_token", "scopes": ["captions:read"]},
        ttl_seconds=-1,
    )

    with pytest.raises(TokenError) as exc:
        verify_access_token(token)

    assert exc.value.code == TokenErrorCode.TOKEN_EXPIRED


def test_wrong_lesson_is_rejected(monkeypatch):
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage17b-secret")
    from app.security.schemas import TokenErrorCode
    from app.security.tokens import TokenError, create_access_token, require_lesson, verify_access_token

    token = create_access_token(
        {"sub": "student-123", "role": "student", "lesson_id": "lesson_a", "scopes": ["captions:read"]},
        ttl_seconds=3600,
    )

    with pytest.raises(TokenError) as exc:
        require_lesson(verify_access_token(token), "lesson_b")

    assert exc.value.code == TokenErrorCode.TOKEN_LESSON_MISMATCH


def test_missing_scope_is_rejected(monkeypatch):
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage17b-secret")
    from app.security.schemas import TokenErrorCode
    from app.security.tokens import TokenError, create_access_token, require_scope, verify_access_token

    token = create_access_token(
        {"sub": "student-123", "role": "student", "lesson_id": "lesson_scope", "scopes": ["zoom:embed"]},
        ttl_seconds=3600,
    )

    with pytest.raises(TokenError) as exc:
        require_scope(verify_access_token(token), "captions:read")

    assert exc.value.code == TokenErrorCode.TOKEN_SCOPE_MISSING


def test_integration_endpoint_issues_student_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "student-token.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/student-token",
            headers=_headers(),
            json={
                "external_student_id": "student-123",
                "display_name": "Student",
                "scopes": ["captions:read", "zoom:embed"],
                "ttl_seconds": 3600,
            },
        )

    payload = response.json()
    assert response.status_code == 200, response.text
    assert payload["token"]
    assert datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00")) > datetime.now(timezone.utc)
    assert f"/ws/v1/lessons/{lesson['lesson_id']}/captions?token=" in payload["captions_websocket_url"]
    assert f"/api/v1/integration/lessons/{lesson['lesson_id']}/zoom/embed-config?token=" in payload["embed_config_url"]
    assert "start_url" not in response.text


def test_integration_endpoint_issues_teacher_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "teacher-token.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/teacher-token",
            headers=_headers(),
            json={
                "external_teacher_id": "teacher-123",
                "display_name": "Teacher",
                "scopes": ["audio:write", "diagnostics:read", "captions:read"],
                "ttl_seconds": 7200,
            },
        )

    payload = response.json()
    assert response.status_code == 200, response.text
    assert payload["token"]
    assert f"/ws/v1/lessons/{lesson['lesson_id']}/audio-ingest?token=" in payload["audio_ingest_websocket_url"]
    assert f"/ws/v1/lessons/{lesson['lesson_id']}/diagnostics?token=" in payload["diagnostics_websocket_url"]


def test_v1_captions_ws_accepts_valid_captions_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "captions-valid.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _token("student-123", "student", lesson["lesson_id"], ["captions:read"])
        with client.websocket_connect(f"/ws/v1/lessons/{lesson['lesson_id']}/captions?token={token}") as websocket:
            websocket.send_text("ping")

    assert app.state.caption_hub.connected_count(lesson["lesson_id"]) == 0


def test_v1_captions_ws_rejects_token_without_captions_scope(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "captions-forbidden.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _token("student-123", "student", lesson["lesson_id"], ["zoom:embed"])
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/ws/v1/lessons/{lesson['lesson_id']}/captions?token={token}"):
                pass

    assert exc.value.code == 4403


def test_audio_ingest_ws_accepts_audio_write_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "audio-valid.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _token("teacher-123", "teacher", lesson["lesson_id"], ["audio:write"])
        with client.websocket_connect(f"/ws/v1/lessons/{lesson['lesson_id']}/audio-ingest?token={token}") as websocket:
            websocket.send_text(json.dumps({"event": "audio_metadata", "sample_rate": 16000, "channels": 1, "format": "pcm_s16le"}))
            websocket.send_bytes(b"abc")
            status = client.get(f"/api/lessons/{lesson['lesson_id']}/browser-audio")

    assert status.status_code == 200
    assert status.json()["chunks_received"] == 1


def test_audio_ingest_ws_rejects_student_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "audio-forbidden.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _token("student-123", "student", lesson["lesson_id"], ["captions:read"])
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/ws/v1/lessons/{lesson['lesson_id']}/audio-ingest?token={token}"):
                pass

    assert exc.value.code == 4403


def test_audio_ingest_ws_rejects_missing_token_in_production_with_clear_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    app = _app(tmp_path, monkeypatch, "audio-missing-token-production.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/audio-ingest"):
                pass

    assert exc.value.code == 4401
    assert exc.value.reason == "WS_TOKEN_MISSING"


def test_audio_ingest_ws_allows_development_bypass_when_flag_is_true_even_if_auth_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("WEBSOCKET_AUTH_ENABLED", "true")
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "true")
    app = _app(tmp_path, monkeypatch, "audio-dev-bypass-auth-enabled.db", allow_dev_ws_without_token=True)

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/audio-ingest") as websocket:
            websocket.send_text(json.dumps({"event": "audio_metadata", "sample_rate": 16000, "channels": 1, "format": "pcm_s16le"}))
            websocket.send_bytes(b"abc")
            status = client.get(f"/api/lessons/{lesson['lesson_id']}/browser-audio")

    assert status.status_code == 200
    assert status.json()["chunks_received"] == 1


def test_audio_ingest_ws_rejects_missing_lesson_with_clear_reason(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "audio-missing-lesson.db")

    with TestClient(app) as client:
        token = _token("teacher-123", "teacher", "lesson_missing", ["audio:write"])
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/ws/v1/lessons/lesson_missing/audio-ingest?token={token}"):
                pass

    assert exc.value.code == 4404
    assert exc.value.reason == "LESSON_NOT_FOUND"


def test_diagnostics_ws_rejects_token_without_diagnostics_scope(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "diagnostics-forbidden.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _token("student-123", "student", lesson["lesson_id"], ["captions:read"])
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/ws/v1/lessons/{lesson['lesson_id']}/diagnostics?token={token}"):
                pass

    assert exc.value.code == 4403


def test_embed_config_accepts_zoom_embed_token_and_excludes_start_url(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_ID", "sdk-client")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_SECRET", "sdk-secret")
    app = _app(tmp_path, monkeypatch, "embed-token.db")
    app.state.zoom_api_client = FakeZoomAPIClient()

    with TestClient(app) as client:
        lesson = _create_lesson(client, mode="zoom", create_zoom_meeting=True)
        token = _token("student-123", "student", lesson["lesson_id"], ["zoom:embed"])
        response = client.get(f"/api/v1/integration/lessons/{lesson['lesson_id']}/zoom/embed-config?token={token}&user_name=Student")

    payload = response.json()
    assert response.status_code == 200, response.text
    assert payload["role"] == 0
    assert "signature" in payload
    assert "start_url" not in payload


def test_dev_mode_can_allow_ws_without_token_when_flag_is_true(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSOCKET_AUTH_ENABLED", "false")
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "true")
    monkeypatch.setenv("APP_ENV", "development")
    app = _app(tmp_path, monkeypatch, "dev-allow.db", websocket_auth_enabled=False)

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/captions") as websocket:
            websocket.send_text("ping")

    assert app.state.caption_hub.connected_count(lesson["lesson_id"]) == 0


def test_auth_enabled_rejects_missing_ws_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "missing-token.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/captions"):
                pass

    assert exc.value.code == 4401


def _app(tmp_path, monkeypatch, db_name: str, websocket_auth_enabled: bool = True, allow_dev_ws_without_token: bool | None = None):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("INTEGRATION_AUTH_ENABLED", "true")
    monkeypatch.setenv("INTEGRATION_API_KEYS", "dev-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage17b-secret")
    monkeypatch.setenv("WEBSOCKET_AUTH_ENABLED", "true" if websocket_auth_enabled else "false")
    if allow_dev_ws_without_token is None:
        allow_dev_ws_without_token = not websocket_auth_enabled
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "true" if allow_dev_ws_without_token else "false")
    monkeypatch.setenv("WEBSOCKET_AUTH_REQUIRED_IN_PRODUCTION", "true")
    return create_app()


def _headers() -> dict:
    return {"X-Integration-Key": "dev-key"}


def _create_lesson(client: TestClient, mode: str = "mock", create_zoom_meeting: bool = False) -> dict:
    response = client.post(
        "/api/v1/integration/lessons",
        headers=_headers(),
        json={
            "external_lesson_id": "ext-lesson",
            "title": "Integration Lesson",
            "mode": mode,
            "stt_provider": "mock",
            "translation_provider": "mock",
            "target_languages": ["kk"],
            "create_zoom_meeting": create_zoom_meeting,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _token(sub: str, role: str, lesson_id: str, scopes: list[str]) -> str:
    from app.security.tokens import create_access_token

    return create_access_token(
        {
            "sub": sub,
            "role": role,
            "lesson_id": lesson_id,
            "external_lesson_id": "ext-lesson",
            "scopes": scopes,
        },
        ttl_seconds=3600,
    )


class FakeZoomAPIClient:
    async def create_meeting(self, title: str) -> ZoomMeeting:
        return ZoomMeeting(
            meeting_id="123456789",
            meeting_uuid="uuid_stage17b",
            join_url="https://zoom.us/j/123456789?pwd=pass123",
            start_url="https://zoom.us/s/123456789?zak=secret",
            topic=title,
            created_at="2026-05-13T10:00:00Z",
            password="pass123",
        )
