from __future__ import annotations

from typing import Any

import fsspec

from skaal.backends.fsspec_blob_backend import FsspecBlobBackend


class GCSBlobBackend(FsspecBlobBackend):
    def __init__(
        self,
        bucket: str,
        namespace: str | None = None,
        filesystem: Any | None = None,
    ) -> None:
        self.bucket_name = bucket
        self.namespace = namespace.strip("/") if namespace else ""
        fs = filesystem or fsspec.filesystem("gcs")
        super().__init__(fs, bucket, namespace=namespace)

    def __repr__(self) -> str:
        return f"GCSBlobBackend(bucket={self.bucket_name!r}, namespace={self.namespace!r})"
