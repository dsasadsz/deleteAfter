# Live Translation Platform

Python/FastAPI microservice for live online-lesson translation. It can create Zoom meetings, issue Meeting SDK embed config, ingest teacher browser microphone audio, run STT and translation, broadcast captions over WebSocket, synthesize TTS, receive student text/voice questions, and export transcripts.

The future C# / ASP.NET Core product owns users, roles, courses, lesson pages, access control, and product UI. Python owns the translation service capabilities and exposes a versioned integration contract.

## C# Integration Boundary

C# must use only:

```text
HTTP: /api/v1/integration/*
WS:   /ws/v1/*
```

Demo/internal routes such as `/api/lessons/*`, `/teacher/{lesson_id}`, `/student/{lesson_id}`, and `/ws/lessons/*` are for local development, manual QA, and diagnostics only. They are not production API for the C# site.

Browser clients must use short-lived scoped token URLs returned by `/student-token` and `/teacher-token`. Do not expose `X-Integration-Key` or `integration_key` to teacher/student browsers.

## Quick Start

```powershell
cd C:\Users\User\Desktop\TranslateInRealTme\live_translation_platform
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://127.0.0.1:8000` for the local demo UI.

For the first full manual call, follow [First Try: первый тестовый звонок](docs/FIRST_TRY.md).

## Database

Local development defaults to SQLite:

```env
DATABASE_URL=sqlite:///./dev.db
```

Production should use PostgreSQL:

```env
DATABASE_URL=postgresql+psycopg://live_translation:${POSTGRES_PASSWORD}@postgres:5432/live_translation
POSTGRES_REQUIRED_IN_PRODUCTION=true
SQLITE_ALLOWED_IN_PRODUCTION=false
```

`/api/health/ready` reports a structured database object with type, configured/readiness flags, and any warning/error. It never returns the connection string, password, or host. See [Production Deployment](docs/production.md) for pool settings, backups, and Compose usage.

## Redis Foundation

Redis is optional and disabled by default:

```env
REDIS_ENABLED=false
REDIS_URL=redis://localhost:6379/0
REDIS_PREFIX=live_translation
REDIS_REQUIRED_IN_PRODUCTION=false
```

When enabled, the app creates an async Redis client, pings it for readiness, and closes it on shutdown. Stage 25B does not move runtime state by itself.

Stage 25C can use Redis for shared fixed-window rate-limit counters:

```env
REDIS_ENABLED=true
REDIS_RATE_LIMIT_ENABLED=true
REDIS_RATE_LIMIT_FAIL_CLOSED=false
```

The in-memory limiter remains the default. Redis rate limiting uses shared counters only.

Stage 25D can use Redis Pub/Sub for live caption, question, debug, and diagnostic fanout:

```env
REDIS_ENABLED=true
REDIS_PUBSUB_ENABLED=true
```

Local WebSocket clients are still owned by each worker. Pub/Sub only fans out JSON events across workers; it has no history and does not carry raw audio chunks. TTS cache, lesson sessions, and audio queues stay in process memory.

## Shared TTS Cache

TTS direct audio remains compatible, and Stage 25E adds shared in-process cache plus optional audio URL mode:

```env
TTS_SHARED_CACHE_ENABLED=true
TTS_SHARED_CACHE_BACKEND=memory
TTS_SHARED_CACHE_MAX_ITEMS=1000
TTS_SHARED_CACHE_TTL_SECONDS=3600
TTS_AUDIO_URL_ENABLED=true
TTS_AUDIO_URL_TTL_SECONDS=3600
```

The cache key includes lesson, caption id, language, provider, voice, and a normalized text hash. Repeated requests for the same caption reuse cached audio, and concurrent requests for the same key are coalesced behind a per-key lock. Student autoplay uses URL mode when the server reports it available; old backlog and unavailable/waiting translation text are still skipped.

## Provider Quotas

`GET /api/providers/status?live=true` reports readiness plus manual quota hints and best-effort runtime usage for STT, Translator, and TTS providers. Configure known limits in `.env` first:

```env
AZURE_STT_MAX_CONCURRENT_SESSIONS=100
ELEVENLABS_STT_MAX_CONCURRENT_SESSIONS=
CARTESIA_STT_MAX_CONCURRENT_SESSIONS=
AZURE_TRANSLATOR_MAX_REQUESTS_PER_SECOND=
AZURE_TTS_MAX_REQUESTS_PER_SECOND=
ELEVENLABS_TTS_MAX_CONCURRENT_REQUESTS=
```

