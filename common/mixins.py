import logging
from typing import Any

from common.logger import LoggerBuilder


class LoggerMixin:
    def __init_subclass__(cls, logger_name: str | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._logger_name = logger_name or cls.__name__

    @property
    def logger(self) -> logging.Logger:
        if not hasattr(self, '_logger'):
            logger = LoggerBuilder.get_logger(self._logger_name)
            setattr(self, '_logger', logger)
        return getattr(self, '_logger')

    @logger.deleter
    def logger(self):
        if hasattr(self, '_logger'):
            logging.shutdown()
            self._logger.handlers = []
            delattr(self, '_logger')
