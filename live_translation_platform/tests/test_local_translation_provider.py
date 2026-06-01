import asyncio
from datetime import datetime

import pytest

from app.audio.mock_audio_source import MockAudioSource
from app.realtime.audio_pipeline import AudioPipeline
from app.stt.base import STTEvent
from app.stt.mock_stt import MockSTT
from app.translation.base import create_translation_provider
from app.translation.local_engines.base import LocalTranslationConfigurationError
from app.translation.local_engines.madlad import MadladTranslationEngine
from app.translation.local_engines.m2m100_ct2 import (
    M2M100Ct2TranslationEngine,
    PROJECT_TO_M2M100_LANG,
    normalize_m2m100_target_language,
    resolve_m2m100_language_code,
)
from app.translation.local_engines.model_loader import (
    LocalModelInferenceError,
    PROJECT_TO_TILMASH_LANG,
    TilmashBackendConfig,
    TransformersTilmashBackend,
    resolve_tilmash_language_code,
)
from app.translation.local_engines.tilmash import TilmashTranslationEngine
from app.translation.local_provider import LocalTranslationProvider
from app.translation.mock_translator import MockTranslator


def test_translation_factory_selects_local_provider():
    provider = create_translation_provider("local")

    assert isinstance(provider, LocalTranslationProvider)
    assert provider.name == "local"


@pytest.mark.asyncio
async def test_local_translation_routes_target_languages_to_expected_engines():
    tilmash = RecordingEngine("tilmash", {"kk", "uz"})
    madlad = RecordingEngine("madlad400", {"zh-Hans"})
    m2m100 = RecordingEngine("m2m100_ct2", {"uz", "zh-Hans"})
    provider = LocalTranslationProvider(
        enabled=True,
        routing_enabled=True,
        route_table={"kk": "tilmash", "uz": "m2m100_ct2", "zh-Hans": "m2m100_ct2"},
        engines={"tilmash": tilmash, "madlad400": madlad, "m2m100_ct2": m2m100},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.5,
    )

    translations = await provider.translate_many("Привет", "ru-RU", ["kk", "uz", "zh-Hans"])

    assert translations == {
        "kk": "tilmash:kk:Привет",
        "uz": "m2m100_ct2:uz:Привет",
        "zh-Hans": "m2m100_ct2:zh-Hans:Привет",
    }
    assert tilmash.calls == [("ru-RU", "kk")]
    assert madlad.calls == []
    assert m2m100.calls == [("ru-RU", "uz"), ("ru-RU", "zh-Hans")]
    assert provider.status()["route_table"] == {"kk": "tilmash", "uz": "m2m100_ct2", "zh-Hans": "m2m100_ct2"}


@pytest.mark.asyncio
async def test_local_translation_timeout_falls_back_without_changing_dict_shape():
    slow_tilmash = RecordingEngine("tilmash", {"kk"}, delay_seconds=0.05)
    provider = LocalTranslationProvider(
        enabled=True,
        routing_enabled=True,
        engines={"tilmash": slow_tilmash},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.01,
    )

    translations = await provider.translate_many("Медленный текст", "ru-RU", ["kk"])

    assert translations == {"kk": "[kk mock] Медленный текст"}
    assert provider.translation_errors_count == 1
    assert provider.translation_fallbacks_count == 1
    assert "timeout" in (provider.translation_last_error or "").lower()


@pytest.mark.asyncio
async def test_tilmash_missing_model_path_reports_clean_not_configured_error():
    engine = TilmashTranslationEngine(enabled=True, model_path="", device="cpu", timeout_seconds=0.1)

    status = engine.status()

    assert status["ready"] is False
    assert status["status"] == "not_configured"
    assert "TILMASH_MODEL_PATH" in status["missing"]
    with pytest.raises(LocalTranslationConfigurationError, match="TILMASH_MODEL_PATH"):
        await engine.translate("Привет", "ru-RU", "kk")


def test_tilmash_disabled_reports_disabled_without_missing_model_path():
    engine = TilmashTranslationEngine(enabled=False, model_path="", device="cpu")

    status = engine.status()

    assert status["ready"] is False
    assert status["status"] == "disabled"
    assert status["missing"] == []
    assert status["enabled"] is False
    assert status["loaded"] is False


def test_tilmash_does_not_load_backend_at_construction():
    backend = FakeTilmashBackend({"kk": "Сәлем"})

    TilmashTranslationEngine(enabled=True, model_path="C:/models/tilmash", backend=backend)

    assert backend.load_calls == 0


