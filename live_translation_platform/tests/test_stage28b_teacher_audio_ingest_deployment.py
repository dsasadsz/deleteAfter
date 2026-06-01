from pathlib import Path

from app.config import Settings
from app.production import production_config_check


ROOT = Path(__file__).resolve().parents[1]


def test_teacher_audio_ingest_deployment_doc_exists_and_states_process_local_limits():
    doc = (ROOT / "docs" / "teacher-audio-ingest-deployment.md").read_text(encoding="utf-8")

    assert "process-local" in doc
    assert "LessonSessionManager" in doc
    assert "BrowserAudioManager" in doc
    assert "raw teacher audio" in doc
    assert "Redis Pub/Sub" in doc
    assert "does not carry raw teacher audio" in doc


def test_production_config_warns_for_multi_worker_teacher_audio_without_sticky_routing():
    settings = Settings(
        app_env="production",
        public_base_url="https://example.test",
        cors_allowed_origins="https://example.test",
        trusted_hosts="example.test",
        database_url="postgresql://user:pass@db:5432/app",
        enable_openapi_docs=False,
        enable_debug_endpoints=False,
        log_format="json",
        zoom_webhook_signature_required_in_production=False,
        websocket_auth_required_in_production=False,
        browser_audio_enabled=True,
        app_worker_count=2,
        websocket_sticky_routing_enabled=False,
        distributed_lesson_sessions_enabled=False,
    )

    result = production_config_check(settings)

    assert result["status"] == "ok"
    assert any(
        "teacher audio ingest" in warning
        and "multi-worker" in warning
        and "sticky routing" in warning
        and "distributed lesson sessions" in warning
        for warning in result["warnings"]
    )
    assert result["checks"]["teacher_audio_ingest"]["safe_deployment_mode"] is False


def test_production_config_accepts_single_worker_teacher_audio_ingest():
    settings = Settings(
        app_env="production",
        public_base_url="https://example.test",
        cors_allowed_origins="https://example.test",
        trusted_hosts="example.test",
        database_url="postgresql://user:pass@db:5432/app",
        enable_openapi_docs=False,
        enable_debug_endpoints=False,
        log_format="json",
        zoom_webhook_signature_required_in_production=False,
        websocket_auth_required_in_production=False,
        browser_audio_enabled=True,
        app_worker_count=1,
        websocket_sticky_routing_enabled=False,
        distributed_lesson_sessions_enabled=False,
    )

    result = production_config_check(settings)

    assert result["status"] == "ok"
    assert not any("teacher audio ingest" in warning for warning in result["warnings"])
    assert result["checks"]["teacher_audio_ingest"]["safe_deployment_mode"] is True


def test_production_config_accepts_multi_worker_teacher_audio_with_sticky_routing():
    settings = Settings(
        app_env="production",
        public_base_url="https://example.test",
        cors_allowed_origins="https://example.test",
        trusted_hosts="example.test",
        database_url="postgresql://user:pass@db:5432/app",
        enable_openapi_docs=False,
        enable_debug_endpoints=False,
        log_format="json",
        zoom_webhook_signature_required_in_production=False,
        websocket_auth_required_in_production=False,
        browser_audio_enabled=True,
        app_worker_count=2,
        websocket_sticky_routing_enabled=True,
        distributed_lesson_sessions_enabled=False,
    )

    result = production_config_check(settings)

    assert result["status"] == "ok"
    assert not any("teacher audio ingest" in warning for warning in result["warnings"])
    assert result["checks"]["teacher_audio_ingest"]["safe_deployment_mode"] is True
