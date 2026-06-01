import pytest

from app.stt.mock_stt import MockSTT
from app.stt.base import create_stt_provider
from app.translation.base import create_translation_provider
from app.translation.mock_translator import MockTranslator


@pytest.mark.asyncio
async def test_mock_stt_emits_partial_and_final_events():
    provider = MockSTT()
    await provider.connect()
    await provider.send_audio(b"mock", {"text": "Сегодня мы изучим переменные в C#."})

    events = []
    async for event in provider.events():
        events.append(event)
        if event.is_final:
            break

    assert [event.is_partial for event in events] == [True, False]
    assert events[-1].text == "Сегодня мы изучим переменные в C#."
    assert events[-1].language == "ru-RU"


@pytest.mark.asyncio
async def test_mock_translator_returns_all_target_languages():
    translator = MockTranslator()
    translations = await translator.translate_many(
        "Теперь рассмотрим циклы for и while.",
        "ru-RU",
        ["kk", "uz", "zh-Hans"],
    )

    assert set(translations) == {"kk", "uz", "zh-Hans"}
    assert "for" in translations["uz"]
    assert translations["zh-Hans"]


def test_provider_factories_return_mock_implementations():
    assert create_stt_provider("mock").name == "mock"
    assert create_translation_provider("mock").name == "mock"


def test_provider_factories_return_faster_whisper_implementation():
    provider = create_stt_provider("faster_whisper", model_path="C:/models/whisper/faster-whisper-small")

    assert provider.name == "faster_whisper"


def test_provider_factories_reject_unknown_provider():
    with pytest.raises(ValueError):
        create_stt_provider("unknown")
    with pytest.raises(ValueError):
        create_translation_provider("unknown")
