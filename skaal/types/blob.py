from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class BlobObject:
    key: str
    size: int
    content_type: str | None = None
    etag: str | None = None
    updated_at: datetime | None = None
    metadata: dict[str, str] = field(default_factory=dict)
