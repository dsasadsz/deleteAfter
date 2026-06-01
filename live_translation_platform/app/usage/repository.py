import json
from datetime import datetime

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import CostEstimate, ProviderPricing, UsageRecord


class UsageRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_pricing(
        self,
        provider_type: str,
        provider_name: str,
        unit: str,
        price_per_unit: float,
        currency: str = "USD",
        effective_from: datetime | None = None,
        effective_to: datetime | None = None,
        source_note: str = "",
        enabled: bool = True,
    ) -> ProviderPricing:
        pricing = ProviderPricing(
            provider_type=provider_type,
            provider_name=provider_name,
            unit=unit,
            price_per_unit=price_per_unit,
            currency=currency,
            effective_from=effective_from or datetime.utcnow(),
            effective_to=effective_to,
            source_note=source_note,
            enabled=enabled,
        )
        self.session.add(pricing)
        self.session.commit()
        self.session.refresh(pricing)
        return pricing

    def list_pricing(self) -> list[ProviderPricing]:
        return list(self.session.scalars(select(ProviderPricing).order_by(ProviderPricing.provider_type, ProviderPricing.provider_name)).all())

    def get_pricing(self, pricing_id: int) -> ProviderPricing | None:
        return self.session.get(ProviderPricing, pricing_id)

    def update_pricing(self, pricing_id: int, **fields) -> ProviderPricing | None:
        pricing = self.get_pricing(pricing_id)
        if pricing is None:
            return None
        for key, value in fields.items():
            if value is not None and hasattr(pricing, key):
                setattr(pricing, key, value)
        pricing.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(pricing)
        return pricing

    def delete_pricing(self, pricing_id: int) -> bool:
        pricing = self.get_pricing(pricing_id)
        if pricing is None:
            return False
        self.session.delete(pricing)
        self.session.commit()
        return True

    def active_pricing(self, provider_type: str, provider_name: str, unit: str) -> ProviderPricing | None:
        return self.session.scalar(
            select(ProviderPricing)
            .where(
                ProviderPricing.provider_type == provider_type,
                ProviderPricing.provider_name == provider_name,
                ProviderPricing.unit == unit,
                ProviderPricing.enabled.is_(True),
            )
            .order_by(desc(ProviderPricing.effective_from), desc(ProviderPricing.id))
        )

    def record_usage(
        self,
        provider_type: str,
        provider_name: str,
        metric_name: str,
        quantity: float,
        unit: str,
        lesson_id: str | None = None,
        smoke_test_id: str | None = None,
        comparison_id: str | None = None,
        metadata: dict | None = None,
    ) -> UsageRecord:
        record = UsageRecord(
            lesson_id=lesson_id,
            smoke_test_id=smoke_test_id,
            comparison_id=comparison_id,
            provider_type=provider_type,
            provider_name=provider_name,
            metric_name=metric_name,
            quantity=quantity,
            unit=unit,
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False, default=str),
        )
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        return record

    def records_for_scope(
        self,
        lesson_id: str | None = None,
        smoke_test_id: str | None = None,
        comparison_id: str | None = None,
    ) -> list[UsageRecord]:
        statement = select(UsageRecord)
        if lesson_id is not None:
            statement = statement.where(UsageRecord.lesson_id == lesson_id)
        if smoke_test_id is not None:
            statement = statement.where(UsageRecord.smoke_test_id == smoke_test_id)
        if comparison_id is not None:
            statement = statement.where(UsageRecord.comparison_id == comparison_id)
        return list(self.session.scalars(statement.order_by(UsageRecord.created_at, UsageRecord.id)).all())

    def list_records(
        self,
        provider: str | None = None,
        provider_type: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[UsageRecord]:
        statement = select(UsageRecord)
        if provider:
            statement = statement.where(UsageRecord.provider_name == provider)
        if provider_type:
            statement = statement.where(UsageRecord.provider_type == provider_type)
        if start:
            statement = statement.where(UsageRecord.created_at >= start)
        if end:
            statement = statement.where(UsageRecord.created_at <= end)
        return list(self.session.scalars(statement.order_by(desc(UsageRecord.created_at))).all())

    def save_cost_estimate(
        self,
        provider_type: str,
        provider_name: str,
        usage_quantity: float,
        usage_unit: str,
        unit_price: float | None,
        estimated_cost: float | None,
        currency: str,
        pricing_id: int | None,
        lesson_id: str | None = None,
        smoke_test_id: str | None = None,
        comparison_id: str | None = None,
        metadata: dict | None = None,
    ) -> CostEstimate:
        estimate = CostEstimate(
            lesson_id=lesson_id,
            smoke_test_id=smoke_test_id,
            comparison_id=comparison_id,
            provider_type=provider_type,
            provider_name=provider_name,
            usage_quantity=usage_quantity,
            usage_unit=usage_unit,
            unit_price=unit_price,
            estimated_cost=estimated_cost,
            currency=currency,
            pricing_id=pricing_id,
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False, default=str),
        )
        self.session.add(estimate)
        self.session.commit()
        self.session.refresh(estimate)
        return estimate
