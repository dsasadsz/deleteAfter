from functools import lru_cache
import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", ".env.local"), env_file_encoding="utf-8", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        if _dotenv_loading_disabled():
            return (init_settings, env_settings, file_secret_settings)
        return (init_settings, env_settings, dotenv_settings, file_secret_settings)

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    log_format: str = "text"
    enable_openapi_docs: bool = True
    enable_debug_endpoints: bool = True
    enable_load_test_endpoints: bool = False
    allow_load_tests: bool = Field(False, alias="ALLOW_LOAD_TESTS")
    app_worker_count: int = 1
    allowed_origins: str = ""
    cors_allowed_origins: str = ""
    allow_wildcard_cors_in_production: bool = False
    trusted_hosts: str = ""
    security_headers_enabled: bool = True
    security_hsts_enabled: bool = True
    max_request_body_bytes: int = 10 * 1024 * 1024
    max_audio_upload_bytes: int = 2 * 1024 * 1024
    token_log_redaction_enabled: bool = True
    docs_enabled_in_production: bool = False
    debug_endpoints_allowed_in_production: bool = False
    database_url: str = "sqlite:///./dev.db"
    database_pool_size: int = 5
    database_max_overflow: int = 10
    database_pool_pre_ping: bool = True
    database_echo: bool = False
    postgres_required_in_production: bool = True
    sqlite_allowed_in_production: bool = False
    redis_enabled: bool = False
    redis_url: str = "redis://localhost:6379/0"
    redis_prefix: str = "live_translation"
    redis_required_in_production: bool = False
    redis_connect_timeout_seconds: float = 2
    redis_health_timeout_seconds: float = 1
    redis_rate_limit_enabled: bool = False
    redis_rate_limit_fail_closed: bool = False
    redis_pubsub_enabled: bool = False
    redis_pubsub_fail_closed: bool = False
    redis_tts_cache_enabled: bool = False
    lesson_mode: str = "mock"
    worker_shutdown_timeout_seconds: int = 30

    zoom_account_id: str = ""
    zoom_client_id: str = ""
    zoom_client_secret: str = ""
    zoom_webhook_secret_token: str = ""
    zoom_webhook_signature_validation_enabled: bool = False
    zoom_webhook_signature_required_in_production: bool = True
    zoom_webhook_timestamp_tolerance_seconds: int = 300
    zoom_rtms_client_id: str = ""
    zoom_rtms_client_secret: str = ""
    zoom_rtms_enabled: bool = False
    rtms_ui_enabled: bool = False
    rtms_experimental_enabled: bool = False
    zoom_rtms_webhook_path: str = "/api/zoom/webhook"
    rtms_debug_audio_log_every_n_chunks: int = 50
    rtms_process_audio: bool = False
    rtms_process_transcript: bool = True
    rtms_arm_timeout_seconds: int = 600
    rtms_auto_start_pipeline_on_webhook: bool = True
    rtms_reconnect_max_attempts: int = 3
    rtms_reconnect_backoff_seconds: float = 2.0
    rtms_audio_queue_max_size: int = 200
    rtms_audio_drop_policy: str = "drop_oldest"
    audio_pipeline_queue_max_size: int = 200
    audio_pipeline_drop_policy: str = "drop_oldest"
    browser_audio_enabled: bool = True
    browser_audio_queue_max_size: int = 200
    browser_audio_drop_policy: str = "drop_oldest"
    browser_audio_expected_sample_rate: int = 16000
    browser_audio_expected_channels: int = 1
    browser_audio_expected_format: str = "pcm_s16le"
    browser_audio_chunk_ms: int = 100
    browser_audio_allow_duplicate_teacher: bool = False
    browser_audio_use_audio_worklet: bool = True
    browser_audio_enable_resample_in_browser: bool = True
    browser_audio_max_segment_duration_ms: int = 5000
    browser_audio_periodic_commit_enabled: bool = True
    browser_audio_primary: bool = True
    default_audio_source: str = "browser_ws"
    public_base_url: str = ""
    public_ws_base_url: str = ""
    zoom_webhook_url: str = ""
    mock_stt_audio_driven: bool = False
    mock_stt_chunks_per_partial: int = 10
    mock_stt_chunks_per_final: int = 30
    mock_stt_min_final_interval_ms: int = 1200
    zoom_oauth_token_url: str = "https://zoom.us/oauth/token"
    zoom_api_base_url: str = "https://api.zoom.us/v2"
    zoom_default_timezone: str = "Asia/Almaty"
    zoom_default_duration_minutes: int = 60
    zoom_http_timeout_seconds: float = 10.0
    zoom_http_max_retries: int = 2
    zoom_meeting_sdk_client_id: str = ""
    zoom_meeting_sdk_client_secret: str = ""
    zoom_meeting_sdk_sdk_key: str = ""
    zoom_meeting_sdk_key: str = Field("", alias="ZOOM_MEETING_SDK_KEY")
    zoom_sdk_key: str = Field("", alias="ZOOM_SDK_KEY")
    zoom_meeting_sdk_secret: str = Field("", alias="ZOOM_MEETING_SDK_SECRET")
    zoom_sdk_secret: str = Field("", alias="ZOOM_SDK_SECRET")
    zoom_meeting_sdk_role_student: int = 0
    zoom_meeting_sdk_role_host: int = 1
    zoom_meeting_sdk_leave_url: str = "http://127.0.0.1:8000/"
    zoom_meeting_sdk_lang: str = "en-US"

    stt_provider: str = "mock"
    stt_default_language: str = "ru-RU"
    disable_auto_language_detection: bool = True
    translation_provider: str = "mock"
    translate_partials: bool = False
    source_language: str = "ru-RU"
    target_languages_raw: str = Field("kk,uz,zh-Hans", alias="TARGET_LANGUAGES")
    faster_whisper_model_path: str = ""
    faster_whisper_device: str = "cpu"
    faster_whisper_compute_type: str = "int8"
    faster_whisper_language: str = "ru"
    faster_whisper_beam_size: int = 1
    faster_whisper_vad_filter: bool = False
    faster_whisper_timeout_seconds: float = 10.0
    faster_whisper_load_on_startup: bool = False
    faster_whisper_segment_seconds: float = 5.0

    elevenlabs_api_key: str = ""
    elevenlabs_stt_model: str = "scribe_v2_realtime"
    elevenlabs_stt_language: str = "ru"
    elevenlabs_stt_audio_format: str = "pcm_16000"
    elevenlabs_stt_sample_rate: int = 16000
    elevenlabs_stt_commit_strategy: str = "vad"
    elevenlabs_stt_manual_commit_after_silence_ms: int = 800
    elevenlabs_stt_force_commit_enabled: bool = True
    elevenlabs_stt_partials_for_live: bool = True
    elevenlabs_stt_enable_partials: bool = True
    elevenlabs_stt_max_reconnects: int = 3
    elevenlabs_stt_connect_timeout_seconds: float = 10.0
    elevenlabs_stt_receive_timeout_seconds: float = 30.0
    azure_speech_key: str = ""
    azure_speech_region: str = ""
    azure_speech_language: str = "ru-RU"
    azure_speech_sample_rate: int = 16000
    azure_speech_bits_per_sample: int = 16
    azure_speech_channels: int = 1
    azure_speech_enable_partials: bool = True
    azure_speech_initial_silence_timeout_ms: int = 5000
    azure_speech_segmentation_silence_timeout_ms: int = 800
    azure_speech_profanity: str = "Masked"
    azure_speech_use_phrase_list: bool = True
    azure_stt_max_concurrent_sessions: str = ""
    elevenlabs_stt_max_concurrent_sessions: str = ""
    cartesia_stt_max_concurrent_sessions: str = ""
    azure_translator_key: str = ""
    azure_translator_region: str = ""
    azure_translator_endpoint: str = "https://api.cognitive.microsofttranslator.com"
    azure_translator_api_version: str = "3.0"
    azure_translator_max_requests_per_second: str = ""
    local_translation_enabled: bool = False
    local_translation_routing_enabled: bool = True
    local_translation_default_engine: str = "tilmash"
    local_translation_fallback_engine: str = "madlad400"
    local_translation_timeout_seconds: float = 2.5
    local_translation_route_kk: str = ""
    local_translation_route_uz: str = ""
    local_translation_route_zh: str = ""
    tilmash_enabled: bool = True
    tilmash_model_path: str = ""
    tilmash_tokenizer_path: str = ""
    tilmash_server_url: str = ""
    tilmash_server_timeout_seconds: float = 1.5
    tilmash_device: str = "cuda"
    tilmash_dtype: str = "auto"
    tilmash_max_batch_size: int = 8
    tilmash_max_new_tokens: int = 128
    tilmash_num_beams: int = 1
    tilmash_timeout_seconds: float = 1.5
    tilmash_load_on_startup: bool = False
    madlad_enabled: bool = False
    madlad_model_path: str = ""
    madlad_tokenizer_path: str = ""
    madlad_server_url: str = ""
    madlad_server_timeout_seconds: float = 4.0
    madlad_device: str = "cuda"
    madlad_dtype: str = "auto"
    madlad_quantization: str = "8bit"
    madlad_max_batch_size: int = 4
    madlad_timeout_seconds: float = 4.0
    madlad_load_on_startup: bool = False
    m2m100_ct2_enabled: bool = False
    m2m100_ct2_model_path: str = ""
    m2m100_ct2_tokenizer_path: str = ""
    m2m100_ct2_device: str = "cpu"
    m2m100_ct2_compute_type: str = "int8"
    m2m100_ct2_timeout_seconds: float = 5.0
    m2m100_ct2_load_on_startup: bool = False
    m2m100_ct2_default_size: str = "418m"
    m2m100_ct2_supported_targets: str = "uz,zh-Hans"
    m2m100_1_2b_ct2_enabled: bool = False
    m2m100_1_2b_ct2_model_path: str = ""
    m2m100_1_2b_ct2_tokenizer_path: str = ""
    m2m100_1_2b_ct2_device: str = "cpu"
    m2m100_1_2b_ct2_compute_type: str = "int8"
    m2m100_1_2b_ct2_timeout_seconds: float = 8.0
    m2m100_1_2b_ct2_load_on_startup: bool = False
    m2m100_1_2b_ct2_supported_targets: str = "uz"
    smoke_audio_chunk_ms: int = 100
    smoke_max_audio_file_mb: int = 10
    smoke_temp_dir: str = "./tmp/smoke"
    google_application_credentials: str = ""
    cartesia_api_key: str = ""
    cartesia_stt_model: str = "ink-whisper"
    cartesia_stt_language: str = "ru"
    cartesia_stt_encoding: str = "pcm_s16le"
    cartesia_stt_sample_rate: int = 16000
    cartesia_stt_enable_partials: bool = True
    cartesia_stt_max_reconnects: int = 3
    cartesia_stt_connect_timeout_seconds: float = 10.0
    cartesia_stt_receive_timeout_seconds: float = 30.0
    cartesia_stt_version: str = "2025-04-16"
    openai_api_key: str = ""
    usage_tracking_enabled: bool = True
    cost_estimation_enabled: bool = True
    default_currency: str = "USD"
    integration_api_keys_raw: str = Field("", alias="INTEGRATION_API_KEYS")
    integration_auth_enabled: bool = True
    integration_require_https: bool = False
    integration_callback_secret: str = ""
    security_signing_secret: str = ""
    access_token_ttl_seconds: int = 3600
    student_ws_token_ttl_seconds: int = 7200
    teacher_audio_token_ttl_seconds: int = 7200
    diagnostics_token_ttl_seconds: int = 1800
    websocket_auth_enabled: bool = False
    websocket_auth_required_in_production: bool = True
    allow_dev_ws_without_token: bool = True
    websocket_broadcast_send_timeout_seconds: float = 2.0
    websocket_broadcast_max_concurrency: int = 100
    websocket_broadcast_drop_on_timeout: bool = True
    websocket_broadcast_metrics_enabled: bool = True
    websocket_sticky_routing_enabled: bool = False
    distributed_lesson_sessions_enabled: bool = False
    student_question_audio_enabled: bool = True
    student_question_max_duration_seconds: int = 20
    student_question_max_audio_bytes: int = 1048576
    student_question_max_queue_size: int = 100
    student_question_stt_provider: str = "elevenlabs"
    student_question_final_timeout_seconds: float = 10
    student_question_stt_connect_timeout_seconds: float = 5
    student_question_stt_total_timeout_seconds: float = 25
    student_question_translation_target: str = "ru"
    tts_enabled: bool = True
    tts_provider: str = "azure"
    tts_default_language: str = "kk"
    tts_audio_format: str = "audio-16khz-32kbitrate-mono-mp3"
    tts_cache_enabled: bool = True
    tts_cache_max_items: int = 500
    tts_shared_cache_enabled: bool = True
    tts_shared_cache_backend: str = "memory"
    tts_shared_cache_dir: str = "./tmp/tts_cache"
    tts_shared_cache_max_items: int = 1000
    tts_shared_cache_ttl_seconds: int = 3600
    tts_shared_cache_disk_max_bytes: int = 1073741824
    tts_cache_distributed_lock_enabled: bool = False
    tts_cache_distributed_lock_ttl_seconds: int = 10
    tts_cache_distributed_lock_wait_timeout_seconds: float = 2.0
    tts_cache_distributed_lock_poll_interval_seconds: float = 0.05
    tts_cache_distributed_lock_fail_closed: bool = False
    tts_audio_url_enabled: bool = True
    tts_audio_url_ttl_seconds: int = 3600
    tts_audio_url_token_required: bool = True
    tts_max_text_chars: int = 500
    tts_autoplay_default: bool = False
    tts_queue_mode: str = "sequential"
    tts_volume_default: float = 1.0
    tts_ducking_enabled: bool = True
    tts_ducking_level: float = 0.2
    tts_ducking_restore_delay_ms: int = 300
    local_tts_enabled: bool = False
    local_tts_default_engine: str = "piper"
    local_tts_ru_engine: str = "silero"
    local_tts_kk_engine: str = "piper"
    local_tts_uz_engine: str = "piper"
    local_tts_zh_engine: str = "piper"
    local_tts_allowed_languages: str = ""
    local_tts_timeout_seconds: float = 5.0
    piper_enabled: bool = True
    piper_bin_path: str = ""
    piper_timeout_seconds: float = 5.0
    piper_default_voice: str = ""
    piper_voice_ru: str = ""
    piper_voice_kk: str = ""
    piper_voice_uz: str = ""
    piper_voice_zh: str = ""
    piper_output_format: str = "wav"
    silero_tts_enabled: bool = False
    silero_tts_model_path: str = ""
    silero_tts_device: str = "cpu"
    silero_tts_timeout_seconds: float = 5.0
    silero_tts_language: str = "ru"
    silero_tts_speaker: str = ""
    kazakh_tts2_enabled: bool = False
    kazakh_tts2_model_path: str = ""
    kazakh_tts2_vocoder_path: str = ""
    kazakh_tts2_tokenizer_path: str = ""
    kazakh_tts2_server_url: str = ""
    kazakh_tts2_server_timeout_seconds: float = 5.0
    kazakh_tts2_device: str = "cuda"
    kazakh_tts2_dtype: str = "auto"
    kazakh_tts2_timeout_seconds: float = 5.0
    kazakh_tts2_load_on_startup: bool = False
    kazakh_tts2_output_format: str = "wav"
    kazakh_tts2_default_voice: str = ""
    e2e_qa_enabled: bool = True
    e2e_qa_debug_capture_enabled: bool = True
    e2e_qa_report_export_enabled: bool = True
    rate_limit_enabled: bool = True
    tts_rate_limit_per_minute: int = 20
    tts_load_test_bypass_rate_limit: bool = False
    question_text_rate_limit_per_minute: int = 10
    question_voice_rate_limit_per_minute: int = 5
    question_moderation_rate_limit_per_minute: int = 60
    azure_tts_key: str = ""
    azure_tts_region: str = ""
    azure_tts_endpoint: str = ""
    azure_tts_max_requests_per_second: str = ""
    elevenlabs_tts_max_concurrent_requests: str = ""
    azure_tts_default_voice_kk: str = ""
    azure_tts_default_voice_uz: str = ""
    azure_tts_default_voice_zh: str = ""
    azure_tts_default_voice_ru: str = ""
    azure_tts_voices_kk: str = ""
    azure_tts_voices_uz: str = ""
    azure_tts_voices_zh: str = ""
    azure_tts_voices_ru: str = ""
    azure_tts_voice_kk_male: str = ""
    azure_tts_voice_kk_female: str = ""
    azure_tts_voice_uz_male: str = ""
    azure_tts_voice_uz_female: str = ""
    azure_tts_voice_zh_male: str = ""
    azure_tts_voice_zh_female: str = ""
    azure_tts_voice_ru_male: str = ""
    azure_tts_voice_ru_female: str = ""
    elevenlabs_tts_voice_id_kk_male: str = ""
    elevenlabs_tts_voice_id_kk_female: str = ""
    elevenlabs_tts_voice_id_uz_male: str = ""
    elevenlabs_tts_voice_id_uz_female: str = ""
    elevenlabs_tts_voice_id_zh_male: str = ""
    elevenlabs_tts_voice_id_zh_female: str = ""
    elevenlabs_tts_voice_id_ru_male: str = ""
    elevenlabs_tts_voice_id_ru_female: str = ""
    elevenlabs_tts_voices_kk: str = ""
    elevenlabs_tts_voices_uz: str = ""
    elevenlabs_tts_voices_zh: str = ""
    elevenlabs_tts_voices_ru: str = ""

    @property
    def target_languages(self) -> list[str]:
        return [item.strip() for item in self.target_languages_raw.split(",") if item.strip()]

    @property
    def has_zoom_credentials(self) -> bool:
        return all([self.zoom_account_id, self.zoom_client_id, self.zoom_client_secret])

    @property
    def allowed_origin_list(self) -> list[str]:
        origins = _split_csv(self.cors_allowed_origins or self.allowed_origins)
        if self.is_production and origins == ["*"] and not self.allow_wildcard_cors_in_production:
            return []
        return origins

    @property
    def trusted_host_list(self) -> list[str]:
        return _split_csv(self.trusted_hosts)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def security_headers_active(self) -> bool:
        return bool(self.security_headers_enabled or self.is_production)

    @property
    def hsts_active(self) -> bool:
        return bool(self.security_hsts_enabled and (self.is_production or self.public_base_url.startswith("https://")))

    @property
    def docs_enabled(self) -> bool:
        return bool(self.enable_openapi_docs and (not self.is_production or self.docs_enabled_in_production))

    @property
    def debug_endpoints_allowed(self) -> bool:
        return bool(self.enable_debug_endpoints and (not self.is_production or self.debug_endpoints_allowed_in_production))

    @property
    def effective_allowed_origins_raw(self) -> str:
        return self.cors_allowed_origins or self.allowed_origins

    @property
    def integration_api_keys(self) -> list[str]:
        return _split_csv(self.integration_api_keys_raw)

    @property
    def websocket_auth_required(self) -> bool:
        return self.websocket_auth_enabled or (self.is_production and self.websocket_auth_required_in_production)

    @property
    def zoom_meeting_sdk_effective_key(self) -> str:
        return self.zoom_meeting_sdk_client_id or self.zoom_meeting_sdk_sdk_key or self.zoom_meeting_sdk_key or self.zoom_sdk_key

    @property
    def zoom_meeting_sdk_effective_secret(self) -> str:
        return self.zoom_meeting_sdk_client_secret or self.zoom_meeting_sdk_secret or self.zoom_sdk_secret

    @property
    def zoom_webhook_signature_required(self) -> bool:
        return self.zoom_webhook_signature_validation_enabled or (self.is_production and self.zoom_webhook_signature_required_in_production)

    @property
    def redis_rate_limit_requested(self) -> bool:
        return bool(self.redis_enabled and self.redis_rate_limit_enabled)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _dotenv_loading_disabled() -> bool:
    return _env_flag_enabled("IGNORE_DOTENV_IN_TESTS") or os.getenv("APP_ENV", "").lower() == "test"


def _env_flag_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
