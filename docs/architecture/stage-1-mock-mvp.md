# Stage 1 Mock MVP Architecture

The service is an independent Python backend module. The demo frontend is only a temporary API/WebSocket client; a future C# site can replace it without changing the realtime pipeline because video, captions, lesson state, providers, and persistence are separated behind HTTP and WebSocket contracts.

## Boundaries

- `api/` exposes lesson, Zoom webhook, health, and caption/debug WebSocket routes.
- `web/` contains the temporary Jinja2 demo site.
- `zoom/` separates Zoom REST/OAuth/RTMS placeholders from mock meeting creation.
- `audio/` owns audio sources. Stage 1 uses `MockAudioSource`; later RTMS plugs into the same interface.
- `stt/` owns STT providers and emits `STTEvent` objects. Stage 1 implements `MockSTT`; real providers are explicit adapter stubs.
- `translation/` owns translation providers. Stage 1 implements deterministic `MockTranslator`; real providers are explicit adapter stubs.
- `realtime/` owns `CaptionHub`, `AudioPipeline`, `LessonSession`, and metrics shaping.
- `db/` owns SQLAlchemy models and repositories for lessons, transcripts, metrics, debug events, and connected student counts.

## Data Flow

`MockAudioSource -> AudioPipeline queue -> STTProvider -> TranslationProvider -> CaptionHub -> WebSocket clients`

Final captions and latency metrics are saved to SQLite. Partial captions are broadcast live; partial translations are controlled by `TRANSLATE_PARTIALS`.

## Video Rule

The student page has a video area, but video is never sent through Python WebSocket. In mock mode it renders a simulated player with a lesson clock. In real Zoom mode the same area is reserved for Zoom SDK/embed integration while captions continue to arrive through `/ws/lessons/{lesson_id}/captions`.

