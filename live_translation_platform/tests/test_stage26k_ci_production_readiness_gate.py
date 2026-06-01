import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ci_production_readiness_gate.py"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_help_works():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--no-network" in result.stdout
    assert "--allow-partial-evidence" in result.stdout
    assert "--result-json" in result.stdout


def test_no_network_gate_writes_result_without_provider_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("INTEGRATION_KEY", raising=False)
    result_path = tmp_path / "ci_result.json"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--no-network",
            "--allow-partial-evidence",
            "--output-dir",
            str(tmp_path / "gate"),
            "--result-json",
            str(result_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["gate_result"] == "PASS_WITH_WARNINGS"
    assert payload["validator_verdict"] == "PARTIAL_EVIDENCE"
    assert Path(payload["real_provider_report_path"]).exists()
    assert Path(payload["validation_result_path"]).exists()
    assert "INTEGRATION_KEY" not in result.stdout


def test_partial_evidence_can_pass_with_warnings(tmp_path):
    result_path = tmp_path / "ci_result.json"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--no-network",
            "--allow-partial-evidence",
            "--output-dir",
            str(tmp_path / "gate"),
            "--result-json",
            str(result_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert result.returncode == 0
    assert "PASS_WITH_WARNINGS" in result.stdout
    assert payload["gate_result"] == "PASS_WITH_WARNINGS"
    assert payload["validator_verdict"] == "PARTIAL_EVIDENCE"


def test_partial_evidence_fails_without_allow_flag(tmp_path):
    result_path = tmp_path / "ci_result.json"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--no-network",
            "--output-dir",
            str(tmp_path / "gate"),
            "--result-json",
            str(result_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert payload["gate_result"] == "FAIL"
    assert payload["validator_verdict"] == "PARTIAL_EVIDENCE"
    assert "partial evidence is not allowed" in " ".join(payload["failed_checks"])


def test_result_json_includes_gate_and_validator_fields(tmp_path):
    result_path = tmp_path / "ci_result.json"

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--no-network",
            "--allow-partial-evidence",
            "--output-dir",
            str(tmp_path / "gate"),
            "--result-json",
            str(result_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    for field in (
        "gate_result",
        "validator_verdict",
        "bundle_dir",
        "real_provider_report_path",
        "validation_result_path",
        "failed_checks",
        "warnings",
        "safety_notes",
        "generated_at_utc",
    ):
        assert field in payload


def test_docs_mention_ci_gate_safe_default_and_real_provider_limit():
    gate_doc = _read("docs/ci-production-readiness-gate.md")
    production = _read("docs/production.md")
    load_testing = _read("docs/load-testing.md")
    bundle_doc = _read("docs/production-evidence-bundle.md")
    validation_doc = _read("docs/production-evidence-validation.md")

    for phrase in (
        "CI gate is safe by default",
        "does not prove real-provider production success",
        "real-provider proof remains manual",
        "dry-run/preflight only",
        "release managers can require stricter flags in staging",
    ):
        assert phrase in gate_doc

    for content in (production, load_testing, bundle_doc, validation_doc):
        assert "docs/ci-production-readiness-gate.md" in content
        assert "CI gate is safe by default" in content
