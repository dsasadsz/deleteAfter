import json
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


ROOT = Path(__file__).resolve().parents[1]

V1_WS_ROUTES = (
    "/ws/v1/lessons/{lesson_id}/captions",
    "/ws/v1/lessons/{lesson_id}/diagnostics",
    "/ws/v1/lessons/{lesson_id}/questions",
    "/ws/v1/lessons/{lesson_id}/student-question-audio",
    "/ws/v1/lessons/{lesson_id}/audio-ingest",
)


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_nginx_reverse_proxy_example_exists_and_preserves_websocket_upgrade_headers():
    config = _read("deploy/nginx/live_translation_platform.conf.example")

    assert "location /ws/" in config
    assert "location /api/" in config
    assert "proxy_http_version 1.1;" in config
    assert "proxy_set_header Upgrade $http_upgrade;" in config
    assert 'proxy_set_header Connection "upgrade";' in config
    assert "proxy_set_header Host $host;" in config
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in config
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in config
    assert "proxy_read_timeout 3600s;" in config
    assert "proxy_send_timeout 3600s;" in config
    assert "client_max_body_size" in config
    assert "ssl_certificate" in config
    assert "WSS requires TLS" in config
    assert "change-this" not in config.lower()


def test_reverse_proxy_doc_lists_required_routes_and_common_wss_failures():
    doc = _read("docs/reverse-proxy.md")

    assert "# Reverse Proxy and WSS Setup" in doc
    for route in V1_WS_ROUTES:
        assert route in doc
    for route in (
        "/ws/lessons/{lesson_id}/captions",
        "/ws/lessons/{lesson_id}/audio-ingest",
        "/ws/lessons/{lesson_id}/questions",
    ):
        assert route in doc
    for phrase in (
        "400/426 WebSocket upgrade failed",
        "403 token issues",
        "1006 abnormal closure",
        "timeout after 60s",
        "mixed content ws://",
        "CORS/trusted hosts",
        "integration key backend-only",
        "browsers use scoped tokens",
    ):
        assert phrase in doc


def test_check_wss_routes_help_works_and_mentions_required_options():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_wss_routes.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--base-url" in result.stdout
    assert "--ws-base-url" in result.stdout
    assert "--lesson-id" in result.stdout
    assert "--token" in result.stdout
    assert "--dev-bypass" in result.stdout


def test_check_wss_routes_refuses_websocket_checks_without_token_or_dev_bypass():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_wss_routes.py"),
            "--base-url",
            "http://127.0.0.1:9",
            "--ws-base-url",
            "ws://127.0.0.1:9",
            "--lesson-id",
            "lesson_1",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert payload["captions_ws_connected"] is False
    assert any("Pass --token or --dev-bypass" in item for item in payload["errors"])
    assert "secret" not in result.stdout.lower()


def test_production_docs_link_reverse_proxy_doc_and_wss_checker():
    production = _read("docs/production.md")
    readme = _read("README.md")
    project_report = _read("docs/PROJECT_REPORT.md")
    load_testing = _read("docs/load-testing.md")

    for content in (production, readme, project_report, load_testing):
        assert "docs/reverse-proxy.md" in content
    assert "scripts/check_wss_routes.py" in production
    assert "deploy/nginx/live_translation_platform.conf.example" in production


def test_env_example_documents_public_https_and_wss_origins():
    env = _read(".env.example")

    assert "PUBLIC_BASE_URL=https://python-service.example.com" in env
    assert "PUBLIC_WS_BASE_URL=wss://python-service.example.com" in env
    assert "TRUSTED_HOSTS=python-service.example.com" in env
    assert "ALLOWED_ORIGINS=https://csharp-site.example.com" in env


def test_public_ws_base_url_overrides_token_websocket_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'public-ws.db').as_posix()}")
    monkeypatch.setenv("INTEGRATION_API_KEYS", "dev-key")
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage26e-test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://python-service.example.com")
    monkeypatch.setenv("PUBLIC_WS_BASE_URL", "wss://ws.python-service.example.com")
    app = create_app()

    with TestClient(app) as client:
        lesson = client.post(
            "/api/v1/integration/lessons",
            headers={"X-Integration-Key": "dev-key"},
            json={
                "external_lesson_id": "stage26e",
                "title": "Stage 26E",
                "mode": "mock",
                "stt_provider": "mock",
                "translation_provider": "mock",
                "target_languages": ["kk"],
            },
        ).json()
        token_response = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/student-token",
            headers={"X-Integration-Key": "dev-key"},
            json={"external_student_id": "student-1"},
        ).json()

    assert token_response["captions_websocket_url"].startswith("wss://ws.python-service.example.com/ws/v1/")
    assert token_response["questions_websocket_url"].startswith("wss://ws.python-service.example.com/ws/v1/")
    assert "integration_key" not in token_response["captions_websocket_url"]


def test_browser_examples_do_not_expose_integration_key():
    for path in (
        "examples/js/student-lesson-client.js",
        "examples/js/captions-client.js",
        "examples/js/captions-client.html",
    ):
        content = _read(path)
        assert "integration_key" not in content
        assert "X-Integration-Key" not in content
