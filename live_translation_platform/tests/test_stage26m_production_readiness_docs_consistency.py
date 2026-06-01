import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

DOCS = [
    "README.md",
    "docs/production.md",
    "docs/load-testing.md",
    "docs/real-provider-e2e.md",
    "docs/real-provider-e2e-report.md",
    "docs/production-evidence-bundle.md",
    "docs/production-evidence-validation.md",
    "docs/ci-production-readiness-gate.md",
    "docs/release-operator-runbook.md",
]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _combined_docs() -> str:
    return "\n".join(_read(path) for path in DOCS)


def test_readme_links_to_release_operator_runbook():
    assert "docs/release-operator-runbook.md" in _read("README.md")


def test_production_docs_mention_ci_gate_and_evidence_validation():
    production = _read("docs/production.md")
    assert "docs/ci-production-readiness-gate.md" in production
    assert "docs/production-evidence-validation.md" in production
    assert "CI gate is safe by default" in production


def test_load_testing_docs_say_mock_1000_is_not_real_provider_proof():
    load_testing = _read("docs/load-testing.md")
    assert "mock 1000 is not real-provider proof" in load_testing
    assert "does not prove real-provider production success" in load_testing


def test_real_provider_report_docs_mention_quota_guard():
    report_doc = _read("docs/real-provider-e2e-report.md")
    assert "quota guard" in report_doc
    assert "not a provider billing check" in report_doc


def test_release_runbook_mentions_go_no_go_and_rollback():
    runbook = _read("docs/release-operator-runbook.md")
    assert "Go / no-go checklist" in runbook
    assert "Rollback checklist" in runbook
    assert "go/no-go" in runbook
    assert "rollback" in runbook


def test_docs_use_consistent_stage26_paths():
    docs = _combined_docs()
    for expected in (
        "tmp/real_provider_e2e_report.json",
        "tmp/production_evidence_test",
        "tmp/ci_readiness_gate_result.json",
    ):
        assert expected in docs
    assert "reports/real_provider_e2e_report.json" not in docs
    assert "tmp/production_evidence " not in docs


def test_docs_do_not_contain_obvious_secret_like_examples():
    docs = _combined_docs()
    forbidden_substrings = [
        "sk-live",
        "sk_test",
        "Bearer real-token",
        "Bearer very-secret",
        "real.secret",
        "provider-secret",
        "signed-audio-secret",
        "secret-token",
        "session=secret",
    ]
    for forbidden in forbidden_substrings:
        assert forbidden not in docs

    assert not re.search(r"Authorization\s*:\s*Bearer\s+(?!<redacted>|<token>|<scoped-token>)[A-Za-z0-9._~+/=-]{8,}", docs)
    assert not re.search(r"[?&](?:token|access_token|api_key|signature)=(?!<redacted>|<token>|<scoped-token>)[^&#\s\"']{8,}", docs)
