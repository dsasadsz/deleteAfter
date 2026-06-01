from fastapi import APIRouter, Request
import httpx

from app.smoke.provider_status import elevenlabs_user_probe_status, provider_status
from app.providers.quotas import enrich_provider_status

router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.get("/status")
async def get_provider_status(request: Request, live: bool = False) -> dict:
    settings = request.app.state.settings
    status = enrich_provider_status(provider_status(settings), settings, request.app if live else None)
    if live and settings.elevenlabs_api_key:
        status["stt"]["elevenlabs"] = await _elevenlabs_live_status(settings)
        status = enrich_provider_status(status, settings, request.app)
    return status


async def _elevenlabs_live_status(settings) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                "https://api.elevenlabs.io/v1/user",
                headers={"xi-api-key": settings.elevenlabs_api_key},
            )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return elevenlabs_user_probe_status(
            has_api_key=True,
            status_code=response.status_code,
            payload=payload,
            stt_endpoint_ready=True,
        )
    except httpx.HTTPError as exc:
        status = elevenlabs_user_probe_status(has_api_key=True, stt_endpoint_ready=True)
        status["warning"] = f"ElevenLabs user endpoint check unavailable; STT readiness is independent: {exc.__class__.__name__}"
        return status
