import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_SCRIPT = ROOT / "scripts/generate_production_evidence_bundle.py"
VALIDATOR_SCRIPT = ROOT / "scripts/validate_production_evidence_bundle.py"
CI_GATE_SCRIPT = ROOT / "scripts/ci_production_readiness_gate.py"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _latest_bundle(output_dir: Path) -> Path:
    bundles = [path for path in output_dir.iterdir() if path.is_dir()]
    assert bundles
    return max(bundles, key=lambda path: path.name)


def _raw_report() -> dict:
    return {
        "overall_result": "pass",
        "mock_only": True,
        "real_provider_proof": False,
        "students": 1000,
        "lessons": 6,
        "aggregate_results": {
            "students_connected": 1000,
            "receive_rate": 1.0,
            "p95_caption_latency_ms": 240,
            "p99_caption_latency_ms": 500,
            "disconnects": 0,
            "errors": 0,
        },
        "per_lesson_results": [
            {"lesson_id": f"lesson_{index}", "students_connected": 167 if index < 4 else 166, "receive_rate": 1.0}
            for index in range(6)
        ],
        "runtime_metrics_after": {
            "redis_pubsub_messages_published_total": 2160,
            "redis_pubsub_messages_received_total": 2160,
            "websocket_send_timeouts_total": 0,
        },
        "audio_url": "https://cdn.example.test/audio/1?token=signed-stage27d-secret",
        "integration_key": "stage27d-integration-secret",
    }


def _analysis(verdict: str = "PASS_WITH_WARNINGS") -> dict:
    return {
        "verdict": verdict,
        "scenario": "stage27b_6_lessons_1000_caption_websocket_mock_load",
        "mock_only": True,
        "real_provider_proof": False,
        "students": 1000,
        "lessons": 6,
        "connected_clients_total": 1000,
        "receive_rate": 1.0,
        "latency": {"p50_ms": 50, "p95_ms": 240, "p99_ms": 500},
        "disconnects": 0,
        "errors_count": 0,
        "failed_thresholds": [],
        "warnings": ["minor warning with Authorization: Bearer stage27d-analysis-secret"],
        "bottleneck_hints": ["No obvious bottleneck detected."],
        "mock_websocket_fanout_only": True,
        "real_provider_capacity_proven": False,
    }


def _write_stage27d_artifacts(tmp_path: Path, analysis_verdict: str = "PASS_WITH_WARNINGS") -> tuple[Path, Path]:
    raw = tmp_path / "raw_report.json"
    analysis = tmp_path / "analysis.json"
    _write_json(raw, _raw_report())
    _write_json(analysis, _analysis(analysis_verdict))
    return raw, analysis


