import io
import json
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from msu_hub_bot.logger import LoggerBuilder
from msu_hub_bot.redaction import RedactingFormatter, redact, redact_json
from msu_hub_bot.settings import MissingIntegration, Settings, load_runtime_environment, settings


def test_required_configuration_and_optional_providers(monkeypatch):
    for key in ("HUB_BOT_TOKEN", "HUB_SUPABASE_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    config = Settings()
    with pytest.raises(ValueError, match="HUB_BOT_TOKEN"):
        config.validate_core()
    with pytest.raises(MissingIntegration):
        config.require("wolfram_token")
    assert "name='hub'" in repr(config)
    assert "bot_token=" not in repr(config)


def supabase_settings(**changes):
    values = {
        "storage_backend": "supabase",
        "bot_token": "123456789:synthetic-token",
        "supabase_url": "http://supabase.invalid:8000",
        "supabase_key": "synthetic-publishable-key",
        "supabase_email": "bot@example.invalid",
        "supabase_password": "synthetic-password",
        **changes,
    }
    return Settings(**values)


def test_required_storage_credentials():
    supabase_settings().validate_core()
    assert Settings().storage_backend == "supabase"
    with pytest.raises(ValueError, match="HUB_SUPABASE_PASSWORD"):
        supabase_settings(supabase_password="").validate_core()
    with pytest.raises(ValueError, match="HUB_BOT_TOKEN"):
        supabase_settings(bot_token="").validate_core()


def test_jev_is_opt_in_and_requires_its_own_credential():
    config = supabase_settings(openrouter_api_key="")
    assert config.jev_enabled is False and config.jev_confidence == 0.8
    config.validate_core()
    config = supabase_settings(jev_enabled=True, openrouter_api_key="synthetic-openrouter-credential")
    config.validate_core()
    assert config.openrouter_api_key not in repr(config)


@pytest.mark.parametrize("key", ["", " \t"])
def test_enabled_jev_rejects_missing_credentials_with_a_safe_error(key):
    with pytest.raises(ValueError) as caught:
        supabase_settings(jev_enabled=True, openrouter_api_key=key).validate_core()
    assert str(caught.value) == "HUB_OPENROUTER_API_KEY is required when HUB_JEV_ENABLED is true"


@pytest.mark.parametrize("confidence", [0.49, 1.01, float("nan"), float("inf")])
def test_jev_rejects_confidence_outside_its_supported_range(confidence):
    with pytest.raises(ValueError):
        supabase_settings(jev_confidence=confidence)


@pytest.mark.parametrize("confidence", [0.5, 1.0])
def test_jev_accepts_confidence_range_endpoints(confidence):
    assert supabase_settings(jev_confidence=confidence).jev_confidence == confidence


@pytest.mark.parametrize("backend", ["edgedb", "unsupported-backend-canary"])
def test_unsupported_storage_backends_fail_closed(backend):
    with pytest.raises(ValueError) as caught:
        supabase_settings(storage_backend=backend)
    assert "unsupported-backend-canary" not in str(caught.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"supabase_url": "file:///private"},
        {"supabase_url": "https://user:synthetic-private@database.invalid"},
        {"supabase_url": "https://database.invalid?key=synthetic-private"},
        {"supabase_url": "https://database.invalid/private"},
        {"supabase_schema": "schema,other"},
    ],
)
def test_supabase_configuration_rejects_unsafe_endpoints_without_values(changes):
    with pytest.raises(ValueError) as caught:
        supabase_settings(**changes).validate_core()
    assert "synthetic-private" not in str(caught.value)


def test_supabase_credentials_are_redacted(monkeypatch):
    for field in ("supabase_key", "supabase_password"):
        value = "supabase-private-" + field
        monkeypatch.setattr(settings, field, value)
        assert value not in redact(value)


