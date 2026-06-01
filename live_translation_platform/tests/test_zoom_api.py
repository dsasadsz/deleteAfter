from datetime import datetime, timedelta

import httpx
import pytest

from app.zoom.zoom_api_client import ZoomAPIClient, ZoomAPIError
from app.zoom.zoom_oauth import ZoomCredentialsError, ZoomOAuthClient


@pytest.mark.asyncio
async def test_zoom_oauth_client_parses_and_caches_access_token():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/oauth/token"
        assert request.url.params["grant_type"] == "account_credentials"
        assert request.url.params["account_id"] == "acct_123"
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, json={"access_token": "token_1", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://zoom.us") as client:
        oauth = ZoomOAuthClient(
            account_id="acct_123",
            client_id="client_123",
            client_secret="secret_123",
            http_client=client,
        )
        first = await oauth.access_token()
        second = await oauth.access_token()

    assert first == "token_1"
    assert second == "token_1"
    assert len(requests) == 1
    assert oauth.expires_at is not None
    assert oauth.expires_at > datetime.utcnow() + timedelta(minutes=50)


@pytest.mark.asyncio
async def test_zoom_oauth_client_requires_credentials():
    oauth = ZoomOAuthClient(account_id="", client_id="", client_secret="")

    with pytest.raises(ZoomCredentialsError, match="ZOOM_ACCOUNT_ID"):
        await oauth.access_token()


@pytest.mark.asyncio
async def test_zoom_api_client_creates_meeting_with_retry_after_server_error():
    calls = []
    oauth = ZoomOAuthClient(
        account_id="acct_123",
        client_id="client_123",
        client_secret="secret_123",
    )
    oauth._access_token = "cached_token"
    oauth.expires_at = datetime.utcnow() + timedelta(minutes=30)

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/v2/users/me/meetings"
        assert request.headers["authorization"] == "Bearer cached_token"
        if len(calls) == 1:
            return httpx.Response(500, json={"message": "temporary"})
        payload = __import__("json").loads(request.content)
        assert payload["topic"] == "C# lesson"
        assert payload["duration"] == 60
        assert payload["timezone"] == "Asia/Almaty"
        return httpx.Response(
            201,
            json={
                "id": 123456789,
                "uuid": "uuid_123",
                "join_url": "https://zoom.us/j/123456789",
                "start_url": "https://zoom.us/s/123456789?zak=secret",
                "topic": "C# lesson",
                "created_at": "2026-05-08T10:00:00Z",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.zoom.us") as client:
        zoom = ZoomAPIClient(
            oauth_client=oauth,
            api_base_url="https://api.zoom.us/v2",
            timezone="Asia/Almaty",
            duration_minutes=60,
            http_client=client,
            max_retries=1,
        )
        meeting = await zoom.create_meeting("C# lesson")

    assert meeting.meeting_id == "123456789"
    assert meeting.meeting_uuid == "uuid_123"
    assert meeting.topic == "C# lesson"
    assert meeting.created_at == "2026-05-08T10:00:00Z"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_zoom_api_client_raises_clear_error_for_zoom_api_failure():
    oauth = ZoomOAuthClient(account_id="acct_123", client_id="client_123", client_secret="secret_123")
    oauth._access_token = "cached_token"
    oauth.expires_at = datetime.utcnow() + timedelta(minutes=30)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Invalid request"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.zoom.us") as client:
        zoom = ZoomAPIClient(oauth_client=oauth, http_client=client, max_retries=0)
        with pytest.raises(ZoomAPIError, match="Invalid request"):
            await zoom.create_meeting("C# lesson")

