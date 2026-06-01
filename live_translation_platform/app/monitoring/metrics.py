from __future__ import annotations

import time
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.providers.quotas import classify_provider_error


KNOWN_PROVIDER_METRIC_LABELS = {"azure", "cartesia", "elevenlabs", "kazakh_tts2", "local", "madlad400", "mock", "piper", "silero", "tilmash"}
DEFAULT_LATENCY_WINDOW_SIZE = 500


class RollingLatencyTracker:
    def __init__(self, maxlen: int = DEFAULT_LATENCY_WINDOW_SIZE) -> None:
        self.maxlen = max(1, int(maxlen or DEFAULT_LATENCY_WINDOW_SIZE))
        self._values: deque[float] = deque(maxlen=self.maxlen)

    def record(self, value_ms: float | int | str | None) -> None:
        if value_ms is None:
            return
        try:
            value = float(value_ms)
        except (TypeError, ValueError):
            return
        if not math.isfinite(value):
            return
        self._values.append(value)

    @property
    def values(self) -> tuple[float, ...]:
        return tuple(self._values)

    @property
    def count(self) -> int:
        return len(self._values)

    @property
    def avg(self) -> float | None:
        return _average(self._values)

    @property
    def p50(self) -> float | None:
        return _percentile(self._values, 50)

    @property
    def p95(self) -> float | None:
        return _percentile(self._values, 95)

    @property
    def p99(self) -> float | None:
        return _percentile(self._values, 99)


