import asyncio
import json
from datetime import datetime

from sqlalchemy.orm import sessionmaker

from app.compare.hub import ComparisonEventHub
from app.db.models import ComparisonRunItem
from app.db.repositories import ComparisonRepository, SmokeTestRepository
from app.realtime.metrics import isoformat_z
from app.smoke.runner import SmokeRunner


class ComparisonRunner:
    def __init__(
        self,
        session_factory: sessionmaker,
        smoke_runner: SmokeRunner,
        comparison_hub: ComparisonEventHub,
        max_parallel: int = 2,
    ) -> None:
        self.session_factory = session_factory
        self.smoke_runner = smoke_runner
        self.comparison_hub = comparison_hub
        self.max_parallel = max(1, max_parallel)

    async def run(self, comparison_id: str) -> None:
        await self._broadcast(comparison_id, "comparison_started", {})
        with self.session_factory() as session:
            repo = ComparisonRepository(session)
            comparison = repo.get_comparison(comparison_id)
            if comparison is None:
                return
            items = repo.items_for_comparison(comparison_id)
            audio_sample_id = comparison.audio_sample_id
            run_mode = comparison.run_mode

        try:
            if run_mode == "parallel":
                semaphore = asyncio.Semaphore(self.max_parallel)
                await asyncio.gather(*(self._run_item(comparison_id, item.id, audio_sample_id, semaphore) for item in items))
            else:
                for item in items:
                    await self._run_item(comparison_id, item.id, audio_sample_id)
            with self.session_factory() as session:
                repo = ComparisonRepository(session)
                comparison = repo.get_comparison(comparison_id)
                summary = _summary(repo.items_for_comparison(comparison_id))
                if comparison is not None:
                    summary["config_snapshot"] = {
                        "audio_mode": comparison.audio_mode,
                        "audio_source": "browser_ws" if comparison.audio_mode == "direct_ws" else comparison.audio_mode,
                        "audio_sample_id": comparison.audio_sample_id,
                        "chunk_ms": getattr(self.smoke_runner.settings, "smoke_audio_chunk_ms", None),
                        "translation_provider": comparison.translation_provider,
                        "target_languages": json.loads(comparison.target_languages_json or "[]"),
                        "stt_providers": json.loads(comparison.stt_providers_json or "[]"),
                        "run_mode": comparison.run_mode,
                        "glossary_id": comparison.glossary_id,
                        "glossary_enabled": comparison.glossary_enabled,
                    }
                repo.complete_comparison(comparison_id, summary)
            await self._broadcast(comparison_id, "comparison_completed", {"summary": summary})
        except Exception as exc:
            with self.session_factory() as session:
                ComparisonRepository(session).complete_comparison(comparison_id, {}, status="error", error=str(exc))
            await self._broadcast(comparison_id, "comparison_error", {"error": str(exc)})

    async def _run_item(self, comparison_id: str, item_id: int, audio_sample_id: str | None, semaphore: asyncio.Semaphore | None = None) -> None:
        if semaphore is not None:
            async with semaphore:
                await self._run_item(comparison_id, item_id, audio_sample_id)
            return
        with self.session_factory() as session:
            repo = ComparisonRepository(session)
            item = session.get(ComparisonRunItem, item_id)
            if item is None:
                return
            repo.update_item_status(item_id, "running")
            smoke_test_id = item.smoke_test_id
            stt_provider = item.stt_provider
        await self._broadcast(comparison_id, "provider_started", {"stt_provider": stt_provider, "smoke_test_id": smoke_test_id})
        await self._broadcast(comparison_id, "provider_progress", {"stt_provider": stt_provider, "status": "running"})
        await self.smoke_runner.run(smoke_test_id, audio_sample_id, comparison_id=comparison_id)
        with self.session_factory() as session:
            smoke = SmokeTestRepository(session).get_run(smoke_test_id)
            result = _result_from_smoke(smoke, stt_provider)
            repo = ComparisonRepository(session)
            if smoke is None or smoke.status == "error":
                repo.update_result(item_id, result, status="error", error=result.get("error"))
                await self._broadcast(comparison_id, "provider_error", {"stt_provider": stt_provider, **result})
            else:
                repo.update_result(item_id, result, status="completed")
                await self._broadcast(comparison_id, "provider_completed", {"stt_provider": stt_provider, **result})

    async def _broadcast(self, comparison_id: str, event_type: str, payload: dict) -> None:
        event = {"event": event_type, "comparison_id": comparison_id, **payload, "created_at": isoformat_z(datetime.utcnow())}
        await self.comparison_hub.broadcast(comparison_id, event)


def _result_from_smoke(smoke, stt_provider: str) -> dict:
    if smoke is None:
        return {
            "stt_provider": stt_provider,
            "status": "error",
            "audio_source": "",
            "original_text": "",
            "translations": {},
            "latency_ms": {},
            "error": "Smoke test not found",
        }
    return {
        "stt_provider": smoke.stt_provider,
        "smoke_test_id": smoke.id,
        "status": smoke.status,
        "audio_source": "browser_ws" if smoke.audio_mode == "direct_ws" else smoke.audio_mode,
        "original_text": smoke.original_text or "",
        "translations": json.loads(smoke.translations_json or "{}"),
        "latency_ms": json.loads(smoke.latency_json or "{}"),
        "error": smoke.error,
        "glossary": json.loads(smoke.provider_metrics_json or "{}").get("glossary", {"enabled": False}),
    }


def _summary(items) -> dict:
    results = [json.loads(item.result_json or "{}") for item in items if item.status == "completed"]
    summary: dict = {"completed": len(results), "errors": len([item for item in items if item.status == "error"])}
    provider_error_counts = {
        item.stt_provider: 0 if item.status == "completed" else 1
        for item in items
    }
    summary["lowest_error_count_provider"] = min(provider_error_counts.items(), key=lambda item: item[1])[0] if provider_error_counts else None
    averages = {}
    for latency_key in ["first_partial", "stt_final", "translation", "total_server", "client_receive"]:
        values = [
            result.get("latency_ms", {}).get(latency_key)
            for result in results
            if isinstance(result.get("latency_ms", {}).get(latency_key), (int, float))
        ]
        averages[latency_key] = round(sum(values) / len(values), 1) if values else 0
    summary["average_latencies_ms"] = averages
    latency_keys = {
        "fastest_first_partial_provider": "first_partial",
        "fastest_final_provider": "stt_final",
        "fastest_total_provider": "total_server",
    }
    for output_key, latency_key in latency_keys.items():
        candidates = [
            (result.get("stt_provider"), result.get("latency_ms", {}).get(latency_key))
            for result in results
            if result.get("latency_ms", {}).get(latency_key) is not None
        ]
        candidates = [(provider, value) for provider, value in candidates if isinstance(value, (int, float))]
        summary[output_key] = min(candidates, key=lambda item: item[1])[0] if candidates else None
    return summary
