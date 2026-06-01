import io
import json
import wave
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
import pytest

from app.audio.base import AudioChunk
from app.config import Settings
from app.db.models import Lesson
from app.db.repositories import LessonRepository, SmokeTestRepository
from app.main import create_app
from app.smoke.audio_samples import chunk_wav_file
from app.smoke.provider_status import elevenlabs_user_probe_status
from app.smoke.runner import SmokeRunner


def test_settings_test_mode_ignores_dotenv_provider_secrets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IGNORE_DOTENV_IN_TESTS", "true")
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_TTS_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "CARTESIA_API_KEY=dotenv-cartesia\nAZURE_TTS_KEY=dotenv-azure\n",
        encoding="utf-8",
    )

    settings = Settings()

    assert settings.cartesia_api_key == ""
    assert settings.azure_tts_key == ""


def test_provider_status_reports_missing_real_keys(tmp_path, monkeypatch):
    db_path = tmp_path / "providers.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")
    monkeypatch.setenv("AZURE_SPEECH_KEY", "")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "")
    monkeypatch.setenv("CARTESIA_API_KEY", "")
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "")

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/providers/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["stt"]["mock"]["ready"] is True
    assert payload["stt"]["elevenlabs"]["ready"] is False
    assert payload["stt"]["elevenlabs"]["missing"] == ["ELEVENLABS_API_KEY"]
    assert payload["stt"]["azure"]["ready"] is False
    assert payload["stt"]["azure"]["missing"] == ["AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION"]
    assert payload["stt"]["cartesia"]["ready"] is False
    assert payload["stt"]["cartesia"]["missing"] == ["CARTESIA_API_KEY"]
    assert payload["stt"]["faster_whisper"]["ready"] is False
    assert payload["stt"]["faster_whisper"]["missing"] == ["FASTER_WHISPER_MODEL_PATH"]
    assert payload["translation"]["mock"]["ready"] is True
    assert payload["translation"]["azure"]["ready"] is False
    assert payload["translation"]["azure"]["missing"] == ["AZURE_TRANSLATOR_KEY"]
    assert payload["translation"]["local"]["ready"] is False
    assert payload["translation"]["local"]["enabled"] is False
    assert payload["translation"]["local"]["engines"]["tilmash"]["status"] in {"disabled", "not_configured"}


def test_provider_status_reports_faster_whisper_configured_not_loaded(tmp_path, monkeypatch):
    db_path = tmp_path / "providers-faster-whisper.db"
    model_path = "C:/private/models/faster-whisper-small"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("FASTER_WHISPER_MODEL_PATH", model_path)
    monkeypatch.setenv("FASTER_WHISPER_DEVICE", "cpu")
    monkeypatch.setenv("FASTER_WHISPER_COMPUTE_TYPE", "int8")
    monkeypatch.setenv("FASTER_WHISPER_LANGUAGE", "ru")
    monkeypatch.setenv("DISABLE_AUTO_LANGUAGE_DETECTION", "true")

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/providers/status")

    assert response.status_code == 200
    status = response.json()["stt"]["faster_whisper"]
    assert status["ready"] is True
    assert status["status"] == "configured"
    assert status["loaded"] is False
    assert status["device"] == "cpu"
    assert status["compute_type"] == "int8"
    assert status["language"] == "ru"
    assert status["auto_language_detection"] is False
    assert "C:/private" not in str(status)


