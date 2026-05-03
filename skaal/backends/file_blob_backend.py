from __future__ import annotations

from pathlib import Path

import fsspec

from skaal.backends.fsspec_blob_backend import FsspecBlobBackend


class FileBlobBackend(FsspecBlobBackend):
    def __init__(self, root_path: str | Path, namespace: str | None = None) -> None:
        base = Path(root_path)
        self.root = base / namespace if namespace else base
        super().__init__(fsspec.filesystem("file"), str(base.resolve()), namespace=namespace)

    def __repr__(self) -> str:
        return f"FileBlobBackend(root={str(self.root)!r})"
