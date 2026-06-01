from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_production_compose_allows_app_env_override_with_safe_default():
    compose = _read("docker-compose.prod.yml")

    assert "env_file:" in compose
    assert "- path: .env" in compose or "- .env" in compose
    assert "APP_ENV: ${APP_ENV:-production}" in compose
    assert "APP_ENV: production" not in compose


def test_production_compose_keeps_load_test_endpoints_disabled_by_default():
    compose = _read("docker-compose.prod.yml")

    assert "ENABLE_LOAD_TEST_ENDPOINTS: ${ENABLE_LOAD_TEST_ENDPOINTS:-false}" in compose
    assert "ENABLE_LOAD_TEST_ENDPOINTS: true" not in compose
    assert 'ENABLE_LOAD_TEST_ENDPOINTS: "true"' not in compose


def test_production_compose_uses_docker_service_hostnames_for_database_and_redis():
    compose = _read("docker-compose.prod.yml")

    assert "postgres:" in compose
    assert "redis:" in compose
    assert "depends_on:" in compose
    assert "postgres:\n        condition: service_healthy" in compose
    assert "redis:\n        condition: service_healthy" in compose
    assert "DATABASE_URL: ${COMPOSE_DATABASE_URL:-postgresql+psycopg://live_translation:${POSTGRES_PASSWORD}@postgres:5432/live_translation}" in compose
    assert "REDIS_URL: ${COMPOSE_REDIS_URL:-redis://redis:6379/0}" in compose
    assert "profiles:" not in compose


def test_production_compose_postgres_service_uses_safe_variable_substitution():
    compose = _read("docker-compose.prod.yml")

    assert "POSTGRES_DB: ${POSTGRES_DB:-live_translation}" in compose
    assert "POSTGRES_USER: ${POSTGRES_USER:-live_translation}" in compose
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}" in compose
    assert "pg_isready -U $${POSTGRES_USER:-live_translation} -d $${POSTGRES_DB:-live_translation}" in compose


def test_loadtest_override_enables_development_load_test_mode_without_secrets():
    override = _read("docker-compose.loadtest.yml")

    assert "app:" in override
    assert "APP_ENV: development" in override
    assert 'ENABLE_LOAD_TEST_ENDPOINTS: "true"' in override
    assert 'TTS_LOAD_TEST_BYPASS_RATE_LIMIT: "true"' in override
    assert "POSTGRES_PASSWORD" not in override
    assert "DATABASE_URL" not in override
    assert "REDIS_URL" not in override
    assert "localhost" not in override
    assert "API_KEY" not in override
    assert "SECRET" not in override


def test_load_testing_docs_show_docker_override_workflow():
    docs = _read("docs/load-testing.md")

    assert "docker compose -f docker-compose.prod.yml -f docker-compose.loadtest.yml up -d --build" in docs
    assert 'docker compose -f docker-compose.prod.yml -f docker-compose.loadtest.yml exec app env | findstr "APP_ENV ENABLE_LOAD_TEST_ENDPOINTS DATABASE_URL REDIS_URL"' in docs
    assert "APP_ENV=development" in docs
    assert "ENABLE_LOAD_TEST_ENDPOINTS=true" in docs
    assert "@postgres:5432" in docs
    assert "REDIS_URL=redis://redis:6379/0" in docs
    assert "docker compose -f docker-compose.prod.yml up -d --build" in docs


def test_compose_files_do_not_embed_real_secret_values():
    forbidden_fragments = (
        "sk-",
        "AIza",
        "-----BEGIN",
        "change-this",
        "dev-key-1",
        "dev-key-2",
    )

    for filename in ("docker-compose.prod.yml", "docker-compose.loadtest.yml"):
        content = _read(filename)
        lowered = content.lower()
        assert "client_secret:" not in lowered
        assert "api_key:" not in lowered
        for fragment in forbidden_fragments:
            assert fragment not in content
