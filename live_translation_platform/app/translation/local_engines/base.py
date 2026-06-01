from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class LocalTranslationConfigurationError(RuntimeError):
    pass


class LocalTranslationTimeoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalTranslationEngineStatus:
    ready: bool
    status: str
    missing: list[str]
    enabled: bool
    engine: str
    supported_source_languages: list[str]
    supported_target_languages: list[str]
    device: str

    def as_dict(self) -> dict:
        return {
            "ready": self.ready,
            "status": self.status,
            "missing": list(self.missing),
            "enabled": self.enabled,
            "engine": self.engine,
            "supported_source_languages": list(self.supported_source_languages),
            "supported_target_languages": list(self.supported_target_languages),
            "device": self.device,
        }


class LocalTranslationEngine(ABC):
    name: str
    supported_targets: set[str]
    model_path_env: str

    def __init__(
        self,
        *,
        enabled: bool,
        model_path: str = "",
        device: str = "cpu",
        timeout_seconds: float = 2.5,
    ) -> None:
        self.enabled = bool(enabled)
        self.model_path = model_path or ""
        self.device = device or "cpu"
        self.timeout_seconds = float(timeout_seconds or 2.5)

    def supports(self, source_language: str, target_language: str) -> bool:
        source = normalize_translation_language(source_language)
        target = normalize_translation_language(target_language)
        return self.enabled and source == "ru" and target in self.supported_targets

    def status(self) -> dict:
        missing = []
        if self.enabled and not self.model_path:
            missing.append(self.model_path_env)
        ready = self.enabled and not missing
        return LocalTranslationEngineStatus(
            ready=ready,
            status="ready" if ready else "not_configured",
            missing=missing,
            enabled=self.enabled,
            engine=self.name,
            supported_source_languages=["ru"],
            supported_target_languages=sorted(self.supported_targets),
            device=self.device,
        ).as_dict()

    def _ensure_ready(self) -> None:
        status = self.status()
        if not status["ready"]:
            missing = ", ".join(status["missing"]) or f"{self.name} disabled"
            raise LocalTranslationConfigurationError(f"{self.name} is not configured: {missing}")

    @abstractmethod
    async def translate(self, text: str, source_language: str, target_language: str) -> str:
        pass


def normalize_translation_language(language: str | None) -> str:
    value = (language or "").strip()
    lowered = value.lower().replace("_", "-")
    if lowered in {"ru", "ru-ru"}:
        return "ru"
    if lowered in {"kk", "kk-kz"}:
        return "kk"
    if lowered in {"uz", "uz-uz"}:
        return "uz"
    if lowered in {"zh", "zh-cn", "zh-hans", "zh-hans-cn"}:
        return "zh-Hans"
    return value