def test_repository_factory_passes_configuration_and_telemetry(monkeypatch):
    from msu_hub_bot.storage import factory

    repository = Mock()
    monkeypatch.setattr(factory, "SupabaseRepository", repository)
    config = supabase_settings()
    sentinel = object()
    chosen = factory.create_repository(config, telemetry=sentinel)
    repository.assert_called_once_with(config, telemetry=sentinel)
    assert chosen is repository.return_value


def test_json_collections_and_deployment_roundtrip(monkeypatch):
    secret = 'test-value-with-$quotes"-and\\slashes\nsecond-line'
    payload = {
        "HUB_SUPABASE_PASSWORD": secret,
        "HUB_FOUNDER_IDS": "[101, 202]",
        "HUB_JDOODLE_TOKENS": '[["client", "secret"]]',
        "HUB_OPENROUTER_API_KEY": secret,
        "HUB_JEV_ENABLED": "true",
        "HUB_JEV_CONFIDENCE": "0.85",
        "LOGFIRE_TOKEN": secret + "-write-token",
    }
    for key in payload:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    monkeypatch.setenv("HUB_CONFIG_JSON", json.dumps(payload))
    load_runtime_environment()
    # Register changes with monkeypatch so the next test sees a clean environment.
    for key in payload:
        monkeypatch.setenv(key, payload[key])
    config = Settings()
    assert config.supabase_password == secret
    assert config.founder_ids == [101, 202]
    assert config.jdoodle_tokens == [("client", "secret")]
    assert config.openrouter_api_key == secret
    assert config.jev_enabled is True and config.jev_confidence == 0.85
    assert os.environ["LOGFIRE_TOKEN"] == payload["LOGFIRE_TOKEN"]
    assert "logfire_token" not in config.model_dump()
    assert secret not in repr(config)
    for key in payload:
        monkeypatch.delenv(key)


def test_redacts_logs_and_tracebacks(monkeypatch):
    secret = 'canary-value-$"/with-newline\nsecond-canary-line'
    monkeypatch.setattr(settings, "supabase_password", secret)
    assert secret not in redact(secret)
    assert quote(secret, safe="") not in redact(quote(secret, safe=""))
    assert json.dumps(secret)[1:-1] not in redact(json.dumps(secret)[1:-1])
    error = RuntimeError(secret)
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "%s", (secret,), (RuntimeError, error, None))
    rendered = RedactingFormatter().format(record)
    assert "canary-value" not in rendered


def test_short_configured_password_is_redacted(monkeypatch):
    monkeypatch.setattr(settings, "supabase_password", "a$3")
    assert "a$3" not in redact("Connection failed with a$3")
    assert quote("a$3", safe="") not in redact(quote("a$3", safe=""))


def test_configured_telegram_ids_remain_visible_in_logs_while_credentials_stay_hidden(monkeypatch):
    owner_id, chat_id = 9876543210, -1009876543210
    secret = "synthetic-credential-value"
    monkeypatch.setattr(settings, "owner_id", owner_id)
    monkeypatch.setattr(settings, "error_chat_id", chat_id)
    monkeypatch.setattr(settings, "founder_ids", [owner_id])
    monkeypatch.setattr(settings, "supabase_password", secret)
    value = f"user_id={owner_id} chat_id={chat_id} failed: {secret}"
    expected = f"user_id={owner_id} chat_id={chat_id} failed: [REDACTED]"
    assert redact(value) == expected
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, value, (), None)
    assert RedactingFormatter().format(record) == expected


def test_ordinary_configuration_is_visible_in_repr_and_diagnostics(monkeypatch):
    public_values = {
        "name": "friends-bot-name",
        "supabase_url": "https://database.example.invalid",
        "supabase_email": "bot@example.invalid",
        "supabase_schema": "friends_bot_api",
        "logs_file": "/runtime/bot-logs/{name}.log",
        "cert": "/runtime/tls/public-cert.pem",
        "web_app_url": "https://app.example.invalid",
        "acrcloud_host": "identify.example.invalid",
        "supporters_base": "contributors-base",
        "supporters_table": "contributors-table",
    }
    for field, value in public_values.items():
        monkeypatch.setattr(settings, field, value)
    text = "\n".join(public_values.values())
    assert redact(text) == text
    config = Settings(**public_values, supabase_password="synthetic-password")
    assert all(value in repr(config) for value in public_values.values())
    assert "synthetic-password" not in repr(config)
    assert "synthetic-password" not in str(config)
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "%s", (text,), None)
    assert RedactingFormatter().format(record) == text
    assert record.msg == "%s" and record.args == (text,)


