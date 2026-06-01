import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_SCRIPT = ROOT / "scripts/validate_production_evidence_bundle.py"
CI_GATE_SCRIPT = ROOT / "scripts/ci_production_readiness_gate.py"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _minimal_bundle(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _write_json(
        path / "manifest.json",
        {
            "generated_at_utc": "2026-05-25T00:00:00Z",
            "base_url": "https://python-service.example.com",
            "included_artifacts": [
                "manifest.json",
                "environment_summary.json",
                "load_test_6_lessons_1000_ws_report.json",
                "load_test_6_lessons_1000_ws_analysis.json",
            ],
            "missing_artifacts": [],
            "safety_notes": ["safe packaging"],
            "final_bundle_status": "complete",
            "sanitized": True,
        },
    )
    _write_json(path / "environment_summary.json", {"no_network": True})


def _raw_report(
    *,
    lessons: int = 6,
    students: int = 500,
    connected_clients_total: int | None = None,
    mock_only: bool = True,
    real_provider_proof: bool = False,
    final_result: str = "pass",
) -> dict:
    connected = students if connected_clients_total is None else connected_clients_total
    return {
        "overall_result": final_result,
        "mock_only": mock_only,
        "real_provider_proof": real_provider_proof,
        "students": students,
        "lessons": lessons,
        "aggregate_results": {
            "students_connected": connected,
            "peak_connected_clients": connected,
            "receive_rate": 1.0,
            "p95_caption_latency_ms": 894.58,
            "p99_caption_latency_ms": 937.64,
            "disconnects": 0,
            "errors": 0,
        },
        "per_lesson_results": [
            {"lesson_id": f"lesson_{index}", "students_connected": connected // max(1, lessons), "receive_rate": 1.0}
            for index in range(lessons)
        ],
    }


def _analysis(
    *,
    lessons: int = 6,
    students: int = 500,
    connected_clients_total: int | None = None,
    verdict: str = "PASS",
    mock_only: bool = True,
    real_provider_proof: bool = False,
) -> dict:
    connected = students if connected_clients_total is None else connected_clients_total
    return {
        "verdict": verdict,
        "scenario": f"stage27b_{lessons}_lessons_{students}_caption_websocket_mock_load",
        "mock_only": mock_only,
        "real_provider_proof": real_provider_proof,
        "students": students,
        "lessons": lessons,
        "connected_clients_total": connected,
        "receive_rate": 1.0,
        "latency": {"p50_ms": 539.04, "p95_ms": 894.58, "p99_ms": 937.64},
        "disconnects": 0,
        "errors_count": 0,
        "failed_thresholds": [],
        "warnings": [],
        "mock_websocket_fanout_only": True,
        "real_provider_capacity_proven": False,
    }


def _bundle_with_ws_evidence(path: Path, raw_report: dict, analysis: dict) -> None:
    _minimal_bundle(path)
    _write_json(path / "load_test_6_lessons_1000_ws_report.json", raw_report)
    _write_json(path / "load_test_6_lessons_1000_ws_analysis.json", analysis)


def _run_realistic_validator(bundle: Path, output: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(VALIDATOR_SCRIPT),
        "--bundle-dir",
        str(bundle),
        "--require-realistic-ws-pass",
    ]
    if output is not None:
        command.extend(["--output-json", str(output)])
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)


def test_validator_passes_fake_6x500_pass_report_with_realistic_flag(tmp_path):
    bundle = tmp_path / "bundle"
    output = tmp_path / "validation.json"
    _bundle_with_ws_evidence(bundle, _raw_report(), _analysis())

    result = _run_realistic_validator(bundle, output)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert result.returncode == 0, result.stdout
    assert payload["verdict"] == "READY_FOR_STAGING"
    assert payload["realistic_ws_status"] == "pass"
    assert payload["realistic_ws_required_lessons"] == 6
    assert payload["realistic_ws_required_students"] == 480
    assert payload["realistic_ws_report_lessons"] == 6
    assert payload["realistic_ws_report_students"] == 500
    assert payload["realistic_ws_connected_clients_total"] == 500
    assert payload["realistic_ws_verdict"] == "PASS"
    assert payload["realistic_ws_mock_only"] is True
    assert payload["realistic_ws_real_provider_proof"] is False
    assert "expected-scale" in result.stdout
    assert "not 1000-client proof" in result.stdout


def test_validator_fails_6x100_with_realistic_flag(tmp_path):
    bundle = tmp_path / "bundle"
    _bundle_with_ws_evidence(bundle, _raw_report(students=100), _analysis(students=100))

    result = _run_realistic_validator(bundle)

    assert result.returncode == 1
    assert "NOT_READY" in result.stdout
    assert "realistic WebSocket proof requires students >= 480" in result.stdout


def test_validator_fails_realistic_when_mock_only_false(tmp_path):
    bundle = tmp_path / "bundle"
    _bundle_with_ws_evidence(bundle, _raw_report(mock_only=False), _analysis(mock_only=False))

    result = _run_realistic_validator(bundle)

    assert result.returncode == 1
    assert "Stage 27B raw report must have mock_only=true" in result.stdout


def test_validator_fails_realistic_when_real_provider_proof_true(tmp_path):
    bundle = tmp_path / "bundle"
    _bundle_with_ws_evidence(bundle, _raw_report(real_provider_proof=True), _analysis(real_provider_proof=True))

    result = _run_realistic_validator(bundle)

    assert result.returncode == 1
    assert "Stage 27B raw report must have real_provider_proof=false" in result.stdout


def test_ci_gate_supports_realistic_ws_pass(tmp_path):
    raw = tmp_path / "raw.json"
    analysis = tmp_path / "analysis.json"
    result_path = tmp_path / "ci_result.json"
    _write_json(raw, _raw_report())
    _write_json(analysis, _analysis())

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
            "--require-realistic-ws-pass",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result_path.read_text(encoding="utf-8"))

    assert result.returncode == 0, result.stdout
    assert payload["gate_result"] == "PASS"
    assert payload["realistic_ws_status"] == "pass"
    assert payload["realistic_ws_report_students"] == 500


def test_help_exposes_realistic_ws_flags():
    validator = subprocess.run(
        [sys.executable, str(VALIDATOR_SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    ci_gate = subprocess.run(
        [sys.executable, str(CI_GATE_SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert validator.returncode == 0
    assert ci_gate.returncode == 0
    assert "--require-realistic-ws-pass" in validator.stdout
    assert "--require-realistic-ws-pass" in ci_gate.stdout


def test_docs_explain_realistic_6x500_scope():
    docs = "\n".join(
        [
            _read("docs/realistic-6-lessons-500-ws-proof.md"),
            _read("docs/production-evidence-validation.md"),
            _read("docs/production-evidence-bundle.md"),
            _read("docs/load-testing.md"),
            _read("docs/stage27f-rerun-matrix-result.md"),
            _read("docs/release-operator-runbook.md"),
        ]
    )

    assert "6 lessons x ~80 students" in docs
    assert "6x500 is valid proof for the current expected scale" in docs
    assert "6x500 is not 1000-client proof" in docs
    assert "1000-client proof remains inconclusive" in docs
