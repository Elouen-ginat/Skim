from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, List

from skaal.backends._spec import BackendSpec, Wiring
from skaal.deploy.kinds import StorageKind
from skaal.errors import SkaalConflict, SkaalUnavailable


class FirestoreBackend:
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
        def _delete() -> None:
            self._col().document(key).delete()

        await self._run(_delete)

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
        def _apply() -> Any:
            try:
                from google.api_core import exceptions as g_exc
                from google.cloud import firestore
            except ImportError as exc:
                raise SkaalUnavailable(
                    "google-cloud-firestore is required for atomic_update"
                ) from exc

            db = self._get_client()
            doc_ref = self._col().document(key)

            @firestore.transactional
            def _update_in_txn(txn: Any) -> Any:
                doc = doc_ref.get(transaction=txn)
                current = json.loads(doc.get("value")) if doc.exists else None
                updated = fn(current)
                txn.set(doc_ref, {"pk": key, "value": json.dumps(updated)})
                return updated

            try:
                return _update_in_txn(db.transaction())
            except g_exc.Aborted as exc:
                raise SkaalConflict(f"atomic_update on {key!r} lost a race") from exc
            except g_exc.ServiceUnavailable as exc:
                raise SkaalUnavailable(f"Firestore unavailable: {exc}") from exc

        return await self._run(_apply)

    async def close(self) -> None:
        self._client = None

    def __repr__(self) -> str:
        return (
            f"FirestoreBackend(collection={self.collection!r}, "
            f"project={self.project!r}, database={self.database!r})"
        )


FIRESTORE_SPEC = BackendSpec(
    name="firestore",
    kinds=frozenset({StorageKind.KV}),
    wiring=Wiring(
        class_name="FirestoreBackend",
        module="skaal.backends.kv.firestore",
        env_prefix="SKAAL_COLLECTION",
    ),
    supported_targets=frozenset({"gcp"}),
    local_fallbacks={StorageKind.KV: "sqlite"},
)

__all__ = ["FIRESTORE_SPEC", "FirestoreBackend"]