def test_provider_status_reports_local_tilmash_configured_not_loaded(tmp_path, monkeypatch):
    db_path = tmp_path / "providers-local.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("LOCAL_TRANSLATION_ENABLED", "true")
    monkeypatch.setenv("TILMASH_ENABLED", "true")
    monkeypatch.setenv("TILMASH_MODEL_PATH", "C:/private/models/tilmash")
    monkeypatch.setenv("TILMASH_TOKENIZER_PATH", "C:/private/models/tilmash-tokenizer")
    monkeypatch.setenv("TILMASH_DEVICE", "cpu")
    monkeypatch.setenv("TILMASH_LOAD_ON_STARTUP", "false")

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/providers/status")

    assert response.status_code == 200
    local = response.json()["translation"]["local"]
    assert local["ready"] is True
    assert local["engines"]["tilmash"]["status"] == "configured"
    assert local["engines"]["tilmash"]["loaded"] is False
    assert local["engines"]["tilmash"]["device"] == "cpu"
    assert local["engines"]["tilmash"]["supported_language_pairs"] == ["ru->kk", "ru->uz"]
    assert "C:/private" not in str(local)


def test_provider_status_reports_local_madlad_configured_not_loaded(tmp_path, monkeypatch):
    db_path = tmp_path / "providers-local-madlad.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("LOCAL_TRANSLATION_ENABLED", "true")
    monkeypatch.setenv("TILMASH_ENABLED", "false")
    monkeypatch.setenv("MADLAD_ENABLED", "true")
    monkeypatch.setenv("MADLAD_MODEL_PATH", "C:/private/models/madlad")
    monkeypatch.setenv("MADLAD_TOKENIZER_PATH", "C:/private/models/madlad-tokenizer")
    monkeypatch.setenv("MADLAD_DEVICE", "cpu")
    monkeypatch.setenv("MADLAD_DTYPE", "float32")
    monkeypatch.setenv("MADLAD_QUANTIZATION", "none")
    monkeypatch.setenv("MADLAD_LOAD_ON_STARTUP", "false")

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/providers/status")

    assert response.status_code == 200
    local = response.json()["translation"]["local"]
    assert local["ready"] is True
    assert local["engines"]["madlad400"]["status"] == "configured"
    assert local["engines"]["madlad400"]["loaded"] is False
    assert local["engines"]["madlad400"]["device"] == "cpu"
    assert local["engines"]["madlad400"]["dtype"] == "float32"
    assert local["engines"]["madlad400"]["quantization"] == "none"
    assert local["engines"]["madlad400"]["supported_language_pairs"] == ["ru->zh-Hans"]
    assert "C:/private" not in str(local)


def test_provider_status_treats_missing_elevenlabs_user_read_as_stt_warning():
    status = elevenlabs_user_probe_status(
        has_api_key=True,
        status_code=401,
        payload={
            "detail": {
                "status": "missing_permissions",
                "message": "The API key you used is missing the permission user_read to execute this operation.",
            }
        },
    )

    assert status["ready"] is True
    assert status["ready_for_stt"] is True
    assert status["user_endpoint_permission"] is False
    assert status["warning"] == "API key lacks user_read but STT works"


def test_smoke_repository_persists_run_and_events(tmp_path, monkeypatch):
    db_path = tmp_path / "repo.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    app = create_app()

    with app.state.database.session_factory() as session:
        lesson = Lesson(
            lesson_id="lesson_repo",
            title="Smoke lesson",
            mode="mock",
            status="created",
            zoom_meeting_id="mock",
            zoom_meeting_uuid="mock",
            zoom_join_url="https://example.test/join",
            zoom_start_url="https://example.test/start",
        )
        LessonRepository(session).create(lesson)
        repo = SmokeTestRepository(session)
        run = repo.create_run(
            lesson_id="lesson_repo",
            stt_provider="mock",
            translation_provider="mock",
            audio_mode="mock_chunks",
            target_languages=["kk", "uz"],
        )
        repo.add_event(run.id, "smoke_started", {"lesson_id": "lesson_repo"})
        repo.mark_completed(
            run.id,
            original_text="hello",
            translations={"kk": "kk hello", "uz": "uz hello"},
            latency_ms={"total_server": 12},
            provider_metrics={"stt": {"name": "mock"}},
        )

        stored = repo.get_run(run.id)
        events = repo.events_for_run(run.id)

    assert stored is not None
    assert stored.status == "completed"
    assert json.loads(stored.translations_json) == {"kk": "kk hello", "uz": "uz hello"}
    assert events[0].event_type == "smoke_started"
    assert json.loads(events[0].payload_json)["lesson_id"] == "lesson_repo"