def test_tilmash_project_language_mapping_uses_explicit_nllb_codes():
    assert PROJECT_TO_TILMASH_LANG["ru"] == "rus_Cyrl"
    assert PROJECT_TO_TILMASH_LANG["ru-RU"] == "rus_Cyrl"
    assert PROJECT_TO_TILMASH_LANG["kk"] == "kaz_Cyrl"
    assert PROJECT_TO_TILMASH_LANG["kk-KZ"] == "kaz_Cyrl"
    assert PROJECT_TO_TILMASH_LANG["en"] == "eng_Latn"
    assert PROJECT_TO_TILMASH_LANG["tr"] == "tur_Latn"
    assert resolve_tilmash_language_code("kk") == "kaz_Cyrl"


def test_tilmash_backend_sets_source_language_and_forced_bos_token_id_for_kk():
    tokenizer = FakeNllbTokenizer({"rus_Cyrl": 256147, "kaz_Cyrl": 256089, "tur_Latn": 256184})
    model = RecordingSeq2SeqModel()
    backend = TransformersTilmashBackend(TilmashBackendConfig(model_path="C:/models/tilmash", max_new_tokens=96, num_beams=2))
    backend.tokenizer = tokenizer
    backend.model = model
    backend.actual_device = "cpu"
    backend.loaded = True

    assert backend._translate_batch_sync(["hello"], "ru-RU", "kk") == ["translated"]

    assert tokenizer.src_lang == "rus_Cyrl"
    assert tokenizer.converted_tokens == ["kaz_Cyrl"]
    assert model.generate_kwargs["forced_bos_token_id"] == 256089
    assert model.generate_kwargs["max_new_tokens"] == 96
    assert model.generate_kwargs["num_beams"] == 2
    assert "max_length" not in model.generate_kwargs
    assert model.generation_config.max_length is None


@pytest.mark.asyncio
async def test_tilmash_unsupported_uzbek_token_falls_back_and_marks_status():
    backend = UnsupportedTargetTilmashBackend("uz", "uzn_Latn")
    engine = TilmashTranslationEngine(
        enabled=True,
        model_path="C:/models/tilmash",
        device="cpu",
        backend=backend,
    )
    provider = LocalTranslationProvider(
        enabled=True,
        engines={"tilmash": engine},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.5,
    )

    translations = await provider.translate_many("hello", "ru-RU", ["uz"])

    assert translations == {"uz": "[uz mock] hello"}
    status = engine.status()
    assert "uz" in status["unsupported_target_languages"]
    assert "unsupported" in (status["last_error"] or "")


@pytest.mark.asyncio
async def test_tilmash_fake_backend_returns_plain_kk_and_uz_text():
    backend = FakeTilmashBackend({"kk": "Сәлем, сынып", "uz": "Salom, sinf"})
    engine = TilmashTranslationEngine(
        enabled=True,
        model_path="C:/models/tilmash",
        tokenizer_path="C:/models/tilmash-tokenizer",
        device="cpu",
        backend=backend,
    )

    kk = await engine.translate("Здравствуйте, класс", "ru-RU", "kk")
    uz = await engine.translate("Здравствуйте, класс", "ru-RU", "uz")

    assert kk == "Сәлем, сынып"
    assert uz == "Salom, sinf"
    assert backend.load_calls == 1
    assert backend.calls == [("ru-RU", "kk", ["Здравствуйте, класс"]), ("ru-RU", "uz", ["Здравствуйте, класс"])]
    status = engine.status()
    assert status["ready"] is True
    assert status["loaded"] is True
    assert status["last_error"] is None
    assert status["request_count"] == 2
    assert status["average_latency_ms"] >= 0


@pytest.mark.asyncio
async def test_tilmash_inference_exception_is_sanitized_in_status_and_provider_error():
    backend = ExplodingTilmashBackend("failed loading C:/private/models/tilmash/checkpoint.bin with token secret-value")
    engine = TilmashTranslationEngine(
        enabled=True,
        model_path="C:/private/models/tilmash",
        tokenizer_path="C:/private/models/tilmash/tokenizer",
        device="cpu",
        backend=backend,
    )
    provider = LocalTranslationProvider(
        enabled=True,
        engines={"tilmash": engine},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.5,
    )

    translations = await provider.translate_many("Ошибка", "ru-RU", ["kk"])

    assert translations == {"kk": "[kk mock] Ошибка"}
    assert "C:/private" not in (engine.status()["last_error"] or "")
    assert "secret-value" not in (engine.status()["last_error"] or "")
    assert "C:/private" not in (provider.translation_last_error or "")
    assert "secret-value" not in (provider.translation_last_error or "")


@pytest.mark.asyncio
async def test_tilmash_load_failure_marks_readiness_error_without_private_path():
    backend = LoadFailingTilmashBackend("cannot open C:/private/models/tilmash/model.bin token secret-value")
    engine = TilmashTranslationEngine(
        enabled=True,
        model_path="C:/private/models/tilmash",
        device="cpu",
        backend=backend,
    )

    with pytest.raises(LocalTranslationConfigurationError):
        await engine.translate("Ошибка", "ru-RU", "kk")

    status = engine.status()
    assert status["ready"] is False
    assert status["status"] == "error"
    assert "C:/private" not in (status["last_error"] or "")
    assert "secret-value" not in (status["last_error"] or "")