@pytest.mark.parametrize(
    "field",
    [
        "bot_token",
        "openrouter_api_key",
        "supabase_key",
        "supabase_password",
        "proxy",
        "pkey",
        "health_check_url",
        "vk_user_token",
        "wolfram_token",
        "lingvanex_authorization",
        "lingvanex_image_authorization",
        "imgur_authorization",
        "owm_key",
        "owm_map_key",
        "mapbox_key",
        "acrcloud_access_key",
        "acrcloud_access_secret",
        "supporters_api_key",
    ],
)
def test_credential_fields_are_hidden_in_repr_and_diagnostics(monkeypatch, field):
    value = "synthetic-value-for-" + field
    monkeypatch.setattr(settings, field, value)
    assert redact(value) == "[REDACTED]"
    assert value not in repr(settings)
    assert value not in str(settings)
    assert settings.model_dump()[field] == value


def test_nested_provider_credentials_and_proxy_password_are_protected(monkeypatch):
    values = ["client-one", "secret-one", "proxy-password"]
    monkeypatch.setattr(settings, "jdoodle_tokens", [(values[0], values[1])])
    monkeypatch.setattr(settings, "proxy", f"socks5://proxy-user:{values[2]}@proxy.example.invalid:1080")
    assert redact("\n".join(values)) == "\n".join(["[REDACTED]"] * len(values))
    assert all(value not in repr(settings) for value in values)


def test_recognized_unconfigured_credentials_are_still_protected():
    token = "123456789:" + "a" * 35
    assert token not in redact(f"https://api.telegram.org/bot{token}/getMe")
    assert "secret-user:secret-password" not in redact("https://secret-user:secret-password@example.invalid/path")


def test_structured_redaction_preserves_json_keys_numbers_and_credential_protection(monkeypatch):
    monkeypatch.setattr(settings, "supabase_password", "id")
    source = {"id": 1700000000, "message_id": 123456789, "text": "id", "nested": [{"password": "unknown", "secret": 42}]}
    redacted = redact_json(source)
    assert json.loads(json.dumps(redacted)) == {
        "id": 1700000000,
        "message_id": 123456789,
        "text": "[REDACTED]",
        "nested": [{"password": "[REDACTED]", "secret": "[REDACTED]"}],
    }
    assert source["text"] == "id"


def test_local_logger_preserves_levels_and_redacts_both_outputs(monkeypatch, tmp_path, capsys):
    secret = 'local-logging-canary-$"\nsecond-canary-line'
    monkeypatch.setattr(settings, "supabase_password", secret)
    monkeypatch.setattr(LoggerBuilder, "default_filename", None)
    logger = LoggerBuilder.get_logger("local-test", level=logging.INFO, filename=str(tmp_path / "test.log"))
    try:
        logger.debug("hidden debug")
        logger.info("visible info")
        try:
            raise RuntimeError(secret)
        except RuntimeError:
            logger.exception("Operation failed: %s", secret)
        for handler in logger.handlers:
            handler.flush()
        console = capsys.readouterr().err
        file_output = (tmp_path / "test.log").read_text()
        assert "visible info" in console
        assert "visible info" not in file_output
        for output in (console, file_output):
            assert "hidden debug" not in output
            assert "Operation failed" in output
            assert "RuntimeError" in output
            assert "[REDACTED]" in output
            assert "local-logging-canary" not in output
            assert "second-canary-line" not in output
    finally:
        for handler in logger.handlers:
            handler.close()


