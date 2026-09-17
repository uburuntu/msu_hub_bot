import logging
import os
from pathlib import Path

from msu_hub_bot.redaction import RedactingFormatter


class LoggerBuilder:
    default_filename: str = None

    @classmethod
    def set_defaults(cls, filename: str = None):
        if filename:
            cls.default_filename = filename

    @classmethod
    def get_logger(cls, component_name: str, level=None, filename=None) -> logging.Logger:
        logger = logging.Logger(component_name)
        logger.setLevel(logging.DEBUG)
        formatter = RedactingFormatter("[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

        if level is None:
            level = os.getenv("LOG_LEVEL", logging.DEBUG)

        handler_console = logging.StreamHandler()
        handler_console.setFormatter(formatter)
        handler_console.setLevel(level)
        logger.addHandler(handler_console)

        filename = filename or cls.default_filename
        if filename:
            Path(filename.format(name=component_name)).parent.mkdir(parents=True, exist_ok=True)
            handler_file = logging.FileHandler(filename.format(name=component_name), encoding="utf-8")
            handler_file.setFormatter(formatter)
            handler_file.setLevel(logging.WARNING)
            logger.addHandler(handler_file)

        return logger