@pytest.mark.asyncio
async def test_tilmash_timeout_triggers_local_provider_fallback_and_timeout_count():
    backend = FakeTilmashBackend({"kk": "too slow"}, delay_seconds=0.05)
    engine = TilmashTranslationEngine(
        enabled=True,
        model_path="C:/models/tilmash",
        device="cpu",
        timeout_seconds=0.01,
        backend=backend,
    )
    provider = LocalTranslationProvider(
        enabled=True,
        engines={"tilmash": engine},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.01,
    )

    translations = await provider.translate_many("Медленно", "ru-RU", ["kk"])

    assert translations == {"kk": "[kk mock] Медленно"}
    assert provider.translation_timeouts_count == 1
    assert engine.status()["timeout_count"] == 1


def test_madlad_disabled_reports_disabled_without_missing_model_path():
    engine = MadladTranslationEngine(enabled=False, model_path="", device="cpu")

    status = engine.status()

    assert status["ready"] is False
    assert status["status"] == "disabled"
    assert status["missing"] == []
    assert status["enabled"] is False
    assert status["loaded"] is False
    assert status["quantization"] == "8bit"


def test_m2m100_ct2_disabled_reports_disabled_without_missing_paths():
    engine = M2M100Ct2TranslationEngine(enabled=False, model_path="", tokenizer_path="", device="cpu")

    status = engine.status()

    assert status["ready"] is False
    assert status["status"] == "disabled"
    assert status["missing"] == []
    assert status["enabled"] is False
    assert status["loaded"] is False
    assert status["compute_type"] == "int8"
    assert status["model_size"] == "418m"
    assert status["supported_target_languages"] == ["uz", "zh-Hans"]


def test_m2m100_1_2b_ct2_disabled_reports_disabled_for_uz_only():
    engine = M2M100Ct2TranslationEngine(
        enabled=False,
        model_path="",
        tokenizer_path="",
        device="cpu",
        default_size="1.2b",
        supported_targets="uz",
        model_path_env="M2M100_1_2B_CT2_MODEL_PATH",
        tokenizer_path_env="M2M100_1_2B_CT2_TOKENIZER_PATH",
        name="m2m100_1_2b_ct2",
    )

    status = engine.status()

    assert status["ready"] is False
    assert status["status"] == "disabled"
    assert status["missing"] == []
    assert status["engine"] == "m2m100_1_2b_ct2"
    assert status["model_size"] == "1.2b"
    assert status["supported_target_languages"] == ["uz"]


@pytest.mark.asyncio
async def test_m2m100_ct2_missing_model_path_reports_not_configured():
    engine = M2M100Ct2TranslationEngine(enabled=True, model_path="", tokenizer_path="", device="cpu")

    status = engine.status()

    assert status["ready"] is False
    assert status["status"] == "not_configured"
    assert "M2M100_CT2_MODEL_PATH" in status["missing"]
    assert "M2M100_CT2_TOKENIZER_PATH" in status["missing"]
    with pytest.raises(LocalTranslationConfigurationError, match="M2M100_CT2_MODEL_PATH"):
        await engine.translate("hello", "ru-RU", "uz")


@pytest.mark.asyncio
async def test_m2m100_1_2b_ct2_missing_paths_report_not_configured():
    engine = M2M100Ct2TranslationEngine(
        enabled=True,
        model_path="",
        tokenizer_path="",
        device="cpu",
        default_size="1.2b",
        supported_targets="uz",
        model_path_env="M2M100_1_2B_CT2_MODEL_PATH",
        tokenizer_path_env="M2M100_1_2B_CT2_TOKENIZER_PATH",
        name="m2m100_1_2b_ct2",
    )

    status = engine.status()

    assert status["ready"] is False
    assert status["status"] == "not_configured"
    assert "M2M100_1_2B_CT2_MODEL_PATH" in status["missing"]
    assert "M2M100_1_2B_CT2_TOKENIZER_PATH" in status["missing"]
    with pytest.raises(LocalTranslationConfigurationError, match="M2M100_1_2B_CT2_MODEL_PATH"):
        await engine.translate("hello", "ru-RU", "uz")


def test_m2m100_ct2_configured_is_lazy_and_not_loaded():
    backend = FakeM2M100Backend({"uz": "Salom"})

    engine = M2M100Ct2TranslationEngine(
        enabled=True,
        model_path="C:/models/m2m100-ct2",
        tokenizer_path="C:/models/m2m100-hf",
        device="cpu",
        compute_type="int8",
        default_size="418m",
        load_on_startup=False,
        backend=backend,
    )

    status = engine.status()
    assert status["ready"] is True
    assert status["status"] == "configured"
    assert status["loaded"] is False
    assert status["device"] == "cpu"
    assert status["compute_type"] == "int8"
    assert status["model_size"] == "418m"
    assert backend.load_calls == 0


