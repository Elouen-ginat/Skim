from __future__ import annotations

import tomllib
from functools import lru_cache
from importlib.resources import files
from typing import Iterable


@lru_cache(maxsize=1)
def _dependency_manifest() -> dict[str, tuple[str, ...]]:
    raw = files("skaal.deploy.data").joinpath("dependency_sets.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(raw)
    dependency_sets = parsed.get("dependency_sets", {})
    return {name: tuple(values) for name, values in dependency_sets.items()}


def is_dependency_set_name(name: str) -> bool:
    return name in _dependency_manifest()


def resolve_dependency_sets(names: Iterable[str]) -> list[str]:
    manifest = _dependency_manifest()
    resolved: list[str] = []
    seen: set[str] = set()

    for name in names:
        try:
            values = manifest[name]
        except KeyError as exc:
            known = sorted(manifest)
            raise ValueError(f"Unknown dependency set {name!r}. Known sets: {known}") from exc
        for value in values:
            if value not in seen:
                resolved.append(value)
                seen.add(value)

    return resolved


__all__ = ["is_dependency_set_name", "resolve_dependency_sets"]
