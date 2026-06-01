from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


ENDPOINTS = (
    "/api/health",
    "/api/health/ready",
    "/api/metrics/runtime",
    "/api/v1/integration/spec",
)


@dataclass
class CheckResult:
    level: str
    name: str
    message: str


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check production deployment readiness endpoints. "
            "Calls /api/health, /api/health/ready, /api/metrics/runtime, "
            "and /api/v1/integration/spec."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base URL for the deployed app, for example https://translation.example.com")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout in seconds per endpoint")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/") + "/"
    results: list[CheckResult] = []
    payloads: dict[str, Any] = {}

    for path in ENDPOINTS:
        url = urljoin(base_url, path.lstrip("/"))
        try:
            payload = _fetch_json(url, timeout=args.timeout)
        except Exception as exc:
            results.append(CheckResult("FAIL", path, _safe_error(exc)))
            continue
        payloads[path] = payload
        results.append(CheckResult("PASS", path, "reachable JSON"))

    ready = payloads.get("/api/health/ready")
    if isinstance(ready, dict):
        if ready.get("status") == "ready":
            results.append(CheckResult("PASS", "readiness", "status=ready"))
        else:
            missing = ready.get("config_missing") or []
            warnings = ready.get("config_warnings") or []
            results.append(CheckResult("FAIL", "readiness", f"status={ready.get('status')}; missing={missing}; warnings={warnings}"))
        _append_bool_result(results, "database", ready.get("database_status") == "ok", "database connectivity")
        redis = ready.get("redis") or {}
        if redis.get("enabled") and redis.get("connected"):
            results.append(CheckResult("PASS", "redis", "enabled and connected"))
        elif redis.get("enabled"):
            results.append(CheckResult("WARN", "redis", "enabled but not connected"))
        else:
            results.append(CheckResult("WARN", "redis", "disabled"))
        pubsub = ready.get("redis_pubsub") or {}
        if pubsub.get("enabled") and pubsub.get("connected"):
            results.append(CheckResult("PASS", "redis_pubsub", "enabled and connected"))
        elif pubsub.get("enabled"):
            results.append(CheckResult("WARN", "redis_pubsub", "enabled but not connected"))
    metrics = payloads.get("/api/metrics/runtime")
    if isinstance(metrics, dict):
        for field in ("redis_enabled", "redis_pubsub_enabled", "redis_rate_limit_enabled", "tts_cache_backend"):
            if field in metrics:
                results.append(CheckResult("PASS", f"metric:{field}", f"{field}={metrics[field]}"))
            else:
                results.append(CheckResult("WARN", f"metric:{field}", "field missing"))
    spec = payloads.get("/api/v1/integration/spec")
    if isinstance(spec, dict) and "/api/v1/integration/spec" in spec.get("http_endpoints", {}):
        results.append(CheckResult("PASS", "integration_spec", "machine-readable contract present"))
    elif isinstance(spec, dict):
        results.append(CheckResult("WARN", "integration_spec", "JSON returned but expected endpoint key missing"))

    for result in results:
        print(f"{result.level}: {result.name} - {result.message}")

    return 1 if any(result.level == "FAIL" for result in results) else 0


def _fetch_json(url: str, *, timeout: float) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "deployment-readiness-check/1.0"})
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("content-type", "")
        body = response.read()
    if "json" not in content_type.lower():
        raise ValueError(f"expected JSON response, got content-type {content_type or 'unknown'}")
    return json.loads(body.decode("utf-8"))


def _append_bool_result(results: list[CheckResult], name: str, passed: bool, message: str) -> None:
    results.append(CheckResult("PASS" if passed else "FAIL", name, message if passed else f"{message} failed"))


def _safe_error(error: Exception) -> str:
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, URLError):
        return error.reason.__class__.__name__ if hasattr(error, "reason") else "URL error"
    return error.__class__.__name__


if __name__ == "__main__":
    sys.exit(main())
