from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from skaal.types import TargetName

DeployPhase = Literal["preview", "up", "destroy", "package", "image"]


@dataclass(slots=True)
class DeployError(Exception):
    target: TargetName
    phase: DeployPhase
    message: str
    diagnostics: str | None = None

    def __post_init__(self) -> None:
        super().__init__(self.__str__())

    def __str__(self) -> str:
        if not self.diagnostics:
            return self.message
        return f"{self.message}\n{self.diagnostics}"
