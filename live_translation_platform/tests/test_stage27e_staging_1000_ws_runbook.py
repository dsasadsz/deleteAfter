from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs/staging-1000-ws-test-runbook.md"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_staging_1000_ws_runbook_exists():
    assert RUNBOOK.exists()


def test_runbook_mentions_mock_only_and_real_provider_limits():
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "mock WebSocket fanout only" in runbook
    assert "real-provider capacity is not proven" in runbook
    assert "does not call real STT, translation, TTS, Azure, ElevenLabs, or Zoom providers" in runbook


def test_runbook_includes_required_stage27b_command():
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "python scripts/load_test_6_lessons_1000_students.py" in runbook
    assert "--lessons 6" in runbook
    assert "--students 1000" in runbook
    assert "--duration-seconds 120" in runbook
    assert "--captions-per-second 3" in runbook
    assert "--assert-min-receive-rate 0.99" in runbook
    assert "--assert-p95-caption-latency-ms 1000" in runbook
    assert "--report-json tmp/load_test_6_lessons_1000_students_report.json" in runbook


def test_runbook_includes_stage27c_bundle_validator_and_ci_commands():
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "python scripts/analyze_6_lessons_1000_ws_report.py" in runbook
    assert "--output-json tmp/load_test_6_lessons_1000_students_analysis.json" in runbook
    assert "python scripts/generate_production_evidence_bundle.py" in runbook
    assert "--include-6-lessons-1000-ws-report tmp/load_test_6_lessons_1000_students_report.json" in runbook
    assert "--include-6-lessons-1000-ws-analysis tmp/load_test_6_lessons_1000_students_analysis.json" in runbook
    assert "python scripts/validate_production_evidence_bundle.py" in runbook
    assert "--require-6-lessons-1000-ws-pass" in runbook
    assert "python scripts/ci_production_readiness_gate.py" in runbook
    assert "--result-json tmp/ci_readiness_gate_result.json" in runbook


def test_runbook_mentions_infrastructure_requirements():
    runbook = RUNBOOK.read_text(encoding="utf-8")

    for phrase in (
        "Redis Pub/Sub",
        "PostgreSQL",
        "WSS",
        "reverse proxy",
        "OS/file descriptor limits",
        "docker compose -f docker-compose.prod.yml -f docker-compose.loadtest.yml up -d --build",
        "python scripts/check_deployment_readiness.py --base-url http://127.0.0.1:8000",
    ):
        assert phrase in runbook


def test_existing_docs_link_to_staging_1000_ws_runbook():
    for path in (
        "README.md",
        "docs/load-testing.md",
        "docs/6-lessons-1000-students-load-test.md",
        "docs/6-lessons-1000-ws-report-analysis.md",
        "docs/production-evidence-bundle.md",
        "docs/production-evidence-validation.md",
        "docs/ci-production-readiness-gate.md",
        "docs/release-operator-runbook.md",
    ):
        content = _read(path)
        assert "docs/staging-1000-ws-test-runbook.md" in content
