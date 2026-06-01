# 1000-User Readiness Audit Report

## Executive Summary

- Overall status: strong staging/MVP foundation, but not ready for real 1000-student production yet.
- Ready/not ready for 1000: ready to continue controlled mock/staging load tests; not ready for real 1000 production with real STT/translation/TTS providers, C# browser URL-mode TTS, multi-worker deployment, and operational SLOs.
- Biggest risks: sequential WebSocket fanout can be held by slow clients; v1 TTS `audio_url` is currently unusable from browser token URLs; runtime state is still mostly process-local; real provider quotas and reconnect behavior are not production-proven; PostgreSQL/Redis are optional rather than enforced production dependencies.
- Recommended next stage: fix the v1 TTS audio URL auth bug first, then implement WebSocket slow-client protection and make PostgreSQL/Redis production deployment requirements explicit.

## Current Proven Capacity

Known local/mock results to carry forward:

- 500 idle captions WebSocket clients were previously proven locally.
- 1 lesson x 1000 simulated students x 3 mock captions/sec was previously reported as passed.
- Stage 25H adds `scripts/load_test_tts.py`; unit coverage verifies per-key TTS cache coalescing for 100 concurrent same-caption requests, and script `--help` passed in this audit.

Important limitation: these are mock/local results only. They prove useful pieces of the Python fanout/cache paths, not real Azure/ElevenLabs/Zoom provider capacity, reverse proxy behavior, browser behavior, or production multi-instance readiness.

## Scorecard

| Area | Score | Notes |
|---|---:|---|
| Captions WebSocket scalability | 6/10 | Local mock fanout looks promising, but broadcast has no slow-client timeout, per-client queue, or delivery latency metrics. |
| STT/translation scalability | 5/10 | Good 1 STT stream per lesson model and bounded queues; reconnect and quota handling are still weak. |
| TTS scalability | 6/10 | Shared cache/audio URL design is good, but v1 audio URL auth is broken and cache remains memory-only. |
| Redis/PostgreSQL readiness | 5/10 | Readiness toggles exist; production Compose still defaults to SQLite and Redis is optional/process-local gaps remain. |
| Monitoring/load test quality | 6/10 | Runtime counters and scripts exist; missing slow-client, proxy, provider, browser, multi-worker, and per-client delivery SLO tests. |
| Security | 6/10 | Scoped token model is solid; v1 audio URL auth mismatch, query-token logging risk, and dev bypass defaults need production hardening. |
| C# integration readiness | 6/10 | Contract is mostly clear; v1 TTS URL-mode bug and C# sample gaps are blockers for handoff. |
| Production deployment readiness | 5/10 | Docker and health checks exist; needs enforced PostgreSQL/Redis policy, migrations, proxy/WSS test, and ops runbooks. |
| Operational readiness | 5/10 | Provider status/quota hints exist, but no real-provider load proof or alert-grade metrics. |

## Critical Findings

| ID | Severity | Area | Issue | Evidence | Impact at 1000 users | Recommended fix | Files |
|---|---|---|---|---|---|---|---|
| C-001 | Critical | C# / TTS / Auth | v1 TTS `audio_url` returned to browser tokens is blocked by integration API-key dependency. | `api_router` applies `Depends(require_integration_key)` to all `/api/v1/integration/*`; `_token_auth_for_path()` has regexes for TTS status/synthesize but not TTS audio. Reproduced in audit: v1 synthesize returned 200 and v1 `audio_url`; GET returned 401 "Missing or invalid integration API key." | C# student pages using URL-mode TTS will receive audio URLs that cannot be played. At 1000 users this defeats the shared audio URL design and creates failed playback or direct-audio fallbacks. | Add `/tts/audio/{audio_id}` token auth to `app/integration/auth.py`, require `tts:play`, and add a regression test that GETs the returned v1 audio URL with the generated token. | `app/api/integration.py:61`, `app/api/integration.py:315`, `app/integration/auth.py:17`, `app/integration/auth.py:172` |
| C-002 | Critical | Captions WebSocket | Caption and question hubs broadcast sequentially with no slow-client isolation. | `CaptionHub._broadcast()` loops clients and awaits `websocket.send_json(payload)` one by one; `QuestionHub.deliver()` does the same. Only `RuntimeError` is caught. | One slow or wedged WebSocket can delay delivery to the rest of a 1000-student lesson and delay the pipeline path that awaits publish. Reconnect storms can amplify this. | Add per-client bounded outgoing queues or per-send timeout, disconnect slow clients, count send failures/latency/queue lag, and test with artificial slow clients. | `app/realtime/caption_hub.py:52`, `app/realtime/question_hub.py:29` |

