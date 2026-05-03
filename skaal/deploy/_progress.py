from __future__ import annotations

import logging
from typing import Any


class ProgressSink:
    """Route Pulumi and Docker progress streams into the Skaal logger tree."""

    def __init__(self, logger: logging.Logger):
        self._pulumi = logger.getChild("pulumi")
        self._docker = logger.getChild("docker")

    def pulumi_output(self, line: str) -> None:
        message = line.rstrip()
        if message:
            self._pulumi.info(message, extra={"source": "pulumi"})

    def pulumi_event(self, event: Any) -> None:
        self._pulumi.debug("pulumi.event", extra={"source": "pulumi", "event": event})

    def docker_log(self, chunk: dict[str, Any]) -> None:
        if "stream" in chunk:
            message = str(chunk["stream"]).rstrip()
            if message:
                self._docker.info(message, extra={"source": "docker"})
            return
        if "error" in chunk:
            self._docker.error(str(chunk["error"]), extra={"source": "docker"})
            return
        self._docker.debug("docker.event", extra={"source": "docker", "event": chunk})
