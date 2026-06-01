#!/usr/bin/env python
"""Validate a sanitized production evidence bundle without network calls."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


READY_FOR_STAGING = "READY_FOR_STAGING"
PARTIAL_EVIDENCE = "PARTIAL_EVIDENCE"
REAL_PROVIDER_NOT_PROVEN = "REAL_PROVIDER_NOT_PROVEN"
NOT_READY = "NOT_READY"

SECRET_KEY_RE = re.compile(
    r"(secret|token|api[_-]?key|apikey|password|authorization|credential|signature|integration[_-]?key|cookie|database[_-]?url|redis[_-]?url)",
    re.IGNORECASE,
)
SAFE_PLACEHOLDERS = {"", "[redacted]", "<redacted>", "redacted", "***", None}
SECRET_TEXT_PATTERNS = (
    re.compile(r"Bearer\s+(?!<redacted>)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"Authorization\s*:\s*(?!<redacted>)(?:Bearer\s+)?[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"(?:Cookie|Set-Cookie)\s*:\s*(?!<redacted>)[^\r\n;=]+=[^\r\n;]{4,}", re.IGNORECASE),
    re.compile(
        r"[?&](?:token|access_token|refresh_token|api_key|apikey|key|password|pwd|signature|integration_key|integration-key|sig)="
        r"(?!<redacted>)[^&#\s\"']{4,}",
        re.IGNORECASE,
    ),
    re.compile(r"[a-z][a-z0-9+.-]*://[^:/@\s]+:(?!<redacted>)[^/@\s]+@", re.IGNORECASE),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a production evidence bundle from local files only. The validator "
            "does not make network calls, provider calls, or mutate production state."
        )
    )
    parser.add_argument("--bundle-dir", required=True, help="Bundle directory, or a parent directory containing timestamped bundles.")
    parser.add_argument("--require-real-provider-proof", action="store_true", help="Require a passing real_provider E2E report.")
    parser.add_argument("--require-load-test-report", action="store_true", help="Require load_test_report.json to be present and passing.")
    parser.add_argument("--require-readiness-ready", action="store_true", help="Require readiness_summary.json with status=ready.")
    parser.add_argument("--require-config-production-safe", action="store_true", help="Require config_check_summary.json to be production-safe.")
    parser.add_argument("--require-runtime-metrics", action="store_true", help="Require runtime_metrics_summary.json to be present and readable.")
    parser.add_argument(
        "--require-6-lessons-1000-ws-pass",
        action="store_true",
        help="Require Stage 27B raw WebSocket load report plus Stage 27C analysis with PASS or PASS_WITH_WARNINGS.",
    )
    parser.add_argument(
        "--require-realistic-ws-pass",
        action="store_true",
        help="Require realistic expected-scale Stage 27B/27C mock WebSocket evidence: 6 lessons and at least 480 clients.",
    )
    parser.add_argument(
        "--allow-ws-backend-only-inconclusive",
        action="store_true",
        help="Surface backend-fanout PASS/client-receive inconclusive WS evidence as useful but not READY_FOR_STAGING.",
    )
    parser.add_argument("--require-ws-lessons", type=int, help="Require Stage 27B/27C WebSocket evidence for this exact lesson count.")
    parser.add_argument("--require-ws-students", type=int, help="Require Stage 27B/27C WebSocket evidence for this exact requested-student count.")
    parser.add_argument("--output-json", help="Optional path for a sanitized JSON validation result.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = validate_bundle(args)
    if args.output_json:
        target = Path(args.output_json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print_human_summary(result)
    return 0 if result["verdict"] in {READY_FOR_STAGING, PARTIAL_EVIDENCE} else 1


def validate_bundle(args: argparse.Namespace) -> dict[str, Any]:
    requested_dir = Path(args.bundle_dir)
    bundle_dir = resolve_bundle_dir(requested_dir)
    checked_files: list[str] = []
    missing_files: list[str] = []
    failed_checks: list[str] = []
    warnings: list[str] = []
    require_strict_ws_evidence = bool(getattr(args, "require_6_lessons_1000_ws_pass", False)) or getattr(args, "require_ws_lessons", None) is not None or getattr(args, "require_ws_students", None) is not None
    require_realistic_ws_evidence = bool(getattr(args, "require_realistic_ws_pass", False))
    require_ws_evidence = require_strict_ws_evidence or require_realistic_ws_evidence
    ws_required_lessons = getattr(args, "require_ws_lessons", None)
    ws_required_students = getattr(args, "require_ws_students", None)
    if getattr(args, "require_6_lessons_1000_ws_pass", False):
        ws_required_lessons = 6 if ws_required_lessons is None else ws_required_lessons
        ws_required_students = 1000 if ws_required_students is None else ws_required_students

    base_result = {
        "bundle_dir": str(bundle_dir or requested_dir),
        "checked_files": checked_files,
        "missing_files": missing_files,
        "failed_checks": failed_checks,
        "warnings": warnings,
        "secret_scan_findings": [],
        "real_provider_proof_status": "not_required",
        "load_test_status": "not_required",
        "stage27b_ws_load_status": "not_required",
        "ws_6_lessons_1000_status": "not_required",
        "ws_6_lessons_1000_verdict": None,
        "ws_6_lessons_1000_mock_only": None,
        "ws_6_lessons_1000_real_provider_proof": None,
        "ws_6_lessons_1000_warnings": [],
        "ws_required_lessons": ws_required_lessons,
        "ws_required_students": ws_required_students,
        "ws_report_lessons": None,
        "ws_report_students": None,
        "ws_connected_clients_total": None,
        "ws_peak_connected_clients": None,
        "ws_scenario_match": None,
        "ws_scenario_status": "not_required",
        "realistic_ws_status": "not_required",
        "realistic_ws_required_lessons": 6 if require_realistic_ws_evidence else None,
        "realistic_ws_required_students": 480 if require_realistic_ws_evidence else None,
        "realistic_ws_report_lessons": None,
        "realistic_ws_report_students": None,
        "realistic_ws_connected_clients_total": None,
        "realistic_ws_verdict": None,
        "realistic_ws_mock_only": None,
        "realistic_ws_real_provider_proof": None,
        "ws_backend_only_status": "not_required",
        "readiness_status": "not_required",
        "config_status": "not_required",
        "runtime_metrics_status": "not_required",
        "safety_notes": [],
    }

    if bundle_dir is None:
        missing_files.append("manifest.json")
        failed_checks.append("manifest.json missing")
        return {**base_result, "verdict": NOT_READY}

    manifest = read_json_file(bundle_dir / "manifest.json", checked_files, missing_files, failed_checks)
    if not isinstance(manifest, dict):
        failed_checks.append("manifest.json is missing or invalid")
        return {**base_result, "verdict": NOT_READY}

    safety_notes = manifest.get("safety_notes") if isinstance(manifest.get("safety_notes"), list) else []
    base_result["safety_notes"] = [str(note) for note in safety_notes]

    if manifest.get("sanitized") is not True:
        failed_checks.append("manifest sanitized=true is required")
    bundle_status = manifest.get("final_bundle_status")
    if bundle_status == "failed":
        failed_checks.append("manifest final_bundle_status=failed")
    elif bundle_status not in {"complete", "partial"}:
        warnings.append(f"manifest final_bundle_status is unexpected: {bundle_status}")

    environment = read_json_file(bundle_dir / "environment_summary.json", checked_files, missing_files, failed_checks, required=False)
    if environment is None:
        warnings.append("environment_summary.json is missing")

    readiness = read_json_file(bundle_dir / "readiness_summary.json", checked_files, missing_files, failed_checks, required=args.require_readiness_ready)
    base_result["readiness_status"] = evaluate_readiness(readiness, args.require_readiness_ready, failed_checks, warnings)

    runtime_metrics = read_json_file(
        bundle_dir / "runtime_metrics_summary.json",
        checked_files,
        missing_files,
        failed_checks,
        required=args.require_runtime_metrics,
    )
    base_result["runtime_metrics_status"] = evaluate_runtime_metrics(runtime_metrics, args.require_runtime_metrics, failed_checks, warnings)

    config = read_json_file(
        bundle_dir / "config_check_summary.json",
        checked_files,
        missing_files,
        failed_checks,
        required=args.require_config_production_safe,
    )
    base_result["config_status"] = evaluate_config(config, args.require_config_production_safe, failed_checks, warnings)

    real_provider = read_json_file(
        bundle_dir / "real_provider_e2e_report.json",
        checked_files,
        missing_files,
        failed_checks,
        required=False,
    )
    real_provider_status = evaluate_real_provider(real_provider, args.require_real_provider_proof, failed_checks, warnings)
    base_result["real_provider_proof_status"] = real_provider_status

    load_test = read_json_file(
        bundle_dir / "load_test_report.json",
        checked_files,
        missing_files,
        failed_checks,
        required=args.require_load_test_report,
    )
    stage27b_ws_load = read_json_file(
        bundle_dir / "load_test_6_lessons_1000_ws_report.json",
        checked_files,
        missing_files,
        failed_checks,
        required=require_ws_evidence,
    )
    stage27c_ws_analysis = read_json_file(
        bundle_dir / "load_test_6_lessons_1000_ws_analysis.json",
        checked_files,
        missing_files,
        failed_checks,
        required=require_ws_evidence,
    )
    load_test_status = evaluate_load_test(load_test, args.require_load_test_report, failed_checks, warnings)
    ws_6_lessons_1000 = evaluate_ws_6_lessons_1000(
        raw_report=stage27b_ws_load,
        analysis=stage27c_ws_analysis,
        required=require_strict_ws_evidence,
        required_lessons=ws_required_lessons,
        required_students=ws_required_students,
        allow_backend_only_inconclusive=bool(getattr(args, "allow_ws_backend_only_inconclusive", False)),
        failed_checks=failed_checks,
        warnings=warnings,
    )
    realistic_ws = evaluate_realistic_ws_proof(
        raw_report=stage27b_ws_load,
        analysis=stage27c_ws_analysis,
        required=require_realistic_ws_evidence,
        allow_backend_only_inconclusive=bool(getattr(args, "allow_ws_backend_only_inconclusive", False)),
        failed_checks=failed_checks,
        warnings=warnings,
    )
    base_result["load_test_status"] = load_test_status
    base_result["stage27b_ws_load_status"] = ws_6_lessons_1000["ws_6_lessons_1000_status"]
    base_result.update(ws_6_lessons_1000)
    base_result.update(realistic_ws)

    secret_findings = scan_for_secrets(bundle_dir)
    base_result["secret_scan_findings"] = secret_findings
    if secret_findings:
        failed_checks.append("critical secret-like values found in bundle artifacts")

    verdict = choose_verdict(
        failed_checks=failed_checks,
        real_provider_status=real_provider_status,
        args=args,
        manifest=manifest,
        optional_artifacts_present={
            "readiness": readiness is not None,
            "runtime_metrics": runtime_metrics is not None,
            "config": config is not None,
            "real_provider": real_provider is not None,
            "load_test": load_test is not None,
            "stage27b_ws_load": stage27b_ws_load is not None,
            "stage27c_ws_analysis": stage27c_ws_analysis is not None,
        },
    )
    return {**base_result, "verdict": verdict}


def resolve_bundle_dir(path: Path) -> Path | None:
    if (path / "manifest.json").exists():
        return path
    if not path.exists() or not path.is_dir():
        return None
    candidates = [child for child in path.iterdir() if child.is_dir() and (child / "manifest.json").exists()]
    if not candidates:
        return None
    return max(candidates, key=lambda child: (child.name, child.stat().st_mtime))


def read_json_file(
    path: Path,
    checked_files: list[str],
    missing_files: list[str],
    failed_checks: list[str],
    *,
    required: bool = True,
) -> Any:
    if not path.exists():
        missing_files.append(path.name)
        if required:
            failed_checks.append(f"{path.name} missing")
        return None
    checked_files.append(path.name)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        failed_checks.append(f"{path.name} is not valid JSON")
        return None


def evaluate_readiness(payload: Any, required: bool, failed_checks: list[str], warnings: list[str]) -> str:
    if payload is None:
        return "missing" if required else "not_present"
    data = unwrap_payload(payload)
    ready = isinstance(data, dict) and data.get("status") == "ready"
    if ready:
        return "pass"
    message = f"readiness status is not ready: {data.get('status') if isinstance(data, dict) else 'unknown'}"
    if required:
        failed_checks.append(message)
        return "fail"
    warnings.append(message)
    return "warning"


def evaluate_runtime_metrics(payload: Any, required: bool, failed_checks: list[str], warnings: list[str]) -> str:
    if payload is None:
        return "missing" if required else "not_present"
    data = unwrap_payload(payload)
    if isinstance(data, dict):
        return "present"
    message = "runtime_metrics_summary.json does not contain a JSON object"
    if required:
        failed_checks.append(message)
        return "fail"
    warnings.append(message)
    return "warning"


def evaluate_config(payload: Any, required: bool, failed_checks: list[str], warnings: list[str]) -> str:
    if payload is None:
        return "missing" if required else "not_present"
    data = unwrap_payload(payload)
    safe = config_is_production_safe(data)
    if safe:
        return "pass"
    message = "config check is not production-safe"
    if required:
        failed_checks.append(message)
        return "fail"
    warnings.append(message)
    return "warning"


def evaluate_real_provider(payload: Any, required: bool, failed_checks: list[str], warnings: list[str]) -> str:
    if payload is None:
        if required:
            return "missing"
        return "not_present"
    if not isinstance(payload, dict):
        if required:
            return "invalid"
        warnings.append("real_provider_e2e_report.json is invalid")
        return "warning"
    run_mode = payload.get("run_mode")
    final_result = payload.get("final_result")
    if run_mode == "real_provider" and final_result == "pass":
        return "pass"
    message = f"real-provider proof is not proven: run_mode={run_mode}, final_result={final_result}"
    if required:
        return message
    warnings.append(message)
    return "warning"


def evaluate_load_test(payload: Any, required: bool, failed_checks: list[str], warnings: list[str]) -> str:
    if payload is None:
        return "missing" if required else "not_present"
    if not isinstance(payload, dict):
        message = "load_test_report.json is invalid"
        if required:
            failed_checks.append(message)
            return "fail"
        warnings.append(message)
        return "warning"
    result = str(payload.get("overall_result") or payload.get("final_result") or payload.get("status") or payload.get("result") or "").lower()
    if result in {"pass", "passed", "ok", "success"}:
        return "pass"
    if not result:
        return "present"
    message = f"load-test report result is not passing: {result}"
    if required:
        failed_checks.append(message)
        return "fail"
    warnings.append(message)
    return "warning"


def evaluate_stage27b_ws_load(payload: Any, failed_checks: list[str], warnings: list[str]) -> str:
    if payload is None:
        return "not_present"
    if not isinstance(payload, dict):
        warnings.append("6_lessons_1000_ws_load_test_report.json is invalid")
        return "warning"
    if payload.get("mock_only") is not True or payload.get("real_provider_proof") is not False:
        warnings.append("Stage 27B WebSocket load report must be mock_only=true and real_provider_proof=false")
        return "warning"
    result = str(payload.get("overall_result") or "").lower()
    if result == "pass":
        return "pass"
    if result == "partial":
        warnings.append("Stage 27B WebSocket load report is partial")
        return "partial"
    if result == "fail":
        warnings.append("Stage 27B WebSocket load report failed")
        return "warning"
    warnings.append(f"Stage 27B WebSocket load report has unexpected result: {result or 'missing'}")
    return "warning"


def evaluate_ws_6_lessons_1000(
    *,
    raw_report: Any,
    analysis: Any,
    required: bool,
    required_lessons: int | None,
    required_students: int | None,
    allow_backend_only_inconclusive: bool,
    failed_checks: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    report_lessons = _int_or_none(raw_report.get("lessons")) if isinstance(raw_report, dict) else None
    report_students = _int_or_none(raw_report.get("students")) if isinstance(raw_report, dict) else None
    connected_clients_total = ws_connected_clients_total(raw_report) if isinstance(raw_report, dict) else None
    peak_connected_clients = ws_peak_connected_clients(raw_report) if isinstance(raw_report, dict) else None
    result = {
        "ws_6_lessons_1000_status": "not_present",
        "ws_6_lessons_1000_verdict": None,
        "ws_6_lessons_1000_mock_only": raw_report.get("mock_only") if isinstance(raw_report, dict) else None,
        "ws_6_lessons_1000_real_provider_proof": raw_report.get("real_provider_proof") if isinstance(raw_report, dict) else None,
        "ws_6_lessons_1000_warnings": [],
        "ws_required_lessons": required_lessons,
        "ws_required_students": required_students,
        "ws_report_lessons": report_lessons,
        "ws_report_students": report_students,
        "ws_connected_clients_total": connected_clients_total,
        "ws_peak_connected_clients": peak_connected_clients,
        "ws_scenario_match": None,
        "ws_scenario_status": "not_present",
        "ws_backend_only_status": "not_required",
    }
    if raw_report is None and analysis is None:
        if not required:
            result["ws_scenario_status"] = "not_required"
        return result
    if not required and required_lessons is None and required_students is None:
        result["ws_6_lessons_1000_status"] = "not_required"
        result["ws_scenario_status"] = "not_required"
        return result

    local_failures: list[str] = []
    local_warnings: list[str] = [
        "Stage 27B/27C proves mock WebSocket fanout only, not real-provider proof.",
    ]
    if not isinstance(raw_report, dict):
        local_failures.append("load_test_6_lessons_1000_ws_report.json is invalid")
    else:
        if raw_report.get("mock_only") is not True:
            local_failures.append("Stage 27B raw report must have mock_only=true")
        if raw_report.get("real_provider_proof") is not False:
            local_failures.append("Stage 27B raw report must have real_provider_proof=false")
        final_result = str(raw_report.get("final_result") or raw_report.get("overall_result") or "").lower()
        if final_result != "pass":
            local_failures.append(f"Stage 27B raw report final result is not pass: {final_result or 'missing'}")
        if required_lessons is None or required_students is None:
            if required:
                local_failures.append("WebSocket proof requires both --require-ws-lessons and --require-ws-students")
        else:
            scenario_match = report_lessons == required_lessons and report_students == required_students
            result["ws_scenario_match"] = scenario_match
            if not scenario_match:
                local_failures.append(
                    f"{required_lessons} lessons / {required_students} WebSocket proof requires lessons={required_lessons} and students={required_students}"
                )
            if connected_clients_total is None or connected_clients_total < required_students:
                local_failures.append(
                    f"{required_lessons} lessons / {required_students} WebSocket proof requires connected_clients_total >= {required_students}"
                )
            if peak_connected_clients is not None and peak_connected_clients < required_students:
                local_failures.append(
                    f"{required_lessons} lessons / {required_students} WebSocket proof requires peak_connected_clients >= {required_students}"
                )
    if not isinstance(analysis, dict):
        local_failures.append("load_test_6_lessons_1000_ws_analysis.json is invalid")
    else:
        verdict = str(analysis.get("verdict") or "").upper()
        result["ws_6_lessons_1000_verdict"] = verdict or None
        result["ws_6_lessons_1000_warnings"] = [str(item) for item in analysis.get("warnings") or []]
        local_warnings.extend(result["ws_6_lessons_1000_warnings"])
        if verdict not in {"PASS", "PASS_WITH_WARNINGS"}:
            if allow_backend_only_inconclusive and _backend_only_inconclusive(analysis):
                result["ws_backend_only_status"] = "backend_fanout_pass_client_inconclusive"
                local_failures.append("backend_fanout_pass_client_inconclusive is useful evidence but not full client receive proof")
            else:
                local_failures.append(f"Stage 27C analysis verdict is not passing: {verdict or 'missing'}")
        failed_thresholds = ws_failed_threshold_names(analysis)
        if "receive_rate" in failed_thresholds:
            local_failures.append("Stage 27C analysis receive rate threshold did not pass")
        if "p95_latency_ms" in failed_thresholds:
            local_failures.append("Stage 27C analysis p95 latency threshold did not pass")

    if local_failures:
        if required:
            failed_checks.extend(local_failures)
        else:
            warnings.extend(local_failures)
        result["ws_6_lessons_1000_status"] = "fail" if required else "warning"
        result["ws_scenario_status"] = "fail" if required else "warning"
        return result

    warnings.extend(local_warnings)
    result["ws_6_lessons_1000_status"] = "pass"
    result["ws_scenario_status"] = "pass"
    result["ws_scenario_match"] = True if result["ws_scenario_match"] is None else result["ws_scenario_match"]
    return result


def evaluate_realistic_ws_proof(
    *,
    raw_report: Any,
    analysis: Any,
    required: bool,
    allow_backend_only_inconclusive: bool,
    failed_checks: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    required_lessons = 6
    required_students = 480
    report_lessons = _int_or_none(raw_report.get("lessons")) if isinstance(raw_report, dict) else None
    report_students = _int_or_none(raw_report.get("students")) if isinstance(raw_report, dict) else None
    connected_clients_total = ws_connected_clients_total(raw_report) if isinstance(raw_report, dict) else None
    result = {
        "realistic_ws_status": "not_present" if required else "not_required",
        "realistic_ws_required_lessons": required_lessons if required else None,
        "realistic_ws_required_students": required_students if required else None,
        "realistic_ws_report_lessons": report_lessons,
        "realistic_ws_report_students": report_students,
        "realistic_ws_connected_clients_total": connected_clients_total,
        "realistic_ws_verdict": None,
        "realistic_ws_mock_only": raw_report.get("mock_only") if isinstance(raw_report, dict) else None,
        "realistic_ws_real_provider_proof": raw_report.get("real_provider_proof") if isinstance(raw_report, dict) else None,
        "ws_backend_only_status": "not_required",
    }
    if not required:
        return result

    local_failures: list[str] = []
    local_warnings: list[str] = [
        "Realistic 6x500 evidence is expected-scale mock WebSocket proof, not 1000-client proof or real-provider proof.",
    ]
    if not isinstance(raw_report, dict):
        local_failures.append("load_test_6_lessons_1000_ws_report.json is invalid")
    else:
        if raw_report.get("mock_only") is not True:
            local_failures.append("Stage 27B raw report must have mock_only=true")
        if raw_report.get("real_provider_proof") is not False:
            local_failures.append("Stage 27B raw report must have real_provider_proof=false")
        final_result = str(raw_report.get("final_result") or raw_report.get("overall_result") or "").lower()
        if final_result != "pass":
            local_failures.append(f"Stage 27B raw report final result is not pass: {final_result or 'missing'}")
        if report_lessons != required_lessons:
            local_failures.append(f"realistic WebSocket proof requires lessons={required_lessons}")
        if report_students is None or report_students < required_students:
            local_failures.append(f"realistic WebSocket proof requires students >= {required_students}")
        if connected_clients_total is None or connected_clients_total < required_students:
            local_failures.append(f"realistic WebSocket proof requires connected_clients_total >= {required_students}")
    if not isinstance(analysis, dict):
        local_failures.append("load_test_6_lessons_1000_ws_analysis.json is invalid")
    else:
        verdict = str(analysis.get("verdict") or "").upper()
        result["realistic_ws_verdict"] = verdict or None
        local_warnings.extend(str(item) for item in analysis.get("warnings") or [])
        if verdict not in {"PASS", "PASS_WITH_WARNINGS"}:
            if allow_backend_only_inconclusive and _backend_only_inconclusive(analysis):
                result["ws_backend_only_status"] = "backend_fanout_pass_client_inconclusive"
                local_failures.append("backend_fanout_pass_client_inconclusive is useful evidence but not full client receive proof")
            else:
                local_failures.append(f"Stage 27C analysis verdict is not passing: {verdict or 'missing'}")
        failed_thresholds = ws_failed_threshold_names(analysis)
        if "receive_rate" in failed_thresholds:
            local_failures.append("Stage 27C analysis receive rate threshold did not pass")
        if "p95_latency_ms" in failed_thresholds:
            local_failures.append("Stage 27C analysis p95 latency threshold did not pass")

    if local_failures:
        failed_checks.extend(local_failures)
        result["realistic_ws_status"] = "fail"
        return result

    warnings.extend(local_warnings)
    result["realistic_ws_status"] = "pass"
    return result


def ws_connected_clients_total(raw_report: dict[str, Any]) -> int | None:
    aggregate = raw_report.get("aggregate_results") if isinstance(raw_report.get("aggregate_results"), dict) else {}
    return first_int(raw_report.get("connected_clients_total"), raw_report.get("students_connected"), aggregate.get("students_connected"))


def ws_peak_connected_clients(raw_report: dict[str, Any]) -> int | None:
    aggregate = raw_report.get("aggregate_results") if isinstance(raw_report.get("aggregate_results"), dict) else {}
    return first_int(raw_report.get("peak_connected_clients"), aggregate.get("peak_connected_clients"))


def ws_failed_threshold_names(analysis: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    failed_thresholds = analysis.get("failed_thresholds")
    if not isinstance(failed_thresholds, list):
        return names
    for item in failed_thresholds:
        if isinstance(item, dict) and item.get("name") is not None:
            names.add(str(item.get("name")))
        elif isinstance(item, str):
            names.add(item)
    return names


def _backend_only_inconclusive(analysis: Any) -> bool:
    if not isinstance(analysis, dict):
        return False
    return (
        str(analysis.get("overall_verdict") or analysis.get("verdict") or "").upper() == "INCONCLUSIVE"
        and str(analysis.get("backend_fanout_verdict") or "").upper() == "PASS"
        and str(analysis.get("client_receive_verdict") or "").upper() == "FAIL"
    )


def choose_verdict(
    *,
    failed_checks: list[str],
    real_provider_status: str,
    args: argparse.Namespace,
    manifest: dict[str, Any],
    optional_artifacts_present: dict[str, bool],
) -> str:
    if failed_checks:
        return NOT_READY
    if args.require_real_provider_proof and real_provider_status != "pass":
        return REAL_PROVIDER_NOT_PROVEN
    required_flags = [
        args.require_readiness_ready,
        args.require_config_production_safe,
        args.require_runtime_metrics,
        args.require_load_test_report,
        args.require_real_provider_proof,
        bool(getattr(args, "require_6_lessons_1000_ws_pass", False)),
        bool(getattr(args, "require_realistic_ws_pass", False)),
        getattr(args, "require_ws_lessons", None) is not None,
        getattr(args, "require_ws_students", None) is not None,
    ]
    if any(required_flags):
        return READY_FOR_STAGING
    if manifest.get("final_bundle_status") == "complete" and all(optional_artifacts_present.values()):
        return READY_FOR_STAGING
    return PARTIAL_EVIDENCE


def unwrap_payload(payload: Any) -> Any:
    if isinstance(payload, dict) and "payload" in payload:
        return payload.get("payload")
    return payload


def first_int(*values: Any) -> int | None:
    for value in values:
        parsed = _int_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def config_is_production_safe(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("production_safe") is True:
        return True
    if payload.get("status") in {"ok", "ready", "production_safe"} and not payload.get("config_missing"):
        return True
    if payload.get("ok") is True and not payload.get("config_missing"):
        return True
    checks = payload.get("checks")
    if isinstance(checks, dict):
        unsafe = [value for value in checks.values() if isinstance(value, dict) and value.get("status") in {"fail", "error", "not_ready"}]
        return not unsafe
    return False


def scan_for_secrets(bundle_dir: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        findings.extend(scan_text_for_secrets(path.relative_to(bundle_dir).as_posix(), text))
        if path.suffix.lower() == ".json":
            try:
                findings.extend(scan_json_for_secret_keys(path.relative_to(bundle_dir).as_posix(), json.loads(text)))
            except json.JSONDecodeError:
                pass
    return findings


def scan_text_for_secrets(file_name: str, text: str) -> list[dict[str, str]]:
    findings = []
    for pattern in SECRET_TEXT_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(
                {
                    "file": file_name,
                    "kind": "secret_like_text",
                    "snippet": sanitize_snippet(text[max(0, match.start() - 24) : match.end() + 24]),
                }
            )
    return findings


def scan_json_for_secret_keys(file_name: str, value: Any, path: str = "$") -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if SECRET_KEY_RE.search(str(key)) and secret_value_is_exposed(child):
                findings.append(
                    {
                        "file": file_name,
                        "kind": "secret_like_json_value",
                        "path": child_path,
                        "snippet": sanitize_snippet(f"{key}: {child}"),
                    }
                )
            findings.extend(scan_json_for_secret_keys(file_name, child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(scan_json_for_secret_keys(file_name, child, f"{path}[{index}]"))
    return findings


def secret_value_is_exposed(value: Any) -> bool:
    if value in SAFE_PLACEHOLDERS:
        return False
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped in SAFE_PLACEHOLDERS:
            return False
        return len(stripped) >= 4
    return False


def sanitize_snippet(value: str) -> str:
    sanitized = re.sub(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>", value, flags=re.IGNORECASE)
    sanitized = re.sub(r"(Authorization\s*:\s*)(?:Bearer\s+)?[^\s,;]+", r"\1<redacted>", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"((?:Cookie|Set-Cookie)\s*:\s*)[^\r\n;]+(?:;[^\r\n]*)?", r"\1<redacted>", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(
        r"([?&](?:token|access_token|refresh_token|api_key|apikey|key|password|pwd|signature|integration_key|integration-key|sig)=)[^&#\s\"']+",
        r"\1<redacted>",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(r"([a-z][a-z0-9+.-]*://[^:/@\s]+:)[^/@\s]+(@)", r"\1<redacted>\2", sanitized, flags=re.IGNORECASE)
    if len(sanitized) > 160:
        sanitized = sanitized[:157] + "..."
    return sanitized


def print_human_summary(result: dict[str, Any]) -> None:
    print(f"verdict: {result['verdict']}")
    print(f"bundle_dir: {result.get('bundle_dir')}")
    print(f"checked_files: {', '.join(result.get('checked_files') or []) or 'none'}")
    print(f"missing_files: {', '.join(result.get('missing_files') or []) or 'none'}")
    print(f"failed_checks: {', '.join(result.get('failed_checks') or []) or 'none'}")
    print(f"warnings: {', '.join(result.get('warnings') or []) or 'none'}")
    print(f"real_provider_proof_status: {result.get('real_provider_proof_status')}")
    print(f"load_test_status: {result.get('load_test_status')}")
    print(f"stage27b_ws_load_status: {result.get('stage27b_ws_load_status')}")
    print(f"ws_6_lessons_1000_status: {result.get('ws_6_lessons_1000_status')}")
    print(f"ws_6_lessons_1000_verdict: {result.get('ws_6_lessons_1000_verdict')}")
    print(f"ws_6_lessons_1000_mock_only: {result.get('ws_6_lessons_1000_mock_only')}")
    print(f"ws_6_lessons_1000_real_provider_proof: {result.get('ws_6_lessons_1000_real_provider_proof')}")
    print(f"ws_required_lessons: {result.get('ws_required_lessons')}")
    print(f"ws_required_students: {result.get('ws_required_students')}")
    print(f"ws_report_lessons: {result.get('ws_report_lessons')}")
    print(f"ws_report_students: {result.get('ws_report_students')}")
    print(f"ws_connected_clients_total: {result.get('ws_connected_clients_total')}")
    print(f"ws_peak_connected_clients: {result.get('ws_peak_connected_clients')}")
    print(f"ws_scenario_match: {result.get('ws_scenario_match')}")
    print(f"ws_scenario_status: {result.get('ws_scenario_status')}")
    print(f"realistic_ws_status: {result.get('realistic_ws_status')}")
    print(f"realistic_ws_required_lessons: {result.get('realistic_ws_required_lessons')}")
    print(f"realistic_ws_required_students: {result.get('realistic_ws_required_students')}")
    print(f"realistic_ws_report_lessons: {result.get('realistic_ws_report_lessons')}")
    print(f"realistic_ws_report_students: {result.get('realistic_ws_report_students')}")
    print(f"realistic_ws_connected_clients_total: {result.get('realistic_ws_connected_clients_total')}")
    print(f"realistic_ws_verdict: {result.get('realistic_ws_verdict')}")
    print(f"realistic_ws_mock_only: {result.get('realistic_ws_mock_only')}")
    print(f"realistic_ws_real_provider_proof: {result.get('realistic_ws_real_provider_proof')}")
    if result.get("realistic_ws_status") == "pass":
        print("realistic_ws_scope: expected-scale mock WebSocket proof, not 1000-client proof")
    print(f"readiness_status: {result.get('readiness_status')}")
    print(f"config_status: {result.get('config_status')}")
    print(f"runtime_metrics_status: {result.get('runtime_metrics_status')}")
    if result.get("secret_scan_findings"):
        print("secret_scan_findings:")
        for finding in result["secret_scan_findings"]:
            path = finding.get("path")
            suffix = f" {path}" if path else ""
            print(f"- {finding.get('file')}: {finding.get('kind')}{suffix}: {finding.get('snippet')}")
    else:
        print("secret_scan_findings: none")
    if result.get("safety_notes"):
        print("safety_notes:")
        for note in result["safety_notes"]:
            print(f"- {note}")


if __name__ == "__main__":
    raise SystemExit(main())
