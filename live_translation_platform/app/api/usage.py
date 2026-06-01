from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.usage.cost_estimator import CostEstimator
from app.usage.pricing import default_pricing_rows
from app.usage.repository import UsageRepository
from app.usage.schemas import ProviderPricingCreate, ProviderPricingUpdate

router = APIRouter(tags=["usage"])


@router.get("/api/usage/pricing")
def list_pricing(request: Request) -> list[dict]:
    with request.app.state.database.session_factory() as session:
        return [_pricing_response(item) for item in UsageRepository(session).list_pricing()]


@router.post("/api/usage/pricing")
def create_pricing(payload: ProviderPricingCreate, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        pricing = UsageRepository(session).create_pricing(**payload.model_dump())
        return _pricing_response(pricing)


@router.put("/api/usage/pricing/{pricing_id}")
def update_pricing(pricing_id: int, payload: ProviderPricingUpdate, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        pricing = UsageRepository(session).update_pricing(pricing_id, **payload.model_dump(exclude_unset=True))
        if pricing is None:
            raise HTTPException(status_code=404, detail="Pricing not found")
        return _pricing_response(pricing)


@router.delete("/api/usage/pricing/{pricing_id}")
def delete_pricing(pricing_id: int, request: Request) -> dict:
    with request.app.state.database.session_factory() as session:
        if not UsageRepository(session).delete_pricing(pricing_id):
            raise HTTPException(status_code=404, detail="Pricing not found")
        return {"status": "deleted", "pricing_id": pricing_id}


@router.post("/api/usage/pricing/defaults")
def load_default_pricing(request: Request) -> dict:
    created = 0
    with request.app.state.database.session_factory() as session:
        repo = UsageRepository(session)
        for row in default_pricing_rows():
            repo.create_pricing(**row)
            created += 1
    return {"status": "loaded", "created": created}


@router.get("/api/lessons/{lesson_id}/usage")
def lesson_usage(lesson_id: str, request: Request) -> dict:
    return _estimator(request).estimate_for_lesson(lesson_id).model_dump(mode="json")


@router.get("/api/lessons/{lesson_id}/cost")
def lesson_cost(lesson_id: str, request: Request) -> dict:
    return _estimator(request).estimate_for_lesson(lesson_id).model_dump(mode="json")


@router.get("/api/smoke/{smoke_test_id}/usage")
def smoke_usage(smoke_test_id: str, request: Request) -> dict:
    return _estimator(request).estimate_for_smoke(smoke_test_id).model_dump(mode="json")


@router.get("/api/compare/{comparison_id}/usage")
def comparison_usage(comparison_id: str, request: Request) -> dict:
    return _estimator(request).estimate_for_comparison(comparison_id).model_dump(mode="json")


@router.get("/api/usage/summary")
def usage_summary(request: Request, provider: str | None = None, provider_type: str | None = None, start: str | None = None, end: str | None = None) -> dict:
    start_dt = _parse_date(start)
    end_dt = _parse_date(end)
    with request.app.state.database.session_factory() as session:
        records = UsageRepository(session).list_records(provider=provider, provider_type=provider_type, start=start_dt, end=end_dt)
    summary = _estimator(request).estimate_from_usage_records(records)
    return {
        "total_audio_minutes": summary.audio_minutes,
        "total_translation_characters": summary.translation_characters,
        "total_captions": summary.captions,
        "total_estimated_cost": summary.total_estimated_cost,
        "currency": summary.currency,
        "provider_costs": [item.model_dump(mode="json") for item in summary.provider_costs],
        "warnings": summary.warnings,
    }


def _estimator(request: Request) -> CostEstimator:
    settings = request.app.state.settings
    return CostEstimator(
        request.app.state.database.session_factory,
        default_currency=settings.default_currency,
        enabled=settings.cost_estimation_enabled,
    )


def _pricing_response(pricing) -> dict:
    return {
        "id": pricing.id,
        "provider_type": pricing.provider_type,
        "provider_name": pricing.provider_name,
        "unit": pricing.unit,
        "price_per_unit": pricing.price_per_unit,
        "currency": pricing.currency,
        "effective_from": pricing.effective_from.isoformat(),
        "effective_to": pricing.effective_to.isoformat() if pricing.effective_to else None,
        "source_note": pricing.source_note,
        "enabled": pricing.enabled,
    }


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
