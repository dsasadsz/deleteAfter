from app.translation.base import UnsupportedTranslationProvider


class LLMTranslator(UnsupportedTranslationProvider):
    def __init__(self) -> None:
        super().__init__("llm")

