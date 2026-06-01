from sqlalchemy.orm import Session, sessionmaker

from app.usage.repository import UsageRepository


def pcm_duration_seconds(byte_count: int, sample_rate: int | None, channels: int | None, bits_per_sample: int = 16) -> float:
    if not sample_rate or not channels or sample_rate <= 0 or channels <= 0:
        return 0.0
    bytes_per_sample = bits_per_sample / 8
    return round(byte_count / (sample_rate * channels * bytes_per_sample), 6)


class UsageTracker:
    def __init__(self, session_factory: sessionmaker[Session], enabled: bool = True) -> None:
        self.session_factory = session_factory
        self.enabled = enabled

    def record_stt_audio(
        self,
        provider_name: str,
        duration_seconds: float,
        byte_count: int,
        chunks: int,
        lesson_id: str | None = None,
        smoke_test_id: str | None = None,
        comparison_id: str | None = None,
        audio_source: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        metadata = {"source": audio_source} if audio_source else None
        with self.session_factory() as session:
            repo = UsageRepository(session)
            repo.record_usage("stt", provider_name, "audio_duration_seconds", duration_seconds, "second", lesson_id, smoke_test_id, comparison_id, metadata)
            repo.record_usage("stt", provider_name, "audio_bytes", byte_count, "byte", lesson_id, smoke_test_id, comparison_id, metadata)
            repo.record_usage("stt", provider_name, "audio_chunks", chunks, "chunk", lesson_id, smoke_test_id, comparison_id, metadata)

    def record_translation(
        self,
        provider_name: str,
        source_text: str,
        target_languages: list[str],
        lesson_id: str | None = None,
        smoke_test_id: str | None = None,
        comparison_id: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        metadata = {"target_languages": target_languages, "target_language_count": len(target_languages)}
        with self.session_factory() as session:
            UsageRepository(session).record_usage(
                "translation",
                provider_name,
                "source_characters",
                len(source_text or ""),
                "character",
                lesson_id,
                smoke_test_id,
                comparison_id,
                metadata,
            )

    def record_caption(
        self,
        stt_provider: str,
        translation_provider: str,
        lesson_id: str | None = None,
        smoke_test_id: str | None = None,
        comparison_id: str | None = None,
        is_final: bool = False,
    ) -> None:
        if not self.enabled:
            return
        with self.session_factory() as session:
            repo = UsageRepository(session)
            repo.record_usage("caption", "caption_hub", "captions", 1, "count", lesson_id, smoke_test_id, comparison_id, {"stt": stt_provider, "translator": translation_provider})
            if is_final:
                repo.record_usage("transcript", "transcript", "final_segments", 1, "count", lesson_id, smoke_test_id, comparison_id)
