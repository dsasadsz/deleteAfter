# Дерево файлов проекта

Коротко: это FastAPI-сервис для онлайн-перевода уроков в реальном времени. Он принимает аудио учителя, делает STT, перевод, субтитры, TTS, вопросы студентов, экспорт транскриптов и интеграцию с будущим C# сайтом.

Примечание: `.venv/`, `.idea/`, `.pytest_cache/`, `__pycache__/`, `.playwright-mcp/` и `tmp/` - служебные/сгенерированные папки. Их смысл описан внизу одним блоком, потому что внутри в основном зависимости, логи, кэш и временные файлы.

```text
TranslateInRealTme/
|- FILE_TREE_RU.md -- это дерево файлов и назначение каждого важного файла проекта.
|- docs/ -- общие заметки и ранние планы проекта.
|  |- dd.md -- черновые заметки/документация верхнего уровня.
|  |- key.md -- заметки по ключам/настройкам; осторожно, может содержать чувствительные данные.
|  |- architecture/
|  |  `- stage-1-mock-mvp.md -- архитектура первого mock-MVP.
|  `- superpowers/
|     `- plans/
|        `- 2026-05-08-stage-1-mock-mvp.md -- план реализации первого mock-MVP.
|- tmp/ -- временная папка верхнего уровня; сейчас без значимых исходников.
`- live_translation_platform/ -- основной Python/FastAPI проект.
   |- .dockerignore -- какие файлы не отправлять в Docker image.
   |- .env -- локальные переменные окружения; секреты и реальные настройки, не коммитить публично.
   |- .env.example -- пример настроек окружения для запуска проекта.
   |- 0` -- пустой/случайный файл, в работе приложения не используется.
   |- azure_kazakh_tts_test.py -- ручной тест Azure TTS для казахского/голосов.
   |- dev.db -- локальная SQLite база для разработки.
   |- docker-compose.yml -- docker-compose для локального запуска.
   |- docker-compose.prod.yml -- docker-compose для production-like запуска.
   |- Dockerfile -- сборка контейнера сервиса.
   |- pytest.ini -- настройки pytest.
   |- README.md -- главный README: запуск, интеграция, команды проверки.
   |- requirements.txt -- Python-зависимости проекта.
   |
   |- app/ -- основной код приложения.
   |  |- __init__.py -- делает `app` Python-пакетом.
   |  |- config.py -- все настройки через env/.env: провайдеры, Zoom, Redis, STT, TTS, безопасность.
   |  |- e2e_qa.py -- логика E2E QA-отчетов, чеклистов и метрик ручной проверки.
   |  |- live_tests.py -- логика live-mic тестов и отчетов по качеству.
   |  |- logging_config.py -- настройка логирования приложения.
   |  |- main.py -- запуск FastAPI: создает app, подключает роуты, БД, менеджеры, middleware.
   |  |- middleware.py -- request id и access-log middleware.
   |  |- production.py -- production-проверки, readiness/config validation, маскирование секретов.
   |  |- runtime.py -- фоновые задачи и корректное завершение managers/Redis.
   |  |
   |  |- api/ -- HTTP/WebSocket API роуты.
   |  |  |- __init__.py -- пакет API.
   |  |  |- captions.py -- WebSocket субтитров, debug, diagnostics и audio ingest.
   |  |  |- compare.py -- API сравнения STT/перевода между провайдерами.
   |  |  |- e2e_qa.py -- API для E2E QA прогонов и отчетов.
   |  |  |- exports.py -- API экспорта транскриптов в JSON/SRT/VTT/Markdown/HTML и генерации заметок.
   |  |  |- glossaries.py -- API глоссариев и терминов.
   |  |  |- health.py -- health/readiness/config endpoints.
   |  |  |- integration.py -- production-контракт `/api/v1/integration/*` для C# backend.
   |  |  |- lessons.py -- demo/internal API уроков.
   |  |  |- live_tests.py -- API live microphone тестов.
   |  |  |- providers.py -- API статуса STT/translation/TTS провайдеров.
   |  |  |- questions.py -- API вопросов студентов и модерации преподавателем.
   |  |  |- real_test.py -- API ручного real-test сценария.
   |  |  |- smoke.py -- API smoke-тестов.
   |  |  |- tts.py -- API синтеза речи и статуса TTS.
   |  |  |- usage.py -- API учета использования и стоимости.
   |  |  `- zoom.py -- API Zoom, webhook и Meeting SDK.
   |  |
   |  |- audio/ -- источники аудио.
   |  |  |- __init__.py -- пакет audio.
   |  |  |- base.py -- базовые типы `AudioChunk` и `AudioSource`.
   |  |  |- browser_mic_audio_source.py -- источник аудио из браузерного микрофона через WebSocket.
   |  |  |- mock_audio_source.py -- fake/mock источник аудио для тестов и демо.
   |  |  `- zoom_rtms_audio_source.py -- источник аудио из Zoom RTMS.
   |  |
   |  |- compare/ -- сравнение провайдеров.
   |  |  |- __init__.py -- пакет compare.
   |  |  |- hub.py -- WebSocket/event hub для событий сравнения.
   |  |  `- runner.py -- запуск comparison jobs и сбор результата.
   |  |
   |  |- db/ -- база данных.
   |  |  |- __init__.py -- пакет db.
   |  |  |- database.py -- SQLAlchemy engine/session/base.
   |  |  |- models.py -- ORM-модели: уроки, сегменты, вопросы, usage, smoke, glossary и т.д.
   |  |  `- repositories.py -- репозитории для чтения/записи моделей.
   |  |
   |  |- export/ -- построение и экспорт транскриптов.
   |  |  |- __init__.py -- пакет export.
   |  |  |- html_exporter.py -- экспорт транскрипта в HTML.
   |  |  |- markdown_exporter.py -- экспорт транскрипта в Markdown.
   |  |  |- notes_generator.py -- генерация summary/notes по уроку.
   |  |  |- schemas.py -- схемы данных экспорта.
   |  |  |- srt_exporter.py -- экспорт субтитров в SRT.
   |  |  |- text_selection.py -- выбор текста/языка/нормализации для экспорта.
   |  |  |- transcript_builder.py -- сборка транскрипта из сегментов БД.
   |  |  `- vtt_exporter.py -- экспорт субтитров в WebVTT.
   |  |
   |  |- glossary/ -- глоссарии и терминология.
   |  |  |- __init__.py -- пакет glossary.
   |  |  |- default_glossaries.py -- встроенные дефолтные термины.
   |  |  |- normalizer.py -- нормализация транскрипта по глоссарию.
   |  |  |- postprocessor.py -- постобработка перевода с учетом терминов.
   |  |  `- schemas.py -- схемы глоссариев и терминов.
   |  |
   |  |- infra/ -- инфраструктурные адаптеры.
   |  |  |- __init__.py -- пакет infra.
   |  |  `- redis.py -- создание Redis-клиента, health-check, ключи и закрытие соединения.
   |  |
   |  |- integration/ -- контракт интеграции с внешним C# backend.
   |  |  |- __init__.py -- пакет integration.
   |  |  |- auth.py -- проверка integration key и scoped tokens.
   |  |  |- callbacks.py -- отправка callback-событий наружу.
   |  |  |- schemas.py -- request/response схемы integration API.
   |  |  `- spec.py -- генерация machine-readable integration contract.
   |  |
   |  |- questions/ -- вопросы студентов.
   |  |  |- __init__.py -- пакет questions.
   |  |  |- audio_handler.py -- обработка голосового вопроса из аудио.
   |  |  |- schemas.py -- схемы вопросов и событий.
   |  |  `- service.py -- бизнес-логика создания, ответа, dismiss и трансляции вопросов.
   |  |
   |  |- realtime/ -- runtime для живого урока.
   |  |  |- __init__.py -- пакет realtime.
   |  |  |- audio_pipeline.py -- STT -> перевод -> captions -> метрики, главная аудио-цепочка.
   |  |  |- browser_audio_manager.py -- управление browser audio ingest с очередями и диагностикой.
   |  |  |- caption_hub.py -- подписчики и broadcast caption-событий.
   |  |  |- lesson_session.py -- жизненный цикл live-сессии урока.
   |  |  |- metrics.py -- расчет latency/метрик real-time pipeline.
   |  |  |- question_hub.py -- broadcast вопросов студент/учитель.
   |  |  `- rtms_manager.py -- управление Zoom RTMS клиентами и аудио/транскрипт событиями.
   |  |
   |  |- schemas/ -- общие Pydantic-схемы.
   |  |  |- __init__.py -- пакет schemas.
   |  |  |- browser_audio.py -- схемы browser audio events/diagnostics.
   |  |  |- caption.py -- схемы caption-событий.
   |  |  |- lesson.py -- схемы урока.
   |  |  |- metrics.py -- схемы метрик.
   |  |  |- provider.py -- схемы статуса провайдеров.
   |  |  `- rtms.py -- схемы Zoom RTMS.
   |  |
   |  |- security/ -- безопасность.
   |  |  |- __init__.py -- пакет security.
   |  |  |- rate_limit.py -- in-memory rate limiter.
   |  |  |- schemas.py -- схемы security/token responses.
   |  |  |- scopes.py -- названия и проверки scopes.
   |  |  `- tokens.py -- создание и проверка подписанных scoped tokens.
   |  |
   |  |- smoke/ -- smoke-тесты провайдеров.
   |  |  |- __init__.py -- пакет smoke.
   |  |  |- audio_samples.py -- загрузка/хранение аудио-сэмплов для smoke.
   |  |  |- hub.py -- event hub smoke-тестов.
   |  |  |- provider_status.py -- проверка готовности провайдеров.
   |  |  `- runner.py -- запуск smoke pipeline и запись результата.
   |  |
   |  |- stt/ -- speech-to-text провайдеры.
   |  |  |- __init__.py -- пакет stt.
   |  |  |- azure_stt.py -- Azure Speech-to-Text provider.
   |  |  |- base.py -- базовые STT-события и интерфейс provider.
   |  |  |- cartesia_stt.py -- Cartesia STT provider.
   |  |  |- elevenlabs_stt.py -- ElevenLabs STT provider.
   |  |  `- mock_stt.py -- mock STT provider для демо/тестов.
   |  |
   |  |- translation/ -- провайдеры перевода.
   |  |  |- __init__.py -- пакет translation.
   |  |  |- azure_translator.py -- Azure Translator provider.
   |  |  |- base.py -- базовый интерфейс переводчика.
   |  |  |- google_translator.py -- Google Translate provider.
   |  |  |- llm_translator.py -- LLM-based переводчик.
   |  |  `- mock_translator.py -- mock переводчик для демо/тестов.
   |  |
   |  |- tts/ -- text-to-speech.
   |  |  |- __init__.py -- пакет tts.
   |  |  |- azure_tts.py -- Azure TTS provider.
   |  |  |- base.py -- базовый интерфейс TTS.
   |  |  |- cache.py -- in-memory cache TTS-аудио.
   |  |  |- elevenlabs_tts.py -- ElevenLabs TTS provider.
   |  |  |- factory.py -- выбор TTS provider по настройкам.
   |  |  |- mock_tts.py -- mock TTS provider.
   |  |  |- schemas.py -- схемы TTS request/response/status.
   |  |  `- voice_catalog.py -- каталог голосов по языкам и провайдерам.
   |  |
   |  |- usage/ -- учет использования и стоимости.
   |  |  |- __init__.py -- пакет usage.
   |  |  |- cost_estimator.py -- расчет стоимости по usage и pricing.
   |  |  |- pricing.py -- дефолтные цены провайдеров.
   |  |  |- repository.py -- запись/чтение usage и pricing.
   |  |  |- schemas.py -- схемы usage/cost.
   |  |  `- usage_tracker.py -- сбор usage: аудио, STT, translation, TTS.
   |  |
   |  |- web/ -- локальный demo UI.
   |  |  |- __init__.py -- пакет web.
   |  |  |- routes.py -- HTML routes для страниц demo UI.
   |  |  |- static/
   |  |  |  |- app.js -- общий JS для demo UI.
   |  |  |  |- audio_worklet_processor.js -- AudioWorklet для захвата микрофона.
   |  |  |  |- captions.js -- клиентская логика отображения captions.
   |  |  |  |- compare.js -- UI сравнения провайдеров.
   |  |  |  |- e2e_test.js -- UI запуска E2E QA теста.
   |  |  |  |- e2e_test_report.js -- UI отчета E2E QA.
   |  |  |  |- glossaries.js -- UI управления глоссариями.
   |  |  |  |- live_tests.js -- UI live microphone tests.
   |  |  |  |- real_test.js -- UI ручного real test.
   |  |  |  |- smoke.js -- UI smoke-тестов.
   |  |  |  |- student_questions.js -- UI вопросов студента, включая голосовой вопрос.
   |  |  |  |- student_tts.js -- UI TTS на странице студента.
   |  |  |  |- styles.css -- стили demo UI.
   |  |  |  |- teacher_mic.js -- захват микрофона преподавателя и отправка audio ingest.
   |  |  |  |- teacher_questions.js -- UI модерации вопросов преподавателем.
   |  |  |  |- transcript.js -- UI просмотра/поиска/заметок транскрипта.
   |  |  |  |- usage.js -- UI usage/cost.
   |  |  |  |- zoom_meeting.js -- встраивание Zoom Meeting SDK и audio ducking.
   |  |  |  `- zoom_placeholder.js -- заглушка видео/таймера для локального UI.
   |  |  `- templates/
   |  |     |- base.html -- базовый layout HTML.
   |  |     |- compare.html -- страница сравнения провайдеров.
   |  |     |- dashboard.html -- dashboard demo UI.
   |  |     |- e2e_test.html -- страница E2E QA теста.
   |  |     |- e2e_test_report.html -- страница отчета E2E QA.
   |  |     |- glossaries.html -- список глоссариев.
   |  |     |- glossary_detail.html -- карточка/редактирование одного глоссария.
   |  |     |- index.html -- главная страница локального demo UI.
   |  |     |- live_tests.html -- страница live microphone tests.
   |  |     |- live_tests_report.html -- отчет live tests.
   |  |     |- real_test.html -- страница ручного real test.
   |  |     |- smoke.html -- страница smoke-тестов.
   |  |     |- student.html -- страница студента.
   |  |     |- teacher.html -- страница преподавателя.
   |  |     |- transcript.html -- страница транскрипта.
   |  |     `- usage.html -- страница usage/cost.
   |  |
   |  `- zoom/ -- интеграция с Zoom.
   |     |- __init__.py -- пакет zoom.
   |     |- meeting_sdk.py -- генерация Meeting SDK embed config/signature.
   |     |- mock_zoom.py -- mock Zoom client для разработки.
   |     |- models.py -- модели/типы Zoom.
   |     |- zoom_api_client.py -- REST-клиент Zoom API.
   |     |- zoom_oauth.py -- OAuth client для Zoom server-to-server token.
   |     |- zoom_rtms_client.py -- клиент Zoom RTMS.
   |     `- zoom_webhooks.py -- проверка и обработка Zoom webhook событий.
   |
   |- docs/ -- документация проекта.
   |  |- superpowers/
   |  |  |- plans/
   |  |  |  |- 2026-05-13-stage-18-live-mic-test-matrix.md -- план Stage 18 live mic test matrix.
   |  |  |  |- 2026-05-14-stage20-tts.md -- план Stage 20 TTS.
   |  |  |  `- 2026-05-14-stage21-audio-ducking.md -- план Stage 21 audio ducking.
   |  |  `- specs/
   |  |     |- 2026-05-13-stage-18-live-mic-test-matrix-design.md -- дизайн Stage 18.
   |  |     |- 2026-05-14-stage20-tts-design.md -- дизайн Stage 20 TTS.
   |  |     |- 2026-05-14-stage21-audio-ducking-design.md -- дизайн Stage 21.
   |  |     `- 2026-05-14-student-questions-design.md -- дизайн вопросов студентов.
   |  |- англ(ориг)/
   |  |  |- ARCHITECTURE.md -- английская архитектура проекта.
   |  |  |- FIRST_TRY.md -- английская инструкция первого полного теста.
   |  |  |- HANDOFF_READINESS_REPORT.md -- английский отчет готовности передачи C# команде.
   |  |  |- integration-contract.json -- машинно-читаемый контракт интеграции.
   |  |  |- integration-contract.md -- английский контракт интеграции.
   |  |  |- production.md -- английская production-инструкция.
   |  |  |- PROJECT_REPORT.md -- английский отчет проекта.
   |  |  `- dontNeed/
   |  |     |- AUDIT_REPORT.md -- старый/вспомогательный audit report.
   |  |     `- AUDIT_STAGE22_REPORT.md -- старый/вспомогательный audit report Stage 22.
   |  `- руский/
   |     |- ARCHITECTURE_RU.md -- русская архитектура проекта.
   |     |- FIRST_TRY_RU.md -- русская инструкция первого теста.
   |     |- HANDOFF_READINESS_REPORT_RU.md -- русский отчет готовности передачи.
   |     |- integration-contract_RU.md -- русский контракт интеграции.
   |     |- production_RU.md -- русская production-инструкция.
   |     `- PROJECT_REPORT_RU.md -- русский отчет проекта.
   |
   |- examples/ -- примеры клиентов для интеграции.
   |  |- csharp/
   |  |  |- Program.cs -- пример сценария C# клиента: создать урок, токены, вопрос, transcript.
   |  |  |- README.md -- как запускать C# пример.
   |  |  `- TranslationServiceClient.cs -- C# HTTP-клиент integration API.
   |  `- js/
   |     |- captions-client.html -- HTML demo клиента captions WebSocket.
   |     |- captions-client.js -- JS demo клиента captions WebSocket.
   |     `- student-lesson-client.js -- JS helper/client для student lesson integration.
   |
   |- tests/ -- автоматические тесты.
   |  |- conftest.py -- общие pytest fixtures.
   |  |- test_audio_pipeline.py -- тесты основной audio pipeline.
   |  |- test_azure_stt.py -- тесты Azure STT.
   |  |- test_azure_translator.py -- тесты Azure Translator.
   |  |- test_caption_hub.py -- тесты caption broadcast hub.
   |  |- test_cartesia_stt.py -- тесты Cartesia STT.
   |  |- test_compare.py -- тесты comparison API/runner.
   |  |- test_elevenlabs_stt.py -- тесты ElevenLabs STT.
   |  |- test_exports.py -- тесты экспорта transcript/notes.
   |  |- test_glossary.py -- тесты glossary normalizer/postprocessor/API.
   |  |- test_integration.py -- тесты integration API.
   |  |- test_meeting_sdk.py -- тесты Zoom Meeting SDK config/signature.
   |  |- test_production.py -- тесты production readiness/config checks.
   |  |- test_providers.py -- тесты provider status endpoints.
   |  |- test_real_test.py -- тесты real-test API.
   |  |- test_rtms.py -- тесты Zoom RTMS логики.
   |  |- test_smoke.py -- тесты smoke runner/API.
   |  |- test_stage14_integration_contract.py -- regression тесты Stage 14 integration contract.
   |  |- test_stage15a_rtms_descoped.py -- regression тесты Stage 15A RTMS descoping.
   |  |- test_stage16_browser_audio.py -- regression тесты browser audio ingest.
   |  |- test_stage17b_websocket_auth.py -- regression тесты WebSocket auth.
   |  |- test_stage18_live_tests.py -- regression тесты live mic test matrix.
   |  |- test_stage19_student_questions.py -- regression тесты student questions.
   |  |- test_stage20_tts.py -- regression тесты TTS.
   |  |- test_stage21_audio_ducking.py -- regression тесты audio ducking.
   |  |- test_stage22_e2e_qa.py -- regression тесты E2E QA.
   |  |- test_stage23b_voice_question_hardening.py -- regression тесты hardening голосовых вопросов.
   |  |- test_stage23c_rate_limits.py -- regression тесты rate limits.
   |  |- test_stage24_integration_tts_questions.py -- regression тесты integration TTS/questions.
   |  |- test_stage24a_tts_voice_selection.py -- regression тесты выбора голосов TTS.
   |  |- test_stage24b_tts_voice_catalog.py -- regression тесты каталога голосов TTS.
   |  |- test_stage25a_redis_foundation.py -- regression тесты Redis foundation.
   |  |- test_stage4a_rtms_audio_pipeline.py -- regression тесты RTMS audio pipeline.
   |  |- test_student_caption_language_selection.py -- тесты выбора языка captions на студентской странице.
   |  |- test_usage.py -- тесты usage tracking/cost endpoints.
   |  `- test_zoom_api.py -- тесты Zoom OAuth/API client.
   |
   `- tmp/ -- runtime/output файлы, не исходный код.
      |- elevenlabs_ru_test.wav -- сгенерированный/тестовый WAV файл.
      |- stage16c-browser.db -- временная SQLite БД для Stage 16C.
      |- uvicorn-*.log -- stdout/stderr логи локальных запусков uvicorn.
      `- smoke/sample_elevenlabs_ru_test_234444.wav -- аудио-сэмпл для smoke-теста.
```

## Служебные папки

```text
.playwright-mcp/ -- снимки страниц и console logs от Playwright MCP.
live_translation_platform/.idea/ -- локальные настройки IDE JetBrains/PyCharm.
live_translation_platform/.pytest_cache/ -- кэш pytest.
live_translation_platform/.venv/ -- виртуальное окружение Python и установленные зависимости.
live_translation_platform/app/**/__pycache__/ -- скомпилированный Python-кэш.
live_translation_platform/tests/**/__pycache__/ -- скомпилированный Python-кэш тестов.
```
