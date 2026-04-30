"""Cloud Firestore storage backend (google-cloud-firestore + thread pool for async compatibility)."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Callable, List

from skaal.errors import SkaalConflict, SkaalUnavailable
from skaal.storage import (
    _cursor_identity,
    _decode_cursor,
    _encode_cursor,
    _field_value,
    _get_backend_indexes,
    _normalize_limit,
    _sort_token,
)
from skaal.types.storage import Page


def _validate_cursor(
    cursor: str | None,
    *,
    mode: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decoded = _decode_cursor(cursor)
    expected = {"mode": mode, **(extra or {})}
    for key, value in expected.items():
        if decoded and decoded.get(key) != value:
            raise ValueError("Cursor does not match this query")
    return decoded


class FirestoreBackend:
    """
    Google Cloud Firestore storage backend.

    Each backend instance maps to a Firestore collection named ``namespace``.
    Documents have the form: {pk: <key>, value: <json-string>}.

    Uses google-cloud-firestore in asyncio.run_in_executor for async
    compatibility (the Firestore SDK is synchronous).

    Requires google-cloud-firestore installed and Application Default
    Credentials configured (e.g. GOOGLE_APPLICATION_CREDENTIALS env var or
    running on GCP with a service account).
    """

    def __init__(
        self,
        collection: str,
        project: str | None = None,
        database: str = "(default)",
    ) -> None:
        self.collection = collection
        self.project = project
        self.database = database
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import firestore
            except ImportError as exc:
                raise ImportError(
                    "google-cloud-firestore is required for FirestoreBackend. "
                    "Install it with: pip install google-cloud-firestore"
                ) from exc
            kwargs: dict[str, Any] = {"database": self.database}
            if self.project is not None:
                kwargs["project"] = self.project
            self._client = firestore.Client(**kwargs)
        return self._client

    def _col(self) -> Any:
        return self._get_client().collection(self.collection)

    def _idx_col(self) -> Any:
        return self._get_client().collection(f"{self.collection}__skaal_indexes")

    def _index_bucket_id(self, index_name: str, partition_key: Any) -> str:
        token = base64.urlsafe_b64encode(
            json.dumps(partition_key, sort_keys=True, default=str).encode("utf-8")
        ).decode("ascii")
        return f"{index_name}:{token}"

    def _read_index_bucket(self, index_name: str, partition_key: Any) -> list[dict[str, Any]]:
        doc = self._idx_col().document(self._index_bucket_id(index_name, partition_key)).get()
        if not doc.exists:
            return []
        data = doc.to_dict() or {}
        return list(data.get("entries", []))

    def _write_index_bucket(
        self,
        index_name: str,
        partition_key: Any,
        entries: list[dict[str, Any]],
    ) -> None:
        doc_ref = self._idx_col().document(self._index_bucket_id(index_name, partition_key))
        if not entries:
            doc_ref.delete()
            return
        doc_ref.set({"entries": entries})

    def _sync_indexes(self, key: str, old_value: Any, new_value: Any) -> None:
        for index_name, index in _get_backend_indexes(self).items():
            old_partition = (
                _field_value(old_value, index.partition_key) if old_value is not None else None
            )
            new_partition = (
                _field_value(new_value, index.partition_key) if new_value is not None else None
            )

            if old_partition is not None:
                entries = [
                    entry
                    for entry in self._read_index_bucket(index_name, old_partition)
                    if entry["pk"] != key
                ]
                self._write_index_bucket(index_name, old_partition, entries)

            if new_partition is not None:
                sort_value = (
                    _field_value(new_value, index.sort_key) if index.sort_key is not None else key
                )
                entries = [
                    entry
                    for entry in self._read_index_bucket(index_name, new_partition)
                    if entry["pk"] != key
                ]
                entries.append({"pk": key, "sort": sort_value})
                entries.sort(key=lambda item: (_sort_token(item.get("sort")), item["pk"]))
                self._write_index_bucket(index_name, new_partition, entries)

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def get(self, key: str) -> Any | None:
        def _get() -> Any | None:
            doc = self._col().document(key).get()
            if not doc.exists:
                return None
            return json.loads(doc.get("value"))

        return await self._run(_get)

    async def set(self, key: str, value: Any) -> None:
        def _set() -> None:
            doc = self._col().document(key).get()
            old_value = json.loads(doc.get("value")) if doc.exists and doc.get("value") else None
            self._col().document(key).set({"pk": key, "value": json.dumps(value)})
            self._sync_indexes(key, old_value, value)

        await self._run(_set)

    async def delete(self, key: str) -> None:
        def _del() -> None:
            doc = self._col().document(key).get()
            old_value = json.loads(doc.get("value")) if doc.exists and doc.get("value") else None
            self._col().document(key).delete()
            if old_value is not None:
                self._sync_indexes(key, old_value, None)

        await self._run(_del)

    async def list(self) -> list[tuple[str, Any]]:
        page = await self.list_page(limit=10_000, cursor=None)
        items = list(page.items)
        while page.has_more:
            page = await self.list_page(limit=10_000, cursor=page.next_cursor)
            items.extend(page.items)
        return items

    async def list_page(self, *, limit: int, cursor: str | None):
        limit = _normalize_limit(limit)
        decoded = _validate_cursor(cursor, mode="list")

        def _list_page() -> Page[tuple[str, Any]]:
            query = self._col().order_by("pk")
            last_key = decoded.get("last_key") if decoded else None
            if last_key is not None:
                query = query.where("pk", ">", last_key)
            docs = list(query.limit(limit + 1).stream())
            page_docs = docs[:limit]
            has_more = len(docs) > limit
            items = []
            for doc in page_docs:
                data = doc.to_dict()
                if data and "value" in data:
                    items.append((doc.id, json.loads(data["value"])))
            next_cursor = None
            if has_more and page_docs:
                next_cursor = _encode_cursor({"mode": "list", "last_key": page_docs[-1].id})
            return Page(items=items, next_cursor=next_cursor, has_more=has_more)

        return await self._run(_list_page)

    async def scan(self, prefix: str = "") -> List[tuple[str, Any]]:
        page = await self.scan_page(prefix=prefix, limit=10_000, cursor=None)
        items = list(page.items)
        while page.has_more:
            page = await self.scan_page(prefix=prefix, limit=10_000, cursor=page.next_cursor)
            items.extend(page.items)
        return items

    async def scan_page(self, prefix: str = "", *, limit: int, cursor: str | None):
        limit = _normalize_limit(limit)
        decoded = _validate_cursor(cursor, mode="scan", extra={"prefix": prefix})

        def _scan_page() -> Page[tuple[str, Any]]:
            query = self._col().order_by("pk")
            if prefix:
                lower_bound = (
                    decoded.get("last_key") if decoded and decoded.get("last_key") else prefix
                )
                comparator = ">" if decoded and decoded.get("last_key") else ">="
                query = query.where("pk", comparator, lower_bound).where(
                    "pk", "<", prefix + "\uffff"
                )
            elif decoded and decoded.get("last_key"):
                query = query.where("pk", ">", decoded["last_key"])
            docs = list(query.limit(limit + 1).stream())
            page_docs = docs[:limit]
            has_more = len(docs) > limit
            items = []
            for doc in page_docs:
                data = doc.to_dict()
                if data and "value" in data:
                    items.append((doc.id, json.loads(data["value"])))
            next_cursor = None
            if has_more and page_docs:
                next_cursor = _encode_cursor(
                    {"mode": "scan", "prefix": prefix, "last_key": page_docs[-1].id}
                )
            return Page(items=items, next_cursor=next_cursor, has_more=has_more)

        return await self._run(_scan_page)

    async def query_index(
        self,
        index_name: str,
        key: Any,
        *,
        limit: int,
        cursor: str | None,
    ):
        limit = _normalize_limit(limit)
        decoded = _validate_cursor(
            cursor,
            mode="index",
            extra={"index_name": index_name, "key": _cursor_identity(key)},
        )

        def _query_index() -> Page[Any]:
            entries = self._read_index_bucket(index_name, key)
            offset = int(decoded.get("offset", 0)) if decoded else 0
            page_entries = entries[offset : offset + limit]
            has_more = offset + len(page_entries) < len(entries)
            items = []
            for entry in page_entries:
                doc = self._col().document(entry["pk"]).get()
                if doc.exists and doc.get("value") is not None:
                    items.append(json.loads(doc.get("value")))
            next_cursor = None
            if has_more:
                next_cursor = _encode_cursor(
                    {
                        "mode": "index",
                        "index_name": index_name,
                        "key": _cursor_identity(key),
                        "offset": offset + len(page_entries),
                    }
                )
            return Page(items=items, next_cursor=next_cursor, has_more=has_more)

        return await self._run(_query_index)

    async def increment_counter(self, key: str, delta: int = 1) -> int:
        """Atomically increment a counter using a Firestore transaction."""

        def _increment() -> int:
            from google.cloud import firestore

            db = self._get_client()
            doc_ref = self._col().document(key)

            @firestore.transactional
            def _update_in_txn(txn: Any) -> int:
                doc = doc_ref.get(transaction=txn)
                current = json.loads(doc.get("value")) if doc.exists else 0
                new_value = int(current) + delta
                txn.set(doc_ref, {"pk": key, "value": json.dumps(new_value)})
                return new_value

            return _update_in_txn(db.transaction())

        return await self._run(_increment)

    async def atomic_update(self, key: str, fn: Callable[[Any], Any]) -> Any:
        """Atomically read, apply *fn*, and write back inside a Firestore transaction.

        Firestore retries the transaction internally on contention; after the
        configured attempts are exhausted the SDK raises
        ``google.api_core.exceptions.Aborted``, which we surface as
        :class:`skaal.errors.SkaalConflict`.
        """

        def _apply() -> Any:
            try:
                from google.api_core import exceptions as g_exc
                from google.cloud import firestore
            except ImportError as exc:  # pragma: no cover
                raise SkaalUnavailable(
                    "google-cloud-firestore is required for atomic_update"
                ) from exc

            db = self._get_client()
            doc_ref = self._col().document(key)
            previous_value: Any = None

            @firestore.transactional
            def _update_in_txn(txn: Any) -> Any:
                nonlocal previous_value
                doc = doc_ref.get(transaction=txn)
                current = json.loads(doc.get("value")) if doc.exists else None
                previous_value = current
                updated = fn(current)
                txn.set(doc_ref, {"pk": key, "value": json.dumps(updated)})
                return updated

            try:
                updated = _update_in_txn(db.transaction())
                self._sync_indexes(key, previous_value, updated)
                return updated
            except g_exc.Aborted as exc:
                raise SkaalConflict(f"atomic_update on {key!r} lost a race") from exc
            except g_exc.ServiceUnavailable as exc:
                raise SkaalUnavailable(f"Firestore unavailable: {exc}") from exc

        return await self._run(_apply)

    async def close(self) -> None:
        # google-cloud-firestore clients don't require explicit closing
        self._client = None

    def __repr__(self) -> str:
        return (
            f"FirestoreBackend(collection={self.collection!r}, "
            f"project={self.project!r}, database={self.database!r})"
        )
