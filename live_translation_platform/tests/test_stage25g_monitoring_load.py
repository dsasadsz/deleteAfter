import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from scripts import load_test_lessons, load_test_students, load_test_tts


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


def test_runtime_metrics_endpoint_returns_stage25g_counts(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "stage25g-metrics.db")

    with TestClient(app) as client:
        response = client.get("/api/metrics/runtime")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["active_lessons"] == 0
    assert payload["caption_ws_clients"] == 0
    assert payload["question_ws_clients"] == 0
    assert payload["diagnostic_ws_clients"] == 0
    assert payload["active_pipelines"] == 0
    assert payload["audio_queue_sizes"] == {}
    assert payload["dropped_audio_chunks"] == 0
    assert payload["captions_sent_total"] == 0
    assert payload["captions_per_second"] == 0
    assert payload["tts_requests_total"] == 0
    assert payload["tts_requests_per_minute"] == 0
    assert payload["questions_total"] == 0
    assert payload["stt_disconnects_total"] == 0
    assert payload["provider_errors_total"] == 0
    assert payload["websocket_broadcasts_total"] == 0
    assert payload["websocket_send_failures_total"] == 0
    assert payload["websocket_send_timeouts_total"] == 0
    assert payload["websocket_clients_dropped_total"] == 0
    assert payload["caption_broadcast_latency_ms_avg"] is None
    assert payload["question_broadcast_latency_ms_avg"] is None


def test_load_test_caption_publish_endpoint_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_LOAD_TEST_ENDPOINTS", "false")
    app = _app(tmp_path, monkeypatch, "stage25g-load-disabled.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(
            f"/api/load-test/lessons/{lesson['lesson_id']}/publish-caption",
            json={"sequence": 1, "original_text": "Load test caption"},
        )

    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_load_test_caption_publish_endpoint_broadcasts_and_updates_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_LOAD_TEST_ENDPOINTS", "true")
    monkeypatch.setenv("APP_ENV", "development")
    app = _app(tmp_path, monkeypatch, "stage25g-load-publish.db")
    caption_ws = FakeWebSocket()

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        await app.state.caption_hub.connect(lesson["lesson_id"], caption_ws)
        response = client.post(
            f"/api/load-test/lessons/{lesson['lesson_id']}/publish-caption",
            json={
                "sequence": 7,
                "original_text": "Load test caption",
                "translations": {"kk": "Жүктеме тесті"},
                "latency_ms": {"translation": 12, "total": 25},
            },
        )
        metrics = client.get("/api/metrics/runtime")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["published"] is True
    assert payload["connected_clients"] == 1
    assert caption_ws.messages[0]["event"] == "caption"
    assert caption_ws.messages[0]["sequence"] == 7
    assert caption_ws.messages[0]["lesson_id"] == lesson["lesson_id"]
    assert caption_ws.messages[0]["translations"]["kk"] == "Жүктеме тесті"
    metrics_payload = metrics.json()
    assert metrics_payload["captions_sent_total"] == 1
    assert metrics_payload["captions_per_second"] > 0
    assert metrics_payload["translation_latency_ms_avg"] == 12


@pytest.mark.asyncio
async def test_hub_connections_and_caption_delivery_update_runtime_metrics(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "stage25g-ws.db")
    caption_ws = FakeWebSocket()
    debug_ws = FakeWebSocket()
    question_ws = FakeWebSocket()

    with TestClient(app) as client:
        await app.state.caption_hub.connect("lesson_1", caption_ws)
        await app.state.caption_hub.connect("lesson_1", debug_ws, debug=True)
        await app.state.question_hub.connect("lesson_1", question_ws)
        await app.state.caption_hub.broadcast_caption("lesson_1", {"event": "caption", "lesson_id": "lesson_1"})
        response = client.get("/api/metrics/runtime")

    payload = response.json()
    assert payload["caption_ws_clients"] == 1
    assert payload["question_ws_clients"] == 1
    assert payload["diagnostic_ws_clients"] == 1
    assert payload["captions_sent_total"] == 1
    assert payload["captions_per_second"] >= 0


def test_tts_requests_increment_runtime_metrics(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "stage25g-tts.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(
            f"/api/lessons/{lesson['lesson_id']}/tts/synthesize",
            json={"text": "Runtime metrics", "language": "kk"},
        )
        metrics = client.get("/api/metrics/runtime")

    assert response.status_code == 200, response.text
    payload = metrics.json()
    assert payload["tts_requests_total"] == 1
    assert payload["tts_requests_per_minute"] >= 1
    assert payload["tts_latency_ms_avg"] is not None


