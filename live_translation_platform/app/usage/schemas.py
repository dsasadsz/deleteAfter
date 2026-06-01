from datetime import datetime

from pydantic import BaseModel, Field


class ProviderPricingCreate(BaseModel):
    provider_type: str
    provider_name: str
    unit: str
    price_per_unit: float
    currency: str = "USD"
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    source_note: str = ""
    enabled: bool = True


class ProviderPricingUpdate(BaseModel):
    provider_type: str | None = None
    provider_name: str | None = None
    unit: str | None = None
    price_per_unit: float | None = None
    currency: str | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    source_note: str | None = None
    enabled: bool | None = None


class ProviderCost(BaseModel):
    provider_type: str
    provider_name: str
    quantity: float
    unit: str
    estimated_cost: float | None = None
    unit_price: float | None = None
    currency: str = "USD"
    pricing_id: int | None = None


class UsageSummary(BaseModel):
    lesson_id: str | None = None
    smoke_test_id: str | None = None
    comparison_id: str | None = None
    audio_minutes: float = 0
    translation_characters: int = 0
    captions: int = 0
    final_transcript_segments: int = 0
    provider_costs: list[ProviderCost] = Field(default_factory=list)
    total_estimated_cost: float = 0
    currency: str = "USD"
    warnings: list[str] = Field(default_factory=list)