def test_cli_file_logs_rotate_without_losing_redaction(monkeypatch, tmp_path):
    from logging.handlers import RotatingFileHandler

    from msu_hub_bot.cli import configure_logging

    secret = "synthetic-rotation-secret"
    monkeypatch.setattr(settings, "supabase_password", secret)
    monkeypatch.setattr(settings, "logs_file", str(tmp_path / "bot.log"))
    monkeypatch.setattr(LoggerBuilder, "default_filename", None)
    captured = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: captured.update(kwargs))
    loggers = [(logging.getLogger(name), logging.getLogger(name).level) for name in ("aiogram.event", "aiogram.dispatcher")]
    stdout, stderr, factory = sys.stdout, sys.stderr, logging.getLogRecordFactory()
    try:
        configure_logging()
        assert sys.stdout is stdout and sys.stderr is stderr
        assert logging.getLogRecordFactory() is factory
        handler = next(handler for handler in captured["handlers"] if isinstance(handler, RotatingFileHandler))
        assert handler.maxBytes == 10 * 1024 * 1024 and handler.backupCount == 2
        handler.maxBytes = 256
        for index in range(20):
            handler.handle(logging.LogRecord("rotation", logging.WARNING, __file__, 1, "%s %s", (index, secret), None))
        handler.flush()
        files = list(tmp_path.glob("bot.log*"))
        assert len(files) == 3
        for path in files:
            text = path.read_text()
            assert secret not in text and "[REDACTED]" in text
        assert LoggerBuilder.default_filename == str(tmp_path / "bot.log")
    finally:
        for handler in captured.get("handlers", []):
            handler.close()
        for logger, level in loggers:
            logger.setLevel(level)


def test_cli_fatal_error_uses_credential_formatter_and_suppresses_raw_traceback(monkeypatch, tmp_path):
    from msu_hub_bot import cli, health
    from msu_hub_bot.telemetry import TelemetryConfig

    secret = "synthetic-runtime-password"
    token = "123456789:" + "a" * 35
    monkeypatch.setattr(settings, "supabase_password", secret)
    monkeypatch.setattr(settings, "bot_token", token)
    monkeypatch.setattr(cli, "settings", supabase_settings())
    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    monkeypatch.setattr(health, "heartbeat_path", lambda: tmp_path / "heartbeat")
    monkeypatch.setattr(TelemetryConfig, "from_env", lambda environ: None)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter())
    logger = logging.Logger("cli-test")
    logger.addHandler(handler)
    shutdown = Mock()
    monkeypatch.setattr(cli, "logging", SimpleNamespace(getLogger=lambda name: logger, shutdown=shutdown))

    async def fail(*args, **kwargs):
        try:
            raise ValueError(f"https://api.telegram.org/bot{token}/getMe")
        except ValueError as cause:
            raise RuntimeError(secret) from cause

    monkeypatch.setitem(sys.modules, "msu_hub_bot.app", SimpleNamespace(run=fail))
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1
    assert caught.value.__suppress_context__
    shutdown.assert_called_once_with()
    output = stream.getvalue()
    assert "Bot stopped unexpectedly" in output and "RuntimeError" in output and "ValueError" in output
    assert secret not in output and token not in output and "[REDACTED]" in output
    unhandled = "".join(traceback.format_exception(caught.value))
    assert secret not in unhandled and token not in unhandled and "RuntimeError" not in unhandled
    handler.close()


def test_example_and_deployment_cover_current_settings():
    root = Path(__file__).resolve().parents[1]
    configured = {"HUB_" + name.upper() for name in Settings.model_fields}
    configured.update(
        {"HUB_TELEMETRY_ENABLED", "HUB_TELEMETRY_SAMPLE_RATE", "HUB_TELEMETRY_METRICS_INTERVAL_SECONDS", "HUB_ENVIRONMENT", "HUB_RELEASE"}
    )
    example = set(re.findall(r"^(HUB_[A-Z0-9_]+)=", (root / ".env.example").read_text(), re.MULTILINE))
    deployed = set(re.findall(r"^\s+(HUB_[A-Z0-9_]+):", (root / ".github/workflows/deploy.yml").read_text(), re.MULTILINE))
    assert example == configured
    assert deployed == configured


