import json

from fastapi.testclient import TestClient

from app.db.repositories import ComparisonRepository
from app.main import create_app


def test_comparison_repository_create_get_update(tmp_path, monkeypatch):
    db_path = tmp_path / "compare-repo.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    app = create_app()

    with app.state.database.session_factory() as session:
        repo = ComparisonRepository(session)
        comparison = repo.create_comparison(
            audio_mode="mock_chunks",
            audio_sample_id=None,
            stt_providers=["mock"],
            translation_provider="mock",
            target_languages=["kk"],
            run_mode="sequential",
            skipped=[],
        )
        item = repo.add_item(comparison.id, "mock", "mock", smoke_test_id="smoke_1", status="running")
        repo.update_item_status(item.id, "completed")
        repo.update_result(item.id, {"original_text": "hello", "latency_ms": {"total_server": 1}})
        repo.complete_comparison(comparison.id, {"fastest_total_provider": "mock"})

        stored = repo.get_comparison(comparison.id)
        items = repo.items_for_comparison(comparison.id)

    assert stored is not None
    assert stored.status == "completed"
    assert json.loads(stored.summary_json)["fastest_total_provider"] == "mock"
    assert items[0].status == "completed"
    assert json.loads(items[0].result_json)["original_text"] == "hello"


def test_compare_run_with_mock_provider_completes_and_saves_result(tmp_path, monkeypatch):
    db_path = tmp_path / "compare.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/compare/run",
            json={
                "audio_mode": "mock_chunks",
                "stt_providers": ["mock"],
                "translation_provider": "mock",
                "target_languages": ["kk", "uz", "zh-Hans"],
            },
        )
        assert response.status_code == 200
        comparison_id = response.json()["comparison_id"]
        status = client.get(f"/api/compare/{comparison_id}").json()

    assert status["status"] == "completed"
    assert status["results"][0]["stt_provider"] == "mock"
    assert status["results"][0]["status"] == "completed"
    assert status["results"][0]["original_text"]
    assert set(status["results"][0]["translations"]) == {"kk", "uz", "zh-Hans"}
    assert status["results"][0]["latency_ms"]["total_server"] >= 0


def test_compare_run_skips_not_ready_real_provider(tmp_path, monkeypatch):
    db_path = tmp_path / "compare-skip.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")

    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/compare/run",
            json={
                "audio_mode": "mock_chunks",
                "stt_providers": ["mock", "elevenlabs"],
                "translation_provider": "mock",
                "target_languages": ["kk"],
            },
        )
        assert response.status_code == 200
        payload = response.json()
        status = client.get(f"/api/compare/{payload['comparison_id']}").json()

    assert payload["skipped"] == [{"stt_provider": "elevenlabs", "reason": "missing ELEVENLABS_API_KEY"}]
    assert status["skipped"] == payload["skipped"]
    assert [result["stt_provider"] for result in status["results"]] == ["mock"]


def test_compare_websocket_receives_provider_progress(tmp_path, monkeypatch):
    db_path = tmp_path / "compare-ws.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/compare/run",
            json={
                "audio_mode": "mock_chunks",
                "stt_providers": ["mock"],
                "translation_provider": "mock",
                "target_languages": ["kk"],
            },
        )
        comparison_id = response.json()["comparison_id"]

        with client.websocket_connect(f"/ws/compare/{comparison_id}") as websocket:
            events = []
            for _ in range(8):
                event = websocket.receive_json()
                events.append(event["event"])
                if event["event"] == "comparison_completed":
                    break

    assert "provider_started" in events
    assert "provider_completed" in events
    assert events[-1] == "comparison_completed"


def test_compare_page_returns_200(tmp_path, monkeypatch):
    db_path = tmp_path / "compare-page.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/compare")

    assert response.status_code == 200
    assert "Provider Comparison" in response.text
