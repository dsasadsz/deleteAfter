#!/usr/bin/env python
"""Collect a sanitized small-scale real-provider E2E snapshot."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


ENDPOINTS = (
    ("health_ready", "/api/health/ready"),
    ("runtime_metrics", "/api/metrics/runtime"),
    ("providers_status", "/api/providers/status"),
    ("tts_status", "/api/tts/status"),
)

SECRET_KEY_RE = re.compile(
    r"(secret|token|api[_-]?key|apikey|password|passwd|authorization|credential|signature|zak|start_url|join_url)",
    re.IGNORECASE,
)
SECRET_QUERY_RE = re.compile(
    r"([?&](?:token|access_token|refresh_token|api_key|apikey|key|password|pwd|signature|zak)=)[^&#\s]+",
    re.IGNORECASE,
)


def _mask_identifier(value: Any) -> str:
    text = str(value)
    if len(text) <= 4:
        return "[masked]"
    return f"[masked:{text[-4:]}]"


def _sanitize(value: Any, key: str | None = None) -> Any:
    if key and "meeting_id" in key.lower():
        return _mask_identifier(value)
    if key and SECRET_KEY_RE.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(child_key): _sanitize(child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return SECRET_QUERY_RE.sub(r"\1[redacted]", value)
    return value


def _fetch_json(url: str, timeout: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "stage26h-e2e-snapshot/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {
                    "ok": False,
                    "status": response.status,
                    "error": "InvalidJSON",
                    "content_type": response.headers.get("content-type"),
                }
            return {"ok": 200 <= response.status < 300, "status": response.status, "json": payload}
    except HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": exc.__class__.__name__}
    except URLError as exc:
        return {"ok": False, "error": exc.__class__.__name__, "reason": exc.reason.__class__.__name__}
    except TimeoutError:
        return {"ok": False, "error": "TimeoutError"}
    except OSError as exc:
        return {"ok": False, "error": exc.__class__.__name__}


def _endpoint_url(base_url: str, path: str) -> str:
    normalized = base_url.rstrip("/") + "/"
    return urljoin(normalized, path.lstrip("/"))


def collect_snapshot(base_url: str, lesson_id: str | None = None, timeout: float = 5.0) -> dict[str, Any]:
    endpoints = list(ENDPOINTS)
    if lesson_id:
        endpoints.append(("lesson_diagnostics", f"/api/lessons/{lesson_id}/diagnostics"))

    result: dict[str, Any] = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "base_url": _sanitize(base_url.rstrip("/")),
        "lesson_id": lesson_id,
        "endpoints": {},
        "notes": [
            "Sanitized snapshot for a small manual real-provider E2E test.",
            "Do not paste secrets into reports, tickets, screenshots, or chat.",
            "Missing or unauthorized optional endpoints are recorded as warnings, not fatal script errors.",
        ],
    }

    for name, path in endpoints:
        url = _endpoint_url(base_url, path)
        payload = _fetch_json(url, timeout)
        result["endpoints"][name] = _sanitize({"path": path, "url": url, **payload})

    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect sanitized JSON from /api/health/ready, /api/metrics/runtime, "
            "/api/providers/status, /api/tts/status, and optionally lesson diagnostics."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Application base URL.")
    parser.add_argument("--lesson-id", default=None, help="Optional lesson_id for /api/lessons/{lesson_id}/diagnostics.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-request timeout in seconds.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    snapshot = collect_snapshot(args.base_url, lesson_id=args.lesson_id, timeout=args.timeout)
    json.dump(snapshot, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