def test_m2m100_language_mapping_is_explicit_and_aliases_normalize():
    assert PROJECT_TO_M2M100_LANG == {
        "ru": "ru",
        "ru-RU": "ru",
        "uz": "uz",
        "uz-UZ": "uz",
        "zh": "zh",
        "zh-CN": "zh",
        "zh-Hans": "zh",
    }
    assert resolve_m2m100_language_code("ru-RU") == "ru"
    assert resolve_m2m100_language_code("uz-UZ") == "uz"
    assert resolve_m2m100_language_code("zh-Hans") == "zh"
    assert normalize_m2m100_target_language("uz-UZ") == "uz"
    assert normalize_m2m100_target_language("zh") == "zh-Hans"
    assert normalize_m2m100_target_language("zh-CN") == "zh-Hans"
    assert normalize_m2m100_target_language("zh-Hans") == "zh-Hans"
    with pytest.raises(LocalTranslationConfigurationError):
        resolve_m2m100_language_code("kk")


@pytest.mark.asyncio
async def test_m2m100_ct2_fake_backend_returns_plain_text_and_normalizes_targets():
    backend = FakeM2M100Backend({"uz": "Salom, sinf", "zh-Hans": "大家好"})
    engine = M2M100Ct2TranslationEngine(
        enabled=True,
        model_path="C:/models/m2m100-ct2",
        tokenizer_path="C:/models/m2m100-hf",
        device="cpu",
        backend=backend,
    )

    uz = await engine.translate("Здравствуйте, класс", "ru-RU", "uz-UZ")
    zh = await engine.translate("Здравствуйте, класс", "ru-RU", "zh-CN")

    assert uz == "Salom, sinf"
    assert zh == "大家好"
    assert backend.load_calls == 1
    assert backend.calls == [
        ("ru-RU", "uz", ["Здравствуйте, класс"]),
        ("ru-RU", "zh-Hans", ["Здравствуйте, класс"]),
    ]
    status = engine.status()
    assert status["status"] == "loaded"
    assert status["request_count"] == 2
    assert status["last_error"] is None


@pytest.mark.asyncio
async def test_m2m100_ct2_timeout_triggers_provider_fallback_and_timeout_count():
    engine = M2M100Ct2TranslationEngine(
        enabled=True,
        model_path="C:/models/m2m100-ct2",
        tokenizer_path="C:/models/m2m100-hf",
        device="cpu",
        timeout_seconds=0.01,
        backend=FakeM2M100Backend({"uz": "too slow"}, delay_seconds=0.05),
    )
    provider = LocalTranslationProvider(
        enabled=True,
        routing_enabled=True,
        route_table={"uz": "m2m100_ct2"},
        engines={"m2m100_ct2": engine},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.01,
    )

    translations = await provider.translate_many("slow caption", "ru-RU", ["uz"])

    assert translations == {"uz": "[uz mock] slow caption"}
    assert provider.translation_timeouts_count == 1
    assert provider.translation_fallbacks_count == 1
    assert engine.status()["timeout_count"] == 1


@pytest.mark.asyncio
async def test_route_alias_can_select_m2m100_1_2b_for_uz_and_keep_418m_for_zh_hans():
    m2m100_418m = M2M100Ct2TranslationEngine(
        enabled=True,
        model_path="C:/models/m2m100-418m-ct2",
        tokenizer_path="C:/models/m2m100-418m-hf",
        device="cpu",
        default_size="418m",
        supported_targets="uz,zh-Hans",
        backend=FakeM2M100Backend({"zh-Hans": "大家好"}),
    )
    m2m100_1_2b = M2M100Ct2TranslationEngine(
        enabled=True,
        model_path="C:/models/m2m100-1-2b-ct2",
        tokenizer_path="C:/models/m2m100-1-2b-hf",
        device="cpu",
        default_size="1.2b",
        supported_targets="uz",
        model_path_env="M2M100_1_2B_CT2_MODEL_PATH",
        tokenizer_path_env="M2M100_1_2B_CT2_TOKENIZER_PATH",
        name="m2m100_1_2b_ct2",
        backend=FakeM2M100Backend({"uz": "Salom"}),
    )
    provider = LocalTranslationProvider(
        enabled=True,
        routing_enabled=True,
        route_table={"uz": "m2m100_1_2b_ct2", "zh-Hans": "m2m100_ct2"},
        engines={"m2m100_ct2": m2m100_418m, "m2m100_1_2b_ct2": m2m100_1_2b},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.5,
    )

    translations = await provider.translate_many("hello", "ru-RU", ["uz", "zh-Hans"])

    assert translations == {"uz": "Salom", "zh-Hans": "大家好"}
    assert m2m100_1_2b.status()["request_count"] == 1
    assert m2m100_418m.status()["request_count"] == 1


