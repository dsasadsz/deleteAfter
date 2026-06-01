from abc import ABC, abstractmethod


class TranslationProvider(ABC):
    name: str

    @abstractmethod
    async def translate_many(
        self,
        text: str,
        source_language: str,
        target_languages: list[str],
    ) -> dict[str, str]:
        pass


class UnsupportedTranslationProvider(TranslationProvider):
    def __init__(self, name: str) -> None:
        self.name = name

    async def translate_many(
        self,
        text: str,
        source_language: str,
        target_languages: list[str],
    ) -> dict[str, str]:
        raise NotImplementedError(f"{self.name} translator adapter is planned for Stage 4.")


def create_translation_provider(name: str, **kwargs) -> TranslationProvider:
    normalized = name.lower()
    if normalized == "mock":
        from app.translation.mock_translator import MockTranslator

        return MockTranslator()
    if normalized == "azure":
        from app.translation.azure_translator import AzureTranslator

        return AzureTranslator(**kwargs)
    if normalized == "local":
        from app.translation.local_provider import LocalTranslationProvider

        return LocalTranslationProvider(**kwargs)
    if normalized in {"google", "llm"}:
        return UnsupportedTranslationProvider(normalized)
    raise ValueError(f"Unknown translation provider: {name}")
