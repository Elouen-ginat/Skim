"""Cloud Firestore storage backend (google-cloud-firestore + thread pool for async compatibility)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, List


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
            self._col().document(key).set({"pk": key, "value": json.dumps(value)})

        await self._run(_set)

    async def delete(self, key: str) -> None:
        def _del() -> None:
            self._col().document(key).delete()

        await self._run(_del)

    async def list(self) -> list[tuple[str, Any]]:
        def _list() -> list[tuple[str, Any]]:
            items: list[tuple[str, Any]] = []
            for doc in self._col().stream():
                data = doc.to_dict()
                if data and "value" in data:
                    items.append((doc.id, json.loads(data["value"])))
            return items

        return await self._run(_list)

    async def scan(self, prefix: str = "") -> List[tuple[str, Any]]:
        def _scan() -> list[tuple[str, Any]]:
            items: list[tuple[str, Any]] = []
            if prefix:
                # Firestore range query: key >= prefix AND key < prefix + '\uffff'
                query = self._col().where("pk", ">=", prefix).where("pk", "<", prefix + "\uffff")
                docs = query.stream()
            else:
                docs = self._col().stream()
            for doc in docs:
                data = doc.to_dict()
                if data and "value" in data:
                    items.append((doc.id, json.loads(data["value"])))
            return items

        return await self._run(_scan)

    async def increment_counter(self, key: str, delta: int = 1) -> int:
        """Atomically increment a counter using Firestore transaction."""

        def _increment() -> int:
            transaction = self._get_client().transaction()
            with transaction:
                doc_ref = self._col().document(key)
                doc = doc_ref.get(transaction=transaction)
                current = json.loads(doc.get("value")) if doc.exists else 0
                new_value = int(current) + delta
                doc_ref.set({"pk": key, "value": json.dumps(new_value)}, transaction=transaction)
            return new_value

        return await self._run(_increment)

    async def close(self) -> None:
        # google-cloud-firestore clients don't require explicit closing
        self._client = None

    def __repr__(self) -> str:
        return (
            f"FirestoreBackend(collection={self.collection!r}, "
            f"project={self.project!r}, database={self.database!r})"
        )