@dataclass
class RuntimeMetrics:
    captions_sent_total: int = 0
    tts_requests_total: int = 0
    questions_total: int = 0
    stt_disconnects_total: int = 0
    provider_errors_total: int = 0
    websocket_broadcasts_total: int = 0
    websocket_send_failures_total: int = 0
    websocket_send_timeouts_total: int = 0
    websocket_clients_dropped_total: int = 0
    redis_pubsub_messages_published_total: int = 0
    redis_pubsub_messages_received_total: int = 0
    redis_pubsub_errors_total: int = 0
    rate_limit_checks_total: int = 0
    rate_limit_blocked_total: int = 0
    redis_rate_limit_errors_total: int = 0
    tts_distributed_lock_acquired_total: int = 0
    tts_distributed_lock_waited_total: int = 0
    tts_distributed_lock_timeout_total: int = 0
    tts_distributed_lock_errors_total: int = 0
    provider_timeout_errors_total: int = 0
    provider_rate_limit_errors_total: int = 0
    provider_auth_errors_total: int = 0
    provider_unknown_errors_total: int = 0
    provider_errors_by_provider: dict[str, int] = field(default_factory=dict)
    provider_timeouts_by_provider: dict[str, int] = field(default_factory=dict)
    _caption_events: deque[float] = field(default_factory=deque)
    _tts_events: deque[float] = field(default_factory=deque)
    _stt_latency_ms: RollingLatencyTracker = field(default_factory=RollingLatencyTracker)
    _translation_latency_ms: RollingLatencyTracker = field(default_factory=RollingLatencyTracker)
    _tts_latency_ms: RollingLatencyTracker = field(default_factory=RollingLatencyTracker)
    _caption_broadcast_latency_ms: RollingLatencyTracker = field(default_factory=RollingLatencyTracker)
    _question_broadcast_latency_ms: RollingLatencyTracker = field(default_factory=RollingLatencyTracker)
    _redis_pubsub_latency_ms: RollingLatencyTracker = field(default_factory=RollingLatencyTracker)

    def record_caption(self, latency_ms: float | int | None = None) -> None:
        now = time.monotonic()
        self.captions_sent_total += 1
        self._caption_events.append(now)
        self._prune(self._caption_events, now, 60)
        if latency_ms is not None:
            self.record_translation_latency(latency_ms)

    def record_tts_request(self, latency_ms: float | int | None = None) -> None:
        now = time.monotonic()
        self.tts_requests_total += 1
        self._tts_events.append(now)
        self._prune(self._tts_events, now, 60)
        if latency_ms is not None:
            self._tts_latency_ms.record(latency_ms)

    def record_question(self) -> None:
        self.questions_total += 1

    def record_stt_disconnect(self) -> None:
        self.stt_disconnects_total += 1

    def record_provider_error(self, provider: Any | None = None, error: Exception | str | None = None) -> None:
        self.provider_errors_total += 1
        provider_name = _provider_name(provider)
        if provider_name is not None:
            self.provider_errors_by_provider[provider_name] = self.provider_errors_by_provider.get(provider_name, 0) + 1
        classification = classify_provider_error(error)
        if error is not None and (isinstance(error, TimeoutError) or classification == "timeout"):
            self.record_provider_timeout(provider)
        elif classification == "rate_limit":
            self.provider_rate_limit_errors_total += 1
        elif classification == "auth":
            self.provider_auth_errors_total += 1
        else:
            self.provider_unknown_errors_total += 1

    def record_provider_timeout(self, provider: Any | None = None) -> None:
        self.provider_timeout_errors_total += 1
        provider_name = _provider_name(provider)
        if provider_name is not None:
            self.provider_timeouts_by_provider[provider_name] = self.provider_timeouts_by_provider.get(provider_name, 0) + 1

    def record_websocket_broadcast(self, kind: str, latency_ms: float | int | None = None) -> None:
        self.websocket_broadcasts_total += 1
        if latency_ms is None:
            return
        if kind == "question":
            self._question_broadcast_latency_ms.record(latency_ms)
        else:
            self._caption_broadcast_latency_ms.record(latency_ms)

    def record_websocket_send_failure(self) -> None:
        self.websocket_send_failures_total += 1

    def record_websocket_send_timeout(self) -> None:
        self.websocket_send_timeouts_total += 1

    def record_websocket_client_dropped(self, count: int = 1) -> None:
        self.websocket_clients_dropped_total += max(0, int(count))

    def record_redis_pubsub_published(self, latency_ms: float | int | None = None) -> None:
        self.redis_pubsub_messages_published_total += 1
        if latency_ms is not None:
            self._redis_pubsub_latency_ms.record(latency_ms)

    def record_redis_pubsub_received(self, latency_ms: float | int | None = None) -> None:
        self.redis_pubsub_messages_received_total += 1
        if latency_ms is not None:
            self._redis_pubsub_latency_ms.record(latency_ms)

    def record_redis_pubsub_error(self) -> None:
        self.redis_pubsub_errors_total += 1

    def record_rate_limit_check(self) -> None:
        self.rate_limit_checks_total += 1

    def record_rate_limit_blocked(self) -> None:
        self.rate_limit_blocked_total += 1

    def record_redis_rate_limit_error(self) -> None:
        self.redis_rate_limit_errors_total += 1

    def record_tts_distributed_lock_acquired(self) -> None:
        self.tts_distributed_lock_acquired_total += 1

    def record_tts_distributed_lock_waited(self) -> None:
        self.tts_distributed_lock_waited_total += 1

    def record_tts_distributed_lock_timeout(self) -> None:
        self.tts_distributed_lock_timeout_total += 1

    def record_tts_distributed_lock_error(self) -> None:
        self.tts_distributed_lock_errors_total += 1

    def redis_pubsub_latency_ms_avg(self) -> float | None:
        return self._redis_pubsub_latency_ms.avg

    def redis_pubsub_latency_ms_p50(self) -> float | None:
        return self._redis_pubsub_latency_ms.p50

    def redis_pubsub_latency_ms_p95(self) -> float | None:
        return self._redis_pubsub_latency_ms.p95

    def redis_pubsub_latency_ms_p99(self) -> float | None:
        return self._redis_pubsub_latency_ms.p99

    def record_stt_latency(self, latency_ms: float | int | None) -> None:
        self._stt_latency_ms.record(latency_ms)

    def record_translation_latency(self, latency_ms: float | int | None) -> None:
        self._translation_latency_ms.record(latency_ms)

    def captions_per_second(self) -> float:
        now = time.monotonic()
        self._prune(self._caption_events, now, 1)
        return float(len(self._caption_events))

    def tts_requests_per_minute(self) -> int:
        now = time.monotonic()
        self._prune(self._tts_events, now, 60)
        return len(self._tts_events)

    def caption_broadcast_latency_ms_avg(self) -> float | None:
        return self._caption_broadcast_latency_ms.avg

    def caption_broadcast_latency_ms_p50(self) -> float | None:
        return self._caption_broadcast_latency_ms.p50

    def caption_broadcast_latency_ms_p95(self) -> float | None:
        return self._caption_broadcast_latency_ms.p95

    def caption_broadcast_latency_ms_p99(self) -> float | None:
        return self._caption_broadcast_latency_ms.p99

    def question_broadcast_latency_ms_avg(self) -> float | None:
        return self._question_broadcast_latency_ms.avg

    def question_broadcast_latency_ms_p50(self) -> float | None:
        return self._question_broadcast_latency_ms.p50

    def question_broadcast_latency_ms_p95(self) -> float | None:
        return self._question_broadcast_latency_ms.p95

    def question_broadcast_latency_ms_p99(self) -> float | None:
        return self._question_broadcast_latency_ms.p99

    @staticmethod
    def _prune(events: deque[float], now: float, window_seconds: float) -> None:
        cutoff = now - window_seconds
        while events and events[0] < cutoff:
            events.popleft()