## Important Findings

| ID | Severity | Area | Issue | Evidence | Impact at 1000 users | Recommended fix | Files |
|---|---|---|---|---|---|---|---|
| I-001 | High | TTS rate limits | TTS rate limit is enforced before cache lookup, so cache hits are rate-limited. | `_enforce_tts_rate_limit()` runs before `_synthesize_with_shared_cache()`; tests explicitly assert cached second request can be 429 when limit is 1/min. | If TTS is enabled for frequent captions, students hit `TTS_RATE_LIMITED` even when the provider call would be saved by cache. | Decide product policy: separate synthesize/provider-call limit from cached playback/audio URL fetch limit; allow cached URL mode at a higher per-student rate. | `app/api/tts.py:85`, `tests/test_stage25e_shared_tts_cache.py` |
| I-002 | High | Runtime state / deployment | Multi-worker/multi-instance is still not safe by default. | Docker pins `--workers 1`; docs say WebSocket clients are local to each worker and TTS cache, sessions, audio queues remain in memory. Redis Pub/Sub is optional and live-only. | Horizontal scaling can split clients, sessions, queues, cache, and provider state. A single worker becomes the practical ceiling and single point of load. | Keep production `workers=1` until Redis Pub/Sub is enabled and tested; then move session coordination and TTS cache/audio storage out of process before multi-instance. | `Dockerfile:23`, `docs/production.md:76`, `README.md:82` |
| I-003 | High | STT provider resilience | Config exposes reconnect counts, but real STT providers do not actually reconnect streams in the live pipeline. | ElevenLabs/Cartesia accept `max_reconnects`; code stores it but no reconnect loop uses it. Pipeline `_handle_stt_failure()` sets `_accepting_audio=False` and stops running. ElevenLabs `receive_timeout_seconds` is not applied to `recv()`. | One provider disconnect can stop captions for the lesson until manual restart. With 10-20 lessons, transient vendor issues become visible class-wide outages. | Implement reconnect with bounded buffering/drop policy, explicit provider status events, and user-facing degraded/recovering states. | `app/stt/elevenlabs_stt.py:59`, `app/stt/elevenlabs_stt.py:195`, `app/stt/cartesia_stt.py:50`, `app/realtime/audio_pipeline.py:375` |
| I-004 | High | Provider quotas | Quota handling is advisory, not enforcement or capacity proof. | Stage 25F reports manual quota hints and local counters; docs warn real-provider load tests are manual. | 1000 students do not multiply STT streams for one lesson, but 10-20 simultaneous lessons and TTS variants can exceed STT/translation/TTS quotas. | Add provider admission checks before starting lessons, alerting on 429s, quota dashboards, and a small real-provider E2E load rehearsal. | `app/providers/quotas.py`, `docs/production.md:142` |
| I-005 | High | Database | Production still defaults to SQLite unless operator opts into PostgreSQL. No Alembic migration system exists. | `docker-compose.prod.yml` default `DATABASE_URL` is SQLite and `POSTGRES_REQUIRED_IN_PRODUCTION=false`; production docs note no Alembic. | SQLite write contention and ad hoc schema creation are not appropriate for real 1000-user operations, exports, questions, usage, and captions. | Require PostgreSQL for production, set `POSTGRES_REQUIRED_IN_PRODUCTION=true`, add migrations, backup/restore rehearsal, and DB pool sizing. | `docker-compose.prod.yml:17`, `docker-compose.prod.yml:22`, `docs/production.md:194` |
| I-006 | High | TTS cache/storage | Shared TTS cache is memory-only and bounded by item count, not bytes. | `MemoryTTSSharedCache(max_items=1000)` stores full audio bytes in process memory. `REDIS_TTS_CACHE_ENABLED` is reserved/future. | 1000 cached audio items can be much larger than expected depending on provider/audio format; restart loses cache and triggers provider call bursts. | Move audio objects to Redis/disk/S3/object storage or CDN, add byte-size cap and eviction metrics, and keep in-process only as L1. | `app/tts/shared_cache.py:36`, `app/main.py:130`, `docs/production.md:85` |
| I-007 | High | Load testing | Current mock load tests do not cover slow clients, reconnect storms, real browser playback, reverse proxy WSS, or real providers. | Scripts cover simulated caption WS receive counts and mock caption publishing; TTS script targets cache. Docs warn mock captions are not TTS/provider proof. | A 1000 mock pass can hide real bottlenecks: proxy timeouts, browser memory, send backpressure, provider throttling, and audio egress. | Add staged load suite with per-client received counts, slow readers, reconnect storm, browser test, proxy test, TTS 100/500/1000 URL-mode test, and small real-provider test. | `scripts/load_test_students.py`, `scripts/load_test_lessons.py`, `scripts/load_test_tts.py`, `docs/load-testing.md:62` |
| I-008 | High | C# sample | C# sample does not model URL-mode TTS and does not assemble fragmented WebSocket messages. | `TtsSynthesizeRequest` lacks `return_mode`; `SynthesizeTtsAsync` always returns bytes; `ConnectCaptionsAsync` reads one `ReceiveAsync` buffer and deserializes immediately. | C# team may implement direct audio instead of shared URL mode, or hit rare JSON truncation if WS messages fragment. | Add URL-mode DTO, audio URL playback notes, and robust WebSocket receive loop until `EndOfMessage`; include a minimal `.csproj` compile check. | `examples/csharp/TranslationServiceClient.cs:82`, `examples/csharp/TranslationServiceClient.cs:145`, `examples/csharp/TranslationServiceClient.cs:252` |

