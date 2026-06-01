from collections import defaultdict

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import UsageRecord
from app.usage.repository import UsageRepository
from app.usage.schemas import ProviderCost, UsageSummary


class CostEstimator:
    def __init__(self, session_factory: sessionmaker[Session], default_currency: str = "USD", enabled: bool = True) -> None:
        self.session_factory = session_factory
        self.default_currency = default_currency
        self.enabled = enabled

    def estimate_for_lesson(self, lesson_id: str) -> UsageSummary:
        with self.session_factory() as session:
            records = UsageRepository(session).records_for_scope(lesson_id=lesson_id)
        summary = self.estimate_from_usage_records(records)
        summary.lesson_id = lesson_id
        return summary

    def estimate_for_smoke(self, smoke_test_id: str) -> UsageSummary:
        with self.session_factory() as session:
            records = UsageRepository(session).records_for_scope(smoke_test_id=smoke_test_id)
        summary = self.estimate_from_usage_records(records)
        summary.smoke_test_id = smoke_test_id
        return summary

    def estimate_for_comparison(self, comparison_id: str) -> UsageSummary:
        with self.session_factory() as session:
            records = UsageRepository(session).records_for_scope(comparison_id=comparison_id)
        summary = self.estimate_from_usage_records(records)
        summary.comparison_id = comparison_id
        return summary

    def estimate_from_usage_records(self, records: list[UsageRecord]) -> UsageSummary:
        audio_seconds = sum(record.quantity for record in records if record.metric_name == "audio_duration_seconds")
        translation_chars = int(sum(record.quantity for record in records if record.metric_name == "source_characters"))
        captions = int(sum(record.quantity for record in records if record.metric_name == "captions"))
        final_segments = int(sum(record.quantity for record in records if record.metric_name == "final_segments"))
        provider_costs, warnings = self._provider_costs(records)
        total = round(sum(item.estimated_cost or 0 for item in provider_costs), 6)
        return UsageSummary(
            audio_minutes=round(audio_seconds / 60, 4),
            translation_characters=translation_chars,
            captions=captions,
            final_transcript_segments=final_segments,
            provider_costs=provider_costs,
            total_estimated_cost=total,
            currency=self.default_currency,
            warnings=warnings,
        )

    def _provider_costs(self, records: list[UsageRecord]) -> tuple[list[ProviderCost], list[str]]:
        grouped: dict[tuple[str, str, str], float] = defaultdict(float)
        for record in records:
            mapped = _cost_metric(record)
            if mapped is None:
                continue
            provider_type, provider_name, unit, quantity = mapped
            grouped[(provider_type, provider_name, unit)] += quantity

        costs = []
        warnings = []
        with self.session_factory() as session:
            repo = UsageRepository(session)
            for (provider_type, provider_name, unit), quantity in grouped.items():
                pricing = repo.active_pricing(provider_type, provider_name, unit)
                if pricing is None:
                    warnings.append(f"Missing pricing for {provider_type}/{provider_name}/{unit}")
                    costs.append(ProviderCost(provider_type=provider_type, provider_name=provider_name, quantity=round(quantity, 6), unit=unit, currency=self.default_currency))
                    continue
                estimated = round(quantity * pricing.price_per_unit, 6)
                costs.append(
                    ProviderCost(
                        provider_type=provider_type,
                        provider_name=provider_name,
                        quantity=round(quantity, 6),
                        unit=unit,
                        estimated_cost=estimated,
                        unit_price=pricing.price_per_unit,
                        currency=pricing.currency,
                        pricing_id=pricing.id,
                    )
                )
        return costs, warnings


def _cost_metric(record: UsageRecord) -> tuple[str, str, str, float] | None:
    if record.provider_type == "stt" and record.metric_name == "audio_duration_seconds":
        return record.provider_type, record.provider_name, "audio_minute", record.quantity / 60
    if record.provider_type == "translation" and record.metric_name == "source_characters":
        return record.provider_type, record.provider_name, "million_characters", record.quantity / 1_000_000
    if record.metric_name in {"request", "session"}:
        return record.provider_type, record.provider_name, record.metric_name, record.quantity
    return None
