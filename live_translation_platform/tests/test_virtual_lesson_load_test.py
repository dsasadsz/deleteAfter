import asyncio
import json
import math
import subprocess
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.loadtest.audio_normalizer import AudioNormalizationError, normalize_lesson_audio
from app.loadtest.quality_metrics import stt_quality_report, translation_quality_report
from app.loadtest.report_builder import build_local_load_test_report, percentile_summary, sanitize_for_report
from app.loadtest.virtual_lesson_bots import (
    StudentCaptionEvent,
    TeacherBotConfig,
    TtsRequestPlanner,
    VirtualStudentBot,
    VirtualTeacherBot,
    VirtualTtsBot,
)
from app.loadtest.virtual_lesson_runner import LocalLoadTestRequest, LocalLoadTestRun, LocalLoadTestRunner


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_virtual_lesson_load_test.py"


class MemoryTeacherSocket:
    def __init__(self) -> None:
        self.text_messages = []
        self.binary_messages = []

    async def send_text(self, text: str) -> None:
        self.text_messages.append(json.loads(text))

    async def send_bytes(self, data: bytes) -> None:
        self.binary_messages.append(data)


class FakeTtsHttpClient:
    def __init__(self) -> None:
        self.requests = []
        self.cached_sequence = [False, True]

    async def post_json(self, path: str, payload: dict, headers: dict | None = None) -> dict:
        self.requests.append({"path": path, "payload": payload, "headers": headers or {}})
        cached = self.cached_sequence.pop(0)
        return {
            "status": 200,
            "json": {
                "audio_url": f"/api/lessons/{payload['lesson_id']}/tts/audio/audio-1?token=secret-token",
                "cached": cached,
            },
            "headers": {"x-tts-cache": "hit" if cached else "miss"},
            "latency_ms": 8.5 if cached else 42.0,
        }


@pytest.mark.asyncio
async def test_teacher_bot_chunks_audio_and_sends_force_commits():
    socket = MemoryTeacherSocket()
    audio = b"\x01\x02" * 1600
    bot = VirtualTeacherBot(
        TeacherBotConfig(
            lesson_id="lesson_1",
            audio_bytes=audio,
            chunk_ms=50,
            sample_rate=16000,
            channels=1,
            sample_width=2,
            force_commit_every_seconds=0.05,
            realtime=False,
        )
    )

    result = await bot.stream(socket)

    assert [len(chunk) for chunk in socket.binary_messages] == [1600, 1600]
    assert socket.text_messages[0]["event"] == "audio_metadata"
    assert [message["event"] for message in socket.text_messages].count("audio_chunk") == 2
    assert any(message["event"] == "force_commit" for message in socket.text_messages)
    assert result.chunks_sent == 2
    assert result.bytes_sent == len(audio)


def test_student_bot_records_caption_events():
    bot = VirtualStudentBot(student_id="student_1", lesson_id="lesson_1")
    received_at = datetime(2026, 5, 31, tzinfo=timezone.utc)

    event = bot.record_caption(
        {
            "sequence": 7,
            "is_final": True,
            "original_text": "hello world",
            "translations": {"kk": "salem alem"},
            "latency_ms": {"stt": 12, "translation": 20},
            "timestamps": {"websocket_sent_at": "2026-05-31T00:00:00Z"},
        },
        received_at=received_at,
    )

    assert event.student_id == "student_1"
    assert event.caption_sequence == 7
    assert event.is_final is True
    assert event.source_text == "hello world"
    assert event.translations["kk"] == "salem alem"
    assert event.provider_latency_ms["translation"] == 20
    assert bot.captions_received == 1


def test_tts_request_planner_respects_ratio_deterministically():
    planner = TtsRequestPlanner(request_ratio=0.25)

    selected = [planner.should_request(student_index=index, total_students=8) for index in range(8)]

    assert selected.count(True) == 2
    assert selected == [True, False, False, False, True, False, False, False]


@pytest.mark.asyncio
async def test_tts_bot_records_cache_hit_and_miss():
    http_client = FakeTtsHttpClient()
    bot = VirtualTtsBot(
        student_id="student_0",
        lesson_id="lesson_1",
        language="kk",
        http_client=http_client,
        enabled=True,
        bypass_rate_limit=True,
    )
    caption = StudentCaptionEvent(
        student_id="student_0",
        lesson_id="lesson_1",
        caption_sequence=1,
        caption_id="caption_1",
        is_final=True,
        source_text="hello",
        translations={"kk": "salem"},
        provider_latency_ms={},
        received_at=datetime.now(timezone.utc),
    )

    first = await bot.request_tts(caption)
    second = await bot.request_tts(caption)

    assert [event.cache_status for event in bot.events] == ["miss", "hit"]
    assert first.cached is False
    assert second.cached is True
    assert all("secret-token" not in json.dumps(event.model_dump()) for event in bot.events)
    assert http_client.requests[0]["payload"]["return_mode"] == "url"


