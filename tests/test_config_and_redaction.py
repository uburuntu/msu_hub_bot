import io
import json
import logging
import re
from pathlib import Path
from urllib.parse import quote

import pytest

from common.logger import LoggerBuilder
from msu_hub_bot.redaction import RedactingFormatter, RedactingStream, redact
from msu_hub_bot.settings import MissingIntegration, Settings, load_runtime_environment, settings


def test_required_configuration_and_optional_providers(monkeypatch):
    for key in ("HUB_BOT_TOKEN", "HUB_REDIS_HOST", "HUB_EDGEDB_DSN"):
        monkeypatch.delenv(key, raising=False)
    config = Settings()
    with pytest.raises(ValueError, match="HUB_BOT_TOKEN"):
        config.validate_core()
    with pytest.raises(MissingIntegration):
        config.require("wolfram_token")
    assert "values='<redacted>'" in repr(config)


def test_json_collections_and_deployment_roundtrip(monkeypatch):
    secret = 'test-value-with-$quotes"-and\\slashes\nsecond-line'
    payload = {"HUB_REDIS_PASSWORD": secret, "HUB_FOUNDER_IDS": "[101, 202]", "HUB_JDOODLE_TOKENS": '[["client", "secret"]]'}
    for key in payload:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    monkeypatch.setenv("HUB_CONFIG_JSON", json.dumps(payload))
    load_runtime_environment()
    # Register changes with monkeypatch so the next test sees a clean environment.
    for key in payload:
        monkeypatch.setenv(key, payload[key])
    config = Settings()
    assert config.redis_password == secret
    assert config.founder_ids == [101, 202]
    assert config.jdoodle_tokens == [("client", "secret")]
    assert secret not in repr(config)
    for key in payload:
        monkeypatch.delenv(key)


def test_redacts_logs_tracebacks_and_streams(monkeypatch):
    secret = 'canary-value-$"/with-newline\nsecond-canary-line'
    monkeypatch.setattr(settings, "redis_password", secret)
    assert secret not in redact(secret)
    assert quote(secret, safe="") not in redact(quote(secret, safe=""))
    assert json.dumps(secret)[1:-1] not in redact(json.dumps(secret)[1:-1])
    stream = io.StringIO()
    writer = RedactingStream(stream)
    writer.write(secret[:12])
    writer.write(secret[12:] + "\n")
    writer.flush()
    assert "canary-value" not in stream.getvalue()
    error = RuntimeError(secret)
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "%s", (secret,), (RuntimeError, error, None))
    rendered = RedactingFormatter().format(record)
    assert "canary-value" not in rendered


def test_short_configured_password_is_redacted(monkeypatch):
    monkeypatch.setattr(settings, "redis_password", "a$3")
    assert "a$3" not in redact("Connection failed with a$3")
    assert quote("a$3", safe="") not in redact(quote("a$3", safe=""))


def test_local_logger_preserves_levels_and_redacts_both_outputs(monkeypatch, tmp_path, capsys):
    secret = 'local-logging-canary-$"\nsecond-canary-line'
    monkeypatch.setattr(settings, "redis_password", secret)
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


def test_example_and_deployment_cover_current_settings():
    root = Path(__file__).resolve().parents[1]
    configured = {"HUB_" + name.upper() for name in Settings.model_fields}
    example = set(re.findall(r"^(HUB_[A-Z0-9_]+)=", (root / ".env.example").read_text(), re.MULTILINE))
    deployed = set(re.findall(r"^\s+(HUB_[A-Z0-9_]+):", (root / ".github/workflows/deploy.yml").read_text(), re.MULTILINE))
    assert example == configured
    assert deployed == configured
