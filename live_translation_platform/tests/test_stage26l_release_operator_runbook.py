from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_release_operator_runbook_exists_and_covers_release_flow():
    runbook_path = ROOT / "docs/release-operator-runbook.md"
    assert runbook_path.exists()
    runbook = runbook_path.read_text(encoding="utf-8")

    required_phrases = [
        "What CI proves",
        "What CI does not prove",
        "Required staging environment variables",
        "Redis Pub/Sub",
        "PostgreSQL",
        "Reverse proxy / WSS",
        "Mock 1000-user load-test flow",
        "Real-provider E2E manual flow",
        "Quota/budget guard flow",
        "Evidence bundle generation",
        "Evidence validation",
        "CI readiness gate interpretation",
        "Go / no-go checklist",
        "Rollback checklist",
        "Common failure modes",
    ]
    for phrase in required_phrases:
        assert phrase in runbook


def test_release_operator_runbook_mentions_key_safety_limits_and_infra():
    runbook = _read("docs/release-operator-runbook.md")

    required_phrases = [
        "CI is safe by default",
        "CI does not prove real-provider production success",
        "real-provider proof is manual",
        "Redis Pub/Sub is required before multi-worker WebSocket delivery",
        "PostgreSQL is required for production",
        "WSS",
        "reverse proxy",
        "evidence bundle",
        "validator",
        "go/no-go",
        "rollback",
    ]
    for phrase in required_phrases:
        assert phrase in runbook


def test_release_operator_runbook_includes_concise_command_sequence():
    runbook = _read("docs/release-operator-runbook.md")

    commands = [
        "pytest -q",
        "python -m compileall -q app tests scripts",
        "python scripts/ci_production_readiness_gate.py",
        "docker compose -f docker-compose.prod.yml up",
        "python scripts/check_deployment_readiness.py",
        "python scripts/check_wss_routes.py",
        "python scripts/run_1000_user_readiness_test.py",
        "python scripts/real_provider_e2e_check.py",
        "--require-quota-confirmation",
        "python scripts/generate_production_evidence_bundle.py",
        "python scripts/validate_production_evidence_bundle.py",
    ]
    for command in commands:
        assert command in runbook


def test_existing_docs_link_to_release_operator_runbook():
    for path in (
        "README.md",
        "docs/production.md",
        "docs/load-testing.md",
        "docs/ci-production-readiness-gate.md",
        "docs/production-evidence-validation.md",
    ):
        content = _read(path)
        assert "docs/release-operator-runbook.md" in content