def test_evidence_bundle_includes_stage27b_raw_and_stage27c_analysis_sanitized(tmp_path):
    raw, analysis = _write_stage27d_artifacts(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(BUNDLE_SCRIPT),
            "--no-network",
            "--output-dir",
            str(tmp_path / "bundles"),
            "--include-6-lessons-1000-ws-report",
            str(raw),
            "--include-6-lessons-1000-ws-analysis",
            str(analysis),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    bundle_dir = _latest_bundle(tmp_path / "bundles")
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    raw_text = (bundle_dir / "load_test_6_lessons_1000_ws_report.json").read_text(encoding="utf-8")
    analysis_text = (bundle_dir / "load_test_6_lessons_1000_ws_analysis.json").read_text(encoding="utf-8")

    assert "load_test_6_lessons_1000_ws_report.json" in manifest["included_artifacts"]
    assert "load_test_6_lessons_1000_ws_analysis.json" in manifest["included_artifacts"]
    assert manifest["load_test_6_lessons_1000_ws_report_included"] is True
    assert manifest["load_test_6_lessons_1000_ws_analysis_included"] is True
    for secret in ("signed-stage27d-secret", "stage27d-integration-secret", "stage27d-analysis-secret"):
        assert secret not in raw_text
        assert secret not in analysis_text


def test_validator_requires_stage27b_stage27c_evidence_when_flag_is_set(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_json(
        bundle / "manifest.json",
        {
            "generated_at_utc": "2026-05-24T00:00:00Z",
            "base_url": "https://python-service.example.com",
            "included_artifacts": ["manifest.json"],
            "missing_artifacts": [],
            "safety_notes": ["safe packaging"],
            "final_bundle_status": "complete",
            "sanitized": True,
        },
    )
    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_SCRIPT),
            "--bundle-dir",
            str(bundle),
            "--require-6-lessons-1000-ws-pass",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "NOT_READY" in result.stdout
    assert "load_test_6_lessons_1000_ws_report.json missing" in result.stdout
    assert "load_test_6_lessons_1000_ws_analysis.json missing" in result.stdout


def test_validator_passes_required_stage27b_stage27c_evidence(tmp_path):
    raw, analysis = _write_stage27d_artifacts(tmp_path)
    subprocess.run(
        [
            sys.executable,
            str(BUNDLE_SCRIPT),
            "--no-network",
            "--output-dir",
            str(tmp_path / "bundles"),
            "--include-6-lessons-1000-ws-report",
            str(raw),
            "--include-6-lessons-1000-ws-analysis",
            str(analysis),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    bundle_dir = _latest_bundle(tmp_path / "bundles")
    output = tmp_path / "validation.json"
    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_SCRIPT),
            "--bundle-dir",
            str(bundle_dir),
            "--require-6-lessons-1000-ws-pass",
            "--output-json",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert result.returncode == 0, result.stdout
    assert payload["verdict"] == "READY_FOR_STAGING"
    assert payload["ws_6_lessons_1000_status"] == "pass"
    assert payload["ws_6_lessons_1000_verdict"] == "PASS_WITH_WARNINGS"
    assert payload["ws_6_lessons_1000_mock_only"] is True
    assert payload["ws_6_lessons_1000_real_provider_proof"] is False
    assert payload["ws_6_lessons_1000_warnings"]


def test_validator_fails_required_stage27c_analysis_failure(tmp_path):
    raw, analysis = _write_stage27d_artifacts(tmp_path, analysis_verdict="FAIL")
    subprocess.run(
        [
            sys.executable,
            str(BUNDLE_SCRIPT),
            "--no-network",
            "--output-dir",
            str(tmp_path / "bundles"),
            "--include-6-lessons-1000-ws-report",
            str(raw),
            "--include-6-lessons-1000-ws-analysis",
            str(analysis),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_SCRIPT),
            "--bundle-dir",
            str(_latest_bundle(tmp_path / "bundles")),
            "--require-6-lessons-1000-ws-pass",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "Stage 27C analysis verdict is not passing" in result.stdout


def test_ci_gate_can_package_provided_stage27b_stage27c_artifacts_without_sockets(tmp_path):
    raw, analysis = _write_stage27d_artifacts(tmp_path)
    result_path = tmp_path / "ci_result.json"
    result = subprocess.run(
        [
            sys.executable,
            str(CI_GATE_SCRIPT),
            "--no-network",
            "--output-dir",
            str(tmp_path / "gate"),
            "--result-json",
            str(result_path),
            "--include-6-lessons-1000-ws-report",
            str(raw),
            "--include-6-lessons-1000-ws-analysis",
            str(analysis),
            "--require-6-lessons-1000-ws-pass",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    bundle_dir = Path(payload["bundle_dir"])

    assert result.returncode == 0, result.stdout
    assert payload["gate_result"] == "PASS"
    assert payload["ws_6_lessons_1000_status"] == "pass"
    assert (bundle_dir / "load_test_6_lessons_1000_ws_report.json").exists()
    assert (bundle_dir / "load_test_6_lessons_1000_ws_analysis.json").exists()


def test_docs_say_ci_does_not_run_1000_sockets_and_mock_only_proof():
    docs = "\n".join(
        [
            _read("docs/6-lessons-1000-students-load-test.md"),
            _read("docs/6-lessons-1000-ws-report-analysis.md"),
            _read("docs/production-evidence-bundle.md"),
            _read("docs/production-evidence-validation.md"),
            _read("docs/ci-production-readiness-gate.md"),
            _read("docs/release-operator-runbook.md"),
            _read("docs/load-testing.md"),
        ]
    )

    assert "Stage 27B creates raw load evidence" in docs
    assert "Stage 27C analyzes raw evidence" in docs
    assert "Evidence bundle packages both" in docs
    assert "--require-6-lessons-1000-ws-pass" in docs
    assert "CI gate should not run the 1000-socket load test itself" in docs
    assert "Passing Stage 27B/27C proves mock WebSocket fanout only" in docs
    assert "not real-provider capacity" in docs