def test_question_creation_increments_runtime_metrics(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "stage25g-questions.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(
            f"/api/lessons/{lesson['lesson_id']}/questions/text",
            json={"text": "What does that mean?", "source_language": "ru", "student_id": "student_1"},
        )
        metrics = client.get("/api/metrics/runtime")

    assert response.status_code == 201, response.text
    assert metrics.json()["questions_total"] >= 1


def test_runtime_metrics_include_queue_sizes_dropped_chunks_and_provider_errors(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "stage25g-queues.db")

    with TestClient(app) as client:
        app.state.browser_audio_manager.prepare_lesson("lesson_1")
        queue = app.state.browser_audio_manager.get_audio_queue("lesson_1")
        queue.put_nowait({"kind": "audio"})
        app.state.browser_audio_manager.chunks_dropped["lesson_1"] = 2
        app.state.runtime_metrics.record_provider_error()
        app.state.runtime_metrics.record_stt_disconnect()
        response = client.get("/api/metrics/runtime")

    payload = response.json()
    assert payload["audio_queue_sizes"]["browser_audio:lesson_1"] == 1
    assert payload["dropped_audio_chunks"] == 2
    assert payload["provider_errors_total"] == 1
    assert payload["stt_disconnects_total"] == 1


def test_load_test_scripts_expose_argparse_help_without_external_providers():
    students = subprocess.run(
        [sys.executable, "scripts/load_test_students.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    lessons = subprocess.run(
        [sys.executable, "scripts/load_test_lessons.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    tts = subprocess.run(
        [sys.executable, "scripts/load_test_tts.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert students.returncode == 0, students.stderr
    assert lessons.returncode == 0, lessons.stderr
    assert tts.returncode == 0, tts.stderr
    assert "--students" in students.stdout
    assert "--mock" in lessons.stdout
    assert "--requests" in tts.stdout
    assert "--concurrency" in tts.stdout
    assert "--return-mode" in tts.stdout
    assert "--same-caption" in tts.stdout
    assert "--disable-rate-limit-for-load-test" in tts.stdout
    assert "--use-v1" in tts.stdout
    assert "--integration-key" in tts.stdout
    assert "--student-token" in tts.stdout


def test_student_load_test_latency_uses_load_test_publish_timestamp():
    published_at = (datetime.now(timezone.utc) - timedelta(milliseconds=25)).isoformat()

    latency = load_test_students._message_latency_ms(
        json.dumps({"event": "caption", "load_test_published_at": published_at})
    )

    assert latency is not None
    assert 0 <= latency < 1000


def test_lesson_load_test_simulate_captions_posts_to_load_test_endpoint(monkeypatch):
    posted = []

    def fake_post_json(base_url: str, path: str, payload: dict) -> dict:
        posted.append({"base_url": base_url, "path": path, "payload": payload})
        return {"published": True, "sequence": payload["sequence"]}

    monkeypatch.setattr(load_test_lessons, "_post_json", fake_post_json)

    published = load_test_lessons._publish_mock_captions(
        SimpleNamespace(base_url="http://testserver", captions_per_second=3, duration_seconds=1.0),
        [{"lesson_id": "lesson_1"}],
    )

    assert published == 3
    assert [item["path"] for item in posted] == [
        "/api/load-test/lessons/lesson_1/publish-caption",
        "/api/load-test/lessons/lesson_1/publish-caption",
        "/api/load-test/lessons/lesson_1/publish-caption",
    ]
    assert [item["payload"]["sequence"] for item in posted] == [1, 2, 3]


def test_lesson_load_test_can_publish_to_existing_lesson_id(monkeypatch, capsys):
    captured_lessons = []

    def fail_create_lesson(base_url: str, index: int) -> dict:
        raise AssertionError("existing lesson load tests should not create new lessons")

    def fake_publish(args, lessons, pace=False) -> int:
        captured_lessons.extend(lessons)
        return 2

    monkeypatch.setattr(load_test_lessons, "_create_mock_lesson", fail_create_lesson)
    monkeypatch.setattr(load_test_lessons, "_publish_mock_captions", fake_publish)
    monkeypatch.setattr(load_test_lessons, "_get_json", lambda base_url, path: {"captions_sent_total": 2})

    result = load_test_lessons.run(
        load_test_lessons.build_parser().parse_args(
            [
                "--base-url",
                "http://testserver",
                "--lesson-id",
                "lesson_existing",
                "--simulate-captions",
                "--duration-seconds",
                "1",
            ]
        )
    )

    assert result == 0
    assert captured_lessons == [{"lesson_id": "lesson_existing"}]
    assert json.loads(capsys.readouterr().out)["captions_published"] == 2


def test_tts_load_test_fake_run_reports_cache_and_provider_deltas(monkeypatch, capsys):
    posted = []
    fetched_audio_urls = []
    metric_snapshots = [
        {
            "tts_cache_hits_total": 10,
            "tts_cache_misses_total": 2,
            "tts_provider_calls_total": 2,
            "tts_provider_calls_saved_total": 10,
        },
        {
            "tts_cache_hits_total": 14,
            "tts_cache_misses_total": 3,
            "tts_provider_calls_total": 3,
            "tts_provider_calls_saved_total": 14,
        },
    ]

    def fake_get_json(base_url: str, path: str) -> dict:
        assert path == "/api/metrics/runtime"
        return metric_snapshots.pop(0)

    def fake_post_json(base_url: str, path: str, payload: dict, headers: dict | None = None) -> dict:
        posted.append({"path": path, "payload": payload, "headers": headers or {}})
        if path == "/api/lessons":
            return {"lesson_id": "lesson_tts"}
        assert path == "/api/lessons/lesson_tts/tts/synthesize"
        return {
            "status": 200,
            "headers": {"x-tts-cache": "hit"},
            "json": {
                "audio_url": "/api/lessons/lesson_tts/tts/audio/audio-1?token=token",
                "cached": True,
            },
            "latency_ms": 5.0,
        }

    def fake_get_audio_url(base_url: str, audio_url: str) -> dict:
        fetched_audio_urls.append(load_test_tts._audio_url_request_url(base_url, audio_url))
        return {"status": 200, "latency_ms": 1.0}

    monkeypatch.setattr(load_test_tts, "_get_json", fake_get_json)
    monkeypatch.setattr(load_test_tts, "_post_json", fake_post_json)
    monkeypatch.setattr(load_test_tts, "_get_audio_url", fake_get_audio_url)

    result = load_test_tts.run(
        load_test_tts.build_parser().parse_args(
            [
                "--base-url",
                "http://testserver",
                "--requests",
                "5",
                "--concurrency",
                "2",
                "--return-mode",
                "url",
                "--same-caption",
                "--disable-rate-limit-for-load-test",
                "--provider",
                "mock",
            ]
        )
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["total_requests"] == 5
    assert report["success"] == 5
    assert report["failed"] == 0
    assert report["audio_url_success"] == 5
    assert report["audio_url_failed"] == 0
    assert report["auth_401_count"] == 0
    assert report["cache_hits"] == 4
    assert report["cache_misses"] == 1
    assert report["provider_calls_before"] == 2
    assert report["provider_calls_after"] == 3
    assert report["provider_calls_saved"] == 4
    assert report["avg_latency_ms"] == 5.0
    assert report["p95_latency_ms"] == 5.0
    synth_posts = [item for item in posted if item["path"].endswith("/tts/synthesize")]
    assert len(synth_posts) == 5
    assert {item["payload"]["caption_id"] for item in synth_posts} == {"load-caption-1"}
    assert {item["payload"]["return_mode"] for item in synth_posts} == {"url"}
    assert {item["headers"].get("X-TTS-Load-Test-Bypass-Rate-Limit") for item in synth_posts} == {"true"}
    assert fetched_audio_urls == ["http://testserver/api/lessons/lesson_tts/tts/audio/audio-1?token=token"] * 5


def test_tts_load_test_audio_url_join_preserves_query_token():
    assert (
        load_test_tts._audio_url_request_url(
            "http://testserver/base",
            "/api/lessons/lesson_tts/tts/audio/audio-1?token=signed.token.value",
        )
        == "http://testserver/api/lessons/lesson_tts/tts/audio/audio-1?token=signed.token.value"
    )


def test_tts_load_test_url_mode_counts_audio_url_401_without_crashing(monkeypatch, capsys):
    monkeypatch.setattr(load_test_tts, "_get_json", lambda base_url, path: {})
    monkeypatch.setattr(load_test_tts, "_post_json", lambda *args, **kwargs: {"lesson_id": "lesson_tts"} if args[1] == "/api/lessons" else {
        "status": 200,
        "headers": {},
        "json": {"audio_url": "/api/lessons/lesson_tts/tts/audio/audio-1?token=bad"},
        "latency_ms": 5.0,
    })
    monkeypatch.setattr(load_test_tts, "_get_audio_url", lambda base_url, audio_url: {"status": 401, "error": "Missing or invalid TTS access token.", "latency_ms": 1.0})

    result = load_test_tts.run(
        load_test_tts.build_parser().parse_args(
            [
                "--base-url",
                "http://testserver",
                "--requests",
                "2",
                "--concurrency",
                "1",
                "--return-mode",
                "url",
                "--same-caption",
                "--provider",
                "mock",
            ]
        )
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 1
    assert report["success"] == 0
    assert report["failed"] == 2
    assert report["audio_url_success"] == 0
    assert report["audio_url_failed"] == 2
    assert report["auth_401_count"] == 2


def test_tts_load_test_use_v1_requires_token_or_integration_key(capsys):
    result = load_test_tts.run(
        load_test_tts.build_parser().parse_args(
            [
                "--base-url",
                "http://testserver",
                "--requests",
                "1",
                "--return-mode",
                "url",
                "--use-v1",
            ]
        )
    )

    assert result == 2
    assert "return_mode=url requires student tts:play token or integration token flow" in capsys.readouterr().out


def test_tts_load_test_use_v1_obtains_student_token_without_printing_it(monkeypatch, capsys):
    posted = []

    def fake_post_json(base_url: str, path: str, payload: dict, headers: dict | None = None) -> dict:
        posted.append({"path": path, "payload": payload, "headers": headers or {}})
        if path == "/api/v1/integration/lessons":
            return {"lesson_id": "lesson_v1"}
        if path == "/api/v1/integration/lessons/lesson_v1/student-token":
            return {"token": "secret-student-token"}
        assert path == "/api/v1/integration/lessons/lesson_v1/tts/synthesize?token=secret-student-token"
        return {
            "status": 200,
            "headers": {},
            "json": {"audio_url": "/api/v1/integration/lessons/lesson_v1/tts/audio/audio-1?token=signed-audio-token"},
            "latency_ms": 5.0,
        }

    monkeypatch.setattr(load_test_tts, "_get_json", lambda base_url, path: {})
    monkeypatch.setattr(load_test_tts, "_post_json", fake_post_json)
    monkeypatch.setattr(load_test_tts, "_get_audio_url", lambda base_url, audio_url: {"status": 200, "latency_ms": 1.0})

    result = load_test_tts.run(
        load_test_tts.build_parser().parse_args(
            [
                "--base-url",
                "http://testserver",
                "--requests",
                "1",
                "--return-mode",
                "url",
                "--use-v1",
                "--integration-key",
                "secret-integration-key",
            ]
        )
    )

    output = capsys.readouterr().out
    report = json.loads(output)
    assert result == 0
    assert report["success"] == 1
    assert "secret-student-token" not in output
    assert "secret-integration-key" not in output
    assert posted[0]["headers"] == {"X-Integration-Key": "secret-integration-key"}
    assert posted[1]["headers"] == {"X-Integration-Key": "secret-integration-key"}


def test_tts_load_test_does_not_send_bypass_header_by_default(monkeypatch):
    captured = {}

    def fake_post_json(base_url: str, path: str, payload: dict, headers: dict | None = None) -> dict:
        captured["headers"] = headers or {}
        return {"status": 200, "headers": {}, "json": {}, "latency_ms": 1.0}

    monkeypatch.setattr(load_test_tts, "_post_json", fake_post_json)

    result = load_test_tts._send_synthesize(
        load_test_tts.build_parser().parse_args(
            [
                "--base-url",
                "http://testserver",
                "--lesson-id",
                "lesson_tts",
                "--same-caption",
            ]
        ),
        "lesson_tts",
        0,
    )

    assert result["status"] == 200
    assert "X-TTS-Load-Test-Bypass-Rate-Limit" not in captured["headers"]


def _app(tmp_path, monkeypatch, db_name: str):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("APP_ENV", os.environ.get("APP_ENV", "development"))
    monkeypatch.setenv("TTS_PROVIDER", "mock")
    monkeypatch.setenv("TTS_ENABLED", "true")
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "true")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    return create_app()


def _create_lesson(client: TestClient) -> dict:
    response = client.post("/api/lessons", json={"title": "Stage 25G", "mode": "mock", "stt_provider": "mock", "translation_provider": "mock"})
    assert response.status_code == 201, response.text
    return response.json()
