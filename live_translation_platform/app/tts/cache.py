from collections import OrderedDict
from dataclasses import replace
from hashlib import sha256
from urllib.parse import quote

from app.tts.base import TTSProvider, TTSResult


class TTSCache:
    def __init__(self, max_items: int = 500) -> None:
        self.max_items = max(1, max_items)
        self._items: OrderedDict[str, TTSResult] = OrderedDict()

    def get(self, key: str) -> TTSResult | None:
        item = self._items.get(key)
        if item is None:
            return None
        self._items.move_to_end(key)
        return item.with_cached(True)

    def set(self, key: str, value: TTSResult) -> None:
        self._items[key] = value.with_cached(False)
        self._items.move_to_end(key)
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)


def tts_cache_key(
    provider: str,
    language: str,
    voice: str | None,
    audio_format: str | None,
    text: str,
) -> str:
    text_digest = sha256(text.encode("utf-8")).hexdigest()
    fields = (provider, language, voice or "", audio_format or "")
    return "|".join((*[quote(field, safe="") for field in fields], text_digest))


async def synthesize_with_cache(
    cache: TTSCache | None,
    provider: TTSProvider,
    text: str,
    language: str,
    voice: str | None,
    audio_format: str | None,
    metadata: dict | None = None,
    voice_gender: str | None = None,
) -> TTSResult:
    cache_voice = voice or (f"gender:{voice_gender}" if voice_gender and voice_gender != "auto" else None)
    key = tts_cache_key(provider.name, language, cache_voice, audio_format, text)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            if metadata:
                return replace(cached, metadata={**dict(cached.metadata), **metadata})
            return cached

    result = await provider.synthesize(text, language, voice, audio_format, metadata, voice_gender=voice_gender)
    if cache is not None:
        cached_metadata = dict(result.metadata)
        for metadata_key in metadata or {}:
            cached_metadata.pop(metadata_key, None)
        cache.set(key, replace(result, metadata=cached_metadata))
    return result
