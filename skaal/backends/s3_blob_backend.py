from __future__ import annotations

from typing import Any

import fsspec

from skaal.backends.fsspec_blob_backend import FsspecBlobBackend


class S3BlobBackend(FsspecBlobBackend):
    def __init__(
        self,
        bucket: str,
        namespace: str | None = None,
        filesystem: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self.namespace = namespace.strip("/") if namespace else ""
        fs = filesystem or fsspec.filesystem("s3")
        super().__init__(fs, bucket, namespace=namespace)

    def __repr__(self) -> str:
        return f"S3BlobBackend(bucket={self.bucket!r}, namespace={self.namespace!r})"
