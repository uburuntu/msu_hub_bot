"""Installed executable; imports are canonical and allocate no services."""

import asyncio
import logging
from pathlib import Path

from msu_hub_bot.settings import settings


def configure_logging() -> None:
    from common.logger import LoggerBuilder
    from msu_hub_bot.redaction import RedactingFormatter, install_redaction

    install_redaction()
    filename = settings.logs_file.format(name=settings.name)
    LoggerBuilder.set_defaults(filename)
    formatter = RedactingFormatter("[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    handlers: list[logging.Handler] = [console]
    if filename:
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        file = logging.FileHandler(filename, encoding="utf-8")
        file.setLevel(logging.WARNING)
        file.setFormatter(formatter)
        handlers.append(file)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    # aiogram's routine records include bot/user/update identifiers. Aggregate
    # middleware diagnostics own dispatch logging instead.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiogram.dispatcher").setLevel(logging.WARNING)


def main() -> None:
    settings.validate_core()
    configure_logging()
    from hub_bot.app import run
    from msu_hub_bot.health import heartbeat_path

    heartbeat_path().unlink(missing_ok=True)
    try:
        asyncio.run(run(settings))
    finally:
        logging.shutdown()


if __name__ == "__main__":
    main()
