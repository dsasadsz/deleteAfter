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
    students: int = 1000,
    connected_clients_total: int | None = None,
    peak_connected_clients: int | None = None,
    mock_only: bool = True,
    real_provider_proof: bool = False,
    final_result: str = "pass",
) -> dict:
    connected = students if connected_clients_total is None else connected_clients_total
    report = {
        "overall_result": final_result,
        "mock_only": mock_only,
        "real_provider_proof": real_provider_proof,
        "students": students,
        "lessons": lessons,
        "aggregate_results": {
            "students_connected": connected,
            "receive_rate": 1.0,
            "p95_caption_latency_ms": 240,
            "p99_caption_latency_ms": 500,
            "disconnects": 0,
            "errors": 0,
        },
        "per_lesson_results": [
            {"lesson_id": f"lesson_{index}", "students_connected": connected // max(1, lessons), "receive_rate": 1.0}
            for index in range(lessons)
        ],
    }
    if peak_connected_clients is not None:
        report["peak_connected_clients"] = peak_connected_clients
    return report


def _analysis(
    *,
    lessons: int = 6,
    students: int = 1000,
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
        "latency": {"p50_ms": 50, "p95_ms": 240, "p99_ms": 500},
        "disconnects": 0,
        "errors_count": 0,
        "failed_thresholds": [],
        "warnings": [],
        "mock_websocket_fanout_only": True,
        "real_provider_capacity_proven": False,
    }


def _bundle_with_ws_evidence(path: Path, raw_report: dict, analysis: dict | None) -> None:
    _minimal_bundle(path)
    _write_json(path / "load_test_6_lessons_1000_ws_report.json", raw_report)
    if analysis is not None:
        _write_json(path / "load_test_6_lessons_1000_ws_analysis.json", analysis)


def _run_validator(bundle: Path, output: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(VALIDATOR_SCRIPT),
        "--bundle-dir",
        str(bundle),
        "--require-6-lessons-1000-ws-pass",
    ]
    if output is not None:
        command.extend(["--output-json", str(output)])
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)


def test_help_exposes_generic_ws_scenario_flags():
    result = subprocess.run(
        [sys.executable, str(VALIDATOR_SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--require-ws-lessons" in result.stdout
    assert "--require-ws-students" in result.stdout


def test_validator_fails_when_6x500_pass_evidence_is_used_for_6x1000(tmp_path):
    bundle = tmp_path / "bundle"
    _bundle_with_ws_evidence(bundle, _raw_report(students=500), _analysis(students=500))

    result = _run_validator(bundle)

    assert result.returncode == 1
    assert "NOT_READY" in result.stdout
    assert "6 lessons / 1000 WebSocket proof requires lessons=6 and students=1000" in result.stdout


def test_validator_passes_fake_6x1000_pass_evidence(tmp_path):
    bundle = tmp_path / "bundle"
    output = tmp_path / "validation.json"
    _bundle_with_ws_evidence(bundle, _raw_report(peak_connected_clients=1000), _analysis())

    result = _run_validator(bundle, output)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert result.returncode == 0, result.stdout
    assert payload["verdict"] == "READY_FOR_STAGING"
    assert payload["ws_required_lessons"] == 6
    assert payload["ws_required_students"] == 1000
    assert payload["ws_report_lessons"] == 6
    assert payload["ws_report_students"] == 1000
    assert payload["ws_connected_clients_total"] == 1000
    assert payload["ws_peak_connected_clients"] == 1000
    assert payload["ws_scenario_match"] is True
    assert payload["ws_scenario_status"] == "pass"
    assert payload["ws_6_lessons_1000_status"] == "pass"


def test_validator_fails_for_1x1000(tmp_path):
    bundle = tmp_path / "bundle"
    _bundle_with_ws_evidence(bundle, _raw_report(lessons=1), _analysis(lessons=1))

    result = _run_validator(bundle)

    assert result.returncode == 1
    assert "6 lessons / 1000 WebSocket proof requires lessons=6 and students=1000" in result.stdout


def test_validator_fails_for_6x999(tmp_path):
    bundle = tmp_path / "bundle"
    _bundle_with_ws_evidence(bundle, _raw_report(students=999), _analysis(students=999))

    result = _run_validator(bundle)

    assert result.returncode == 1
    assert "6 lessons / 1000 WebSocket proof requires lessons=6 and students=1000" in result.stdout


def test_validator_fails_when_mock_only_false(tmp_path):
    bundle = tmp_path / "bundle"
    _bundle_with_ws_evidence(bundle, _raw_report(mock_only=False), _analysis(mock_only=False))

    result = _run_validator(bundle)

    assert result.returncode == 1
    assert "Stage 27B raw report must have mock_only=true" in result.stdout


def test_validator_fails_when_real_provider_proof_true(tmp_path):
    bundle = tmp_path / "bundle"
    _bundle_with_ws_evidence(bundle, _raw_report(real_provider_proof=True), _analysis(real_provider_proof=True))

    result = _run_validator(bundle)

    assert result.returncode == 1
    assert "Stage 27B raw report must have real_provider_proof=false" in result.stdout


def test_validator_fails_when_analysis_is_missing(tmp_path):
    bundle = tmp_path / "bundle"
    _bundle_with_ws_evidence(bundle, _raw_report(), None)

    result = _run_validator(bundle)

    assert result.returncode == 1
    assert "load_test_6_lessons_1000_ws_analysis.json missing" in result.stdout


def test_validator_fails_when_analysis_fails(tmp_path):
    bundle = tmp_path / "bundle"
    _bundle_with_ws_evidence(bundle, _raw_report(), _analysis(verdict="FAIL"))

    result = _run_validator(bundle)

    assert result.returncode == 1
    assert "Stage 27C analysis verdict is not passing" in result.stdout


def test_ci_gate_fails_when_6x500_evidence_is_required_for_6x1000(tmp_path):
    raw = tmp_path / "raw.json"
    analysis = tmp_path / "analysis.json"
    result_path = tmp_path / "ci_result.json"
    _write_json(raw, _raw_report(students=500))
    _write_json(analysis, _analysis(students=500))

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

    assert result.returncode == 1
    assert payload["gate_result"] == "FAIL"
    assert payload["ws_scenario_status"] == "fail"
    assert "6 lessons / 1000 WebSocket proof requires lessons=6 and students=1000" in "\n".join(payload["failed_checks"])


def test_docs_mention_6x500_must_not_satisfy_1000_client_proof():
    docs = "\n".join(
        [
            _read("docs/production-evidence-validation.md"),
            _read("docs/ci-production-readiness-gate.md"),
            _read("docs/production-evidence-bundle.md"),
            _read("docs/staging-1000-ws-test-runbook.md"),
            _read("docs/stage27f-rerun-matrix-result.md"),
            _read("docs/load-testing.md"),
        ]
    )

    assert "6x500 PASS is valid evidence for about 480 users" in docs
    assert "6x500 must not satisfy 1000-client proof" in docs
    assert "requires exact 6 lessons / 1000 requested students" in docs
    assert "mock WebSocket proof only" in docs
