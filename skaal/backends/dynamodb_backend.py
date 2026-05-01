"""DynamoDB storage backend (boto3 + thread pool for async compatibility)."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Callable, List

from skaal.errors import SkaalConflict, SkaalUnavailable
from skaal.storage import (
    _cursor_identity,
    _encode_cursor,
    _field_value,
    _get_backend_indexes,
    _normalize_limit,
    _sort_token,
    _validate_cursor,
)
from skaal.types.storage import Page


class DynamoBackend:
    """
    AWS DynamoDB storage backend.

    Table schema: pk (String, hash key), value (String, JSON-encoded).
    Uses boto3 in asyncio.run_in_executor for async compatibility.
    Requires boto3 installed and AWS credentials configured.

    All methods delegate to synchronous boto3 calls via run_in_executor
    to avoid blocking the event loop.
    """

    def __init__(self, table_name: str, region: str = "us-east-1") -> None:
        self.table_name = table_name
        self.region = region
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:
                raise ImportError(
                    "boto3 is required for DynamoBackend. " "Install it with: pip install boto3"
                ) from exc
            self._client = boto3.client("dynamodb", region_name=self.region)
        return self._client

    def _index_bucket_pk(self, index_name: str, partition_key: Any) -> str:
        token = base64.urlsafe_b64encode(
            json.dumps(partition_key, sort_keys=True, default=str).encode("utf-8")
        ).decode("ascii")
        return f"__skaal_idx__:{index_name}:{token}"

    def _read_index_bucket(
        self, client: Any, index_name: str, partition_key: Any
    ) -> list[dict[str, Any]]:
        resp = client.get_item(
            TableName=self.table_name,
            Key={"pk": {"S": self._index_bucket_pk(index_name, partition_key)}},
        )
        item = resp.get("Item")
        if item is None or "entries" not in item:
            return []
        return json.loads(item["entries"]["S"])

    def _write_index_bucket(
        self,
        client: Any,
        index_name: str,
        partition_key: Any,
        entries: list[dict[str, Any]],
    ) -> None:
        bucket_pk = self._index_bucket_pk(index_name, partition_key)
        if not entries:
            client.delete_item(TableName=self.table_name, Key={"pk": {"S": bucket_pk}})
            return
        client.put_item(
            TableName=self.table_name,
            Item={
                "pk": {"S": bucket_pk},
                "kind": {"S": "index_bucket"},
                "entries": {"S": json.dumps(entries)},
            },
        )

    def _sync_indexes(self, client: Any, key: str, old_value: Any, new_value: Any) -> None:
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
                    for entry in self._read_index_bucket(client, index_name, old_partition)
                    if entry["pk"] != key
                ]
                self._write_index_bucket(client, index_name, old_partition, entries)

            if new_partition is not None:
                sort_value = (
                    _field_value(new_value, index.sort_key) if index.sort_key is not None else key
                )
                entries = [
                    entry
                    for entry in self._read_index_bucket(client, index_name, new_partition)
                    if entry["pk"] != key
                ]
                entries.append({"pk": key, "sort": sort_value})
                entries.sort(key=lambda item: (_sort_token(item.get("sort")), item["pk"]))
                self._write_index_bucket(client, index_name, new_partition, entries)

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def get(self, key: str) -> Any | None:
        client = self._get_client()

        def _get() -> Any | None:
            resp = client.get_item(
                TableName=self.table_name,
                Key={"pk": {"S": key}},
            )
            item = resp.get("Item")
            if item is None:
                return None
            return json.loads(item["value"]["S"])

        return await self._run(_get)

    async def set(self, key: str, value: Any) -> None:
        client = self._get_client()

        def _put() -> None:
            current = client.get_item(
                TableName=self.table_name,
                Key={"pk": {"S": key}},
            ).get("Item")
            old_value = (
                json.loads(current["value"]["S"]) if current and "value" in current else None
            )
            client.put_item(
                TableName=self.table_name,
                Item={
                    "pk": {"S": key},
                    "kind": {"S": "item"},
                    "value": {"S": json.dumps(value)},
                },
            )
            self._sync_indexes(client, key, old_value, value)

        await self._run(_put)

    async def delete(self, key: str) -> None:
        client = self._get_client()

        def _del() -> None:
            current = client.get_item(
                TableName=self.table_name,
                Key={"pk": {"S": key}},
            ).get("Item")
            old_value = (
                json.loads(current["value"]["S"]) if current and "value" in current else None
            )
            client.delete_item(
                TableName=self.table_name,
                Key={"pk": {"S": key}},
            )
            if old_value is not None:
                self._sync_indexes(client, key, old_value, None)

        await self._run(_del)

    async def list(self) -> list[tuple[str, Any]]:
        page = await self.list_page(limit=10_000, cursor=None)
        items = list(page.items)
        while page.has_more:
            page = await self.list_page(limit=10_000, cursor=page.next_cursor)
            items.extend(page.items)
        return items

    async def list_page(self, *, limit: int, cursor: str | None):
        return await self._scan_page_native(prefix=None, limit=limit, cursor=cursor, mode="list")

    async def scan(self, prefix: str = "") -> List[tuple[str, Any]]:
        page = await self.scan_page(prefix=prefix, limit=10_000, cursor=None)
        items = list(page.items)
        while page.has_more:
            page = await self.scan_page(prefix=prefix, limit=10_000, cursor=page.next_cursor)
            items.extend(page.items)
        return items

    async def scan_page(self, prefix: str = "", *, limit: int, cursor: str | None):
        return await self._scan_page_native(
            prefix=prefix,
            limit=limit,
            cursor=cursor,
            mode="scan",
        )

    async def _scan_page_native(
        self,
        *,
        prefix: str | None,
        limit: int,
        cursor: str | None,
        mode: str,
    ) -> Page[tuple[str, Any]]:
        client = self._get_client()
        limit = _normalize_limit(limit)
        extra = {"prefix": prefix or ""} if mode == "scan" else None
        decoded = _validate_cursor(cursor, mode=mode, extra=extra)

        def _page() -> Page[tuple[str, Any]]:
            collected: list[tuple[str, Any]] = []
            last_key = decoded.get("exclusive_start_key") if decoded else None
            while len(collected) < limit + 1:
                kwargs: dict[str, Any] = {
                    "TableName": self.table_name,
                    "Limit": limit + 1 - len(collected),
                    "FilterExpression": "(attribute_not_exists(#kind) OR #kind = :item)"
                    + (" AND begins_with(pk, :pfx)" if prefix else ""),
                    "ExpressionAttributeNames": {"#kind": "kind"},
                    "ExpressionAttributeValues": {":item": {"S": "item"}},
                }
                if prefix:
                    kwargs["ExpressionAttributeValues"][":pfx"] = {"S": prefix}
                if last_key is not None:
                    kwargs["ExclusiveStartKey"] = last_key
                resp = client.scan(**kwargs)
                for item in resp.get("Items", []):
                    if "value" not in item:
                        continue
                    collected.append((item["pk"]["S"], json.loads(item["value"]["S"])))
                    if len(collected) >= limit + 1:
                        break
                last_key = resp.get("LastEvaluatedKey")
                if not last_key:
                    break

            page_items = collected[:limit]
            has_more = len(collected) > limit or bool(last_key)
            next_cursor = None
            if has_more and last_key is not None:
                payload = {"mode": mode, "exclusive_start_key": last_key}
                if prefix is not None and mode == "scan":
                    payload["prefix"] = prefix
                next_cursor = _encode_cursor(payload)
            return Page(items=page_items, next_cursor=next_cursor, has_more=has_more)

        return await self._run(_page)

    async def query_index(
        self,
        index_name: str,
        key: Any,
        *,
        limit: int,
        cursor: str | None,
    ):
        client = self._get_client()
        limit = _normalize_limit(limit)
        decoded = _validate_cursor(
            cursor,
            mode="index",
            extra={"index_name": index_name, "key": _cursor_identity(key)},
        )

        def _query() -> Page[Any]:
            entries = self._read_index_bucket(client, index_name, key)
            offset = int(decoded.get("offset", 0)) if decoded else 0
            page_entries = entries[offset : offset + limit]
            has_more = offset + len(page_entries) < len(entries)
            if not page_entries:
                return Page(items=[], next_cursor=None, has_more=False)

            batch = client.batch_get_item(
                RequestItems={
                    self.table_name: {
                        "Keys": [{"pk": {"S": entry["pk"]}} for entry in page_entries]
                    }
                }
            )
            items_by_pk = {
                item["pk"]["S"]: json.loads(item["value"]["S"])
                for item in batch.get("Responses", {}).get(self.table_name, [])
                if "value" in item
            }
            ordered_items = [
                items_by_pk[entry["pk"]] for entry in page_entries if entry["pk"] in items_by_pk
            ]
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
            return Page(items=ordered_items, next_cursor=next_cursor, has_more=has_more)

        return await self._run(_query)

    async def increment_counter(self, key: str, delta: int = 1) -> int:
        """Atomically increment a counter using DynamoDB UpdateItem.

        Uses a single ``UpdateItem`` with ``if_not_exists`` to handle both
        the create-if-missing and increment cases atomically — no separate
        ``put_item`` needed.
        """
        client = self._get_client()

        def _increment() -> int:
            resp = client.update_item(
                TableName=self.table_name,
                Key={"pk": {"S": key}},
                UpdateExpression="SET #v = if_not_exists(#v, :zero) + :d",
                ExpressionAttributeNames={"#v": "counter"},
                ExpressionAttributeValues={
                    ":zero": {"N": "0"},
                    ":d": {"N": str(delta)},
                },
                ReturnValues="ALL_NEW",
            )
            new_val = resp["Attributes"]["counter"]
            if isinstance(new_val, dict) and "N" in new_val:
                return int(new_val["N"])
            return int(new_val)

        return await self._run(_increment)

    async def atomic_update(
        self,
        key: str,
        fn: Callable[[Any], Any],
        *,
        max_retries: int = 8,
    ) -> Any:
        """Atomically read-modify-write using an optimistic ``version`` attribute.

        Each row carries a monotonic ``ver`` number; writes use
        ``ConditionExpression`` to only succeed when the version hasn't
        changed since the read.  After *max_retries* contended attempts a
        :class:`skaal.errors.SkaalConflict` is raised.
        """
        try:
            import botocore.exceptions
        except ImportError as exc:  # pragma: no cover — boto3 always ships botocore
            raise SkaalUnavailable("botocore is required for DynamoBackend") from exc

        client = self._get_client()

        def _once() -> tuple[bool, Any]:
            resp = client.get_item(
                TableName=self.table_name,
                Key={"pk": {"S": key}},
                ConsistentRead=True,
            )
            item = resp.get("Item")
            if item is None:
                current: Any = None
                current_ver = 0
            else:
                current = json.loads(item["value"]["S"])
                current_ver = int(item.get("ver", {}).get("N", "0"))

            updated = fn(current)
            next_ver = current_ver + 1

            try:
                if item is None:
                    client.put_item(
                        TableName=self.table_name,
                        Item={
                            "pk": {"S": key},
                            "kind": {"S": "item"},
                            "value": {"S": json.dumps(updated)},
                            "ver": {"N": str(next_ver)},
                        },
                        ConditionExpression="attribute_not_exists(pk)",
                    )
                else:
                    client.put_item(
                        TableName=self.table_name,
                        Item={
                            "pk": {"S": key},
                            "kind": {"S": "item"},
                            "value": {"S": json.dumps(updated)},
                            "ver": {"N": str(next_ver)},
                        },
                        ConditionExpression="ver = :cur",
                        ExpressionAttributeValues={":cur": {"N": str(current_ver)}},
                    )
            except botocore.exceptions.ClientError as client_exc:
                code = client_exc.response.get("Error", {}).get("Code", "")
                if code == "ConditionalCheckFailedException":
                    return False, None
                raise
            self._sync_indexes(client, key, current, updated)
            return True, updated

        async def _loop() -> Any:
            for _ in range(max_retries):
                try:
                    ok, updated = await self._run(_once)
                except botocore.exceptions.EndpointConnectionError as net_exc:
                    raise SkaalUnavailable(f"DynamoDB unreachable: {net_exc}") from net_exc
                if ok:
                    return updated
            raise SkaalConflict(f"atomic_update on {key!r} lost {max_retries} consecutive races")

        return await _loop()

    async def close(self) -> None:
        # boto3 clients don't need explicit closing
        self._client = None

    def __repr__(self) -> str:
        return f"DynamoBackend(table={self.table_name!r}, region={self.region!r})"
