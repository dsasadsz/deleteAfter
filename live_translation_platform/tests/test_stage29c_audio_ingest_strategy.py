from pathlib import Path

from app.config import Settings
from app.production import production_config_check


ROOT = Path(__file__).resolve().parents[1]
ADR_PATH = ROOT / "docs" / "adr" / "audio-ingest-session-ownership.md"
WARNING = "MULTI_WORKER_AUDIO_INGEST_REQUIRES_STICKY_ROUTING"


def test_audio_ingest_session_ownership_adr_exists():
    assert ADR_PATH.exists()


def test_audio_ingest_session_ownership_adr_compares_scaling_options():
    doc = ADR_PATH.read_text(encoding="utf-8")

    for option in (
        "single worker",
        "sticky routing",
        "Redis Streams",
        "queue-based audio ingest",
        "dedicated lesson worker",
        "external media pipeline",
        "actor/session ownership service",
    ):
        assert option in doc


def test_audio_ingest_session_ownership_adr_recommends_mvp_and_defers_distributed_ingest():
    doc = ADR_PATH.read_text(encoding="utf-8")

    assert "6 lessons x ~80 students" in doc
    assert "Redis Pub/Sub for captions/questions" in doc
    assert "postpone distributed audio ingest" in doc


def test_production_config_warns_with_stage29c_token_for_risky_multi_worker_audio_ingest():
    result = production_config_check(
        _production_settings(
            app_worker_count=2,
            browser_audio_enabled=True,
            websocket_sticky_routing_enabled=False,
            distributed_lesson_sessions_enabled=False,
        )
    )

    assert WARNING in result["warnings"]
    assert result["checks"]["teacher_audio_ingest"]["safe_deployment_mode"] is False


def test_production_config_does_not_warn_when_sticky_routing_is_declared():
    result = production_config_check(
        _production_settings(
            app_worker_count=2,
            browser_audio_enabled=True,
            websocket_sticky_routing_enabled=True,
            distributed_lesson_sessions_enabled=False,
        )
    )

    assert WARNING not in result["warnings"]
    assert result["checks"]["teacher_audio_ingest"]["safe_deployment_mode"] is True


def test_docs_state_redis_pubsub_does_not_carry_raw_audio():
    docs = [
        ADR_PATH.read_text(encoding="utf-8"),
        (ROOT / "docs" / "production.md").read_text(encoding="utf-8"),
        (ROOT / "docs" / "load-testing.md").read_text(encoding="utf-8"),
    ]

    assert any("Redis Pub/Sub does not carry raw" in doc for doc in docs)


def _production_settings(**overrides) -> Settings:
    defaults = {
        "app_env": "production",
        "public_base_url": "https://example.test",
        "cors_allowed_origins": "https://example.test",
        "trusted_hosts": "example.test",
        "database_url": "postgresql://user:pass@db:5432/app",
        "enable_openapi_docs": False,
        "enable_debug_endpoints": False,
        "log_format": "json",
        "zoom_webhook_signature_required_in_production": False,
        "websocket_auth_required_in_production": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)
