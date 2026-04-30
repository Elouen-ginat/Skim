"""
File upload API — FastAPI mounted over Skaal blob storage.

Run locally:

    pip install "skaal[examples]"
    skaal run examples.file_upload_api:app

Then try:

    curl -s -X POST "http://localhost:8000/files" \
        -F "owner=alice" \
        -F "file=@README.md"

    curl -s "http://localhost:8000/files?prefix=uploads/" | jq
"""

from __future__ import annotations

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from skaal import App, BlobStore

app = App("file-upload-api")
api = FastAPI(title="Skaal File Upload API")


@app.storage(kind="blob", read_latency="< 500ms", durability="durable")
class Uploads(BlobStore):
    pass


def _object_payload(obj: object) -> dict[str, object]:
    blob = obj
    return {
        "key": getattr(blob, "key"),
        "size": getattr(blob, "size"),
        "content_type": getattr(blob, "content_type"),
        "etag": getattr(blob, "etag"),
        "updated_at": (
            getattr(blob, "updated_at").isoformat() if getattr(blob, "updated_at") else None
        ),
        "metadata": getattr(blob, "metadata"),
    }


def _normalize_list_prefix(prefix: str | None) -> str:
    if prefix is None:
        return "uploads/"
    value = prefix.strip()
    if not value or value.lower() in {"null", "none"}:
        return "uploads/"
    return value.lstrip("/")


def _normalize_cursor(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    value = cursor.strip()
    if not value or value.lower() in {"null", "none"}:
        return None
    return value


@api.post("/files")
async def upload_file(
    file: UploadFile = File(...),
    owner: str = Form("anonymous"),
    folder: str = Form("uploads"),
) -> dict[str, object]:
    folder_name = folder.strip("/") or "uploads"
    filename = (file.filename or "upload.bin").strip("/")
    key = f"{folder_name}/{filename}"
    payload = await file.read()
    created = await Uploads.put_bytes(
        key,
        payload,
        content_type=file.content_type,
        metadata={"owner": owner},
    )
    return _object_payload(created)


@api.get("/files")
async def list_files(
    prefix: str | None = Query(
        None,
        description="Key prefix to list. Defaults to the upload folder.",
    ),
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(
        None,
        description="Opaque token from a previous response. Leave empty for the first page.",
    ),
) -> dict[str, object]:
    normalized_prefix = _normalize_list_prefix(prefix)
    normalized_cursor = _normalize_cursor(cursor)
    try:
        page = await Uploads.list_page(
            prefix=normalized_prefix,
            limit=limit,
            cursor=normalized_cursor,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid cursor. Leave it empty for the first page and reuse "
                "next_cursor from the previous /files response."
            ),
        ) from exc
    return {
        "items": [_object_payload(item) for item in page.items],
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


@api.get("/files/{path:path}")
async def download_file(path: str) -> Response:
    stat = await Uploads.stat(path)
    if stat is None:
        raise HTTPException(status_code=404, detail=f"Blob {path!r} not found")
    body = await Uploads.get_bytes(path)
    headers = {"ETag": stat.etag} if stat.etag else None
    return Response(
        content=body,
        media_type=stat.content_type or "application/octet-stream",
        headers=headers,
    )


@api.delete("/files/{path:path}")
async def delete_file(path: str) -> dict[str, object]:
    await Uploads.delete(path)
    return {"deleted": path}


app.mount_asgi(api, attribute="api")