def test_report_computes_percentiles_and_verdicts():
    summary = percentile_summary([10, 20, 30, 40, 50])
    report = build_local_load_test_report(
        {
            "run_id": "run_1",
            "status": "completed",
            "request": {"sessions": 1, "students_per_session": 4, "mode": "light"},
            "sessions": [{"lesson_id": "lesson_1", "status": "completed"}],
            "students": [{"connected": True}, {"connected": True}, {"connected": True}, {"connected": True}],
            "caption_events": [{"student_receive_latency_ms": value} for value in [10, 20, 30, 40, 50]],
            "tts_events": [
                {"cache_status": "miss", "latency_ms": 120},
                {"cache_status": "hit", "latency_ms": 20},
                {"cache_status": "hit", "latency_ms": 30},
            ],
            "provider_errors": [],
            "dropped_chunks": 0,
        }
    )

    assert summary == {"count": 5, "p50": 30.0, "p95": 48.0, "p99": 49.6}
    assert report["latency"]["student_receive_latency_ms"]["p95"] == 48.0
    assert report["tts"]["cache_hit_ratio"] == 2 / 3
    assert report["overall_verdict"] == "PASS"


def test_stt_wer_cer_and_diff_work_on_known_examples():
    report = stt_quality_report("hello world", "hello there world", segments=[{"text": "hello there world"}])

    assert report["wer"] == 0.5
    assert report["cer"] > 0
    assert report["extra_words"] == ["there"]
    assert report["missing_words"] == []
    assert report["segments"][0]["text"] == "hello there world"


def test_translation_quality_detects_repetition_but_ignores_programming_terms():
    repeated = translation_quality_report("uz", "salom salom salom salom")
    technical = translation_quality_report("uz", "Git Python Docker Redis API WebSocket")

    assert repeated["checks"]["repetition"]["status"] == "warn"
    assert technical["checks"]["code_mixing"]["status"] == "pass"


def test_runtime_metrics_snapshots_are_stored():
    request = LocalLoadTestRequest(sessions=1, students_per_session=1, mode="light", duration_limit_seconds=1)
    run = LocalLoadTestRun(run_id="run_1", request=request)

    run.add_metric_snapshot({"active_lessons": 1, "caption_ws_clients": 90}, collected_at="2026-05-31T00:00:00Z")

    assert run.metric_snapshots[0]["metrics"]["active_lessons"] == 1
    assert run.metric_snapshots[0]["collected_at"] == "2026-05-31T00:00:00Z"


def test_local_load_test_api_validates_limits_and_renders_ui(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "local-load-validation.db")

    with TestClient(app) as client:
        ui = client.get("/load-tests/local")
        sessions = client.post(
            "/api/load-tests/local",
            headers={"x-integration-key": "test-key"},
            json={"sessions": 7, "students_per_session": 1, "mode": "light"},
        )
        students = client.post(
            "/api/load-tests/local",
            headers={"x-integration-key": "test-key"},
            json={"sessions": 1, "students_per_session": 121, "mode": "light"},
        )

    assert ui.status_code == 200
    assert "Local Virtual Lesson Load Test" in ui.text
    assert "local_load_tests.js" in ui.text
    assert sessions.status_code == 422
    assert students.status_code == 422


def test_stop_endpoint_cancels_active_run_and_report_downloads(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "local-load-stop.db")

    with TestClient(app) as client:
        created = client.post(
            "/api/load-tests/local",
            headers={"x-integration-key": "test-key"},
            json={"sessions": 1, "students_per_session": 1, "mode": "light", "duration_limit_seconds": 30},
        )
        assert created.status_code == 201, created.text
        run_id = created.json()["run_id"]
        stopped = client.post(f"/api/load-tests/local/{run_id}/stop", headers={"x-integration-key": "test-key"})
        fetched = client.get(f"/api/load-tests/local/{run_id}")
        report_json = client.get(f"/api/load-tests/local/{run_id}/report/json")
        report_markdown = client.get(f"/api/load-tests/local/{run_id}/report/markdown")
        report_html = client.get(f"/api/load-tests/local/{run_id}/report/html")

    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] in {"stopping", "stopped", "cancelled"}
    assert fetched.json()["run_id"] == run_id
    assert report_json.status_code == 200
    assert report_markdown.headers["content-type"].startswith("text/markdown")
    assert report_html.headers["content-type"].startswith("text/html")


def test_safety_requires_debug_or_explicit_allowance(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENABLE_DEBUG_ENDPOINTS", "false")
    monkeypatch.setenv("ALLOW_LOAD_TESTS", "false")
    monkeypatch.setenv("INTEGRATION_API_KEYS", "test-key")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'local-load-safe.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/api/load-tests/local",
            headers={"x-integration-key": "test-key"},
            json={"sessions": 1, "students_per_session": 1, "mode": "light"},
        )

    assert response.status_code == 403
    assert "ALLOW_LOAD_TESTS" in response.text