## Medium/Low Findings

| ID | Severity | Area | Issue | Evidence | Impact at 1000 users | Recommended fix | Files |
|---|---|---|---|---|---|---|---|
| M-001 | Medium | Metrics | `captions_sent_total` counts caption events, not per-client deliveries. | `RuntimeMetrics.record_caption()` increments once in `deliver_caption()` before broadcasting. | At 3 captions/sec and 1000 clients, metric reads 3/sec, not 3000 client deliveries/sec; snapshot after tests can mislead. | Add `caption_client_deliveries_total`, send failures, per-client latency/p95, and slow-client disconnect counters. | `app/monitoring/metrics.py:21`, `app/realtime/caption_hub.py:37` |
| M-002 | Medium | Runtime cleanup/metrics | Stopped lesson sessions remain in `LessonSessionManager.sessions`; active lesson count can drift upward. | `stop()` stops session but does not remove it; runtime snapshot uses `len(sessions)` for `active_lessons`. | Long-running service can show misleading active lesson counts and retain process-local objects. | Remove stopped sessions or distinguish `known_sessions` from `active_lessons`; add cleanup test. | `app/realtime/lesson_session.py:134`, `app/monitoring/metrics.py:84` |
| M-003 | Medium | WebSocket robustness | Broadcast catches only `RuntimeError`. | Send errors other than `RuntimeError` can abort the broadcast loop and leave stale clients. | A single unexpected send failure can skip later clients in the same broadcast. | Catch expected WebSocket send exception classes, log sanitized reason, disconnect stale client, increment metrics. | `app/realtime/caption_hub.py:56`, `app/realtime/question_hub.py:35` |
| M-004 | Medium | Audio ingest | Browser audio manager keeps per-lesson queues/state in memory without explicit retirement. | Queues are created in `_ensure_queue()` and disconnect does not remove them. | Many lessons over time can grow process-local state. | Add lesson/session cleanup on stop/end and retention policy for inactive lesson state. | `app/realtime/browser_audio_manager.py:369`, `app/realtime/browser_audio_manager.py:167` |
| M-005 | Medium | Security | TTS audio endpoint accepts `captions:read` as fallback scope although spec says `tts:play`. | `_authorize_tts_http()` requires `tts:play` or falls back to `captions:read`; integration spec says `audio_scope: tts:play`. | Broader-than-documented token can fetch guessed audio IDs. Audio IDs are hard to guess, but scope semantics should be tight. | Require `tts:play` for audio bytes, or explicitly document `captions:read` fallback and risk. | `app/api/tts.py:421`, `app/integration/spec.py` |
| M-006 | Medium | Security/ops | Browser tokens are transported in query strings. App access logs omit query path, but clients/proxies may log full URLs. | Student/teacher token URLs use `?token=...`; C# sample prints URLs. | Tokens may appear in browser history, proxy logs, screenshots, or support logs. | Keep short TTLs, disable query logging at proxy, prefer `Authorization` or `Sec-WebSocket-Protocol` where practical, and document log hygiene. | `app/api/integration.py:250`, `examples/csharp/Program.cs:29` |
| M-007 | Medium | Monitoring | CPU/RAM metrics are optional but `psutil` is not in requirements. | Runtime metrics return null if import fails; requirements do not include `psutil`. | Operators may expect CPU/RAM in `/api/metrics/runtime` but receive null in Docker. | Add `psutil` to production requirements or document null as default. | `app/monitoring/metrics.py:133`, `requirements.txt` |
| M-008 | Low | Docs | Architecture header still says current implemented status through Stage 22 while later sections describe Stage 25. | Stage summary includes 25G, but opening text is stale. | Confusing for C#/ops handoff. | Refresh docs after Stage 25H. | `docs/ARCHITECTURE.md` |
| M-009 | Low | Framework lifecycle | Test suite reports FastAPI `on_event` deprecation warnings. | `pytest -q` passed but emitted warnings for startup/shutdown `on_event`. | Not a 1000-user blocker; future FastAPI maintenance issue. | Move to lifespan handler when doing maintenance. | `app/main.py:277`, `app/main.py:297` |