Responses include `quotas`, `runtime`, `recommendation`, and `recommended_action`. Secrets are redacted; no vendor billing APIs are called.

## Monitoring and Load Testing

`GET /api/metrics/runtime` reports live in-process counters for active lessons, caption/question WebSocket clients, active pipelines, audio queue sizes, dropped chunks, caption/TTS/question totals, STT disconnects, provider errors, and best-effort latency averages. CPU/RAM are included when `psutil` is installed.

Load testing is mock-first and does not call real providers by default:

```powershell
python scripts/load_test_students.py --help
python scripts/load_test_lessons.py --help
```

See [Load Testing](docs/load-testing.md) for a 1 lesson x 50/100 students workflow and manual-provider cautions.

For the staging 6 lessons / 1000 caption WebSocket mock-provider run, follow [Staging 1000 WebSocket Test Runbook](docs/staging-1000-ws-test-runbook.md). It explains how to run Stage 27B, analyze Stage 27C, package Stage 27D evidence, and keep the result scoped to mock WebSocket fanout only.

For a single production-like mock readiness command covering health, Redis Pub/Sub captions, and token-safe TTS URL cache behavior, see [1000-User Readiness Test](docs/1000-user-readiness-test.md):

```powershell
python scripts/run_1000_user_readiness_test.py --base-url http://127.0.0.1:8000
```

For Docker:

```powershell
docker build -t live-translation-platform:prod .
docker run --env-file .env -p 8000:8000 live-translation-platform:prod
```

For a production-like stack with PostgreSQL, Redis, disk TTS cache, and HTTPS/WSS reverse proxy examples, use:

- [Security Hardening Baseline](docs/security.md)
- [Release Operator Runbook](docs/release-operator-runbook.md)
- [Production Deployment Checklist](docs/production-deployment-checklist.md)
- [Reverse Proxy and WSS Setup](docs/reverse-proxy.md)
- [Real Provider E2E Test Guide](docs/real-provider-e2e.md)
- [Production Nginx Template](deploy/nginx/live_translation_platform.conf.example)
- [Nginx Reverse Proxy Example](docs/reverse-proxy-nginx.example.conf)
- [Caddy Reverse Proxy Example](docs/reverse-proxy-caddy.example)

After deployment:

```powershell
python scripts/check_deployment_readiness.py --base-url https://translation.example.com
python scripts/check_wss_routes.py --base-url https://translation.example.com --ws-base-url wss://translation.example.com --lesson-id lesson_123 --token "<scoped-token>"
python scripts/real_provider_e2e_check.py --base-url https://translation.example.com
```

## C# Handoff Docs

- [Project Report](docs/PROJECT_REPORT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [First Try: первый тестовый звонок](docs/FIRST_TRY.md)
- [Integration Contract](docs/integration-contract.md)
- [Machine-Readable Integration Contract](docs/integration-contract.json)
- [Production Deployment](docs/production.md)
- [Security Hardening Baseline](docs/security.md)
- [Release Operator Runbook](docs/release-operator-runbook.md)
- [Production Deployment Checklist](docs/production-deployment-checklist.md)
- [Load Testing](docs/load-testing.md)
- [1000-User Readiness Test](docs/1000-user-readiness-test.md)
- [Staging 1000 WebSocket Test Runbook](docs/staging-1000-ws-test-runbook.md)
- [Handoff Readiness Report](docs/HANDOFF_READINESS_REPORT.md)
- [C# Examples](examples/csharp/README.md)
- [JS Browser Examples](examples/js/)

## Minimal Integration Flow

1. C# backend calls `POST /api/v1/integration/lessons` with `X-Integration-Key`.
2. C# stores the returned Python `lesson_id` beside its own lesson row.
3. C# backend calls `/student-token` and `/teacher-token`.
4. C# pages pass only scoped token URLs to browsers.
5. Teacher browser opens Zoom host flow and streams microphone audio through `audio_ingest_websocket_url`.
6. Student browser uses `embed_config_url`, `captions_websocket_url`, TTS URLs, and question URLs from the token response.
7. C# backend fetches transcript/export/usage/cost through `/api/v1/integration/*` after the lesson.

## Verification

```powershell
pytest -q
python -m compileall -q app tests
python -m json.tool docs/integration-contract.json
node --check examples/js/student-lesson-client.js
node --check app/web/static/captions.js
node --check app/web/static/student_tts.js
```
