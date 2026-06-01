from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime
from typing import Callable

import httpx


def build_callback_payload(event: str, lesson_id: str, external_lesson_id: str | None = None, data: dict | None = None) -> dict:
    return {
        "event": event,
        "version": "1.0",
        "lesson_id": lesson_id,
        "external_lesson_id": external_lesson_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "data": data or {},
    }


class IntegrationCallbackSender:
    def __init__(
        self,
        callback_secret: str = "",
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
        client_factory: Callable | None = None,
    ) -> None:
        self.callback_secret = callback_secret
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)
        self.client_factory = client_factory or httpx.AsyncClient

    async def send(self, callback_url: str | None, payload: dict) -> dict:
        if not callback_url:
            return {"ok": True, "attempts": 0, "status_code": None, "error": None}
        last_error: str | None = None
        last_status: int | None = None
        headers = self._headers(payload)
        for attempt in range(1, self.max_attempts + 1):
            try:
                async with self.client_factory() as client:
                    response = await client.post(callback_url, json=payload, headers=headers, timeout=10)
                last_status = response.status_code
                if 200 <= response.status_code < 300:
                    return {"ok": True, "attempts": attempt, "status_code": response.status_code, "error": None}
                last_error = f"Callback failed with status {response.status_code}"
            except Exception as exc:
                last_error = str(exc)
            if attempt < self.max_attempts and self.backoff_seconds:
                await asyncio.sleep(self.backoff_seconds * attempt)
        return {"ok": False, "attempts": self.max_attempts, "status_code": last_status, "error": last_error}

    def _headers(self, payload: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.callback_secret:
            message = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            digest = hmac.new(self.callback_secret.encode(), message, hashlib.sha256).hexdigest()
            headers["X-Translation-Service-Signature"] = digest
        return headers
