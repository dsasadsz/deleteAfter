import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_reverse_proxy_examples_exist_and_include_websocket_upgrade_headers():
    nginx = (ROOT / "docs" / "reverse-proxy-nginx.example.conf").read_text(encoding="utf-8")
    caddy = (ROOT / "docs" / "reverse-proxy-caddy.example").read_text(encoding="utf-8")

    assert "proxy_set_header Upgrade $http_upgrade;" in nginx
    assert 'proxy_set_header Connection "upgrade";' in nginx
    assert "proxy_http_version 1.1;" in nginx
    assert "proxy_read_timeout" in nginx
    assert "location /api/" in nginx
    assert "location /ws/" in nginx
    assert "location /static/" in nginx
    assert "WSS" in nginx or "wss://" in nginx
    assert "reverse_proxy app:8000" in caddy
    assert "WebSocket" in caddy or "websocket" in caddy


def test_production_checklist_includes_wss_and_safe_required_services():
    checklist = (ROOT / "docs" / "production-deployment-checklist.md").read_text(encoding="utf-8")

    required_phrases = [
        "WSS",
        "WebSocket upgrade",
        "APP_ENV=production",
        "WEBSOCKET_AUTH_ENABLED=true",
        "ENABLE_DEBUG_ENDPOINTS=false",
        "ENABLE_LOAD_TEST_ENDPOINTS=false",
        "DATABASE_URL=postgresql+psycopg://",
        "REDIS_ENABLED=true",
        "REDIS_PUBSUB_ENABLED=true",
        "REDIS_RATE_LIMIT_ENABLED=true",
        "TTS_SHARED_CACHE_BACKEND=disk",
        "/api/health/ready",
        "python scripts/check_deployment_readiness.py",
    ]
    for phrase in required_phrases:
        assert phrase in checklist


def test_production_compose_contains_postgres_redis_and_shared_tts_cache_volume():
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "postgres:" in compose
    assert "redis:" in compose
    assert "postgres_data:" in compose
    assert "tts_cache:" in compose
    assert "DATABASE_URL: ${COMPOSE_DATABASE_URL:-postgresql+psycopg://live_translation:${POSTGRES_PASSWORD}@postgres:5432/live_translation}" in compose
    assert "REDIS_URL: ${COMPOSE_REDIS_URL:-redis://redis:6379/0}" in compose
    assert "REDIS_ENABLED: ${REDIS_ENABLED:-true}" in compose
    assert "REDIS_PUBSUB_ENABLED: ${REDIS_PUBSUB_ENABLED:-true}" in compose
    assert "REDIS_RATE_LIMIT_ENABLED: ${REDIS_RATE_LIMIT_ENABLED:-true}" in compose
    assert "TTS_SHARED_CACHE_BACKEND: ${TTS_SHARED_CACHE_BACKEND:-disk}" in compose
    assert "TTS_SHARED_CACHE_DIR: ${TTS_SHARED_CACHE_DIR:-/app/tmp/tts_cache}" in compose
    assert "postgres:\n        condition: service_healthy" in compose
    assert "redis:\n        condition: service_healthy" in compose
    assert "change-this" not in compose.lower()
    assert "secret" not in compose.lower()


def test_deployment_readiness_script_help_works():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_deployment_readiness.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--base-url" in result.stdout
    assert "/api/health/ready" in result.stdout


def test_integration_contract_json_remains_valid():
    payload = json.loads((ROOT / "docs" / "integration-contract.json").read_text(encoding="utf-8"))

    assert isinstance(payload, dict)
    assert "http_endpoints" in payload
    assert "/api/v1/integration/spec" in payload["http_endpoints"]


def test_production_docs_reference_reverse_proxy_and_readiness_script():
    production = (ROOT / "docs" / "production.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    project_report = (ROOT / "docs" / "PROJECT_REPORT.md").read_text(encoding="utf-8")
    integration = (ROOT / "docs" / "integration-contract.md").read_text(encoding="utf-8")

    assert "reverse-proxy-nginx.example.conf" in production
    assert "reverse-proxy-caddy.example" in production
    assert "check_deployment_readiness.py" in production
    assert "wss://" in production
    assert "production-deployment-checklist.md" in readme
    assert "WSS" in project_report
    assert "WSS" in integration
