from base64 import b64encode
from datetime import datetime, timedelta

import httpx


class ZoomCredentialsError(RuntimeError):
    pass


class ZoomOAuthError(RuntimeError):
    pass


class ZoomOAuthClient:
    def __init__(
        self,
        account_id: str,
        client_id: str,
        client_secret: str,
        token_url: str = "https://zoom.us/oauth/token",
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.account_id = account_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self.http_client = http_client
        self.timeout_seconds = timeout_seconds
        self._access_token: str | None = None
        self.expires_at: datetime | None = None

    async def access_token(self) -> str:
        self._ensure_credentials()
        if self._access_token and self.expires_at and self.expires_at > datetime.utcnow() + timedelta(seconds=60):
            return self._access_token

        close_client = self.http_client is None
        client = self.http_client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            response = await client.post(
                self.token_url,
                params={"grant_type": "account_credentials", "account_id": self.account_id},
                headers={"Authorization": f"Basic {self._basic_auth_token()}"},
            )
        except httpx.HTTPError as exc:
            raise ZoomOAuthError(f"Zoom OAuth request failed: {exc}") from exc
        finally:
            if close_client:
                await client.aclose()

        if response.status_code >= 400:
            raise ZoomOAuthError(f"Zoom OAuth failed ({response.status_code}): {self._error_message(response)}")

        payload = response.json()
        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 0))
        if not token or expires_in <= 0:
            raise ZoomOAuthError("Zoom OAuth response did not include a valid access_token and expires_in.")

        self._access_token = token
        self.expires_at = datetime.utcnow() + timedelta(seconds=max(0, expires_in - 60))
        return token

    def _ensure_credentials(self) -> None:
        missing = []
        if not self.account_id:
            missing.append("ZOOM_ACCOUNT_ID")
        if not self.client_id:
            missing.append("ZOOM_CLIENT_ID")
        if not self.client_secret:
            missing.append("ZOOM_CLIENT_SECRET")
        if missing:
            raise ZoomCredentialsError(f"Missing Zoom credentials: {', '.join(missing)}")

    def _basic_auth_token(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return b64encode(raw).decode()

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text
        return str(payload.get("message") or payload)


ZoomServerToServerOAuth = ZoomOAuthClient