def test_wav_chunker_splits_pcm_without_exposing_raw_audio(tmp_path):
    wav_path = tmp_path / "sample.wav"
    pcm = b"\x01\x02" * 1600
    with wave.open(str(wav_path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(pcm)

    sample = chunk_wav_file(wav_path, chunk_ms=50)

    assert sample.warning is None
    assert sample.sample_rate == 16000
    assert sample.channels == 1
    assert len(sample.chunks) == 2
    assert sample.metadata == {"sample_rate": 16000, "channels": 1, "format": "L16"}


def test_smoke_run_with_mock_providers_completes_and_persists(tmp_path, monkeypatch):
    db_path = tmp_path / "smoke.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    app = create_app()
    with TestClient(app) as client:
        run_response = client.post(
            "/api/smoke/run",
            json={
                "audio_mode": "mock_chunks",
                "stt_provider": "mock",
                "translation_provider": "mock",
                "target_languages": ["kk", "uz", "zh-Hans"],
            },
        )
        assert run_response.status_code == 200
        smoke_test_id = run_response.json()["smoke_test_id"]

        status = _wait_for_smoke(client, smoke_test_id)

    assert status["status"] == "completed"
    assert status["providers"] == {"stt": "mock", "translator": "mock"}
    assert status["results"]["original_text"]
    assert set(status["results"]["translations"]) == {"kk", "uz", "zh-Hans"}
    assert status["latency_ms"]["first_partial"] >= 0
    assert status["latency_ms"]["total_server"] >= status["latency_ms"]["stt_final"]


def test_smoke_websocket_receives_events(tmp_path, monkeypatch):
    db_path = tmp_path / "smoke-ws.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    app = create_app()
    with TestClient(app) as client:
        run_response = client.post(
            "/api/smoke/run",
            json={
                "audio_mode": "mock_chunks",
                "stt_provider": "mock",
                "translation_provider": "mock",
                "target_languages": ["kk"],
            },
        )
        smoke_test_id = run_response.json()["smoke_test_id"]

        with client.websocket_connect(f"/ws/smoke/{smoke_test_id}") as websocket:
            event_types = []
            for _ in range(8):
                event = websocket.receive_json()
                event_types.append(event["event"])
                if event["event"] == "smoke_completed":
                    break

    assert "stt_partial" in event_types
    assert "stt_final" in event_types
    assert "translation_done" in event_types
    assert "caption_sent" in event_types
    assert event_types[-1] == "smoke_completed"


def test_smoke_upload_audio_validates_content_type_and_size(tmp_path, monkeypatch):
    db_path = tmp_path / "upload.db"
    smoke_dir = tmp_path / "smoke"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SMOKE_TEMP_DIR", str(smoke_dir))
    monkeypatch.setenv("SMOKE_MAX_AUDIO_FILE_MB", "1")

    app = create_app()
    wav_body = _wav_bytes()
    with TestClient(app) as client:
        response = client.post(
            "/api/smoke/upload-audio",
            files={"file": ("sample.wav", wav_body, "audio/wav")},
        )

    assert response.status_code == 200
    assert response.json()["audio_sample_id"]
    assert response.json()["warning"] is None
    assert (smoke_dir / f"{response.json()['audio_sample_id']}.wav").exists()


@pytest.mark.asyncio
async def test_smoke_wav_realtime_stream_sends_chunks_with_expected_timing():
    now = [datetime(2026, 1, 1, 12, 0, 0)]
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += timedelta(seconds=seconds)

    runner = SmokeRunner(
        settings=Settings(smoke_audio_chunk_ms=100),
        session_factory=None,
        smoke_hub=None,
        caption_hub=None,
        sleep=fake_sleep,
        clock=lambda: now[0],
    )
    stt = CapturingSTT()
    chunks = [_pcm_chunk() for _ in range(4)]
    timestamps = {}
    metrics = {}

    await runner._send_audio_chunks(stt, chunks, timestamps, metrics, "realtime_stream")

    assert sleeps == [0.1, 0.1, 0.1]
    assert len(stt.sent) == 4
    assert stt.sent[-1]["metadata"]["finalize"] is True
    assert metrics["chunks_count"] == 4
    assert metrics["chunk_ms"] == 100
    assert metrics["audio_duration_ms"] == 400
    assert metrics["elapsed_audio_send_ms"] == 300
    assert metrics["realtime_factor"] == 0.75


@pytest.mark.asyncio
async def test_smoke_wav_fast_upload_does_not_sleep_per_chunk():
    now = [datetime(2026, 1, 1, 12, 0, 0)]
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += timedelta(seconds=seconds)

    runner = SmokeRunner(
        settings=Settings(smoke_audio_chunk_ms=100),
        session_factory=None,
        smoke_hub=None,
        caption_hub=None,
        sleep=fake_sleep,
        clock=lambda: now[0],
    )
    stt = CapturingSTT()
    timestamps = {}
    metrics = {}

    await runner._send_audio_chunks(stt, [_pcm_chunk() for _ in range(3)], timestamps, metrics, "fast_upload")

    assert sleeps == []
    assert len(stt.sent) == 3
    assert stt.sent[-1]["metadata"]["finalize"] is True
    assert metrics["chunks_count"] == 3
    assert metrics["elapsed_audio_send_ms"] == 0
    assert metrics["realtime_factor"] == 0
    assert metrics["streaming_mode"] == "fast_upload"


def test_smoke_run_can_select_azure_stt_and_reports_missing_credentials(tmp_path, monkeypatch):
    db_path = tmp_path / "smoke-azure.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("AZURE_SPEECH_KEY", "")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "")

    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/smoke/run",
            json={
                "audio_mode": "mock_chunks",
                "stt_provider": "azure",
                "translation_provider": "mock",
                "target_languages": ["kk"],
            },
        )
        assert response.status_code == 200
        status = _wait_for_smoke(client, response.json()["smoke_test_id"])

    assert status["status"] == "error"
    assert status["providers"]["stt"] == "azure"
    assert "AZURE_SPEECH_KEY" in status["errors"][0]
    assert "AZURE_SPEECH_REGION" in status["errors"][0]


def test_smoke_run_can_select_cartesia_stt_and_reports_missing_credentials(tmp_path, monkeypatch):
    db_path = tmp_path / "smoke-cartesia.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("CARTESIA_API_KEY", "")

    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/smoke/run",
            json={
                "audio_mode": "mock_chunks",
                "stt_provider": "cartesia",
                "translation_provider": "mock",
                "target_languages": ["kk"],
            },
        )
        assert response.status_code == 200
        status = _wait_for_smoke(client, response.json()["smoke_test_id"])

    assert status["status"] == "error"
    assert status["providers"]["stt"] == "cartesia"
    assert "CARTESIA_API_KEY" in status["errors"][0]


def _wait_for_smoke(client: TestClient, smoke_test_id: str) -> dict:
    for _ in range(40):
        response = client.get(f"/api/smoke/{smoke_test_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"completed", "error"}:
            return payload
    raise AssertionError("Smoke test did not finish")


def _wav_bytes() -> bytes:
    body = io.BytesIO()
    with wave.open(body, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00\x00" * 1600)
    body.seek(0)
    return body.read()


def _pcm_chunk() -> AudioChunk:
    return AudioChunk(
        data=b"\x00\x00" * 1600,
        source="smoke_wav_upload",
        sample_rate=16000,
        channels=1,
        format="L16",
    )


class CapturingSTT:
    name = "capture"

    def __init__(self):
        self.sent = []

    async def send_audio(self, audio_chunk, metadata=None):
        self.sent.append({"audio": audio_chunk, "metadata": metadata or {}})
