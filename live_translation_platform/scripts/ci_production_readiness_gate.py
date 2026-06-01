#!/usr/bin/env python
"""CI-safe production readiness gate.

This script orchestrates only safe checks: dry-run real-provider reporting,
production evidence bundle generation, and local bundle validation.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import generate_production_evidence_bundle as bundle_generator
from scripts import real_provider_e2e_check
from scripts import validate_production_evidence_bundle as bundle_validator


PASS = "PASS"
PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
FAIL = "FAIL"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a CI-safe production readiness gate. The gate uses dry-run/preflight "
            "evidence only and never calls real providers, creates Zoom meetings, or "
            "mutates production state."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Application base URL for optional safe checks.")
    parser.add_argument("--output-dir", default="tmp/ci_production_readiness_gate", help="Directory for generated CI gate artifacts.")
    parser.add_argument("--allow-partial-evidence", action="store_true", help="Allow PARTIAL_EVIDENCE to pass with warnings.")
    parser.add_argument("--require-readiness-ready", action="store_true", help="Require readiness evidence to be ready.")
    parser.add_argument("--require-config-production-safe", action="store_true", help="Require config-check evidence to be production-safe.")
    parser.add_argument("--require-runtime-metrics", action="store_true", help="Require runtime metrics evidence to be present.")
    parser.add_argument("--result-json", help="Path for final CI gate JSON result.")
    parser.add_argument("--no-network", action="store_true", help="Do not make HTTP requests; package and validate local-only evidence.")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout for safe preflight checks when network is enabled.")
    parser.add_argument("--include-6-lessons-1000-ws-report", help="Path to a pre-generated Stage 27B raw WebSocket load report.")
    parser.add_argument("--include-6-lessons-1000-ws-analysis", help="Path to a pre-generated Stage 27C WebSocket load analysis report.")
    parser.add_argument(
        "--require-6-lessons-1000-ws-pass",
        action="store_true",
        help="Require provided Stage 27B/27C WebSocket load evidence to pass. The CI gate does not run the load test.",
    )
    parser.add_argument(
        "--require-realistic-ws-pass",
        action="store_true",
        help="Require provided Stage 27B/27C WebSocket evidence to pass at realistic expected scale: 6 lessons and at least 480 clients.",
    )
    parser.add_argument("--require-ws-lessons", type=int, help="Require provided WebSocket evidence for this exact lesson count.")
    parser.add_argument("--require-ws-students", type=int, help="Require provided WebSocket evidence for this exact requested-student count.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_gate(args)
    if args.result_json:
        write_json(Path(args.result_json), result)
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if result["gate_result"] in {PASS, PASS_WITH_WARNINGS} else 1


def run_gate(args: argparse.Namespace) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    real_provider_report_path = output_dir / "real_provider_e2e_dry_run_report.json"
    validation_result_path = output_dir / "production_evidence_validation_result.json"
    bundle_parent = output_dir / "production_evidence"

    real_provider_report = generate_dry_run_report(args)
    real_provider_e2e_check.write_report_json(real_provider_report, real_provider_report_path)

    bundle_summary = bundle_generator.generate_bundle(
        SimpleNamespace(
            base_url=args.base_url,
            output_dir=str(bundle_parent),
            include_runtime_metrics=bool(args.require_runtime_metrics),
            include_config_check=bool(args.require_config_production_safe),
            include_real_provider_report=str(real_provider_report_path),
            include_load_test_report=None,
            include_6_lessons_1000_ws_report=args.include_6_lessons_1000_ws_report,
            include_6_lessons_1000_ws_analysis=args.include_6_lessons_1000_ws_analysis,
            include_readiness_check=bool(args.require_readiness_ready),
            no_network=bool(args.no_network),
            timeout=args.timeout,
        )
    )
    bundle_dir = Path(str(bundle_summary["bundle_dir"]))

    validation_result = bundle_validator.validate_bundle(
        SimpleNamespace(
            bundle_dir=str(bundle_dir),
            require_real_provider_proof=False,
            require_load_test_report=False,
            require_readiness_ready=bool(args.require_readiness_ready),
            require_config_production_safe=bool(args.require_config_production_safe),
            require_runtime_metrics=bool(args.require_runtime_metrics),
            require_6_lessons_1000_ws_pass=bool(args.require_6_lessons_1000_ws_pass),
            require_realistic_ws_pass=bool(args.require_realistic_ws_pass),
            require_ws_lessons=args.require_ws_lessons,
            require_ws_students=args.require_ws_students,
            output_json=str(validation_result_path),
        )
    )
    write_json(validation_result_path, validation_result)

    gate_result, gate_failures, gate_warnings = decide_gate_result(validation_result, allow_partial=args.allow_partial_evidence)
    failed_checks = [*gate_failures, *list(validation_result.get("failed_checks") or [])]
    warnings = [*gate_warnings, *list(validation_result.get("warnings") or [])]

    return {
        "gate_result": gate_result,
        "validator_verdict": validation_result.get("verdict"),
        "bundle_dir": str(bundle_dir),
        "real_provider_report_path": str(real_provider_report_path),
        "validation_result_path": str(validation_result_path),
        "failed_checks": failed_checks,
        "warnings": warnings,
        "ws_6_lessons_1000_status": validation_result.get("ws_6_lessons_1000_status"),
        "ws_6_lessons_1000_verdict": validation_result.get("ws_6_lessons_1000_verdict"),
        "ws_6_lessons_1000_mock_only": validation_result.get("ws_6_lessons_1000_mock_only"),
        "ws_6_lessons_1000_real_provider_proof": validation_result.get("ws_6_lessons_1000_real_provider_proof"),
        "ws_6_lessons_1000_warnings": validation_result.get("ws_6_lessons_1000_warnings"),
        "ws_required_lessons": validation_result.get("ws_required_lessons"),
        "ws_required_students": validation_result.get("ws_required_students"),
        "ws_report_lessons": validation_result.get("ws_report_lessons"),
        "ws_report_students": validation_result.get("ws_report_students"),
        "ws_connected_clients_total": validation_result.get("ws_connected_clients_total"),
        "ws_peak_connected_clients": validation_result.get("ws_peak_connected_clients"),
        "ws_scenario_match": validation_result.get("ws_scenario_match"),
        "ws_scenario_status": validation_result.get("ws_scenario_status"),
        "realistic_ws_status": validation_result.get("realistic_ws_status"),
        "realistic_ws_required_lessons": validation_result.get("realistic_ws_required_lessons"),
        "realistic_ws_required_students": validation_result.get("realistic_ws_required_students"),
        "realistic_ws_report_lessons": validation_result.get("realistic_ws_report_lessons"),
        "realistic_ws_report_students": validation_result.get("realistic_ws_report_students"),
        "realistic_ws_connected_clients_total": validation_result.get("realistic_ws_connected_clients_total"),
        "realistic_ws_verdict": validation_result.get("realistic_ws_verdict"),
        "realistic_ws_mock_only": validation_result.get("realistic_ws_mock_only"),
        "realistic_ws_real_provider_proof": validation_result.get("realistic_ws_real_provider_proof"),
        "safety_notes": [
            "CI gate is safe by default.",
            "No real provider calls are made.",
            "No Zoom meetings are created.",
            "No production state is mutated.",
            "CI gate packages pre-generated Stage 27B/27C artifacts only; it does not run the 1000-socket load test.",
            "Realistic WebSocket proof is expected-scale mock evidence only, not 1000-client or real-provider proof.",
            *list(validation_result.get("safety_notes") or []),
        ],
        "generated_at_utc": generated_at.isoformat().replace("+00:00", "Z"),
    }


def generate_dry_run_report(args: argparse.Namespace) -> dict[str, Any]:
    if args.no_network:
        timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return real_provider_e2e_check.sanitize_for_report(
            {
                "run_mode": "dry_run",
                "timestamp_utc": timestamp,
                "base_url": str(args.base_url).rstrip("/"),
                "checked_endpoints": [],
                "tts_check_requested": False,
                "tts_real_call_allowed": False,
                "zoom_call_allowed": False,
                "status_summary": {
                    "health_ready": False,
                    "providers_status_checked": False,
                    "tts_status_checked": False,
                    "lesson_created": False,
                    "tts_check_ran": False,
                    "tts_check_success": False,
                    "warnings_count": 1,
                },
                "latency_ms": {},
                "sanitized_errors": [],
                "health_ready": False,
                "providers_status": {},
                "tts_status": {},
                "lesson_created": False,
                "lesson_id": None,
                "tts_check": {"ran": False, "success": False, "latency_ms": None, "provider": None, "language": None, "error": None},
                "warnings": ["No-network CI gate generated local-only dry-run report."],
                "next_steps": ["Run staging preflight or manual real-provider E2E outside normal CI when needed."],
                "quota_guard_enabled": False,
                "quota_confirmed": False,
                "max_tts_calls": None,
                "max_zoom_meetings": None,
                "max_total_provider_calls": None,
                "planned_provider_calls": {"tts_calls": 0, "zoom_meetings": 0, "total_provider_calls": 0},
                "quota_guard_result": "not_required",
                "latency_threshold_ms": None,
                "latency_threshold_violations": [],
                "final_result": "not_run",
            }
        )
    return real_provider_e2e_check.run_check(
        real_provider_e2e_check.RunOptions(
            base_url=args.base_url,
            dry_run=True,
            allow_real_provider_calls=False,
            allow_zoom_call=False,
            create_lesson=False,
            run_tts_check=False,
            timeout=args.timeout,
        )
    )


def decide_gate_result(validation_result: dict[str, Any], *, allow_partial: bool) -> tuple[str, list[str], list[str]]:
    verdict = validation_result.get("verdict")
    failed_checks: list[str] = []
    warnings: list[str] = []
    if validation_result.get("secret_scan_findings"):
        failed_checks.append("secret scan findings are present")
        return FAIL, failed_checks, warnings
    if verdict == bundle_validator.READY_FOR_STAGING:
        return PASS, failed_checks, warnings
    if verdict == bundle_validator.PARTIAL_EVIDENCE:
        if allow_partial:
            warnings.append("partial evidence accepted because --allow-partial-evidence was set")
            return PASS_WITH_WARNINGS, failed_checks, warnings
        failed_checks.append("partial evidence is not allowed without --allow-partial-evidence")
        return FAIL, failed_checks, warnings
    if verdict == bundle_validator.REAL_PROVIDER_NOT_PROVEN:
        failed_checks.append("real-provider proof was required but not proven")
        return FAIL, failed_checks, warnings
    failed_checks.append(f"validator verdict is {verdict}")
    return FAIL, failed_checks, warnings


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
