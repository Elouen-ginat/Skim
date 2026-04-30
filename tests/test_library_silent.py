from __future__ import annotations

import io
import logging

import skaal


def test_library_logging_is_silent_by_default(capsys) -> None:
    logger = logging.getLogger("skaal.plan")
    logger.info("hidden")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_library_logging_flows_to_attached_handler() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("skaal")
    existing_handlers = list(logger.handlers)
    previous_level = logger.level

    try:
        logger.handlers = [
            existing for existing in logger.handlers if isinstance(existing, logging.NullHandler)
        ]
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        logging.getLogger("skaal.plan").info("visible")
    finally:
        logger.handlers = existing_handlers
        logger.setLevel(previous_level)

    assert isinstance(skaal.__version__, str)
    assert "visible" in stream.getvalue()