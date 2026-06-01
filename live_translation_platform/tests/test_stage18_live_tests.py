from fastapi.testclient import TestClient

from app.db.models import LiveMicTestRun
from app.db.repositories import TranscriptRepository
from app.main import create_app


def _create_lesson(client: TestClient) -> dict:
    response = client.post(
        "/api/lessons",
        json={
            "title": "Live mic baseline lesson",
            "mode": "mock",
            "audio_source": "browser_ws",
            "stt_provider": "mock",
            "translation_provider": "mock",
            "target_languages": ["kk", "uz", "zh-Hans"],
        },
    )
    assert response.status_code == 201
    return response.json()


def _create_live_test(client: TestClient, lesson_id: str, **overrides) -> dict:
    payload = {
        "lesson_id": lesson_id,
        "stt_provider": "mock",
        "translation_provider": "mock",
        "chunk_ms": 100,
        "silence_commit_ms": 1000,
        "max_segment_duration_ms": 6000,
        "partials_enabled": True,
        "test_phrase_label": "short phrase",
        "expected_text": "Здравствуйте, это проверка живого микрофона.",
    }
    payload.update(overrides)
    response = client.post("/api/live-tests", json=payload)
    assert response.status_code == 201
    return response.json()


def _fake_caption(lesson_id: str) -> dict:
    return {
        "event": "caption",
        "lesson_id": lesson_id,
        "provider": {"stt": "mock", "translator": "mock"},
        "audio_source": "browser_ws",
        "original_text": "Здравствуйте, это проверка живого микрофона.",
        "translations": {"kk": "[kk mock] Здравствуйте", "uz": "[uz mock] Здравствуйте"},
        "is_final": True,
        "latency_ms": {
            "first_partial_latency_ms": 120,
            "final_latency_ms": 850,
            "translation_latency_ms": 35,
            "total_latency_ms": 920,
        },
        "audio": {"dropped_chunks": 1, "commit_reason": "silence_timeout"},
        "dropped_chunks": 1,
        "commit_reason": "silence_timeout",
    }