## 1000-User Scenario Analysis

### 1. 1 lesson x 1000 students captions only

- Expected bottleneck: WebSocket fanout send path, event-loop pressure, reverse proxy connection limits.
- Provider API risk: low for students, because this is still 1 teacher audio stream and 1 STT/translation path.
- Server risk: sequential `send_json()` fanout and slow clients; no per-client latency/send-failure metrics.
- Required architecture: one worker with enough file descriptors and WSS proxy tuning can be staged; before production add slow-client protection and proxy test.

### 2. 1 lesson x 1000 students with TTS enabled for 10%

- Expected bottleneck: TTS request rate and audio byte egress for about 100 students.
- Provider API risk: low only when all 100 request the same caption/language/voice and shared cache works; high if voices/languages vary.
- Server risk: current per-token TTS limit can block cache hits; v1 audio URL bug breaks C# URL mode.
- Required architecture: fixed v1 audio URL auth, shared cache enabled, URL mode enabled, clear policy that TTS is opt-in and not every caption by default.

### 3. 1 lesson x 1000 students with TTS enabled for 100%

- Expected bottleneck: audio URL GET egress and per-student request rate.
- Provider API risk: one provider call per caption only if same language/voice; up to language x voice variants per caption otherwise.
- Server risk: in-memory audio cache and Python serving audio bytes become hot path; rate limit at 20/min/student blocks frequent caption playback.
- Required architecture: object storage/CDN or Redis/disk audio backend, cache-aware rate limit, provider quota guardrails, and TTS UX that does not autoplay for everyone by default.

### 4. 10 lessons x 100 students

