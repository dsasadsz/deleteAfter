from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Protocol

import httpx


class LocalModelLoadError(RuntimeError):
    pass


class LocalModelInferenceError(RuntimeError):
    pass


class LocalTranslationBackend(Protocol):
    loaded: bool

    async def load(self) -> None:
        ...

    async def translate_batch(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        ...


@dataclass(frozen=True)
class TilmashBackendConfig:
    model_path: str = ""
    tokenizer_path: str = ""
    server_url: str = ""
    server_timeout_seconds: float = 1.5
    device: str = "cuda"
    dtype: str = "auto"
    max_batch_size: int = 8
    max_new_tokens: int = 128
    num_beams: int = 1


@dataclass(frozen=True)
class MadladBackendConfig:
    model_path: str = ""
    tokenizer_path: str = ""
    server_url: str = ""
    server_timeout_seconds: float = 4.0
    device: str = "cuda"
    dtype: str = "auto"
    quantization: str = "8bit"
    max_batch_size: int = 4


PROJECT_TO_TILMASH_LANG = {
    "ru": "rus_Cyrl",
    "ru-RU": "rus_Cyrl",
    "kk": "kaz_Cyrl",
    "kk-KZ": "kaz_Cyrl",
    "tr": "tur_Latn",
    "en": "eng_Latn",
}

_PROJECT_TO_TILMASH_LANG_NORMALIZED = {
    key.lower().replace("_", "-"): value for key, value in PROJECT_TO_TILMASH_LANG.items()
}
_TILMASH_UZBEK_PROJECT_LANGS = {"uz", "uz-uz"}
_TILMASH_UZBEK_CANDIDATES = ("uzn_Latn", "uzb_Latn")
_TILMASH_DIAGNOSTIC_CODES = (
    "rus_Cyrl",
    "kaz_Cyrl",
    "tur_Latn",
    "eng_Latn",
    "uzn_Latn",
    "uzb_Latn",
)


class TransformersTilmashBackend:
    def __init__(self, config: TilmashBackendConfig) -> None:
        self.config = config
        self.loaded = False
        self.model = None
        self.tokenizer = None
        self.actual_device = config.device
        self.warning: str | None = None

    async def load(self) -> None:
        if self.loaded:
            return
        await asyncio.to_thread(self._load_sync)

    async def translate_batch(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        await self.load()
        return await asyncio.to_thread(self._translate_batch_sync, texts, source_language, target_language)

    def _load_sync(self) -> None:
        try:
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except Exception as exc:
            raise LocalModelLoadError("Python package transformers is required for local Tilmash model loading.") from exc
        try:
            import torch
        except Exception:
            torch = None

        tokenizer_path = self.config.tokenizer_path or self.config.model_path
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, local_files_only=True)
            kwargs = {"local_files_only": True}
            if torch is not None:
                dtype = _torch_dtype(torch, self.config.dtype)
                if dtype is not None:
                    kwargs["torch_dtype"] = dtype
            self.model = AutoModelForSeq2SeqLM.from_pretrained(self.config.model_path, **kwargs)
            if torch is not None and self.config.device.startswith("cuda") and not torch.cuda.is_available():
                self.actual_device = "cpu"
                self.warning = "CUDA requested but unavailable; using CPU degraded mode."
            else:
                self.actual_device = self.config.device
            if hasattr(self.model, "to"):
                self.model.to(self.actual_device)
            if hasattr(self.model, "eval"):
                self.model.eval()
            _disable_generation_max_length_warning(self.model, self.config.max_new_tokens)
            self.loaded = True
        except Exception as exc:
            raise LocalModelLoadError(str(exc)) from exc

    def _translate_batch_sync(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        if self.model is None or self.tokenizer is None:
            raise LocalModelInferenceError("Tilmash model is not loaded.")
        try:
            source_code = resolve_tilmash_language_code(source_language, tokenizer=self.tokenizer)
            _set_tokenizer_language(self.tokenizer, "src_lang", source_code)
            prepared = [_prepare_input(text, target_language) for text in texts]
            inputs = self.tokenizer(
                prepared,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=256,
            )
            if hasattr(inputs, "to"):
                inputs = inputs.to(self.actual_device)
            generation_kwargs = {
                "max_new_tokens": max(1, int(self.config.max_new_tokens or 128)),
                "num_beams": max(1, int(self.config.num_beams or 1)),
                "do_sample": False,
            }
            _disable_generation_max_length_warning(self.model, generation_kwargs["max_new_tokens"])
            forced_bos_token_id = _forced_bos_token_id(self.tokenizer, target_language)
            generation_kwargs["forced_bos_token_id"] = forced_bos_token_id
            outputs = self.model.generate(**inputs, **generation_kwargs)
            decoded = self.tokenizer.batch_decode(outputs, skip_special_tokens=True)
            return [_plain_translation_text(item) for item in decoded]
        except Exception as exc:
            raise LocalModelInferenceError(str(exc)) from exc

    def language_status(self) -> dict:
        return tilmash_language_diagnostics(self.tokenizer)


class HTTPTilmashBackend:
    def __init__(self, config: TilmashBackendConfig) -> None:
        self.config = config
        self.loaded = False

    async def load(self) -> None:
        self.loaded = True

    async def translate_batch(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        await self.load()
        try:
            async with httpx.AsyncClient(timeout=self.config.server_timeout_seconds) as client:
                response = await client.post(
                    self.config.server_url,
                    json={
                        "texts": texts,
                        "source_language": source_language,
                        "target_language": target_language,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            raise LocalModelInferenceError(f"Tilmash HTTP server error: {exc.__class__.__name__}") from exc
        results = _translations_from_http_payload(payload, target_language)
        if len(results) != len(texts):
            raise LocalModelInferenceError("Tilmash HTTP server returned an unexpected translation count.")
        return [_plain_translation_text(item) for item in results]


def create_tilmash_backend(config: TilmashBackendConfig) -> LocalTranslationBackend:
    if config.server_url:
        return HTTPTilmashBackend(config)
    return TransformersTilmashBackend(config)


class TransformersMadladBackend:
    def __init__(self, config: MadladBackendConfig) -> None:
        self.config = config
        self.loaded = False
        self.model = None
        self.tokenizer = None
        self.actual_device = config.device
        self.warning: str | None = None

    async def load(self) -> None:
        if self.loaded:
            return
        await asyncio.to_thread(self._load_sync)

    async def translate_batch(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        await self.load()
        return await asyncio.to_thread(self._translate_batch_sync, texts, source_language, target_language)

    def _load_sync(self) -> None:
        try:
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except Exception as exc:
            raise LocalModelLoadError("Python package transformers is required for local MADLAD model loading.") from exc
        try:
            import torch
        except Exception:
            torch = None

        tokenizer_path = self.config.tokenizer_path or self.config.model_path
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, local_files_only=True)
            kwargs = {"local_files_only": True}
            quantization_kwargs = _quantization_kwargs(self.config.quantization)
            if quantization_kwargs:
                kwargs.update(quantization_kwargs)
            elif torch is not None:
                dtype = _torch_dtype(torch, self.config.dtype)
                if dtype is not None:
                    kwargs["torch_dtype"] = dtype
            self.model = AutoModelForSeq2SeqLM.from_pretrained(self.config.model_path, **kwargs)
            if torch is not None and self.config.device.startswith("cuda") and not torch.cuda.is_available():
                self.actual_device = "cpu"
                self.warning = "CUDA requested but unavailable; using CPU degraded mode."
            else:
                self.actual_device = self.config.device
            if not quantization_kwargs and hasattr(self.model, "to"):
                self.model.to(self.actual_device)
            if hasattr(self.model, "eval"):
                self.model.eval()
            self.loaded = True
        except Exception as exc:
            raise LocalModelLoadError(str(exc)) from exc

    def _translate_batch_sync(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        if self.model is None or self.tokenizer is None:
            raise LocalModelInferenceError("MADLAD model is not loaded.")
        try:
            prepared = [_prepare_madlad_input(text, target_language) for text in texts]
            inputs = self.tokenizer(
                prepared,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=256,
            )
            if hasattr(inputs, "to"):
                inputs = inputs.to(self.actual_device)
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=128,
                num_beams=1,
                do_sample=False,
            )
            decoded = self.tokenizer.batch_decode(outputs, skip_special_tokens=True)
            return [_plain_translation_text(item) for item in decoded]
        except Exception as exc:
            raise LocalModelInferenceError(str(exc)) from exc


class HTTPMadladBackend:
    def __init__(self, config: MadladBackendConfig) -> None:
        self.config = config
        self.loaded = False

    async def load(self) -> None:
        self.loaded = True

    async def translate_batch(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        await self.load()
        try:
            async with httpx.AsyncClient(timeout=self.config.server_timeout_seconds) as client:
                response = await client.post(
                    self.config.server_url,
                    json={
                        "texts": texts,
                        "source_language": source_language,
                        "target_language": target_language,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            raise LocalModelInferenceError(f"MADLAD HTTP server error: {exc.__class__.__name__}") from exc
        results = _translations_from_http_payload(payload, target_language, engine_name="MADLAD")
        if len(results) != len(texts):
            raise LocalModelInferenceError("MADLAD HTTP server returned an unexpected translation count.")
        return [_plain_translation_text(item) for item in results]


def create_madlad_backend(config: MadladBackendConfig) -> LocalTranslationBackend:
    if config.server_url:
        return HTTPMadladBackend(config)
    return TransformersMadladBackend(config)


def sanitize_error_message(message: object, *redactions: str) -> str:
    text = str(message or "").strip() or "unknown error"
    for value in redactions:
        if value:
            text = text.replace(value, "<redacted>")
            text = text.replace(value.replace("\\", "/"), "<redacted>")
    text = re.sub(r"[A-Za-z]:[\\/][^\s,\"')]+", "<redacted-path>", text)
    text = re.sub(r"/(?:[^/\s,\"')]+/)+[^/\s,\"')]+", "<redacted-path>", text)
    text = re.sub(r"(?i)(token|secret|key|password)[=:\s]+[^\s,\"')]+", r"\1=<redacted>", text)
    return text[:500]


def _plain_translation_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        for key in ("translation", "translated_text", "text", "output"):
            item = payload.get(key)
            if isinstance(item, str):
                text = item.strip()
                break
    text = re.sub(r"^```(?:\w+)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    text = re.sub(r"(?i)^(translation|translated text|answer)\s*:\s*", "", text).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else text


def _prepare_input(text: str, target_language: str) -> str:
    return text.strip()


def resolve_tilmash_language_code(project_language: str, tokenizer=None) -> str:
    normalized = (project_language or "").strip().replace("_", "-")
    mapped = PROJECT_TO_TILMASH_LANG.get(normalized) or _PROJECT_TO_TILMASH_LANG_NORMALIZED.get(normalized.lower())
    if mapped:
        return mapped
    if normalized.lower() in _TILMASH_UZBEK_PROJECT_LANGS:
        if tokenizer is None:
            raise LocalModelInferenceError("Tilmash Uzbek target language requires tokenizer language-token detection.")
        for candidate in _TILMASH_UZBEK_CANDIDATES:
            if _token_id_for_language_code(tokenizer, candidate) is not None:
                return candidate
        candidates = ", ".join(_TILMASH_UZBEK_CANDIDATES)
        raise LocalModelInferenceError(f"Tilmash target language uz is unsupported by tokenizer: missing {candidates}")
    raise LocalModelInferenceError(f"Tilmash language is unsupported: {project_language}")


def tilmash_language_diagnostics(tokenizer) -> dict:
    token_ids: dict[str, int | None] = {}
    if tokenizer is None:
        return {
            "tokenizer_class": None,
            "language_token_ids": token_ids,
            "unsupported_project_languages": [],
            "project_language_codes": dict(PROJECT_TO_TILMASH_LANG),
        }
    for code in _TILMASH_DIAGNOSTIC_CODES:
        token_ids[code] = _token_id_for_language_code(tokenizer, code)
    unsupported = []
    if all(token_ids.get(code) is None for code in _TILMASH_UZBEK_CANDIDATES):
        unsupported.append("uz")
    return {
        "tokenizer_class": tokenizer.__class__.__name__,
        "language_token_ids": token_ids,
        "unsupported_project_languages": unsupported,
        "project_language_codes": dict(PROJECT_TO_TILMASH_LANG),
        "uzbek_candidate_codes": list(_TILMASH_UZBEK_CANDIDATES),
    }


def _forced_bos_token_id(tokenizer, target_language: str) -> int:
    target_code = resolve_tilmash_language_code(target_language, tokenizer=tokenizer)
    token_id = _token_id_for_language_code(tokenizer, target_code)
    if token_id is None:
        raise LocalModelInferenceError(f"Tilmash target language {target_language} is unsupported by tokenizer: missing {target_code}")
    return int(token_id)


def _token_id_for_language_code(tokenizer, language_code: str) -> int | None:
    mapping = getattr(tokenizer, "lang_code_to_id", None)
    if isinstance(mapping, dict):
        value = mapping.get(language_code)
        if value is not None:
            return int(value)
    converter = getattr(tokenizer, "convert_tokens_to_ids", None)
    if not callable(converter):
        return None
    value = converter(language_code)
    if value is None:
        return None
    try:
        token_id = int(value)
    except (TypeError, ValueError):
        return None
    unknown_id = getattr(tokenizer, "unk_token_id", None)
    if unknown_id is not None and token_id == int(unknown_id):
        return None
    return token_id


def _set_tokenizer_language(tokenizer, attribute: str, language_code: str) -> None:
    try:
        setattr(tokenizer, attribute, language_code)
    except Exception:
        return


def _disable_generation_max_length_warning(model, max_new_tokens: int | None) -> None:
    if not max_new_tokens:
        return
    generation_config = getattr(model, "generation_config", None)
    if generation_config is None or not hasattr(generation_config, "max_length"):
        return
    try:
        generation_config.max_length = None
    except Exception:
        return


def _translations_from_http_payload(payload: object, target_language: str, *, engine_name: str = "Tilmash") -> list[str]:
    if isinstance(payload, list):
        return [str(item) for item in payload]
    if not isinstance(payload, dict):
        raise LocalModelInferenceError(f"{engine_name} HTTP server returned a non-JSON-object response.")
    for key in ("translations", "translated_texts", "outputs", "texts"):
        value = payload.get(key)
        if isinstance(value, list):
            return [str(item) for item in value]
    value = payload.get(target_language)
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    raise LocalModelInferenceError(f"{engine_name} HTTP server response did not include translations.")


def _prepare_madlad_input(text: str, target_language: str) -> str:
    target_tags = {
        "zh-Hans": "<2zh>",
        "kk": "<2kk>",
        "uz": "<2uz>",
    }
    prefix = target_tags.get(target_language, "")
    stripped = text.strip()
    return f"{prefix} {stripped}".strip()


def _quantization_kwargs(quantization: str) -> dict:
    normalized = (quantization or "").strip().lower()
    if normalized in {"", "none", "false", "off", "no"}:
        return {}
    if normalized not in {"8bit", "int8", "4bit", "int4"}:
        return {}
    try:
        from transformers import BitsAndBytesConfig
    except Exception as exc:
        raise LocalModelLoadError("Transformers BitsAndBytesConfig is required for MADLAD quantization.") from exc
    try:
        if normalized in {"8bit", "int8"}:
            return {"quantization_config": BitsAndBytesConfig(load_in_8bit=True), "device_map": "auto"}
        return {"quantization_config": BitsAndBytesConfig(load_in_4bit=True), "device_map": "auto"}
    except Exception as exc:
        raise LocalModelLoadError("bitsandbytes support is required for MADLAD quantization.") from exc


def _torch_dtype(torch, dtype: str):
    normalized = (dtype or "auto").lower()
    if normalized == "auto":
        return None
    if normalized in {"float16", "fp16"}:
        return torch.float16
    if normalized in {"bfloat16", "bf16"}:
        return torch.bfloat16
    if normalized in {"float32", "fp32"}:
        return torch.float32
    return None
