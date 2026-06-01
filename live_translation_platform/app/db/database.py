from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(
        self,
        database_url: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_pre_ping: bool = True,
        echo: bool = False,
    ) -> None:
        self.database_url = database_url
        self.database_type = database_type_from_url(database_url)
        engine_url = normalize_database_url_for_sync_engine(database_url)
        connect_args = {"check_same_thread": False} if self.database_type == "sqlite" else {}
        engine_kwargs = {
            "connect_args": connect_args,
            "echo": echo,
        }
        if self.database_type != "sqlite":
            engine_kwargs.update(
                {
                    "pool_size": pool_size,
                    "max_overflow": max_overflow,
                    "pool_pre_ping": pool_pre_ping,
                }
            )
        self.engine = create_engine(engine_url, **engine_kwargs)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )

    def create_all(self) -> None:
        from app.db import models  # noqa: F401

        Base.metadata.create_all(bind=self.engine)
        self._add_missing_sqlite_columns()

    def session(self) -> Generator[Session, None, None]:
        db = self.session_factory()
        try:
            yield db
        finally:
            db.close()

    def _add_missing_sqlite_columns(self) -> None:
        if self.engine.dialect.name != "sqlite":
            return
        inspector = inspect(self.engine)
        table_names = inspector.get_table_names()
        if "lessons" not in table_names:
            return
        existing = {column["name"] for column in inspector.get_columns("lessons")}
        additions = {
            "zoom_topic": "ALTER TABLE lessons ADD COLUMN zoom_topic VARCHAR(255) DEFAULT ''",
            "zoom_password": "ALTER TABLE lessons ADD COLUMN zoom_password VARCHAR(128) DEFAULT ''",
            "zoom_created_at": "ALTER TABLE lessons ADD COLUMN zoom_created_at VARCHAR(64)",
            "audio_source": "ALTER TABLE lessons ADD COLUMN audio_source VARCHAR(64) DEFAULT 'mock'",
            "rtms_stream_id": "ALTER TABLE lessons ADD COLUMN rtms_stream_id VARCHAR(128)",
            "rtms_session_id": "ALTER TABLE lessons ADD COLUMN rtms_session_id VARCHAR(128)",
            "rtms_started_at": "ALTER TABLE lessons ADD COLUMN rtms_started_at DATETIME",
            "rtms_connected_at": "ALTER TABLE lessons ADD COLUMN rtms_connected_at DATETIME",
            "rtms_last_audio_at": "ALTER TABLE lessons ADD COLUMN rtms_last_audio_at DATETIME",
            "rtms_last_transcript_at": "ALTER TABLE lessons ADD COLUMN rtms_last_transcript_at DATETIME",
            "rtms_error": "ALTER TABLE lessons ADD COLUMN rtms_error TEXT",
            "rtms_armed": "ALTER TABLE lessons ADD COLUMN rtms_armed BOOLEAN DEFAULT 0",
            "rtms_armed_at": "ALTER TABLE lessons ADD COLUMN rtms_armed_at DATETIME",
            "audio_chunks_received": "ALTER TABLE lessons ADD COLUMN audio_chunks_received INTEGER DEFAULT 0",
            "transcript_events_received": "ALTER TABLE lessons ADD COLUMN transcript_events_received INTEGER DEFAULT 0",
            "audio_chunks_dropped": "ALTER TABLE lessons ADD COLUMN audio_chunks_dropped INTEGER DEFAULT 0",
            "browser_audio_status": "ALTER TABLE lessons ADD COLUMN browser_audio_status VARCHAR(64) DEFAULT 'not_connected'",
            "browser_audio_connected_at": "ALTER TABLE lessons ADD COLUMN browser_audio_connected_at DATETIME",
            "browser_audio_last_chunk_at": "ALTER TABLE lessons ADD COLUMN browser_audio_last_chunk_at DATETIME",
            "browser_audio_chunks_received": "ALTER TABLE lessons ADD COLUMN browser_audio_chunks_received INTEGER DEFAULT 0",
            "browser_audio_bytes_received": "ALTER TABLE lessons ADD COLUMN browser_audio_bytes_received INTEGER DEFAULT 0",
            "browser_audio_chunks_dropped": "ALTER TABLE lessons ADD COLUMN browser_audio_chunks_dropped INTEGER DEFAULT 0",
            "browser_audio_error": "ALTER TABLE lessons ADD COLUMN browser_audio_error TEXT",
            "pipeline_status": "ALTER TABLE lessons ADD COLUMN pipeline_status VARCHAR(64) DEFAULT 'created'",
            "pipeline_audio_source": "ALTER TABLE lessons ADD COLUMN pipeline_audio_source VARCHAR(64)",
            "pipeline_chunks_processed": "ALTER TABLE lessons ADD COLUMN pipeline_chunks_processed INTEGER DEFAULT 0",
            "stt_events_generated": "ALTER TABLE lessons ADD COLUMN stt_events_generated INTEGER DEFAULT 0",
            "captions_sent": "ALTER TABLE lessons ADD COLUMN captions_sent INTEGER DEFAULT 0",
            "stt_provider_status": "ALTER TABLE lessons ADD COLUMN stt_provider_status VARCHAR(64) DEFAULT 'not_connected'",
            "stt_provider_connected_at": "ALTER TABLE lessons ADD COLUMN stt_provider_connected_at DATETIME",
            "stt_provider_audio_chunks_sent": "ALTER TABLE lessons ADD COLUMN stt_provider_audio_chunks_sent INTEGER DEFAULT 0",
            "stt_provider_partial_events": "ALTER TABLE lessons ADD COLUMN stt_provider_partial_events INTEGER DEFAULT 0",
            "stt_provider_final_events": "ALTER TABLE lessons ADD COLUMN stt_provider_final_events INTEGER DEFAULT 0",
            "stt_provider_no_match_count": "ALTER TABLE lessons ADD COLUMN stt_provider_no_match_count INTEGER DEFAULT 0",
            "stt_provider_canceled_count": "ALTER TABLE lessons ADD COLUMN stt_provider_canceled_count INTEGER DEFAULT 0",
            "stt_provider_audio_bytes_sent": "ALTER TABLE lessons ADD COLUMN stt_provider_audio_bytes_sent INTEGER DEFAULT 0",
            "stt_provider_last_event_at": "ALTER TABLE lessons ADD COLUMN stt_provider_last_event_at DATETIME",
            "stt_provider_errors_count": "ALTER TABLE lessons ADD COLUMN stt_provider_errors_count INTEGER DEFAULT 0",
            "stt_provider_last_error": "ALTER TABLE lessons ADD COLUMN stt_provider_last_error TEXT",
            "stt_provider_last_transcript": "ALTER TABLE lessons ADD COLUMN stt_provider_last_transcript TEXT",
            "translation_requests_count": "ALTER TABLE lessons ADD COLUMN translation_requests_count INTEGER DEFAULT 0",
            "translation_errors_count": "ALTER TABLE lessons ADD COLUMN translation_errors_count INTEGER DEFAULT 0",
            "translation_last_error": "ALTER TABLE lessons ADD COLUMN translation_last_error TEXT",
            "translation_last_success_at": "ALTER TABLE lessons ADD COLUMN translation_last_success_at DATETIME",
            "translation_avg_latency_ms": "ALTER TABLE lessons ADD COLUMN translation_avg_latency_ms FLOAT DEFAULT 0.0",
            "glossary_id": "ALTER TABLE lessons ADD COLUMN glossary_id VARCHAR(64)",
            "glossary_enabled": "ALTER TABLE lessons ADD COLUMN glossary_enabled BOOLEAN DEFAULT 1",
            "external_lesson_id": "ALTER TABLE lessons ADD COLUMN external_lesson_id VARCHAR(128)",
            "external_course_id": "ALTER TABLE lessons ADD COLUMN external_course_id VARCHAR(128)",
            "external_teacher_id": "ALTER TABLE lessons ADD COLUMN external_teacher_id VARCHAR(128)",
            "external_tenant_id": "ALTER TABLE lessons ADD COLUMN external_tenant_id VARCHAR(128)",
            "callback_url": "ALTER TABLE lessons ADD COLUMN callback_url TEXT",
            "integration_metadata_json": "ALTER TABLE lessons ADD COLUMN integration_metadata_json TEXT",
        }
        with self.engine.begin() as connection:
            for column, statement in additions.items():
                if column not in existing:
                    connection.execute(text(statement))
        self._add_missing_sqlite_columns_for_table(
            "transcript_segments",
            {
                "original_text_raw": "ALTER TABLE transcript_segments ADD COLUMN original_text_raw TEXT",
                "original_text_normalized": "ALTER TABLE transcript_segments ADD COLUMN original_text_normalized TEXT",
                "normalization_applied": "ALTER TABLE transcript_segments ADD COLUMN normalization_applied BOOLEAN DEFAULT 0",
                "normalization_changes_json": "ALTER TABLE transcript_segments ADD COLUMN normalization_changes_json TEXT DEFAULT '[]'",
                "translation_postprocess_applied": "ALTER TABLE transcript_segments ADD COLUMN translation_postprocess_applied BOOLEAN DEFAULT 0",
                "translation_postprocess_changes_json": "ALTER TABLE transcript_segments ADD COLUMN translation_postprocess_changes_json TEXT DEFAULT '[]'",
                "start_time": "ALTER TABLE transcript_segments ADD COLUMN start_time DATETIME",
                "end_time": "ALTER TABLE transcript_segments ADD COLUMN end_time DATETIME",
                "speaker_json": "ALTER TABLE transcript_segments ADD COLUMN speaker_json TEXT DEFAULT '{}'",
                "latency_json": "ALTER TABLE transcript_segments ADD COLUMN latency_json TEXT DEFAULT '{}'",
            },
        )
        self._add_missing_sqlite_columns_for_table(
            "smoke_test_runs",
            {
                "glossary_id": "ALTER TABLE smoke_test_runs ADD COLUMN glossary_id VARCHAR(64)",
                "glossary_enabled": "ALTER TABLE smoke_test_runs ADD COLUMN glossary_enabled BOOLEAN DEFAULT 0",
            },
        )
        self._add_missing_sqlite_columns_for_table(
            "comparison_runs",
            {
                "glossary_id": "ALTER TABLE comparison_runs ADD COLUMN glossary_id VARCHAR(64)",
                "glossary_enabled": "ALTER TABLE comparison_runs ADD COLUMN glossary_enabled BOOLEAN DEFAULT 0",
            },
        )

    def _add_missing_sqlite_columns_for_table(self, table_name: str, additions: dict[str, str]) -> None:
        inspector = inspect(self.engine)
        if table_name not in inspector.get_table_names():
            return
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        with self.engine.begin() as connection:
            for column, statement in additions.items():
                if column not in existing:
                    connection.execute(text(statement))


def database_type_from_url(database_url: str) -> str:
    if not database_url:
        return "missing"
    try:
        drivername = make_url(database_url).drivername
    except Exception:
        return "unknown"
    backend = drivername.split("+", 1)[0]
    if backend in {"postgres", "postgresql"}:
        return "postgresql"
    if backend == "sqlite":
        return backend
    return "unknown"


def database_url_configured(database_url: str) -> bool:
    return bool(str(database_url or "").strip())


def normalize_database_url_for_sync_engine(database_url: str):
    try:
        url = make_url(database_url)
    except Exception:
        return database_url
    if url.drivername in {"postgresql", "postgresql+asyncpg"}:
        return url.set(drivername="postgresql+psycopg")
    return database_url