- Expected bottleneck: 10 simultaneous STT streams, translation calls per final segment, and per-lesson fanout.
- Provider API risk: real STT concurrency and translation RPS start matter; quota hints are not enforcement.
- Server risk: one worker runs all pipelines and WebSockets; slow fanout in one lesson mainly delays that lesson's pipeline, but CPU/DB are shared.
- Required architecture: PostgreSQL, provider quota admission checks, slow-client isolation, and load test with 10 live mock lessons.

### 5. 20 lessons x 50 students

- Expected bottleneck: 20 STT streams and translator/TTS quotas rather than caption fanout count alone.
- Provider API risk: STT concurrent session limits and translation RPS become primary risk.
- Server risk: one-worker deployment may be near practical ceiling; multi-worker requires Redis Pub/Sub and more shared state work.
- Required architecture: PostgreSQL, Redis Pub/Sub tested, explicit one-worker vs multi-worker guidance, and real-provider small-scale rehearsal.

## Required Architecture Before Real 1000 Users

- PostgreSQL required in production with migrations, backup/restore, pool sizing, and `POSTGRES_REQUIRED_IN_PRODUCTION=true`.
- Redis required for production rate limiting and cross-worker Pub/Sub when scaling beyond one worker.
- Redis Pub/Sub or equivalent for `CaptionHub`/`QuestionHub` before multi-worker/multi-instance.
- Shared TTS cache plus audio URL mode, with v1 audio URL auth fixed.
- Durable TTS audio backend: Redis/disk/S3/object storage/CDN, not only process memory.
- Provider quota checks before starting lessons; operator-visible limits for STT concurrent streams, translator RPS, TTS RPS/concurrency.
- Monitoring for WebSocket send failures, per-client delivery latency, disconnect reasons, queue lag, TTS hit ratio, provider calls saved, and 429s.
- Reverse proxy with WSS support, high connection limits, timeout tuning, disabled query logging for tokens, and health/readiness routing.
- Load tests that include real WebSocket delivery counts, slow clients, reconnect storms, TTS cache load, questions flow, browser clients, proxy, and small real-provider checks.
- C# token-safe integration: backend owns `X-Integration-Key`; browsers receive only scoped token URLs; C# UI renders Zoom video directly and captions JSON separately.
- One-worker guidance: keep `uvicorn --workers 1` until shared state/session coordination is fully implemented and tested.

## Load Test Gaps

- Captions test with per-client received counts at 500/1000 clients while publishing 3-5 captions/sec.
- Slow WebSocket reader test to prove one client cannot delay all others.
- Reconnect storm test: 1000 clients reconnecting over 30-60 seconds.
- TTS cache test: 100/500/1000 same-caption URL-mode requests against a live local server.
- TTS variant test: different voice/language/provider combinations to estimate provider-call fanout.
- Student questions text WebSocket test with teacher subscribers.
- Voice question test with max duration/bytes/rate limits and provider timeout paths.
- Real provider small-scale E2E: 1-2 lessons, low caption/TTS rates, quotas checked first.
- Browser-based test for actual C#/student page memory, audio autoplay, and Zoom embed coexistence.
- Reverse proxy WSS test through the intended production proxy/load balancer.
- Multi-worker Pub/Sub test only after Redis is required and configured.

## Security/Production Checklist

- [ ] Do not expose `X-Integration-Key` or integration key query params to browser pages.
- [ ] Fix v1 TTS audio URL token auth and require `tts:play`.
- [ ] Set `SECURITY_SIGNING_SECRET` in production secret storage.
- [ ] Disable demo/dev bypasses in production: `ALLOW_DEV_WS_WITHOUT_TOKEN=false`, `WEBSOCKET_AUTH_REQUIRED_IN_PRODUCTION=true`.
- [ ] Keep `ENABLE_LOAD_TEST_ENDPOINTS=false` except isolated development load tests.
- [ ] Keep `ENABLE_DEBUG_ENDPOINTS=false` and `ENABLE_OPENAPI_DOCS=false` in production unless deliberately exposed behind auth.
- [ ] Set `ALLOWED_ORIGINS` and `TRUSTED_HOSTS`.
- [ ] Enforce HTTPS/WSS and set `INTEGRATION_REQUIRE_HTTPS=true`.
- [ ] Ensure proxy logs do not record token query strings.
- [ ] Never send Zoom `start_url` to students; only backend/teacher flow may see it.
- [ ] Keep real provider keys out of scripts, command lines, screenshots, and logs.
- [ ] Require PostgreSQL/Redis health when production policy depends on them.

