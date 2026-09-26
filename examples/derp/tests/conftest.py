"""Separate-process native checks with synthetic settings and no network."""

import os
import socket
import sys
from pathlib import Path

if any((Path.cwd() / name).exists() for name in (".env", ".env.prod")):
    raise RuntimeError("Run native checks from an isolated checkout without environment files")

path = os.environ.get("PATH", "")
os.environ.clear()
os.environ.update(
    PATH=path,
    ENVIRONMENT="dev",
    TELEGRAM_BOT_TOKEN="123456:TEST_TOKEN_FOR_TESTING",
    DATABASE_URL="postgresql+asyncpg://derp_test:derp_test@localhost:5433/derp_test",
    GOOGLE_API_PAID_KEY="synthetic-unused-key",
    OPENROUTER_API_KEY="synthetic-unused-key",
    LOGFIRE_TOKEN="synthetic-unused-key",
    LOGFIRE_IGNORE_NO_CONFIG="1",
    LOGFIRE_SEND_TO_LOGFIRE="false",
)


def deny_network(event: str, args: tuple[object, ...]) -> None:
    if event in {"socket.connect", "socket.sendto", "socket.bind"} and getattr(args[0], "family", None) != socket.AF_UNIX:
        raise RuntimeError("Native checks prohibit network I/O")
    if event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
        raise RuntimeError("Native checks prohibit DNS")


sys.addaudithook(deny_network)


def pytest_configure() -> None:
    from pydantic_ai import models

    models.ALLOW_MODEL_REQUESTS = False
