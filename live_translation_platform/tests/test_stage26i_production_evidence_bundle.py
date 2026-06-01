import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/generate_production_evidence_bundle.py"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _latest_bundle(output_dir: Path) -> Path:
    bundles = [path for path in output_dir.iterdir() if path.is_dir()]
    assert bundles
    return max(bundles, key=lambda path: path.name)


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
    assert "--include-real-provider-report" in result.stdout
    assert "--include-load-test-report" in result.stdout


def test_no_network_bundle_generation_creates_manifest_and_readme(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--no-network",
            "--output-dir",
            str(tmp_path),
            "--base-url",
            "https://python-service.example.com",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    bundle_dir = _latest_bundle(tmp_path)
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    environment = json.loads((bundle_dir / "environment_summary.json").read_text(encoding="utf-8"))
    readme = (bundle_dir / "README.md").read_text(encoding="utf-8")

    assert manifest["base_url"] == "https://python-service.example.com"
    assert manifest["sanitized"] is True
    assert manifest["final_bundle_status"] in {"complete", "partial"}
    assert "environment_summary.json" in manifest["included_artifacts"]
    assert (bundle_dir / "README.md").exists()
    assert environment["no_network"] is True
    assert "what this bundle proves" in readme.lower()
    assert "real-provider E2E must stay manual" in readme


def test_fake_real_provider_report_is_copied_and_sanitized(tmp_path):
    fake_report = tmp_path / "fake_real_provider_report.json"
    fake_report.write_text(
        json.dumps(
            {
                "run_mode": "real_provider",
                "final_result": "pass",
                "api_key": "provider-secret",
                "authorization": "Bearer very-secret-token",
                "cookie": "session=secret-cookie",
                "database_url": "postgresql://live_translation:db-secret@example.test/app",
                "audio_url": "https://cdn.example.test/audio/1?token=signed-audio-secret",
                "warnings": [
                    "Authorization: Bearer another-secret",
                    "https://example.test/path?integration_key=browser-secret",
                ],
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--no-network",
            "--output-dir",
            str(tmp_path / "bundles"),
            "--include-real-provider-report",
            str(fake_report),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    bundle_dir = _latest_bundle(tmp_path / "bundles")
    copied = (bundle_dir / "real_provider_e2e_report.json").read_text(encoding="utf-8")
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))

    assert "real_provider_e2e_report.json" in manifest["included_artifacts"]
    for secret in (
        "provider-secret",
        "very-secret-token",
        "secret-cookie",
        "db-secret",
        "signed-audio-secret",
        "another-secret",
        "browser-secret",
    ):
        assert secret not in copied
    assert "<redacted>" in copied
    assert '"final_result": "pass"' in copied


def test_sanitizer_redacts_json_and_freeform_strings():
    sys.path.insert(0, str(ROOT))
    from scripts.generate_production_evidence_bundle import sanitize_for_bundle

    sanitized = sanitize_for_bundle(
        {
            "integration_key": "secret-key",
            "nested": {
                "message": "Authorization: Bearer bearer-secret; audio=https://x.test/a?token=audio-secret",
                "redis_url": "redis://:redis-secret@redis:6379/0",
            },
        }
    )
    encoded = json.dumps(sanitized)

    assert "secret-key" not in encoded
    assert "bearer-secret" not in encoded
    assert "audio-secret" not in encoded
    assert "redis-secret" not in encoded
    assert "<redacted>" in encoded or "[redacted]" in encoded


def test_docs_mention_evidence_bundle_and_real_provider_limitations():
    bundle_doc = _read("docs/production-evidence-bundle.md")
    production = _read("docs/production.md")
    load_testing = _read("docs/load-testing.md")
    report_doc = _read("docs/real-provider-e2e-report.md")

    for phrase in (
        "production evidence bundle",
        "safe packaging",
        "does not itself prove real-provider success",
        "normal CI can generate dry-run bundles",
        "manual approval",
    ):
        assert phrase in bundle_doc

    for content in (production, load_testing, report_doc):
        assert "docs/production-evidence-bundle.md" in content
        assert "does not itself prove real-provider success" in content
