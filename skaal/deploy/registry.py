from __future__ import annotations

from dataclasses import dataclass, field

from skaal.deploy.targets.base import Target


@dataclass
class TargetRegistry:
    _targets: dict[str, Target] = field(default_factory=dict)

    def register(self, target: Target) -> Target:
        for name in (target.name, *target.aliases):
            if name in self._targets:
                raise ValueError(f"Target {name!r} is already registered.")
            self._targets[name] = target
        return target

    def get(self, name: str) -> Target:
        try:
            return self._targets[name]
        except KeyError as exc:
            known = sorted({target.name for target in self._targets.values()})
            raise ValueError(f"Unknown deploy target {name!r}. Supported targets: {known}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted({target.name for target in self._targets.values()}))


target_registry = TargetRegistry()


def register_target(target: Target) -> Target:
    return target_registry.register(target)


def get_target(name: str) -> Target:
    return target_registry.get(name)


from skaal.deploy.targets import BUILTIN_TARGETS  # noqa: E402

for _target in BUILTIN_TARGETS:
    register_target(_target)


__all__ = [
    "target_registry",
    "get_target",
    "register_target",
]
