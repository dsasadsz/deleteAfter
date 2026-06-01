import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _timeout_seconds(config: str, directive: str) -> int:
    match = re.search(rf"{directive}\s+(\d+)s;", config)
    assert match, f"missing {directive}"
    return int(match.group(1))


def test_nginx_examples_include_websocket_upgrade_headers():
    for path in (
        "deploy/nginx/live_translation_platform.conf.example",
        "docs/reverse-proxy-nginx.example.conf",
    ):
        config = _read(path)

        assert "proxy_http_version 1.1" in config
        assert "proxy_set_header Upgrade $http_upgrade;" in config
        assert "proxy_set_header Connection" in config


def test_nginx_examples_use_long_timeouts_and_disable_ws_buffering():
    for path in (
        "deploy/nginx/live_translation_platform.conf.example",
        "docs/reverse-proxy-nginx.example.conf",
    ):
        config = _read(path)

        assert _timeout_seconds(config, "proxy_read_timeout") >= 3600
        assert _timeout_seconds(config, "proxy_send_timeout") >= 3600
        assert "proxy_buffering off;" in config


def test_reverse_proxy_docs_cover_wss_tls_forwarded_headers_and_sticky_audio_routing():
    docs = _read("docs/reverse-proxy.md")

    assert "WSS" in docs
    assert "TLS" in docs
    assert "X-Forwarded-For" in docs
    assert "X-Forwarded-Proto" in docs
    assert "sticky routing" in docs or "single worker" in docs
    assert "teacher audio ingest" in docs


def test_reverse_proxy_docs_list_all_production_v1_websocket_routes():
    docs = _read("docs/reverse-proxy.md")

    for route in (
        "/ws/v1/lessons/{lesson_id}/captions",
        "/ws/v1/lessons/{lesson_id}/diagnostics",
        "/ws/v1/lessons/{lesson_id}/questions",
        "/ws/v1/lessons/{lesson_id}/student-question-audio",
        "/ws/v1/lessons/{lesson_id}/audio-ingest",
    ):
        assert route in docs


def test_reverse_proxy_docs_cover_common_wss_failure_modes():
    docs = _read("docs/reverse-proxy.md").lower()

    for expected in (
        "400/426",
        "401/403",
        "timeout",
        "buffer",
        "ws://",
        "wss://",
        "host",
        "forwarded",
    ):
        assert expected in docs


def test_reverse_proxy_docs_state_pubsub_does_not_move_raw_audio_ownership():
    docs = _read("docs/reverse-proxy.md")

    assert "Redis Pub/Sub" in docs
    assert "raw audio" in docs
    assert "session ownership" in docs


def test_reverse_proxy_docs_warn_load_test_endpoints_disabled_in_production():
    docs = _read("docs/reverse-proxy.md")

    assert "ENABLE_LOAD_TEST_ENDPOINTS=false" in docs
    assert "production" in docs


def test_caddy_example_mentions_wss_and_forwarded_headers():
    config = _read("docs/reverse-proxy-caddy.example")

    assert "WSS" in config
    assert "header_up Host {host}" in config
    assert "header_up X-Forwarded-Proto https" in config
    assert "header_up X-Forwarded-For {remote_host}" in config


def test_caddy_example_mentions_ws_route_and_audio_affinity_note():
    config = _read("docs/reverse-proxy-caddy.example")

    assert "/ws/" in config
    assert "sticky routing" in config or "single-worker" in config or "single worker" in config
    assert "teacher audio ingest" in config


def test_check_wss_routes_help_works():
    result = subprocess.run(
        [sys.executable, "scripts/check_wss_routes.py", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--base-url" in result.stdout
    assert "--ws-base-url" in result.stdout
    assert "--dev-bypass" in result.stdout


def test_check_wss_routes_help_documents_tokens_readiness_and_optional_routes():
    result = subprocess.run(
        [sys.executable, "scripts/check_wss_routes.py", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "after readiness" in result.stdout.lower()
    assert "scoped" in result.stdout.lower()
    assert "token" in result.stdout.lower()
    assert "--include-diagnostics" in result.stdout
    assert "--include-audio-routes" in result.stdout


def test_check_wss_routes_script_knows_representative_v1_routes():
    script = _read("scripts/check_wss_routes.py")

    for route in (
        "/ws/v1/lessons/{lesson_id}/captions",
        "/ws/v1/lessons/{lesson_id}/diagnostics",
        "/ws/v1/lessons/{lesson_id}/questions",
        "/ws/v1/lessons/{lesson_id}/student-question-audio",
        "/ws/v1/lessons/{lesson_id}/audio-ingest",
    ):
        assert route in script


def test_production_load_testing_and_runbooks_reference_wss_check_and_safety():
    for path in (
        "docs/production.md",
        "docs/load-testing.md",
        "docs/release-operator-runbook.md",
        "docs/staging-1000-ws-test-runbook.md",
    ):
        docs = _read(path)

        assert "check_wss_routes.py" in docs
        assert "ENABLE_LOAD_TEST_ENDPOINTS=false" in docs or "load-test endpoints disabled" in docs
        assert "WSS" in docs
        assert "sticky routing" in docs or "single worker" in docs or "single-worker" in docs