def test_reports_and_logs_do_not_expose_tokens():
    sanitized = sanitize_for_report(
        {
            "student_token": "private-student-token",
            "teacher_token": "private-teacher-token",
            "audio_url": "/api/lessons/lesson_1/tts/audio/audio-1?token=secret-token",
            "nested": {"authorization": "Bearer secret-bearer"},
        }
    )
    encoded = json.dumps(sanitized)

    assert "private-student-token" not in encoded
    assert "private-teacher-token" not in encoded
    assert "secret-token" not in encoded
    assert "secret-bearer" not in encoded


def test_cli_help_exposes_virtual_lesson_options():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "--sessions" in result.stdout
    assert "--students-per-session" in result.stdout
    assert "--audio-wav" in result.stdout
    assert "--reference-text" in result.stdout
    assert "--tts-request-ratio" in result.stdout


def test_wav_audio_is_normalized_to_16khz_mono_pcm(tmp_path):
    source = tmp_path / "source_stereo_44k.wav"
    _write_wav(source, sample_rate=44100, channels=2, seconds=0.25)

    normalized = normalize_lesson_audio(source, output_dir=tmp_path / "normalized")

    assert normalized.sample_rate == 16000
    assert normalized.channels == 1
    assert normalized.sample_width == 2
    assert normalized.format == "pcm_s16le"
    assert normalized.duration_seconds == pytest.approx(0.25, abs=0.02)
    assert normalized.pcm_path.exists()
    with wave.open(str(normalized.wav_path), "rb") as reader:
        assert reader.getframerate() == 16000
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getnframes() > 0
    assert normalized.pcm_path.read_bytes()


def test_mp3_normalization_reports_missing_decoder_when_no_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("app.loadtest.audio_normalizer.shutil.which", lambda _name: None)
    monkeypatch.setattr("app.loadtest.audio_normalizer._pydub_available", lambda: False)
    source = tmp_path / "lesson.mp3"
    source.write_bytes(b"not-really-mp3")

    with pytest.raises(AudioNormalizationError) as exc:
        normalize_lesson_audio(source, output_dir=tmp_path / "normalized")

    assert "ffmpeg or pydub" in str(exc.value)


def test_audio_normalization_metadata_is_in_load_test_report(tmp_path):
    source = tmp_path / "source.wav"
    _write_wav(source, sample_rate=44100, channels=2, seconds=0.1)
    normalized = normalize_lesson_audio(source, output_dir=tmp_path / "normalized")
    request = LocalLoadTestRequest(sessions=1, students_per_session=1, mode="light", audio_file_id=str(source))
    run = LocalLoadTestRun(run_id="run_audio", request=request)
    run.audio = normalized.model_dump(mode="json")

    report = run.build_report()

    assert report["audio"]["normalized"]["sample_rate"] == 16000
    assert report["audio"]["normalized"]["channels"] == 1
    assert report["audio"]["normalized"]["duration_seconds"] == pytest.approx(0.1, abs=0.02)


@pytest.mark.asyncio
async def test_runner_normalizes_audio_before_completing_run(tmp_path):
    source = tmp_path / "lesson.wav"
    _write_wav(source, sample_rate=44100, channels=2, seconds=0.1)
    runner = LocalLoadTestRunner(report_dir=tmp_path / "reports")
    request = LocalLoadTestRequest(sessions=1, students_per_session=1, mode="light", audio_file_id=str(source), duration_limit_seconds=1)

    run = await runner.start(request)
    await asyncio.wait_for(runner._tasks[run.run_id], timeout=5)
    report = runner.report(run.run_id)

    assert run.status == "completed"
    assert run.audio["normalized"]["sample_rate"] == 16000
    assert run.audio["normalized"]["channels"] == 1
    assert report["audio"]["normalized"]["duration_seconds"] == pytest.approx(0.1, abs=0.02)


def _write_wav(path: Path, *, sample_rate: int, channels: int, seconds: float) -> None:
    frames = int(sample_rate * seconds)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        payload = bytearray()
        for index in range(frames):
            sample = int(math.sin(index / 12) * 12000)
            frame = sample.to_bytes(2, "little", signed=True)
            payload.extend(frame * channels)
        writer.writeframes(bytes(payload))


def _app(tmp_path, monkeypatch, db_name: str):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENABLE_DEBUG_ENDPOINTS", "true")
    monkeypatch.setenv("ALLOW_LOAD_TESTS", "true")
    monkeypatch.setenv("INTEGRATION_API_KEYS", "test-key")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("TTS_PROVIDER", "mock")
    monkeypatch.setenv("TTS_AUDIO_URL_TOKEN_REQUIRED", "false")
    return create_app()
