"""Types for the catalog loader's pre-validation layer.

The :class:`CatalogSource` value object describes the resolved chain of
catalog files a single ``load_catalog`` call traversed.  It exists to make
``[skaal] extends = "..."`` overlays introspectable from tests and from
``skaal catalog sources`` without re-parsing.

The typed Pydantic :class:`~skaal.catalog.models.Catalog` model lives one
layer above this — it consumes the merged dict produced by walking a
``CatalogSource`` chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CatalogRaw = dict[str, Any]
"""Raw TOML payload for one catalog file, with the reserved ``[skaal]``
table already stripped off."""


@dataclass(frozen=True)
class CatalogSource:
    """One link in a resolved ``extends`` chain.

    Attributes:
        path:    Absolute path the file was loaded from.  ``None`` for
            bundled catalogs loaded via :mod:`importlib.resources`.
        raw:     This file's TOML contents (minus the reserved ``[skaal]``
            table).  Not yet merged with any parent.
        parent:  The parent this file ``extends``, or ``None`` for the root.
        removes: Dotted paths the file declared under
            ``[skaal] remove = [...]``, applied to the merged result.
    """

    path: Path | None
    raw: CatalogRaw
    parent: "CatalogSource | None" = None
    removes: tuple[str, ...] = field(default_factory=tuple)

    def chain(self) -> tuple["CatalogSource", ...]:
        """Return the chain root-first (parent → ... → self)."""
        chain: list[CatalogSource] = []
        node: CatalogSource | None = self
        while node is not None:
            chain.append(node)
            node = node.parent
        return tuple(reversed(chain))


__all__ = ["CatalogRaw", "CatalogSource"]