@pytest.mark.asyncio
async def test_experimental_uzbek_1_2b_route_does_not_fallback_to_mock_when_missing():
    m2m100_1_2b = M2M100Ct2TranslationEngine(
        enabled=True,
        model_path="",
        tokenizer_path="",
        device="cpu",
        default_size="1.2b",
        supported_targets="uz",
        model_path_env="M2M100_1_2B_CT2_MODEL_PATH",
        tokenizer_path_env="M2M100_1_2B_CT2_TOKENIZER_PATH",
        name="m2m100_1_2b_ct2",
    )
    provider = LocalTranslationProvider(
        enabled=True,
        routing_enabled=True,
        route_table={"uz": "m2m100_1_2b_ct2"},
        engines={"m2m100_1_2b_ct2": m2m100_1_2b},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.5,
    )

    translations = await provider.translate_many("caption", "ru-RU", ["uz"])

    assert translations == {"uz": "Translation not configured for uz"}
    assert not translations["uz"].startswith("[uz mock]")
    status = provider.status()
    assert status["metrics"]["fallback_count"] == 0
    assert status["route_status_by_language"]["uz"]["engine"] == "m2m100_1_2b_ct2"
    assert status["route_status_by_language"]["uz"]["status"] == "not_configured"
    assert status["route_status_by_language"]["uz"]["experimental"] is True
    assert status["route_status_by_language"]["uz"]["production_ready"] is False


def test_provider_status_reports_experimental_uzbek_translation_route():
    from app.config import Settings
    from app.smoke.provider_status import provider_status

    settings = Settings(
        translation_provider="local",
        local_translation_enabled=True,
        local_translation_route_kk="tilmash",
        local_translation_route_zh="m2m100_ct2",
        local_translation_route_uz="m2m100_1_2b_ct2",
        m2m100_1_2b_ct2_enabled=True,
        m2m100_1_2b_ct2_model_path="C:/models/m2m100-1-2b-ct2",
        m2m100_1_2b_ct2_tokenizer_path="C:/models/m2m100-1-2b-hf",
    )

    status = provider_status(settings)
    uz = status["translation"]["local"]["route_status_by_language"]["uz"]

    assert uz["route"] == "m2m100_1_2b_ct2"
    assert uz["status"] == "degraded"
    assert uz["experimental"] is True
    assert uz["production_ready"] is False


@pytest.mark.asyncio
async def test_m2m100_ct2_error_is_sanitized_in_status_and_provider_error():
    backend = ExplodingM2M100Backend("failed C:/private/models/m2m100/model.bin with api_key secret-value and very long text " + ("x" * 800))
    engine = M2M100Ct2TranslationEngine(
        enabled=True,
        model_path="C:/private/models/m2m100-ct2",
        tokenizer_path="C:/private/models/m2m100-hf",
        device="cpu",
        backend=backend,
    )
    provider = LocalTranslationProvider(
        enabled=True,
        routing_enabled=True,
        route_table={"zh-Hans": "m2m100_ct2"},
        engines={"m2m100_ct2": engine},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.5,
    )

    translations = await provider.translate_many("error caption", "ru-RU", ["zh-Hans"])

    assert translations == {"zh-Hans": "[zh-Hans mock] error caption"}
    assert "C:/private" not in (engine.status()["last_error"] or "")
    assert "secret-value" not in (engine.status()["last_error"] or "")
    assert len(engine.status()["last_error"] or "") <= 500
    assert "C:/private" not in (provider.translation_last_error or "")
    assert "secret-value" not in (provider.translation_last_error or "")


@pytest.mark.asyncio
async def test_route_engine_not_configured_uses_fallback_and_marks_degraded_status():
    m2m100 = M2M100Ct2TranslationEngine(enabled=False, model_path="", tokenizer_path="", device="cpu")
    provider = LocalTranslationProvider(
        enabled=True,
        routing_enabled=True,
        route_table={"uz": "m2m100_ct2"},
        engines={"m2m100_ct2": m2m100},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.5,
    )

    translations = await provider.translate_many("caption", "ru-RU", ["uz"])

    status = provider.status()
    assert translations == {"uz": "[uz mock] caption"}
    assert status["status"] == "degraded"
    assert status["metrics"]["fallback_count"] == 1
    assert status["route_table"] == {"uz": "m2m100_ct2"}


