from datetime import datetime

from fastapi.testclient import TestClient

from app.db.models import Lesson
from app.main import create_app
from app.usage.cost_estimator import CostEstimator
from app.usage.pricing import default_pricing_rows
from app.usage.repository import UsageRepository
from app.usage.usage_tracker import UsageTracker, pcm_duration_seconds


def test_usage_duration_calculation_from_pcm_bytes():
    assert pcm_duration_seconds(byte_count=32000, sample_rate=16000, channels=1, bits_per_sample=16) == 1.0
    assert pcm_duration_seconds(byte_count=64000, sample_rate=16000, channels=2, bits_per_sample=16) == 1.0


def test_usage_tracker_records_stt_audio_and_translation(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'usage-tracker.db').as_posix()}")
    app = create_app()

    tracker = UsageTracker(app.state.database.session_factory)
    tracker.record_stt_audio("mock", lesson_id="lesson_usage", duration_seconds=60, byte_count=32000, chunks=10)
    tracker.record_translation("mock", lesson_id="lesson_usage", source_text="Привет C#", target_languages=["kk", "uz"])

    with app.state.database.session_factory() as session:
        records = UsageRepository(session).records_for_scope(lesson_id="lesson_usage")

    assert any(record.metric_name == "audio_duration_seconds" and record.quantity == 60 for record in records)
    assert any(record.metric_name == "source_characters" and record.quantity == len("Привет C#") for record in records)


def test_cost_estimator_warns_without_pricing_and_calculates_with_price(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'cost.db').as_posix()}")
    app = create_app()
    tracker = UsageTracker(app.state.database.session_factory)
    tracker.record_stt_audio("elevenlabs", lesson_id="lesson_cost", duration_seconds=120, byte_count=64000, chunks=20)

    missing = CostEstimator(app.state.database.session_factory, default_currency="USD").estimate_for_lesson("lesson_cost")
    assert missing.total_estimated_cost == 0
    assert missing.warnings

    with app.state.database.session_factory() as session:
        UsageRepository(session).create_pricing(
            provider_type="stt",
            provider_name="elevenlabs",
            unit="audio_minute",
            price_per_unit=0.2,
            currency="USD",
            effective_from=datetime.utcnow(),
            source_note="test price",
        )
    priced = CostEstimator(app.state.database.session_factory, default_currency="USD").estimate_for_lesson("lesson_cost")

    assert priced.total_estimated_cost == 0.4
    assert priced.provider_costs[0].estimated_cost == 0.4


def test_pricing_crud_and_default_loader(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'pricing.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        created = client.post(
            "/api/usage/pricing",
            json={
                "provider_type": "translation",
                "provider_name": "azure_translator",
                "unit": "million_characters",
                "price_per_unit": 10,
                "currency": "USD",
                "source_note": "manual test price",
            },
        )
        pricing_id = created.json()["id"]
        updated = client.put(f"/api/usage/pricing/{pricing_id}", json={"price_per_unit": 12})
        defaults = client.post("/api/usage/pricing/defaults")
        listed = client.get("/api/usage/pricing")

    assert created.status_code == 200
    assert updated.json()["price_per_unit"] == 12
    assert defaults.json()["created"] >= len(default_pricing_rows())
    assert any("Placeholder pricing" in item["source_note"] for item in listed.json())


def test_lesson_usage_and_cost_endpoints_return_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'usage-api.db').as_posix()}")
    app = create_app()
    _create_lesson(app, "lesson_usage_api")
    tracker = UsageTracker(app.state.database.session_factory)
    tracker.record_stt_audio("mock", lesson_id="lesson_usage_api", duration_seconds=30, byte_count=16000, chunks=5)
    tracker.record_translation("mock", lesson_id="lesson_usage_api", source_text="Сегодня C#", target_languages=["kk"])
    tracker.record_caption("mock", "mock", lesson_id="lesson_usage_api", is_final=True)

    with TestClient(app) as client:
        usage = client.get("/api/lessons/lesson_usage_api/usage")
        cost = client.get("/api/lessons/lesson_usage_api/cost")
        summary = client.get("/api/usage/summary")

    assert usage.status_code == 200
    assert usage.json()["audio_minutes"] == 0.5
    assert usage.json()["translation_characters"] == len("Сегодня C#")
    assert usage.json()["captions"] == 1
    assert cost.status_code == 200
    assert "provider_costs" in cost.json()
    assert summary.json()["total_audio_minutes"] >= 0.5


def test_usage_page_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'usage-page.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/usage")

    assert response.status_code == 200
    assert "Usage / Cost Analytics" in response.text


def _create_lesson(app, lesson_id: str) -> None:
    with app.state.database.session_factory() as session:
        session.add(
            Lesson(
                lesson_id=lesson_id,
                title="Usage Lesson",
                mode="mock",
                status="stopped",
                zoom_meeting_id="mock_usage",
                zoom_meeting_uuid="uuid_usage",
                zoom_join_url="https://example.test/join",
                zoom_start_url="https://example.test/start",
                stt_provider="mock",
                translation_provider="mock",
                target_languages="kk,uz,zh-Hans",
            )
        )
        session.commit()