def runtime_metrics_snapshot(app: Any) -> dict:
    metrics = getattr(getattr(app, "state", None), "runtime_metrics", None) or RuntimeMetrics()
    session_manager = getattr(app.state, "session_manager", None)
    caption_hub = getattr(app.state, "caption_hub", None)
    question_hub = getattr(app.state, "question_hub", None)
    rtms_manager = getattr(app.state, "rtms_manager", None)
    browser_audio_manager = getattr(app.state, "browser_audio_manager", None)
    provider_runtime = getattr(app.state, "provider_runtime", {}) or {}
    tts_shared_cache = getattr(app.state, "tts_shared_cache", None)
    settings = getattr(app.state, "settings", None)
    redis_status = getattr(app.state, "redis_status", None)
    rate_limiter = getattr(app.state, "rate_limiter", None)
    redis_status_payload = redis_status.to_dict() if redis_status is not None and hasattr(redis_status, "to_dict") else {}

    sessions = getattr(session_manager, "sessions", {}) or {}
    payload = {
        "active_lessons": len(sessions),
        "caption_ws_clients": _hub_count(getattr(caption_hub, "_caption_clients", {})),
        "question_ws_clients": _hub_count(getattr(question_hub, "_clients", {})),
        "diagnostic_ws_clients": _hub_count(getattr(caption_hub, "_debug_clients", {})),
        "active_pipelines": _active_pipeline_count(sessions),
        "audio_queue_sizes": _audio_queue_sizes(rtms_manager, browser_audio_manager, sessions),
        "dropped_audio_chunks": _dropped_audio_chunks(browser_audio_manager, sessions),
        "captions_sent_total": metrics.captions_sent_total,
        "captions_per_second": metrics.captions_per_second(),
        "tts_requests_total": metrics.tts_requests_total,
        "tts_requests_per_minute": metrics.tts_requests_per_minute(),
        "questions_total": metrics.questions_total,
        "stt_disconnects_total": metrics.stt_disconnects_total,
        "provider_errors_total": metrics.provider_errors_total + int(provider_runtime.get("provider_429_count", 0) or 0),
        "provider_errors_by_provider": dict(metrics.provider_errors_by_provider),
        "provider_timeouts_by_provider": dict(metrics.provider_timeouts_by_provider),
        "provider_timeout_errors_total": metrics.provider_timeout_errors_total,
        "provider_rate_limit_errors_total": metrics.provider_rate_limit_errors_total,
        "provider_auth_errors_total": metrics.provider_auth_errors_total,
        "provider_unknown_errors_total": metrics.provider_unknown_errors_total,
        "stt_latency_ms_avg": metrics._stt_latency_ms.avg,
        "stt_latency_ms_p50": metrics._stt_latency_ms.p50,
        "stt_latency_ms_p95": metrics._stt_latency_ms.p95,
        "stt_latency_ms_p99": metrics._stt_latency_ms.p99,
        "translation_latency_ms_avg": metrics._translation_latency_ms.avg,
        "translation_latency_ms_p50": metrics._translation_latency_ms.p50,
        "translation_latency_ms_p95": metrics._translation_latency_ms.p95,
        "translation_latency_ms_p99": metrics._translation_latency_ms.p99,
        "tts_latency_ms_avg": metrics._tts_latency_ms.avg,
        "tts_latency_ms_p50": metrics._tts_latency_ms.p50,
        "tts_latency_ms_p95": metrics._tts_latency_ms.p95,
        "tts_latency_ms_p99": metrics._tts_latency_ms.p99,
        "websocket_broadcasts_total": metrics.websocket_broadcasts_total,
        "websocket_send_failures_total": metrics.websocket_send_failures_total,
        "websocket_send_timeouts_total": metrics.websocket_send_timeouts_total,
        "websocket_clients_dropped_total": metrics.websocket_clients_dropped_total,
        "caption_broadcast_latency_ms_avg": metrics.caption_broadcast_latency_ms_avg(),
        "caption_broadcast_latency_ms_p50": metrics.caption_broadcast_latency_ms_p50(),
        "caption_broadcast_latency_ms_p95": metrics.caption_broadcast_latency_ms_p95(),
        "caption_broadcast_latency_ms_p99": metrics.caption_broadcast_latency_ms_p99(),
        "question_broadcast_latency_ms_avg": metrics.question_broadcast_latency_ms_avg(),
        "question_broadcast_latency_ms_p50": metrics.question_broadcast_latency_ms_p50(),
        "question_broadcast_latency_ms_p95": metrics.question_broadcast_latency_ms_p95(),
        "question_broadcast_latency_ms_p99": metrics.question_broadcast_latency_ms_p99(),
        "redis_enabled": bool(getattr(settings, "redis_enabled", False)),
        "redis_connected": bool(redis_status_payload.get("connected", False)),
        "redis_pubsub_enabled": bool(getattr(settings, "redis_pubsub_enabled", False)),
        "redis_pubsub_messages_published_total": metrics.redis_pubsub_messages_published_total,
        "redis_pubsub_messages_received_total": metrics.redis_pubsub_messages_received_total,
        "redis_pubsub_errors_total": metrics.redis_pubsub_errors_total,
        "redis_pubsub_latency_ms_avg": metrics.redis_pubsub_latency_ms_avg(),
        "redis_pubsub_latency_ms_p50": metrics.redis_pubsub_latency_ms_p50(),
        "redis_pubsub_latency_ms_p95": metrics.redis_pubsub_latency_ms_p95(),
        "redis_pubsub_latency_ms_p99": metrics.redis_pubsub_latency_ms_p99(),
        "rate_limit_checks_total": metrics.rate_limit_checks_total,
        "rate_limit_blocked_total": metrics.rate_limit_blocked_total,
        "redis_rate_limit_errors_total": metrics.redis_rate_limit_errors_total,
        "redis_rate_limit_enabled": rate_limiter.__class__.__name__ == "RedisRateLimiter",
        "tts_distributed_lock_acquired_total": metrics.tts_distributed_lock_acquired_total,
        "tts_distributed_lock_waited_total": metrics.tts_distributed_lock_waited_total,
        "tts_distributed_lock_timeout_total": metrics.tts_distributed_lock_timeout_total,
        "tts_distributed_lock_errors_total": metrics.tts_distributed_lock_errors_total,
    }
    if tts_shared_cache is not None and hasattr(tts_shared_cache, "stats"):
        payload.update(tts_shared_cache.stats())
    else:
        payload.update(
            {
                "tts_cache_hits_total": 0,
                "tts_cache_misses_total": 0,
                "tts_cache_items": 0,
                "tts_audio_url_requests_total": 0,
                "tts_provider_calls_total": 0,
                "tts_provider_calls_saved_total": 0,
                "tts_cache_backend": "none",
                "tts_cache_disk_bytes": 0,
                "tts_cache_evictions_total": 0,
            }
        )
    payload.update(_system_metrics())
    return payload