@pytest.mark.asyncio
async def test_disabled_translation_route_returns_disabled_result_without_fallback():
    tilmash = RecordingEngine("tilmash", {"kk", "uz"})
    madlad = RecordingEngine("madlad400", {"uz", "zh-Hans"})
    m2m100 = RecordingEngine("m2m100_ct2", {"uz", "zh-Hans"})
    provider = LocalTranslationProvider(
        enabled=True,
        routing_enabled=True,
        route_table={"uz": "disabled"},
        engines={"tilmash": tilmash, "madlad400": madlad, "m2m100_ct2": m2m100},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.5,
    )

    translations = await provider.translate_many("caption", "ru-RU", ["uz"])

    assert translations == {"uz": "Translation disabled for uz"}
    assert not translations["uz"].startswith("[uz mock]")
    assert tilmash.calls == []
    assert madlad.calls == []
    assert m2m100.calls == []
    status = provider.status()
    assert status["route_table"] == {"uz": "disabled"}
    assert status["route_status_by_language"]["uz"]["status"] == "disabled"
    assert status["metrics"]["fallback_count"] == 0


@pytest.mark.asyncio
async def test_disabled_uzbek_translation_does_not_publish_fake_caption_text():
    published = []
    provider = LocalTranslationProvider(
        enabled=True,
        routing_enabled=True,
        route_table={"kk": "tilmash", "uz": "disabled"},
        engines={"tilmash": RecordingEngine("tilmash", {"kk", "uz"})},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.5,
    )
    pipeline = AudioPipeline(
        lesson_id="lesson_local_translation_disabled",
        meeting_id="meeting-1",
        source=MockAudioSource(interval_seconds=0.01, max_chunks=0),
        stt=MockSTT(),
        translator=provider,
        target_languages=["kk", "uz"],
        translate_partials=False,
        publish=lambda payload: _capture_async(published, payload),
        save_caption=lambda payload: None,
        save_metric=lambda payload: None,
        publish_debug=lambda payload: None,
    )

    await pipeline._handle_event(
        STTEvent(
            text="Final caption",
            is_partial=False,
            is_final=True,
            language="ru-RU",
            confidence=None,
            provider="mock",
            timestamp=datetime.utcnow(),
            speaker_id="teacher",
            raw={"event_id": "final-disabled-uz"},
            audio_received_at=datetime.utcnow(),
        )
    )

    assert published[0]["translations"]["kk"] == "tilmash:kk:Final caption"
    assert published[0]["translations"]["uz"] == "Translation disabled for uz"
    assert not published[0]["translations"]["uz"].startswith("[uz mock]")
    assert provider.status()["metrics"]["fallback_count"] == 0


def test_provider_status_reports_disabled_translation_route():
    from app.config import Settings
    from app.smoke.provider_status import provider_status

    settings = Settings(
        translation_provider="local",
        local_translation_enabled=True,
        local_translation_route_kk="tilmash",
        local_translation_route_zh="m2m100_ct2",
        local_translation_route_uz="disabled",
    )

    status = provider_status(settings)

    assert status["translation"]["local"]["route_status_by_language"]["uz"]["status"] == "disabled"


@pytest.mark.asyncio
async def test_madlad_missing_model_or_server_reports_clean_not_configured_error():
    engine = MadladTranslationEngine(enabled=True, model_path="", server_url="", device="cpu", timeout_seconds=0.1)

    status = engine.status()

    assert status["ready"] is False
    assert status["status"] == "not_configured"
    assert "MADLAD_MODEL_PATH" in status["missing"]
    with pytest.raises(LocalTranslationConfigurationError, match="MADLAD_MODEL_PATH"):
        await engine.translate("hello", "ru-RU", "zh-Hans")


def test_madlad_configured_lazy_status_and_no_backend_load_at_construction():
    backend = FakeMadladBackend({"zh-Hans": "你好"})

    engine = MadladTranslationEngine(
        enabled=True,
        model_path="C:/models/madlad",
        tokenizer_path="C:/models/madlad-tokenizer",
        device="cpu",
        dtype="float32",
        max_batch_size=4,
        load_on_startup=False,
        backend=backend,
    )

    status = engine.status()
    assert status["ready"] is True
    assert status["status"] == "configured"
    assert status["loaded"] is False
    assert status["device"] == "cpu"
    assert status["dtype"] == "float32"
    assert status["supported_language_pairs"] == ["ru->zh-Hans"]
    assert backend.load_calls == 0


@pytest.mark.asyncio
async def test_madlad_fake_backend_returns_plain_zh_hans_text():
    backend = FakeMadladBackend({"zh-Hans": "请打开代码编辑器"})
    engine = MadladTranslationEngine(
        enabled=True,
        model_path="C:/models/madlad",
        tokenizer_path="C:/models/madlad-tokenizer",
        device="cpu",
        backend=backend,
    )

    text = await engine.translate("open the code editor", "ru-RU", "zh-Hans")

    assert text == "请打开代码编辑器"
    assert backend.load_calls == 1
    assert backend.calls == [("ru-RU", "zh-Hans", ["open the code editor"])]
    status = engine.status()
    assert status["ready"] is True
    assert status["loaded"] is True
    assert status["request_count"] == 1
    assert status["last_error"] is None