def test_live_test_run_can_be_created_and_applies_tuning(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'live-create.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        created = _create_live_test(client, lesson["lesson_id"], chunk_ms=50, silence_commit_ms=700, max_segment_duration_ms=4000, partials_enabled=False)
        tuning = client.get(f"/api/lessons/{lesson['lesson_id']}/browser-audio/tuning").json()

    assert created["live_test_id"].startswith("live_")
    assert created["lesson_id"] == lesson["lesson_id"]
    assert created["status"] == "active"
    assert created["tuning_applied"] is True
    assert created["teacher_url"] == f"/teacher/{lesson['lesson_id']}"
    assert created["student_url"] == f"/student/{lesson['lesson_id']}"
    assert tuning["chunk_ms"] == 50
    assert tuning["silence_commit_ms"] == 700
    assert tuning["max_segment_duration_ms"] == 4000
    assert tuning["partials_enabled"] is False


def test_fake_final_caption_updates_active_live_test_run(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'live-capture.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        created = _create_live_test(client, lesson["lesson_id"])
        capture = client.post(f"/api/live-tests/{created['live_test_id']}/capture", json=_fake_caption(lesson["lesson_id"]))
        detail = client.get(f"/api/live-tests/{created['live_test_id']}")

    assert capture.status_code == 200
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["transcript"] == "Здравствуйте, это проверка живого микрофона."
    assert payload["translations"]["kk"].startswith("[kk mock]")
    assert payload["first_partial_latency_ms"] == 120
    assert payload["final_latency_ms"] == 850
    assert payload["translation_latency_ms"] == 35
    assert payload["total_latency_ms"] == 920
    assert payload["chunks_dropped"] == 1
    assert payload["commit_reason"] == "silence_timeout"


def test_transcript_save_final_captures_active_live_test_run(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'live-subscriber.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        created = _create_live_test(client, lesson["lesson_id"])

    TranscriptRepository(app.state.database.session_factory, final_caption_capture=app.state.final_caption_capture.capture).save_final(_fake_caption(lesson["lesson_id"]))

    with TestClient(app) as client:
        detail = client.get(f"/api/live-tests/{created['live_test_id']}")

    assert detail.status_code == 200
    assert detail.json()["transcript"] == "Здравствуйте, это проверка живого микрофона."


def test_starting_new_live_test_finishes_existing_active_run(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'live-single-active.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        first = _create_live_test(client, lesson["lesson_id"], test_phrase_label="short phrase")
        second = _create_live_test(client, lesson["lesson_id"], test_phrase_label="technical terms phrase")
        first_detail = client.get(f"/api/live-tests/{first['live_test_id']}").json()
        second_detail = client.get(f"/api/live-tests/{second['live_test_id']}").json()

    assert first_detail["status"] == "completed"
    assert first_detail["completed_by"] == "auto"
    assert second_detail["status"] == "active"


def test_starting_new_live_test_closes_all_existing_active_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'live-all-active.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        first = _create_live_test(client, lesson["lesson_id"], test_phrase_label="short phrase")
        second = _create_live_test(client, lesson["lesson_id"], test_phrase_label="technical terms phrase")
        with app.state.database.session_factory() as session:
            session.get(LiveMicTestRun, first["live_test_id"]).status = "active"
            session.commit()
        third = _create_live_test(client, lesson["lesson_id"], test_phrase_label="long continuous speech")
        statuses = {
            first["live_test_id"]: client.get(f"/api/live-tests/{first['live_test_id']}").json()["status"],
            second["live_test_id"]: client.get(f"/api/live-tests/{second['live_test_id']}").json()["status"],
            third["live_test_id"]: client.get(f"/api/live-tests/{third['live_test_id']}").json()["status"],
        }

    assert statuses[first["live_test_id"]] == "completed"
    assert statuses[second["live_test_id"]] == "completed"
    assert statuses[third["live_test_id"]] == "active"


def test_capture_helper_disabled_when_debug_endpoints_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'live-capture-disabled.db').as_posix()}")
    monkeypatch.setenv("ENABLE_DEBUG_ENDPOINTS", "false")
    app = create_app()

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        created = _create_live_test(client, lesson["lesson_id"])
        response = client.post(f"/api/live-tests/{created['live_test_id']}/capture", json=_fake_caption(lesson["lesson_id"]))

    assert response.status_code == 403


def test_provider_metrics_snapshot_includes_lesson_counters(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'live-provider-metrics.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        created = _create_live_test(client, lesson["lesson_id"])
        client.post(f"/api/live-tests/{created['live_test_id']}/capture", json=_fake_caption(lesson["lesson_id"]))
        detail = client.get(f"/api/live-tests/{created['live_test_id']}").json()

    assert detail["provider_metrics"]["provider"] == {"stt": "mock", "translator": "mock"}
    assert "lesson_counters" in detail["provider_metrics"]
    assert "stt_provider_final_events" in detail["provider_metrics"]["lesson_counters"]


def test_live_test_notes_finish_and_report_recommendation(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'live-report.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        created = _create_live_test(client, lesson["lesson_id"])
        client.post(f"/api/live-tests/{created['live_test_id']}/capture", json=_fake_caption(lesson["lesson_id"]))
        notes = client.post(
            f"/api/live-tests/{created['live_test_id']}/notes",
            json={"transcript_quality": "good", "translation_quality": "good", "quality_notes": "Clean baseline."},
        )
        finish = client.post(f"/api/live-tests/{created['live_test_id']}/finish")
        report = client.get("/api/live-tests/report", params={"lesson_id": lesson["lesson_id"], "stt_provider": "mock", "translation_provider": "mock"})

    assert notes.status_code == 200
    assert finish.status_code == 200
    assert finish.json()["status"] == "completed"
    assert finish.json()["completed_by"] == "manual"
    assert report.status_code == 200
    payload = report.json()
    assert payload["total_runs"] == 1
    assert payload["recommended_settings"]["chunk_ms"] == 100
    assert "transcript_quality=good" in payload["recommendation_reason"]
    assert payload["rows"][0]["quality_status"] == "rated"


def test_unrated_completed_run_is_not_recommended(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'live-unrated.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        created = _create_live_test(client, lesson["lesson_id"])
        client.post(f"/api/live-tests/{created['live_test_id']}/capture", json=_fake_caption(lesson["lesson_id"]))
        client.post(f"/api/live-tests/{created['live_test_id']}/finish")
        report = client.get("/api/live-tests/report")

    assert report.status_code == 200
    assert report.json()["recommended_settings"] is None
    assert report.json()["recommendation_reason"] == "insufficient completed good-quality runs"
    assert report.json()["rows"][0]["quality_status"] == "quality_unrated"


def test_live_test_pages_render_without_microphone(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'live-pages.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        page = client.get("/live-tests")
        report = client.get("/live-tests/report")

    assert page.status_code == 200
    assert "Live Browser Mic Test Matrix" in page.text
    assert "does not record audio" in page.text
    assert report.status_code == 200
    assert "Live Mic Baseline Report" in report.text
    assert "name=\"lesson_id\"" in report.text
    assert "name=\"stt_provider\"" in report.text
    assert "name=\"translation_provider\"" in report.text
    assert "name=\"test_phrase_label\"" in report.text


def test_live_test_report_page_applies_query_filters(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'live-page-filter.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        first = _create_live_test(client, lesson["lesson_id"], stt_provider="mock", test_phrase_label="short phrase")
        client.post(f"/api/live-tests/{first['live_test_id']}/finish")
        second = _create_live_test(client, lesson["lesson_id"], stt_provider="azure", test_phrase_label="technical terms phrase")
        client.post(f"/api/live-tests/{second['live_test_id']}/finish")
        page = client.get("/live-tests/report", params={"stt_provider": "azure"})

    assert page.status_code == 200
    assert second["live_test_id"] in page.text
    assert first["live_test_id"] not in page.text
