import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.audio.base import AudioChunk, AudioSource
from app.glossary.normalizer import TranscriptNormalizer
from app.glossary.postprocessor import TranslationPostProcessor
from app.glossary.schemas import GlossaryTermData
from app.main import create_app
from app.realtime.audio_pipeline import AudioPipeline
from app.stt.mock_stt import MockSTT
from app.translation.base import TranslationProvider


def test_create_default_programming_glossary(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'glossary-default.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        response = client.post("/api/glossaries/defaults/programming-ru")
        glossaries = client.get("/api/glossaries")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "programming_ru"
    assert payload["terms_count"] >= 10
    assert any(item["name"] == "programming_ru" for item in glossaries.json())


def test_glossary_crud_and_lesson_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'glossary-crud.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        glossary = client.post(
            "/api/glossaries",
            json={
                "name": "math_ru",
                "description": "Math terms",
                "domain": "math",
                "source_language": "ru-RU",
                "target_languages": ["kk", "uz"],
            },
        ).json()
        term = client.post(
            f"/api/glossaries/{glossary['id']}/terms",
            json={
                "source": "плюс минус",
                "canonical": "±",
                "aliases": ["plus minus"],
                "translations": {"kk": "±", "uz": "±"},
                "match_type": "phrase",
            },
        ).json()
        client.put(f"/api/glossaries/{glossary['id']}/terms/{term['id']}", json={"priority": 20, "enabled": True})
        lesson = client.post("/api/lessons", json={"title": "Glossary lesson", "mode": "mock"}).json()
        selection = client.post(
            f"/api/lessons/{lesson['lesson_id']}/glossary",
            json={"glossary_id": glossary["id"], "enabled": True},
        )
        current = client.get(f"/api/lessons/{lesson['lesson_id']}/glossary")

    assert selection.status_code == 200
    assert current.json()["glossary_id"] == glossary["id"]
    assert current.json()["enabled"] is True


def test_transcript_normalizer_exact_phrase_alias_regex_disabled_and_priority():
    terms = [
        GlossaryTermData(id="broad", source="си", canonical="C", aliases=[], translations={}, match_type="phrase", priority=1),
        GlossaryTermData(id="csharp", source="си шарп", canonical="C#", aliases=["C sharp", "сишарп"], translations={}, match_type="phrase", priority=10),
        GlossaryTermData(id="sql", source="SQL", canonical="SQL", aliases=[], translations={}, match_type="exact", priority=5),
        GlossaryTermData(id="regex", source=r"\bapi\b", canonical="API", aliases=[], translations={}, match_type="regex", priority=8),
        GlossaryTermData(id="disabled", source="фронтенд", canonical="frontend", aliases=[], translations={}, enabled=False),
    ]

    result = TranscriptNormalizer().normalize("си шарп и C sharp используют sql api фронтенд", terms)

    assert result.normalized_text == "C# и C# используют SQL API фронтенд"
    assert [change["term_id"] for change in result.changes] == ["csharp", "csharp", "sql", "regex"]


def test_translation_postprocessor_enforces_term_translations():
    terms = [
        GlossaryTermData(
            id="ef",
            source="энтити фреймворк",
            canonical="Entity Framework",
            aliases=["entity framework"],
            translations={"kk": "Entity Framework", "uz": "Entity Framework", "zh-Hans": "Entity Framework"},
            match_type="phrase",
            priority=10,
        )
    ]

    result = TranslationPostProcessor().postprocess(
        original_text="Сегодня Entity Framework",
        translations={
            "kk": "Бүгін Энтити Фреймворк",
            "uz": "Bugun Entiti Freymvork",
            "zh-Hans": "今天 实体框架",
        },
        glossary_terms=terms,
    )

    assert result.translations["kk"].endswith("Entity Framework")
    assert result.translations["uz"].endswith("Entity Framework")
    assert result.translations["zh-Hans"].endswith("Entity Framework")
    assert len(result.changes) == 3


@pytest.mark.asyncio
async def test_audio_pipeline_applies_glossary_before_translation_and_caption_contains_metadata():
    events = []
    saved = []
    terms = [
        GlossaryTermData(
            id="csharp",
            source="си шарп",
            canonical="C#",
            aliases=["сишарп"],
            translations={"kk": "C#"},
            match_type="phrase",
            priority=10,
        )
    ]

    async def publish(payload):
        events.append(payload)

    pipeline = AudioPipeline(
        lesson_id="lesson_glossary",
        meeting_id="meeting",
        source=OneChunkSource("Сегодня мы изучим си шарп"),
        stt=MockSTT(),
        translator=EchoTranslator(),
        target_languages=["kk"],
        translate_partials=False,
        publish=publish,
        save_caption=lambda payload: saved.append(payload),
        save_metric=lambda payload: None,
        publish_debug=lambda payload: None,
        glossary_terms=terms,
        glossary_id="glossary_1",
        glossary_enabled=True,
    )

    await pipeline.start()
    await asyncio.sleep(0.2)
    await pipeline.stop()

    final = [event for event in events if event["is_final"]][0]
    assert final["original_text_raw"] == "Сегодня мы изучим си шарп"
    assert final["original_text"] == "Сегодня мы изучим C#"
    assert final["translations"]["kk"] == "[kk] Сегодня мы изучим C#"
    assert final["glossary"]["enabled"] is True
    assert final["glossary"]["normalization_changes"][0]["to"] == "C#"
    assert saved[0]["glossary"]["normalization_changes"]


def test_smoke_run_can_enable_glossary(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'smoke-glossary.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        glossary = client.post("/api/glossaries/defaults/programming-ru").json()
        response = client.post(
            "/api/smoke/run",
            json={
                "audio_mode": "mock_chunks",
                "stt_provider": "mock",
                "translation_provider": "mock",
                "target_languages": ["kk"],
                "glossary_id": glossary["id"],
                "glossary_enabled": True,
            },
        )
        smoke_id = response.json()["smoke_test_id"]
        status = client.get(f"/api/smoke/{smoke_id}").json()

    assert response.status_code == 200
    assert status["provider_metrics"]["glossary"]["enabled"] is True
    assert "original_text_normalized" in status["results"]


def test_compare_run_can_enable_glossary(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'compare-glossary.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        glossary = client.post("/api/glossaries/defaults/programming-ru").json()
        response = client.post(
            "/api/compare/run",
            json={
                "audio_mode": "mock_chunks",
                "stt_providers": ["mock"],
                "translation_provider": "mock",
                "target_languages": ["kk"],
                "glossary_id": glossary["id"],
                "glossary_enabled": True,
            },
        )
        comparison = client.get(f"/api/compare/{response.json()['comparison_id']}").json()

    assert response.status_code == 200
    assert comparison["summary"]["config_snapshot"]["glossary_enabled"] is True
    assert comparison["results"][0]["glossary"]["enabled"] is True


class OneChunkSource(AudioSource):
    name = "one_chunk"

    def __init__(self, text: str) -> None:
        self.text = text

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        yield AudioChunk(
            data=self.text.encode("utf-8"),
            lesson_id="lesson_glossary",
            source=self.name,
            sample_rate=16000,
            channels=1,
            format="L16",
        )

    async def close(self) -> None:
        return None


class EchoTranslator(TranslationProvider):
    name = "echo"

    async def translate_many(self, text: str, source_language: str, target_languages: list[str]) -> dict[str, str]:
        return {language: f"[{language}] {text}" for language in target_languages}