@pytest.mark.asyncio
async def test_local_routing_uses_madlad_for_zh_hans_and_tilmash_for_kk_uz():
    tilmash = RecordingEngine("tilmash", {"kk", "uz"})
    madlad = MadladTranslationEngine(
        enabled=True,
        model_path="C:/models/madlad",
        device="cpu",
        backend=FakeMadladBackend({"zh-Hans": "中文翻译"}),
    )
    provider = LocalTranslationProvider(
        enabled=True,
        routing_enabled=True,
        engines={"tilmash": tilmash, "madlad400": madlad},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.5,
    )

    translations = await provider.translate_many("short caption", "ru-RU", ["kk", "uz", "zh-Hans"])

    assert translations == {
        "kk": "tilmash:kk:short caption",
        "uz": "tilmash:uz:short caption",
        "zh-Hans": "中文翻译",
    }
    assert tilmash.calls == [("ru-RU", "kk"), ("ru-RU", "uz")]


@pytest.mark.asyncio
async def test_madlad_timeout_triggers_local_provider_fallback_and_timeout_count():
    engine = MadladTranslationEngine(
        enabled=True,
        model_path="C:/models/madlad",
        device="cpu",
        timeout_seconds=0.01,
        backend=FakeMadladBackend({"zh-Hans": "too slow"}, delay_seconds=0.05),
    )
    provider = LocalTranslationProvider(
        enabled=True,
        routing_enabled=True,
        engines={"madlad400": engine},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.01,
    )

    translations = await provider.translate_many("slow caption", "ru-RU", ["zh-Hans"])

    assert translations == {"zh-Hans": "[zh-Hans mock] slow caption"}
    assert provider.translation_timeouts_count == 1
    assert provider.translation_fallbacks_count == 1
    assert engine.status()["timeout_count"] == 1


@pytest.mark.asyncio
async def test_madlad_inference_exception_is_sanitized_in_status_and_provider_error():
    backend = ExplodingMadladBackend("failed loading C:/private/models/madlad/model.bin with token secret-value")
    engine = MadladTranslationEngine(
        enabled=True,
        model_path="C:/private/models/madlad",
        tokenizer_path="C:/private/models/madlad/tokenizer",
        server_url="http://127.0.0.1:9999/translate",
        device="cpu",
        backend=backend,
    )
    provider = LocalTranslationProvider(
        enabled=True,
        routing_enabled=True,
        engines={"madlad400": engine},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.5,
    )

    translations = await provider.translate_many("error caption", "ru-RU", ["zh-Hans"])

    assert translations == {"zh-Hans": "[zh-Hans mock] error caption"}
    assert "C:/private" not in (engine.status()["last_error"] or "")
    assert "secret-value" not in (engine.status()["last_error"] or "")
    assert "127.0.0.1:9999" not in (engine.status()["last_error"] or "")
    assert "C:/private" not in (provider.translation_last_error or "")
    assert "secret-value" not in (provider.translation_last_error or "")


@pytest.mark.asyncio
async def test_madlad_load_failure_marks_readiness_error_without_private_path():
    backend = LoadFailingMadladBackend("cannot open C:/private/models/madlad/model.bin password hidden-value")
    engine = MadladTranslationEngine(
        enabled=True,
        model_path="C:/private/models/madlad",
        tokenizer_path="C:/private/models/madlad/tokenizer",
        device="cpu",
        backend=backend,
    )

    with pytest.raises(LocalTranslationConfigurationError):
        await engine.translate("load failure", "ru-RU", "zh-Hans")

    status = engine.status()
    assert status["ready"] is False
    assert status["status"] == "error"
    assert "C:/private" not in (status["last_error"] or "")
    assert "hidden-value" not in (status["last_error"] or "")


@pytest.mark.asyncio
async def test_local_translation_keeps_final_caption_payload_shape_unchanged():
    published = []
    provider = LocalTranslationProvider(
        enabled=True,
        routing_enabled=True,
        engines={"tilmash": RecordingEngine("tilmash", {"kk"})},
        fallback_provider=MockTranslator(),
        timeout_seconds=0.5,
    )
    pipeline = AudioPipeline(
        lesson_id="lesson_local_translation",
        meeting_id="meeting-1",
        source=MockAudioSource(interval_seconds=0.01, max_chunks=0),
        stt=MockSTT(),
        translator=provider,
        target_languages=["kk"],
        translate_partials=False,
        publish=lambda payload: _capture_async(published, payload),
        save_caption=lambda payload: None,
        save_metric=lambda payload: None,
        publish_debug=lambda payload: None,
    )

    await pipeline._handle_event(
        STTEvent(
            text="Финальная подпись",
            is_partial=False,
            is_final=True,
            language="ru-RU",
            confidence=None,
            provider="mock",
            timestamp=datetime.utcnow(),
            speaker_id="teacher",
            raw={"event_id": "final-1"},
            audio_received_at=datetime.utcnow(),
        )
    )

    assert len(published) == 1
    payload = published[0]
    assert set(payload) == {
        "event",
        "lesson_id",
        "meeting_id",
        "provider",
        "audio_source",
        "pipeline_id",
        "caption_id",
        "segment_id",
        "text_hash",
        "provider_event_id",
        "audio",
        "source_language",
        "original_text",
        "original_text_raw",
        "original_text_normalized",
        "translations",
        "glossary",
        "is_partial",
        "is_final",
        "speaker",
        "timestamps",
        "latency_ms",
        "pipeline_queue_size",
        "dropped_chunks",
        "commit_reason",
        "segment_duration_ms",
        "sequence",
    }
    assert payload["provider"] == {"stt": "mock", "translator": "local"}
    assert payload["translations"] == {"kk": "tilmash:kk:Финальная подпись"}


