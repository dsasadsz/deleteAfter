import json
import subprocess
import sys

from scripts import run_1000_user_readiness_test as runner


def test_readiness_runner_help_works():
    result = subprocess.run(
        [sys.executable, "scripts/run_1000_user_readiness_test.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--students" in result.stdout
    assert "--tts-requests" in result.stdout
    assert "--allow-skip-tts" in result.stdout


def test_sanitize_url_redacts_signed_audio_token_query():
    sanitized = runner.sanitize_url(
        "http://127.0.0.1:8000/api/v1/integration/lessons/lesson_1/tts/audio/audio-1?token=signed.secret&format=mp3"
    )

    assert "signed.secret" not in sanitized
    assert "token=<redacted>" in sanitized
    assert "format=mp3" in sanitized


def test_env_parser_finds_integration_key_without_printing_it(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "AZURE_TTS_KEY=real-provider-key",
                "INTEGRATION_API_KEYS=first-secret, second-secret",
            ]
        ),
        encoding="utf-8",
    )

    resolved = runner.resolve_integration_key(
        explicit_key="",
        integration_key_env="INTEGRATION_API_KEYS",
        env_file=env_file,
    )
    sanitized = runner.sanitize_for_report({"integration_key": resolved.value, "audio_url": "http://x/a?token=abc"})

    assert resolved.value == "first-secret"
    assert resolved.source == "INTEGRATION_API_KEYS"
    assert json.dumps(sanitized) == '{"integration_key": "[redacted]", "audio_url": "http://x/a?token=<redacted>"}'


def test_missing_integration_key_message_is_actionable():
    message = runner.integration_key_missing_message("INTEGRATION_API_KEYS", ".env")

    assert "INTEGRATION_API_KEYS" in message
    assert ".env" in message
    assert "--integration-key" in message


def test_child_script_system_exit_message_is_preserved():
    def fake_run(args):
        raise SystemExit("HTTP 403: Load-test endpoints require ENABLE_LOAD_TEST_ENDPOINTS=true and APP_ENV=development.")

    exit_code, payload, output = runner.run_load_test_script(fake_run, runner.build_parser().parse_args([]))

    assert exit_code == 1
    assert output == ""
    assert "ENABLE_LOAD_TEST_ENDPOINTS=true" in payload["error"]


def test_captions_pass_fail_evaluator_uses_1000_user_thresholds():
    passing = {
        "captions_published": 180,
        "runtime_metrics": {
            "captions_per_second": 3.0,
            "websocket_send_failures_total": 0,
            "websocket_send_timeouts_total": 0,
            "websocket_clients_dropped_total": 0,
            "redis_pubsub_messages_published_total": 12,
            "redis_pubsub_messages_received_total": 12,
            "redis_pubsub_errors_total": 0,
        },
    }
    failing = {
        "captions_published": 179,
        "runtime_metrics": {
            "captions_per_second": 2.4,
            "websocket_send_failures_total": 1,
            "websocket_send_timeouts_total": 0,
            "websocket_clients_dropped_total": 0,
            "redis_pubsub_messages_published_total": 0,
            "redis_pubsub_messages_received_total": 0,
            "redis_pubsub_errors_total": 1,
        },
    }

    assert runner.overall_verdict(runner.evaluate_captions_result(passing)) == "PASS"
    failed_checks = runner.evaluate_captions_result(failing)
    assert runner.overall_verdict(failed_checks) == "FAIL"
    assert {check.name for check in failed_checks if check.status == "FAIL"} >= {
        "captions_published",
        "captions_per_second",
        "websocket_send_failures_total",
        "redis_pubsub_messages_published_total",
        "redis_pubsub_messages_received_total",
        "redis_pubsub_errors_total",
    }


def test_tts_pass_fail_evaluator_uses_url_cache_thresholds():
    passing = {
        "total_requests": 1000,
        "success": 1000,
        "failed": 0,
        "audio_url_success": 1000,
        "audio_url_failed": 0,
        "auth_401_count": 0,
        "cache_hits": 999,
        "cache_misses": 1,
        "provider_calls_before": 10,
        "provider_calls_after": 11,
        "provider_calls_saved": 999,
    }
    failing = {
        "total_requests": 1000,
        "success": 999,
        "failed": 1,
        "audio_url_success": 999,
        "audio_url_failed": 1,
        "auth_401_count": 1,
        "cache_hits": 10,
        "cache_misses": 50,
        "provider_calls_before": 10,
        "provider_calls_after": 20,
        "provider_calls_saved": 100,
    }

    assert runner.overall_verdict(runner.evaluate_tts_result(passing, expected_requests=1000)) == "PASS"
    failed_checks = runner.evaluate_tts_result(failing, expected_requests=1000)
    assert runner.overall_verdict(failed_checks) == "FAIL"
    assert {check.name for check in failed_checks if check.status == "FAIL"} >= {
        "tts_success",
        "tts_failed",
        "tts_audio_url_success",
        "tts_audio_url_failed",
        "tts_auth_401_count",
        "tts_provider_calls_delta",
        "tts_provider_calls_saved",
    }


def test_report_writer_creates_markdown_and_json_with_sanitized_payload(tmp_path):
    report = {
        "verdict": "PASS",
        "base_url": "http://127.0.0.1:8000",
        "checks": [{"name": "sample", "status": "PASS", "message": "ok", "value": 1}],
        "tts": {"audio_url": "http://x/a?token=secret-token", "integration_key": "secret-key"},
        "limitations": ["mock providers only"],
        "next_recommended_step": "real-provider small E2E using docs/REAL_PROVIDER_E2E_TEST.md",
    }

    paths = runner.write_reports(report, tmp_path)
    json_payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
    md_text = paths.markdown_path.read_text(encoding="utf-8")

    assert paths.json_path.exists()
    assert paths.markdown_path.exists()
    assert "secret-token" not in paths.json_path.read_text(encoding="utf-8")
    assert "secret-key" not in paths.json_path.read_text(encoding="utf-8")
    assert json_payload["tts"]["audio_url"] == "http://x/a?token=<redacted>"
    assert "Overall Verdict: PASS" in md_text
    assert "mock providers only" in md_text


def test_docs_mention_1000_user_runner_command_and_limitations():
    docs = open("docs/1000-user-readiness-test.md", encoding="utf-8").read()
    load_testing = open("docs/load-testing.md", encoding="utf-8").read()

    assert "python scripts/run_1000_user_readiness_test.py --base-url http://127.0.0.1:8000" in docs
    assert "mock readiness" in docs.lower()
    assert "not real Azure/ElevenLabs/Zoom proof" in docs
    assert "docs/1000-user-readiness-test.md" in load_testing
