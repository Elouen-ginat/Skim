from __future__ import annotations

import re


def resource_slug(name: str, *, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        slug = "skaal"
    if not slug[0].isalpha():
        slug = f"skaal-{slug}"
    return slug[:max_len].rstrip("-") or "skaal"


def database_name(app_name: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "_", app_name.lower()).strip("_")
    if not name:
        name = "skaal"
    if not name[0].isalpha():
        name = f"skaal_{name}"
    return name[:63]


def safe_key(route_key: str) -> str:
    return re.sub(r"[^a-zA-Z0-9-]", "-", route_key).strip("-")
