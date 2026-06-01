import hashlib
import hmac
import io
import json
import logging
import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.realtime.rtms_manager import RTMSManager
from app.schemas.rtms import RTMSStatus
from app.zoom.zoom_webhooks import build_url_validation_response, extract_zoom_webhook_context, validate_zoom_webhook_signature


def _create_lesson(client: TestClient, mode: str = "mock") -> dict:
    response = client.post(
        "/api/lessons",
        json={
            "title": "RTMS lesson",
            "mode": mode,
            "stt_provider": "mock",
            "translation_provider": "mock",
            "target_languages": ["kk", "uz", "zh-Hans"],
        },
    )
    assert response.status_code == 201
    return response.json()


def _signed_zoom_headers(raw_body: bytes, secret: str, timestamp: str | None = None) -> dict[str, str]:
    timestamp = timestamp or str(int(time.time()))
    message = b"v0:" + timestamp.encode() + b":" + raw_body
    signature = "v0=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return {"x-zm-request-timestamp": timestamp, "x-zm-signature": signature}


def _post_zoom_webhook(client: TestClient, payload: dict, headers: dict[str, str] | None = None):
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    return client.post("/api/zoom/webhook", content=raw_body, headers={"content-type": "application/json", **(headers or {})})


def test_zoom_webhook_url_validation_response_uses_secret_token():
    response = build_url_validation_response("plain-token", "secret-token")

    assert response["plainToken"] == "plain-token"
    assert len(response["encryptedToken"]) == 64


def test_validate_zoom_webhook_signature_returns_structured_ok_result():
    raw_body = b'{"event":"meeting.rtms_started"}'
    result = validate_zoom_webhook_signature(_signed_zoom_headers(raw_body, "secret-token"), raw_body, "secret-token", 300)

    assert result.valid is True
    assert result.reason == "ok"


def test_validate_zoom_webhook_signature_rejects_stale_timestamp():
    raw_body = b'{"event":"meeting.rtms_started"}'
    old_timestamp = str(int(time.time()) - 600)
    result = validate_zoom_webhook_signature(_signed_zoom_headers(raw_body, "secret-token", old_timestamp), raw_body, "secret-token", 300)

    assert result.valid is False
    assert result.reason == "timestamp_out_of_tolerance"


def test_extract_zoom_webhook_context_supports_multiple_payload_shapes():
    context = extract_zoom_webhook_context(
        {
            "event": "meeting.rtms_started",
            "payload": {
                "object": {
                    "id": "123456789",
                    "uuid": "uuid_123",
                    "rtms_stream_id": "stream_123",
                }
            },
        }
    )

    assert context.event == "meeting.rtms_started"
    assert context.meeting_id == "123456789"
    assert context.meeting_uuid == "uuid_123"
    assert context.rtms_stream_id == "stream_123"


def test_zoom_webhook_url_validation_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'url-validation.db').as_posix()}")
    monkeypatch.setenv("ZOOM_WEBHOOK_SECRET_TOKEN", "secret-token")
    monkeypatch.setenv("ZOOM_WEBHOOK_SIGNATURE_VALIDATION_ENABLED", "true")
    app = create_app()

    with TestClient(app) as client:
        payload = {"event": "endpoint.url_validation", "payload": {"plainToken": "plain-token"}}
        raw_body = json.dumps(payload, separators=(",", ":")).encode()
        response = _post_zoom_webhook(client, payload, _signed_zoom_headers(raw_body, "secret-token"))

    assert response.status_code == 200
    assert response.json()["plainToken"] == "plain-token"
    assert "encryptedToken" in response.json()


def test_zoom_webhook_valid_signature_is_accepted_when_validation_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'valid-signature.db').as_posix()}")
    monkeypatch.setenv("ZOOM_WEBHOOK_SECRET_TOKEN", "secret-token")
    monkeypatch.setenv("ZOOM_WEBHOOK_SIGNATURE_VALIDATION_ENABLED", "true")
    app = create_app()

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        payload = {
            "event": "meeting.rtms_started",
            "payload": {"rtms_stream_id": "stream_123", "object": {"id": lesson["zoom"]["meeting_id"], "uuid": lesson["zoom"]["meeting_uuid"]}},
        }
        raw_body = json.dumps(payload, separators=(",", ":")).encode()
        response = _post_zoom_webhook(client, payload, _signed_zoom_headers(raw_body, "secret-token"))

    assert response.status_code == 200
    assert response.json()["matched_lesson_id"] == lesson["lesson_id"]


def test_zoom_webhook_invalid_signature_is_rejected_when_validation_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'invalid-signature.db').as_posix()}")
    monkeypatch.setenv("ZOOM_WEBHOOK_SECRET_TOKEN", "secret-token")
    monkeypatch.setenv("ZOOM_WEBHOOK_SIGNATURE_VALIDATION_ENABLED", "true")
    app = create_app()

    with TestClient(app) as client:
        response = _post_zoom_webhook(
            client,
            {"event": "meeting.rtms_started", "payload": {"object": {"id": "missing"}}},
            {"x-zm-request-timestamp": str(int(time.time())), "x-zm-signature": "v0=bad"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "HTTP_401"


def test_zoom_webhook_missing_headers_rejected_when_validation_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'missing-headers.db').as_posix()}")
    monkeypatch.setenv("ZOOM_WEBHOOK_SECRET_TOKEN", "secret-token")
    monkeypatch.setenv("ZOOM_WEBHOOK_SIGNATURE_VALIDATION_ENABLED", "true")
    app = create_app()

    with TestClient(app) as client:
        response = _post_zoom_webhook(client, {"event": "meeting.rtms_started", "payload": {"object": {"id": "missing"}}})

    assert response.status_code == 401


