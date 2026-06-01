from app.translation.base import UnsupportedTranslationProvider


class GoogleTranslator(UnsupportedTranslationProvider):
    def __init__(self) -> None:
        super().__init__("google")

