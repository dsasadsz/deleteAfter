from fastapi.testclient import TestClient

from app.main import create_app


def test_e2e_qa_run_can_be_created_and_checklist_updated(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "e2e-create.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        created = _create_e2e_run(client, lesson["lesson_id"])
        updated = client.post(
            f"/api/e2e-tests/{created['e2e_test_id']}/checklist",
            json={"key": "student.tts_audio", "status": "pass", "notes": "Student heard Kazakh TTS."},
        )
        detail = client.get(f"/api/e2e-tests/{created['e2e_test_id']}")

    assert created["e2e_test_id"].startswith("e2e_")
    assert created["lesson_id"] == lesson["lesson_id"]
    assert created["teacher_url"] == f"/teacher/{lesson['lesson_id']}"
    assert created["student_url"] == f"/student/{lesson['lesson_id']}"
    assert updated.status_code == 200, updated.text
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["checklist"]["student.tts_audio"]["status"] == "pass"
    assert payload["checklist"]["student.tts_audio"]["notes"] == "Student heard Kazakh TTS."


def test_fake_final_caption_and_tts_capture_update_e2e_metrics(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "e2e-caption-tts.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        created = _create_e2e_run(client, lesson["lesson_id"])
        caption = client.post(
            f"/api/e2e-tests/{created['e2e_test_id']}/capture",
            json={
                "event_type": "final_caption",
                "lesson_id": lesson["lesson_id"],
                "original_text": "Здравствуйте, это проверка урока.",
                "translations": {"kk": "Сәлеметсіз бе"},
                "latency_ms": {"total_latency_ms": 940, "final_latency_ms": 850, "translation_latency_ms": 40},
            },
        )
        tts = client.post(
            f"/api/e2e-tests/{created['e2e_test_id']}/capture",
            json={
                "event_type": "tts",
                "enabled": True,
                "provider": "mock",
                "language": "kk",
                "queue_mode": "sequential",
                "latency_ms": 120,
                "ducking_status": "ducked_restored",
            },
        )
        detail = client.get(f"/api/e2e-tests/{created['e2e_test_id']}")

    assert caption.status_code == 200, caption.text
    assert tts.status_code == 200, tts.text
    payload = detail.json()
    assert payload["metrics"]["captions"]["final_count"] == 1
    assert payload["metrics"]["captions"]["total_latency_ms"] == 940
    assert payload["metrics"]["tts"]["provider"] == "mock"
    assert payload["metrics"]["tts"]["latency_ms"] == 120
    assert payload["metrics"]["ducking"]["status"] == "ducked_restored"
    assert payload["checklist"]["student.translated_captions"]["status"] == "pass"
    assert payload["checklist"]["student.tts_audio"]["status"] == "pass"
    assert payload["checklist"]["student.audio_ducking"]["status"] == "pass"


def test_fake_student_question_capture_updates_text_voice_and_teacher_checklist(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "e2e-questions.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        created = _create_e2e_run(client, lesson["lesson_id"])
        text_question = client.post(
            f"/api/e2e-tests/{created['e2e_test_id']}/capture",
            json={"event_type": "student_question", "input_type": "text", "translated_text_ru": "Можно повторить?", "status": "answered"},
        )
        voice_question = client.post(
            f"/api/e2e-tests/{created['e2e_test_id']}/capture",
            json={"event_type": "student_question", "input_type": "voice", "translated_text_ru": "Голосовой вопрос", "status": "dismissed"},
        )
        detail = client.get(f"/api/e2e-tests/{created['e2e_test_id']}")

    assert text_question.status_code == 200, text_question.text
    assert voice_question.status_code == 200, voice_question.text
    payload = detail.json()
    assert payload["metrics"]["questions"]["text_count"] == 1
    assert payload["metrics"]["questions"]["voice_count"] == 1
    assert payload["metrics"]["questions"]["translated_ru_count"] == 2
    assert payload["metrics"]["questions"]["answered_or_dismissed_count"] == 2
    assert payload["checklist"]["student.text_question"]["status"] == "pass"
    assert payload["checklist"]["student.voice_question"]["status"] == "pass"
    assert payload["checklist"]["teacher.translated_questions"]["status"] == "pass"
    assert payload["checklist"]["teacher.answer_dismiss"]["status"] == "pass"


def test_finish_report_markdown_and_recommended_defaults_use_only_passed_completed_runs(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "e2e-report.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        failed = _create_e2e_run(client, lesson["lesson_id"], title="Failed run")
        client.post(f"/api/e2e-tests/{failed['e2e_test_id']}/checklist", json={"key": "student.tts_audio", "status": "fail", "notes": "No audio"})
        client.post(f"/api/e2e-tests/{failed['e2e_test_id']}/finish")

        passed = _create_e2e_run(client, lesson["lesson_id"], title="Passed run", chunk_ms=50, silence_commit_ms=700)
        client.post(
            f"/api/e2e-tests/{passed['e2e_test_id']}/capture",
            json={"event_type": "final_caption", "lesson_id": lesson["lesson_id"], "translations": {"kk": "ok"}, "latency_ms": {"total_latency_ms": 800}},
        )
        for key in _required_keys():
            client.post(f"/api/e2e-tests/{passed['e2e_test_id']}/checklist", json={"key": key, "status": "pass"})
        finish = client.post(f"/api/e2e-tests/{passed['e2e_test_id']}/finish")
        report = client.get("/api/e2e-tests/report")

    assert finish.status_code == 200, finish.text
    assert finish.json()["status"] == "completed"
    payload = report.json()
    assert payload["total_runs"] == 2
    assert payload["recommended_defaults"]["chunk_ms"] == 50
    assert payload["recommended_defaults"]["silence_commit_ms"] == 700
    assert "# Stage 22 E2E QA Report" in payload["markdown"]
    assert failed["e2e_test_id"] in payload["markdown"]
    assert passed["e2e_test_id"] in payload["markdown"]


def test_e2e_capture_helper_disabled_when_debug_endpoints_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_DEBUG_ENDPOINTS", "false")
    app = _app(tmp_path, monkeypatch, "e2e-capture-disabled.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        created = _create_e2e_run(client, lesson["lesson_id"])
        response = client.post(f"/api/e2e-tests/{created['e2e_test_id']}/capture", json={"event_type": "tts", "enabled": True})

    assert response.status_code == 403


def test_e2e_pages_render_and_docs_include_manual_qa(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "e2e-pages.db")

    with TestClient(app) as client:
        page = client.get("/e2e-test")
        report = client.get("/e2e-test/report")

    assert page.status_code == 200
    assert "Stage 22 E2E Manual QA" in page.text
    assert "Zoom video works" in page.text
    assert report.status_code == 200
    assert "E2E QA Report" in report.text

    readme = open("README.md", encoding="utf-8").read()
    architecture = open("docs/ARCHITECTURE.md", encoding="utf-8").read()
    assert "Handoff Readiness Report" in readme
    assert "Stage 22" in architecture
    assert "/api/e2e-tests" in architecture


def _app(tmp_path, monkeypatch, db_name: str):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("TRANSLATION_PROVIDER", "mock")
    monkeypatch.setenv("STT_PROVIDER", "mock")
    monkeypatch.setenv("TTS_PROVIDER", "mock")
    monkeypatch.setenv("TTS_ENABLED", "true")
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage22-secret")
    monkeypatch.setenv("WEBSOCKET_AUTH_ENABLED", "false")
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "true")
    return create_app()


def _create_lesson(client: TestClient) -> dict:
    response = client.post(
        "/api/lessons",
        json={"title": "Stage 22", "mode": "mock", "audio_source": "browser_ws", "stt_provider": "mock", "translation_provider": "mock"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_e2e_run(client: TestClient, lesson_id: str, **overrides) -> dict:
    payload = {
        "lesson_id": lesson_id,
        "title": "Stage 22 manual QA",
        "stt_provider": "mock",
        "translation_provider": "mock",
        "tts_provider": "mock",
        "tts_language": "kk",
        "tts_queue_mode": "sequential",
        "chunk_ms": 100,
        "silence_commit_ms": 1000,
        "max_segment_duration_ms": 6000,
        "partials_enabled": True,
    }
    payload.update(overrides)
    response = client.post("/api/e2e-tests", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _required_keys() -> list[str]:
    return [
        "teacher.zoom_lesson_created",
        "teacher.zoom_video_works",
        "teacher.browser_mic_streams",
        "teacher.pipeline_captions",
        "student.translated_captions",
        "student.tts_audio",
        "student.audio_ducking",
        "student.text_question",
        "student.voice_question",
        "teacher.translated_questions",
        "teacher.answer_dismiss",
    ]