class RecordingEngine:
    def __init__(self, name: str, targets: set[str], delay_seconds: float = 0.0) -> None:
        self.name = name
        self.supported_targets = targets
        self.delay_seconds = delay_seconds
        self.calls: list[tuple[str, str]] = []

    def supports(self, source_language: str, target_language: str) -> bool:
        return source_language.startswith("ru") and target_language in self.supported_targets

    def status(self) -> dict:
        return {"ready": True, "status": "ready", "missing": []}

    async def translate(self, text: str, source_language: str, target_language: str) -> str:
        self.calls.append((source_language, target_language))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return f"{self.name}:{target_language}:{text}"


async def _capture_async(items, item):
    items.append(item)


class FakeTilmashBackend:
    def __init__(self, translations: dict[str, str], delay_seconds: float = 0.0) -> None:
        self.translations = translations
        self.delay_seconds = delay_seconds
        self.loaded = False
        self.load_calls = 0
        self.calls: list[tuple[str, str, list[str]]] = []

    async def load(self) -> None:
        self.load_calls += 1
        self.loaded = True

    async def translate_batch(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        self.calls.append((source_language, target_language, list(texts)))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return [self.translations[target_language] for _ in texts]


class ExplodingTilmashBackend(FakeTilmashBackend):
    def __init__(self, message: str) -> None:
        super().__init__({})
        self.message = message

    async def translate_batch(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        raise RuntimeError(self.message)


class LoadFailingTilmashBackend(FakeTilmashBackend):
    def __init__(self, message: str) -> None:
        super().__init__({})
        self.message = message

    async def load(self) -> None:
        self.load_calls += 1
        raise RuntimeError(self.message)


class UnsupportedTargetTilmashBackend(FakeTilmashBackend):
    def __init__(self, target_language: str, tokenizer_code: str) -> None:
        super().__init__({})
        self.target_language = target_language
        self.tokenizer_code = tokenizer_code

    async def translate_batch(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        self.calls.append((source_language, target_language, list(texts)))
        raise LocalModelInferenceError(
            f"Tilmash target language {self.target_language} is unsupported by tokenizer: missing {self.tokenizer_code}"
        )


class FakeTokenBatch(dict):
    def to(self, device: str):
        self["device"] = device
        return self


class FakeNllbTokenizer:
    unk_token_id = 3

    def __init__(self, token_ids: dict[str, int]) -> None:
        self.token_ids = token_ids
        self.src_lang = None
        self.converted_tokens: list[str] = []

    def __call__(self, texts, **kwargs):
        self.texts = list(texts)
        self.call_kwargs = dict(kwargs)
        return FakeTokenBatch({"input_ids": [[1, 2, 3]]})

    def convert_tokens_to_ids(self, token: str) -> int:
        self.converted_tokens.append(token)
        return self.token_ids.get(token, self.unk_token_id)

    def batch_decode(self, outputs, skip_special_tokens: bool = True):
        self.outputs = outputs
        self.skip_special_tokens = skip_special_tokens
        return ["translated"]


class RecordingSeq2SeqModel:
    def __init__(self) -> None:
        self.generation_config = FakeGenerationConfig()

    def generate(self, **kwargs):
        self.generate_kwargs = dict(kwargs)
        return [[42]]


class FakeGenerationConfig:
    max_length = 200


class FakeMadladBackend(FakeTilmashBackend):
    pass


class FakeM2M100Backend(FakeTilmashBackend):
    pass


class ExplodingMadladBackend(FakeMadladBackend):
    def __init__(self, message: str) -> None:
        super().__init__({})
        self.message = message

    async def translate_batch(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        raise RuntimeError(self.message)


class LoadFailingMadladBackend(FakeMadladBackend):
    def __init__(self, message: str) -> None:
        super().__init__({})
        self.message = message

    async def load(self) -> None:
        self.load_calls += 1
        raise RuntimeError(self.message)


class ExplodingM2M100Backend(FakeM2M100Backend):
    def __init__(self, message: str) -> None:
        super().__init__({})
        self.message = message

    async def translate_batch(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        raise RuntimeError(self.message)
