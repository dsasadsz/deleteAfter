import asyncio
from datetime import datetime, timezone

import httpx

from app.zoom.models import ZoomMeeting
from app.zoom.zoom_oauth import ZoomOAuthClient


class ZoomAPIError(RuntimeError):
    pass


class ZoomAPIClient:
    def __init__(
        self,
        oauth_client: ZoomOAuthClient,
        api_base_url: str = "https://api.zoom.us/v2",
        timezone: str = "Asia/Almaty",
        duration_minutes: int = 60,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
    ) -> None:
        self.oauth_client = oauth_client
        self.api_base_url = api_base_url.rstrip("/")
        self.timezone = timezone
        self.duration_minutes = duration_minutes
        self.http_client = http_client
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    async def create_meeting(self, title: str) -> ZoomMeeting:
        token = await self.oauth_client.access_token()
        response = await self._post_with_retry(
            "/users/me/meetings",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "topic": title,
                "type": 2,
                "start_time": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "duration": self.duration_minutes,
                "timezone": self.timezone,
                "settings": {
                    "join_before_host": False,
                    "waiting_room": True,
                    "approval_type": 2,
                    "audio": "both",
                },
            },
        )
        payload = response.json()
        return ZoomMeeting(
            meeting_id=str(payload.get("id", "")),
            meeting_uuid=str(payload.get("uuid", "")),
            join_url=str(payload.get("join_url", "")),
            start_url=str(payload.get("start_url", "")),
            topic=str(payload.get("topic") or title),
            created_at=payload.get("created_at"),
            password=str(payload.get("password") or ""),
        )

    async def _post_with_retry(self, path: str, **kwargs) -> httpx.Response:
        close_client = self.http_client is None
        client = self.http_client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            last_response: httpx.Response | None = None
            for attempt in range(self.max_retries + 1):
                try:
                    response = await client.post(f"{self.api_base_url}{path}", **kwargs)
                except httpx.HTTPError as exc:
                    if attempt >= self.max_retries:
                        raise ZoomAPIError(f"Zoom API request failed: {exc}") from exc
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue

                if response.status_code < 400:
                    return response
                last_response = response
                if response.status_code not in {429, 500, 502, 503, 504} or attempt >= self.max_retries:
                    raise ZoomAPIError(f"Zoom API failed ({response.status_code}): {self._error_message(response)}")
                await asyncio.sleep(0.2 * (attempt + 1))

            if last_response is not None:
                raise ZoomAPIError(f"Zoom API failed ({last_response.status_code}): {self._error_message(last_response)}")
            raise ZoomAPIError("Zoom API request failed without a response.")
        finally:
            if close_client:
                await client.aclose()

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text
        return str(payload.get("message") or payload)