## C# Handoff Notes

- Zoom video does not go through Python. Students use Zoom Meeting SDK in the C# page.
- Python sends JSON captions over `/ws/v1/lessons/{lesson_id}/captions`; C# renders captions and should keep only the last 6-8 visible.
- Teacher microphone audio must be sent from the teacher browser to Python via `audio_ingest_websocket_url`.
- TTS should use URL mode and shared cache when enabled; do not have every student request direct audio bytes for every caption.
- C# backend must never pass `X-Integration-Key` to browsers.
- Student browsers should use URLs returned by `/student-token`; teacher browsers should use URLs returned by `/teacher-token`.
- C# should not auto-enable TTS for everyone by default at 1000 users. Make it opt-in, language/voice explicit, and consider "latest only" mode.
- C# WebSocket client sample should be updated to assemble fragmented messages until `EndOfMessage`.
- C# sample should add `return_mode=url` DTOs and audio URL playback/HTTP fetch examples after C-001 is fixed.
- Demo endpoints such as `/api/lessons/*`, `/teacher/*`, `/student/*`, and `/ws/lessons/*` are for Python local QA, not C# production.

## Recommended Next Stages

1. Fix v1 TTS audio URL auth regression and add end-to-end token test for returned audio URL.
2. PostgreSQL readiness hardening: require PostgreSQL in production, add Alembic migrations, backup/restore and pool test.
3. Redis foundation as production requirement for shared rate limits and readiness fail-closed.
4. Redis Pub/Sub for CaptionHub/QuestionHub with multi-worker load test.
5. TTS cache backend to Redis/disk/S3/object storage and byte-size limits.
6. Provider quotas/status: admission checks, alert-grade metrics, and provider 429/error dashboards.
7. WebSocket slow-client protection: per-client queues, send timeouts, disconnect policy, metrics.
8. Production reverse proxy WSS test with 1000 mock clients and reconnect storm.
9. Real provider E2E small-scale test after quotas are configured.
10. C# handoff refresh: compileable sample project, URL-mode TTS DTOs, robust WebSocket receive loop, token-safe browser guidance.

## Verification Results

| Command | Result |
|---|---|
| `pytest -q` | Passed: 388 tests. Warnings: FastAPI `on_event` deprecation and Starlette `TemplateResponse` signature deprecation. |
| `python -m compileall -q app tests scripts` | Passed. |
| `python -m json.tool docs/integration-contract.json` | Passed. |
| `node --check app/web/static/captions.js` | Passed. |
| `node --check app/web/static/student_tts.js` | Passed. |
| `node --check app/web/static/student_questions.js` | Passed. |
| `node --check app/web/static/teacher_mic.js` | Passed. |
| `python scripts/load_test_students.py --help` | Passed. |
| `python scripts/load_test_lessons.py --help` | Passed. |
| `python scripts/load_test_tts.py --help` | Passed. |
| Additional v1 TTS URL check | Reproduced C-001: v1 URL-mode synthesize returned 200; GET of returned v1 audio URL with token returned 401. |

No real Azure, ElevenLabs, Zoom, or large 1000-user load test was run during this audit.

## Final Verdict

Ready for MVP/staging: yes, especially for mock/local and controlled C# integration testing.

Not ready for real 1000 production yet: yes. The current project should not be presented as production-ready for 1000 real students with real providers until the critical TTS auth bug, slow-client WebSocket risk, PostgreSQL/Redis production posture, provider quota/reconnect behavior, and proxy/load-test gaps are addressed.

Ready after required infra work: likely, because the core shape is right: Zoom video is outside Python, teacher audio is one stream per lesson, caption fanout is isolated by lesson, and TTS has a shared-cache design. The next work is hardening rather than a full rewrite.