def test_jev_workflow_passes_the_existing_secret_only_to_deployment():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/deploy.yml").read_text()
    before, deployment = workflow.split("      - name: Deploy with automatic rollback", 1)
    assert "OPENROUTER_API_KEY" not in before
    assert "HUB_OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}" in deployment
    assert "HUB_JEV_ENABLED: ${{ vars.HUB_JEV_ENABLED || 'false' }}" in deployment
    assert "HUB_JEV_CONFIDENCE: ${{ vars.HUB_JEV_CONFIDENCE || '0.8' }}" in deployment


def test_project_write_token_is_redacted_outside_application_settings(monkeypatch):
    token = 'synthetic-project-write-$"/with-newline\nsecond-write-canary'
    monkeypatch.setenv("LOGFIRE_TOKEN", token)
    for rendered in (token, quote(token, safe=""), json.dumps(token)[1:-1], repr(token)[1:-1]):
        assert rendered not in redact(rendered)
    error = RuntimeError(token)
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "%s", (token,), (RuntimeError, error, None))
    assert "synthetic-project-write" not in RedactingFormatter().format(record)


@pytest.mark.parametrize("key", ["LOGFIRE_API_KEY", "LOGFIRE_READ_TOKEN", "LOGFIRE_TOKENS", "OTEL_EXPORTER_OTLP_HEADERS"])
def test_runtime_envelope_rejects_management_and_arbitrary_telemetry_credentials(monkeypatch, key):
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    monkeypatch.setenv("HUB_CONFIG_JSON", json.dumps({"LOGFIRE_TOKEN": "write-canary", key: "private-canary"}))
    with pytest.raises(ValueError, match="Invalid HUB_CONFIG_JSON") as caught:
        load_runtime_environment()
    assert "private-canary" not in str(caught.value)
    assert "LOGFIRE_TOKEN" not in os.environ


def test_runtime_envelope_keeps_explicit_write_token_precedence(monkeypatch):
    monkeypatch.setenv("LOGFIRE_TOKEN", "explicit-canary")
    monkeypatch.setenv("HUB_CONFIG_JSON", json.dumps({"LOGFIRE_TOKEN": "envelope-canary"}))
    load_runtime_environment()
    assert os.environ["LOGFIRE_TOKEN"] == "explicit-canary"


def test_telemetry_workflow_is_opt_in_and_token_stays_out_of_build_steps():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/deploy.yml").read_text()
    before, deployment = workflow.split("      - name: Deploy with automatic rollback", 1)
    assert "LOGFIRE_TOKEN" not in before
    assert "LOGFIRE_API_KEY" not in workflow and "OTEL_" not in workflow
    assert "HUB_TELEMETRY_ENABLED: ${{ vars.HUB_TELEMETRY_ENABLED || 'false' }}" in deployment
    assert "HUB_TELEMETRY_SAMPLE_RATE: ${{ vars.HUB_TELEMETRY_SAMPLE_RATE || '0.1' }}" in deployment
    assert "HUB_TELEMETRY_METRICS_INTERVAL_SECONDS: ${{ vars.HUB_TELEMETRY_METRICS_INTERVAL_SECONDS || '300' }}" in deployment
    assert "HUB_ENVIRONMENT: production" in deployment
    assert "HUB_RELEASE: ${{ github.sha }}" in deployment
    assert "LOGFIRE_TOKEN: ${{ secrets.LOGFIRE_TOKEN }}" in deployment
    example = (root / ".env.example").read_text()
    assert re.search(r"^LOGFIRE_TOKEN=$", example, re.MULTILINE)
    assert re.search(r"^HUB_TELEMETRY_ENABLED=false$", example, re.MULTILINE)