def _hub_count(clients: dict) -> int:
    return sum(len(items) for items in clients.values())


def _active_pipeline_count(sessions: dict) -> int:
    total = 0
    for session in sessions.values():
        if getattr(session, "running", False):
            total += 1
    return total


def _audio_queue_sizes(rtms_manager: Any, browser_audio_manager: Any, sessions: dict) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for lesson_id, queue in getattr(rtms_manager, "audio_queues", {}).items():
        sizes[f"rtms:{lesson_id}"] = _qsize(queue)
    for lesson_id, queue in getattr(browser_audio_manager, "queues", {}).items():
        sizes[f"browser_audio:{lesson_id}"] = _qsize(queue)
    for lesson_id, session in sessions.items():
        pipeline = getattr(session, "pipeline", None)
        queue = getattr(pipeline, "queue", None)
        if queue is not None:
            sizes[f"pipeline:{lesson_id}"] = _qsize(queue)
    return sizes


def _dropped_audio_chunks(browser_audio_manager: Any, sessions: dict) -> int:
    total = int(getattr(browser_audio_manager, "chunks_dropped_total", 0) or 0)
    for session in sessions.values():
        pipeline = getattr(session, "pipeline", None)
        total += int(getattr(pipeline, "pipeline_chunks_dropped", 0) or 0)
    return total


def _qsize(queue: Any) -> int:
    try:
        return int(queue.qsize())
    except Exception:
        return 0


def _average(values: deque[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _percentile(values: deque[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(float(ordered[0]), 2)
    rank = (len(ordered) - 1) * (percent / 100.0)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return round(float(ordered[int(rank)]), 2)
    weight = rank - lower
    return round(float(ordered[lower] * (1 - weight) + ordered[upper] * weight), 2)


def _provider_name(provider: Any | None) -> str | None:
    if provider is None:
        return None
    if not isinstance(provider, str):
        provider = getattr(provider, "name", None)
    value = str(provider or "").strip().lower()
    if not value:
        return None
    if value not in KNOWN_PROVIDER_METRIC_LABELS:
        return "other"
    return value


def _system_metrics() -> dict:
    try:
        import psutil
    except Exception:
        return {"cpu_percent": None, "memory_rss_bytes": None}
    process = psutil.Process()
    return {
        "cpu_percent": process.cpu_percent(interval=None),
        "memory_rss_bytes": process.memory_info().rss,
    }
