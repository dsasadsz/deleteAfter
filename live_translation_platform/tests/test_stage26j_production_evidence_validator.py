import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/validate_production_evidence_bundle.py"


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
            "generated_at_utc": "2026-05-23T00:00:00Z",
            "base_url": "https://python-service.example.com",
            "included_artifacts": ["manifest.json", "environment_summary.json", "README.md"],
            "missing_artifacts": [],
            "safety_notes": ["safe packaging"],
            "final_bundle_status": "complete",
            "sanitized": True,
        },
    )
    _write_json(path / "environment_summary.json", {"no_network": True})
    (path / "README.md").write_text("# Production Evidence Bundle\n", encoding="utf-8")


def _passing_bundle(path: Path) -> None:
    _minimal_bundle(path)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    manifest["included_artifacts"] = [
        "manifest.json",
        "environment_summary.json",
        "README.md",
        "readiness_summary.json",
        "runtime_metrics_summary.json",
        "config_check_summary.json",
        "real_provider_e2e_report.json",
        "load_test_report.json",
    ]
    _write_json(path / "manifest.json", manifest)
    _write_json(path / "readiness_summary.json", {"ok": True, "payload": {"status": "ready"}})
    _write_json(
        path / "config_check_summary.json",
        {
            "ok": True,
            "payload": {
                "production_safe": True,
                "config_missing": [],
                "config_warnings": [],
            },
        },
    )
    _write_json(path / "runtime_metrics_summary.json", {"ok": True, "payload": {"active_lessons": 0}})
    _write_json(path / "real_provider_e2e_report.json", {"run_mode": "real_provider", "final_result": "pass"})
    _write_json(path / "load_test_report.json", {"final_result": "pass", "status": "pass"})


def test_help_works():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--bundle-dir" in result.stdout
    assert "--require-real-provider-proof" in result.stdout
    assert "--output-json" in result.stdout


def test_validator_fails_if_manifest_is_missing(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--bundle-dir", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "NOT_READY" in result.stdout
    assert "manifest.json" in result.stdout


def test_minimal_sanitized_bundle_is_partial_evidence(tmp_path):
    bundle = tmp_path / "bundle"
    _minimal_bundle(bundle)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--bundle-dir", str(bundle)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "PARTIAL_EVIDENCE" in result.stdout


def test_real_provider_required_rejects_dry_run_report(tmp_path):
    bundle = tmp_path / "bundle"
    _minimal_bundle(bundle)
    _write_json(bundle / "real_provider_e2e_report.json", {"run_mode": "dry_run", "final_result": "pass"})

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--bundle-dir",
            str(bundle),
            "--require-real-provider-proof",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "REAL_PROVIDER_NOT_PROVEN" in result.stdout
    assert "dry_run" in result.stdout


def test_ready_for_staging_when_required_artifacts_pass(tmp_path):
    bundle = tmp_path / "bundle"
    _passing_bundle(bundle)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--bundle-dir",
            str(bundle),
            "--require-real-provider-proof",
            "--require-load-test-report",
            "--require-readiness-ready",
            "--require-config-production-safe",
            "--require-runtime-metrics",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "READY_FOR_STAGING" in result.stdout


def test_secret_like_text_makes_bundle_not_ready(tmp_path):
    bundle = tmp_path / "bundle"
    _minimal_bundle(bundle)
    (bundle / "leaky.md").write_text("Authorization: Bearer real-secret-token\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--bundle-dir", str(bundle)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "NOT_READY" in result.stdout
    assert "secret_scan_findings" in result.stdout
    assert "real-secret-token" not in result.stdout


def test_output_json_writes_expected_fields(tmp_path):
    bundle = tmp_path / "bundle"
    output = tmp_path / "validation_result.json"
    _minimal_bundle(bundle)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--bundle-dir", str(bundle), "--output-json", str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    for field in (
        "verdict",
        "checked_files",
        "missing_files",
        "failed_checks",
        "warnings",
        "secret_scan_findings",
        "real_provider_proof_status",
        "load_test_status",
        "readiness_status",
        "config_status",
    ):
        assert field in payload
    assert payload["verdict"] == "PARTIAL_EVIDENCE"


def test_docs_mention_validator_and_ready_for_staging_limits():
    validation_doc = _read("docs/production-evidence-validation.md")
    bundle_doc = _read("docs/production-evidence-bundle.md")
    production = _read("docs/production.md")
    load_testing = _read("docs/load-testing.md")

    for phrase in (
        "bundle validator interprets evidence",
        "does not run real provider calls",
        "READY_FOR_STAGING is not the same as full production proof",
        "real-provider proof requires manual approved Stage 26F-H run",
    ):
        assert phrase in validation_doc

    for content in (bundle_doc, production, load_testing):
        assert "docs/production-evidence-validation.md" in content
        assert "READY_FOR_STAGING is not the same as full production proof" in content
