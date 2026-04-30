from __future__ import annotations

import logging

SKAAL_LOGGER_NAME = "skaal"


def ensure_null_handler() -> logging.Logger:
    """Attach a NullHandler to the Skaal logger tree once."""
    logger = logging.getLogger(SKAAL_LOGGER_NAME)
    for handler in logger.handlers:
        if isinstance(handler, logging.NullHandler):
            return logger
    logger.addHandler(logging.NullHandler())
    return logger


ensure_null_handler()