def test_zoom_webhook_missing_secret_returns_config_error_when_validation_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'missing-secret.db').as_posix()}")
    monkeypatch.setenv("ZOOM_WEBHOOK_SECRET_TOKEN", "")
    monkeypatch.setenv("ZOOM_WEBHOOK_SIGNATURE_VALIDATION_ENABLED", "true")
    app = create_app()

    with TestClient(app) as client:
        response = _post_zoom_webhook(client, {"event": "meeting.rtms_started", "payload": {"object": {"id": "missing"}}})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "HTTP_500"


def test_zoom_webhook_stale_timestamp_rejected_when_validation_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'stale-timestamp.db').as_posix()}")
    monkeypatch.setenv("ZOOM_WEBHOOK_SECRET_TOKEN", "secret-token")
    monkeypatch.setenv("ZOOM_WEBHOOK_SIGNATURE_VALIDATION_ENABLED", "true")
    app = create_app()

    payload = {"event": "meeting.rtms_started", "payload": {"object": {"id": "missing"}}}
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    with TestClient(app) as client:
        response = _post_zoom_webhook(client, payload, _signed_zoom_headers(raw_body, "secret-token", str(int(time.time()) - 600)))

    assert response.status_code == 401


def test_zoom_webhook_dev_bypass_when_validation_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'dev-bypass.db').as_posix()}")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ZOOM_WEBHOOK_SIGNATURE_VALIDATION_ENABLED", "false")
    monkeypatch.setenv("ZOOM_WEBHOOK_SECRET_TOKEN", "secret-token")
    app = create_app()

    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    logger = logging.getLogger("app.api.zoom")
    logger.addHandler(handler)
    try:
        with TestClient(app) as client:
            response = _post_zoom_webhook(client, {"event": "meeting.rtms_started", "payload": {"object": {"id": "missing"}}})
    finally:
        logger.removeHandler(handler)

    assert response.status_code == 200
    assert response.json()["status"] == "unmatched"
    logged = log_stream.getvalue()
    assert "Zoom webhook signature validation is disabled in development." in logged
    assert "secret-token" not in logged
    assert "x-zm-signature" not in logged


def test_zoom_webhook_production_requires_signature_even_when_enable_flag_false(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'prod-required-webhook.db').as_posix()}")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ZOOM_WEBHOOK_SECRET_TOKEN", "secret-token")
    monkeypatch.setenv("ZOOM_WEBHOOK_SIGNATURE_VALIDATION_ENABLED", "false")
    monkeypatch.setenv("ZOOM_WEBHOOK_SIGNATURE_REQUIRED_IN_PRODUCTION", "true")
    app = create_app()

    with TestClient(app) as client:
        response = _post_zoom_webhook(client, {"event": "meeting.rtms_started", "payload": {"object": {"id": "missing"}}})

    assert response.status_code == 401


def test_rtms_started_webhook_maps_to_lesson_by_meeting_id_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'webhook-map.db').as_posix()}")
    monkeypatch.setenv("ZOOM_RTMS_ENABLED", "false")
    app = create_app()

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(
            "/api/zoom/webhook",
            json={
                "event": "meeting.rtms_started",
                "payload": {
                    "rtms_stream_id": "stream_123",
                    "object": {"id": lesson["zoom"]["meeting_id"], "uuid": lesson["zoom"]["meeting_uuid"]},
                },
            },
        )
        status_response = client.get(f"/api/lessons/{lesson['lesson_id']}/rtms")

    assert response.status_code == 200
    assert response.json()["matched_lesson_id"] == lesson["lesson_id"]
    assert status_response.json()["rtms_stream_id"] == "stream_123"
    assert status_response.json()["rtms_status"] == RTMSStatus.NOT_CONFIGURED
    assert "disabled" in status_response.json()["rtms_error"]


def test_zoom_webhook_unknown_meeting_returns_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'unknown.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/api/zoom/webhook",
            json={
                "event": "meeting.rtms_started",
                "payload": {"object": {"id": "missing_meeting", "uuid": "missing_uuid"}},
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "unmatched"


def test_rtms_manager_prevents_duplicate_clients_for_same_lesson(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'duplicates.db').as_posix()}")
    monkeypatch.setenv("ZOOM_RTMS_ENABLED", "false")
    app = create_app()

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        first = client.post(f"/api/lessons/{lesson['lesson_id']}/start-rtms")
        second = client.post(f"/api/lessons/{lesson['lesson_id']}/start-rtms")

    assert first.status_code == 400
    assert second.status_code == 400
    assert len(app.state.rtms_manager.clients) <= 1


def test_debug_websocket_receives_rtms_status_event(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'debug-ws.db').as_posix()}")
    monkeypatch.setenv("ZOOM_RTMS_ENABLED", "false")
    app = create_app()

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/debug") as websocket:
            response = client.post(
                "/api/zoom/webhook",
                json={
                    "event": "meeting.rtms_started",
                    "payload": {
                        "rtms_stream_id": "stream_debug",
                        "object": {"id": lesson["zoom"]["meeting_id"]},
                    },
                },
            )
            event = websocket.receive_json()

    assert response.status_code == 200
    assert event["event"] == "rtms_status"
    assert event["lesson_id"] == lesson["lesson_id"]
    assert event["status"] == RTMSStatus.NOT_CONFIGURED
