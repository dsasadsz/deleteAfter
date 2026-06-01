from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote

from app.infra.redis import build_redis_key
from app.tts.base import TTSConfigurationError, TTSResult


@dataclass(frozen=True)
class TTSSharedCacheItem:
    cache_key: str
    audio_id: str
    lesson_id: str
    result: TTSResult
    expires_at: datetime


@dataclass(frozen=True)
class TTSSharedCacheResult:
    cache_key: str
    audio_id: str
    result: TTSResult
    expires_at: datetime

    @property
    def cached(self) -> bool:
        return self.result.cached


class MemoryTTSSharedCache:
    def __init__(self, max_items: int = 1000, ttl_seconds: int = 3600) -> None:
        self.max_items = max(1, int(max_items))
        self.ttl_seconds = max(0, int(ttl_seconds))
        self._items: OrderedDict[str, TTSSharedCacheItem] = OrderedDict()
        self._audio_ids: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.hits_total = 0
        self.misses_total = 0
        self.audio_url_requests_total = 0
        self.provider_calls_total = 0
        self.provider_calls_saved_total = 0
        self.evictions_total = 0

    async def get_or_synthesize(
        self,
        cache_key: str,
        synthesize: Callable[[], Awaitable[TTSResult]],
        *,
        lesson_id: str | None = None,
    ) -> TTSSharedCacheResult:
        cached = self.get(cache_key)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self.get(cache_key)
            if cached is not None:
                return cached
            self.misses_total += 1
            self.provider_calls_total += 1
            result = await synthesize()
            if self.ttl_seconds <= 0:
                return TTSSharedCacheResult(
                    cache_key=cache_key,
                    audio_id=audio_id_for_cache_key(cache_key),
                    result=result.with_cached(False),
                    expires_at=_now(),
                )
            item = self.set(cache_key, result, lesson_id=lesson_id or _lesson_from_cache_key(cache_key))
            return TTSSharedCacheResult(
                cache_key=cache_key,
                audio_id=item.audio_id,
                result=item.result.with_cached(False),
                expires_at=item.expires_at,
            )

    def get(self, cache_key: str) -> TTSSharedCacheResult | None:
        item = self._items.get(cache_key)
        if item is None:
            return None
        if _is_expired(item):
            self._delete(cache_key)
            return None
        self._items.move_to_end(cache_key)
        self.hits_total += 1
        self.provider_calls_saved_total += 1
        return TTSSharedCacheResult(
            cache_key=cache_key,
            audio_id=item.audio_id,
            result=item.result.with_cached(True),
            expires_at=item.expires_at,
        )

    def get_audio(self, audio_id: str, lesson_id: str) -> TTSSharedCacheResult | None:
        self.audio_url_requests_total += 1
        cache_key = self._audio_ids.get(audio_id)
        if not cache_key:
            return None
        item = self._items.get(cache_key)
        if item is None or item.lesson_id != lesson_id:
            return None
        if _is_expired(item):
            self._delete(cache_key)
            return None
        self._items.move_to_end(cache_key)
        return TTSSharedCacheResult(
            cache_key=cache_key,
            audio_id=audio_id,
            result=item.result.with_cached(True),
            expires_at=item.expires_at,
        )

    def set(self, cache_key: str, result: TTSResult, *, lesson_id: str) -> TTSSharedCacheItem:
        audio_id = audio_id_for_cache_key(cache_key)
        item = TTSSharedCacheItem(
            cache_key=cache_key,
            audio_id=audio_id,
            lesson_id=lesson_id,
            result=result.with_cached(False),
            expires_at=_now() + timedelta(seconds=self.ttl_seconds),
        )
        self._items[cache_key] = item
        self._items.move_to_end(cache_key)
        self._audio_ids[audio_id] = cache_key
        while len(self._items) > self.max_items:
            old_key, old_item = self._items.popitem(last=False)
            self._audio_ids.pop(old_item.audio_id, None)
            self._locks.pop(old_key, None)
            self.evictions_total += 1
        return item

    def _delete(self, cache_key: str, *, count_eviction: bool = True) -> None:
        item = self._items.pop(cache_key, None)
        if item is not None:
            self._audio_ids.pop(item.audio_id, None)
            if count_eviction:
                self.evictions_total += 1
        self._locks.pop(cache_key, None)

    def cleanup_expired(self) -> None:
        for cache_key, item in list(self._items.items()):
            if _is_expired(item):
                self._delete(cache_key)

    def stats(self) -> dict[str, int | str]:
        self.cleanup_expired()
        return {
            "tts_cache_hits_total": self.hits_total,
            "tts_cache_misses_total": self.misses_total,
            "tts_cache_items": len(self._items),
            "tts_audio_url_requests_total": self.audio_url_requests_total,
            "tts_provider_calls_total": self.provider_calls_total,
            "tts_provider_calls_saved_total": self.provider_calls_saved_total,
            "tts_cache_backend": "memory",
            "tts_cache_disk_bytes": 0,
            "tts_cache_evictions_total": self.evictions_total,
        }


class DiskTTSSharedCache:
    def __init__(
        self,
        cache_dir: str | Path,
        max_items: int = 1000,
        ttl_seconds: int = 3600,
        max_bytes: int = 1073741824,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_items = max(1, int(max_items))
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.max_bytes = max(0, int(max_bytes))
        self._locks: dict[str, asyncio.Lock] = {}
        self.hits_total = 0
        self.misses_total = 0
        self.audio_url_requests_total = 0
        self.provider_calls_total = 0
        self.provider_calls_saved_total = 0
        self.evictions_total = 0

    async def get_or_synthesize(
        self,
        cache_key: str,
        synthesize: Callable[[], Awaitable[TTSResult]],
        *,
        lesson_id: str | None = None,
    ) -> TTSSharedCacheResult:
        cached = self.get(cache_key)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self.get(cache_key)
            if cached is not None:
                return cached
            self.misses_total += 1
            self.provider_calls_total += 1
            result = await synthesize()
            if self.ttl_seconds <= 0:
                return TTSSharedCacheResult(
                    cache_key=cache_key,
                    audio_id=audio_id_for_cache_key(cache_key),
                    result=result.with_cached(False),
                    expires_at=_now(),
                )
            item = self.set(cache_key, result, lesson_id=lesson_id or _lesson_from_cache_key(cache_key))
            return TTSSharedCacheResult(
                cache_key=cache_key,
                audio_id=item.audio_id,
                result=item.result.with_cached(False),
                expires_at=item.expires_at,
            )

    def get(self, cache_key: str) -> TTSSharedCacheResult | None:
        audio_id = audio_id_for_cache_key(cache_key)
        item = self._read_item(audio_id)
        if item is None or item.cache_key != cache_key:
            return None
        if _is_expired(item):
            self._delete_audio_id(audio_id)
            return None
        self.hits_total += 1
        self.provider_calls_saved_total += 1
        self._touch(audio_id)
        return TTSSharedCacheResult(
            cache_key=cache_key,
            audio_id=audio_id,
            result=item.result.with_cached(True),
            expires_at=item.expires_at,
        )

    def get_audio(self, audio_id: str, lesson_id: str) -> TTSSharedCacheResult | None:
        self.audio_url_requests_total += 1
        item = self._read_item(audio_id)
        if item is None or item.lesson_id != lesson_id:
            return None
        if _is_expired(item):
            self._delete_audio_id(audio_id)
            return None
        self._touch(audio_id)
        return TTSSharedCacheResult(
            cache_key=item.cache_key,
            audio_id=audio_id,
            result=item.result.with_cached(True),
            expires_at=item.expires_at,
        )

    def set(self, cache_key: str, result: TTSResult, *, lesson_id: str) -> TTSSharedCacheItem:
        audio_id = audio_id_for_cache_key(cache_key)
        expires_at = _now() + timedelta(seconds=self.ttl_seconds)
        item = TTSSharedCacheItem(
            cache_key=cache_key,
            audio_id=audio_id,
            lesson_id=lesson_id,
            result=result.with_cached(False),
            expires_at=expires_at,
        )
        self._write_item(item)
        self.cleanup_expired()
        self.cleanup_limits()
        return item

    def cleanup_expired(self) -> None:
        for meta_path in self.cache_dir.glob("*.json"):
            item = self._read_item(meta_path.stem)
            if item is None or _is_expired(item):
                self._delete_audio_id(meta_path.stem)

    def cleanup_limits(self) -> None:
        entries = self._entries_by_age()
        while len(entries) > self.max_items:
            audio_id, _mtime, _size = entries.pop(0)
            self._delete_audio_id(audio_id)
        while self._disk_audio_bytes(entries) > self.max_bytes and entries:
            audio_id, _mtime, _size = entries.pop(0)
            self._delete_audio_id(audio_id)

    def stats(self) -> dict[str, int | str]:
        self.cleanup_expired()
        self.cleanup_limits()
        entries = self._entries_by_age()
        return {
            "tts_cache_hits_total": self.hits_total,
            "tts_cache_misses_total": self.misses_total,
            "tts_cache_items": len(entries),
            "tts_audio_url_requests_total": self.audio_url_requests_total,
            "tts_provider_calls_total": self.provider_calls_total,
            "tts_provider_calls_saved_total": self.provider_calls_saved_total,
            "tts_cache_backend": "disk",
            "tts_cache_disk_bytes": self._disk_audio_bytes(entries),
            "tts_cache_evictions_total": self.evictions_total,
        }

    def _write_item(self, item: TTSSharedCacheItem) -> None:
        audio_path = self._audio_path(item.audio_id)
        meta_path = self._meta_path(item.audio_id)
        _atomic_write_bytes(audio_path, item.result.audio_bytes)
        _atomic_write_text(meta_path, json.dumps(_item_to_metadata(item), sort_keys=True, separators=(",", ":")))

    def _read_item(self, audio_id: str) -> TTSSharedCacheItem | None:
        if not _safe_audio_id(audio_id):
            return None
        audio_path = self._audio_path(audio_id)
        meta_path = self._meta_path(audio_id)
        if not audio_path.exists() or not meta_path.exists():
            return None
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            audio_bytes = audio_path.read_bytes()
            expires_at = datetime.fromisoformat(metadata["expires_at"])
            result = TTSResult(
                audio_bytes=audio_bytes,
                content_type=str(metadata["content_type"]),
                language=str(metadata["language"]),
                voice=metadata.get("voice"),
                provider=str(metadata["provider"]),
                duration_ms=metadata.get("duration_ms"),
                text_chars=int(metadata.get("text_chars") or 0),
                cached=False,
                latency_ms=int(metadata.get("latency_ms") or 0),
                metadata={},
            )
            return TTSSharedCacheItem(
                cache_key=str(metadata["cache_key"]),
                audio_id=audio_id,
                lesson_id=str(metadata["lesson_id"]),
                result=result,
                expires_at=expires_at,
            )
        except Exception:
            return None

    def _delete_audio_id(self, audio_id: str) -> None:
        deleted = False
        for path in (self._audio_path(audio_id), self._meta_path(audio_id)):
            try:
                path.unlink()
                deleted = True
            except FileNotFoundError:
                pass
        if deleted:
            self.evictions_total += 1

    def _touch(self, audio_id: str) -> None:
        now = _now().timestamp()
        for path in (self._audio_path(audio_id), self._meta_path(audio_id)):
            try:
                os.utime(path, (now, now))
            except FileNotFoundError:
                pass

    def _entries_by_age(self) -> list[tuple[str, float, int]]:
        entries = []
        for audio_path in self.cache_dir.glob("*.audio"):
            audio_id = audio_path.stem
            meta_path = self._meta_path(audio_id)
            if not meta_path.exists():
                self._delete_audio_id(audio_id)
                continue
            try:
                stat = audio_path.stat()
            except FileNotFoundError:
                continue
            entries.append((audio_id, stat.st_mtime, stat.st_size))
        return sorted(entries, key=lambda entry: entry[1])

    @staticmethod
    def _disk_audio_bytes(entries: list[tuple[str, float, int]]) -> int:
        return sum(size for _audio_id, _mtime, size in entries)

    def _audio_path(self, audio_id: str) -> Path:
        return self.cache_dir / f"{audio_id}.audio"

    def _meta_path(self, audio_id: str) -> Path:
        return self.cache_dir / f"{audio_id}.json"


def create_tts_shared_cache(settings, *, ttl_seconds: int | None = None):
    ttl = int(ttl_seconds if ttl_seconds is not None else getattr(settings, "tts_shared_cache_ttl_seconds", 3600))
    backend = str(getattr(settings, "tts_shared_cache_backend", "memory") or "memory").lower()
    if backend == "disk":
        return DiskTTSSharedCache(
            cache_dir=getattr(settings, "tts_shared_cache_dir", "./tmp/tts_cache"),
            max_items=getattr(settings, "tts_shared_cache_max_items", 1000),
            ttl_seconds=ttl,
            max_bytes=getattr(settings, "tts_shared_cache_disk_max_bytes", 1073741824),
        )
    return MemoryTTSSharedCache(
        max_items=getattr(settings, "tts_shared_cache_max_items", 1000),
        ttl_seconds=ttl,
    )


async def get_or_synthesize_with_distributed_lock(
    cache,
    cache_key: str,
    synthesize: Callable[[], Awaitable[TTSResult]],
    *,
    settings,
    redis_client,
    runtime_metrics=None,
    lesson_id: str | None = None,
) -> TTSSharedCacheResult:
    if not _distributed_lock_enabled(settings, redis_client):
        return await cache.get_or_synthesize(cache_key, synthesize, lesson_id=lesson_id)

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    lock_key = build_tts_distributed_lock_key(settings, cache_key)
    lock_token = secrets.token_hex(16)
    ttl_seconds = max(1, int(getattr(settings, "tts_cache_distributed_lock_ttl_seconds", 10) or 10))
    wait_timeout = max(0.0, float(getattr(settings, "tts_cache_distributed_lock_wait_timeout_seconds", 2.0) or 0.0))
    poll_interval = max(0.001, float(getattr(settings, "tts_cache_distributed_lock_poll_interval_seconds", 0.05) or 0.05))

    try:
        acquired = await redis_client.set(lock_key, lock_token, nx=True, ex=ttl_seconds)
    except Exception as exc:
        _record_lock_metric(runtime_metrics, "error")
        return await _distributed_lock_fallback(cache, cache_key, synthesize, settings=settings, error=exc, lesson_id=lesson_id)

    if acquired:
        _record_lock_metric(runtime_metrics, "acquired")
        try:
            return await cache.get_or_synthesize(cache_key, synthesize, lesson_id=lesson_id)
        finally:
            await _release_lock(redis_client, lock_key, lock_token, runtime_metrics)

    _record_lock_metric(runtime_metrics, "waited")
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() <= deadline:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        await asyncio.sleep(poll_interval)

    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    _record_lock_metric(runtime_metrics, "timeout")
    return await _distributed_lock_fallback(cache, cache_key, synthesize, settings=settings, error=None, lesson_id=lesson_id)


def build_tts_distributed_lock_key(settings, cache_key: str) -> str:
    return build_redis_key(settings, "tts", "lock", sha256(cache_key.encode("utf-8")).hexdigest())


def build_tts_shared_cache_key(
    *,
    lesson_id: str,
    caption_id: str | None,
    language: str,
    provider: str,
    voice: str | None,
    text: str,
) -> str:
    fields = (
        "tts",
        lesson_id,
        caption_id or "no-caption",
        language,
        provider,
        voice or "",
        sha256(normalize_tts_text_for_cache(text).encode("utf-8")).hexdigest(),
    )
    return ":".join(quote(field, safe="") for field in fields)


def audio_id_for_cache_key(cache_key: str) -> str:
    return sha256(cache_key.encode("utf-8")).hexdigest()[:40]


def normalize_tts_text_for_cache(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def is_unplayable_tts_text(text: str) -> bool:
    normalized = normalize_tts_text_for_cache(text)
    if not normalized:
        return True
    return (
        normalized.startswith("translation unavailable")
        or normalized.startswith("waiting for ")
        or normalized.startswith("перевод временно недоступен")
        or normalized.startswith("перевод пока недоступен")
    )


def _distributed_lock_enabled(settings, redis_client) -> bool:
    return bool(
        redis_client is not None
        and getattr(settings, "redis_enabled", False)
        and getattr(settings, "tts_cache_distributed_lock_enabled", False)
    )


async def _distributed_lock_fallback(cache, cache_key, synthesize, *, settings, error: Exception | None, lesson_id: str | None):
    if bool(getattr(settings, "tts_cache_distributed_lock_fail_closed", False)):
        if error is not None:
            raise TTSConfigurationError("TTS distributed lock unavailable") from error
        raise TTSConfigurationError("TTS distributed lock timed out")
    return await cache.get_or_synthesize(cache_key, synthesize, lesson_id=lesson_id)


async def _release_lock(redis_client, lock_key: str, lock_token: str, runtime_metrics) -> None:
    try:
        current = await redis_client.get(lock_key)
        if current == lock_token:
            await redis_client.delete(lock_key)
    except Exception:
        _record_lock_metric(runtime_metrics, "error")


def _record_lock_metric(runtime_metrics, event: str) -> None:
    if runtime_metrics is None:
        return
    method = getattr(runtime_metrics, f"record_tts_distributed_lock_{event}", None)
    if method is not None:
        method()


def _lesson_from_cache_key(cache_key: str) -> str:
    parts = cache_key.split(":")
    return parts[1] if len(parts) > 1 else ""


def _is_expired(item: TTSSharedCacheItem) -> bool:
    return item.expires_at <= _now()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _item_to_metadata(item: TTSSharedCacheItem) -> dict:
    result = item.result
    return {
        "cache_key": item.cache_key,
        "audio_id": item.audio_id,
        "lesson_id": item.lesson_id,
        "expires_at": item.expires_at.isoformat(),
        "content_type": result.content_type,
        "language": result.language,
        "voice": result.voice,
        "provider": result.provider,
        "duration_ms": result.duration_ms,
        "text_chars": result.text_chars,
        "latency_ms": result.latency_ms,
    }


def _safe_audio_id(audio_id: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{40}", str(audio_id)))


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(payload)
    tmp_path.replace(path)


def _atomic_write_text(path: Path, payload: str) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(path)
