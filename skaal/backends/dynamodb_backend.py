"""DynamoDB storage backend (boto3 + thread pool for async compatibility)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, List

from skaal.errors import SkaalConflict, SkaalUnavailable


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
            client.put_item(
                TableName=self.table_name,
                Item={"pk": {"S": key}, "value": {"S": json.dumps(value)}},
            )

        await self._run(_put)

    async def delete(self, key: str) -> None:
        client = self._get_client()

        def _del() -> None:
            client.delete_item(
                TableName=self.table_name,
                Key={"pk": {"S": key}},
            )

        await self._run(_del)

    async def list(self) -> list[tuple[str, Any]]:
        client = self._get_client()

        def _scan() -> list[tuple[str, Any]]:
            items: list[tuple[str, Any]] = []
            kwargs: dict[str, Any] = {"TableName": self.table_name}
            while True:
                resp = client.scan(**kwargs)
                for item in resp.get("Items", []):
                    pk = item["pk"]["S"]
                    val = json.loads(item["value"]["S"])
                    items.append((pk, val))
                last = resp.get("LastEvaluatedKey")
                if not last:
                    break
                kwargs["ExclusiveStartKey"] = last
            return items

        return await self._run(_scan)

    async def scan(self, prefix: str = "") -> List[tuple[str, Any]]:
        client = self._get_client()

        def _scan() -> list[tuple[str, Any]]:
            items: list[tuple[str, Any]] = []
            kwargs: dict[str, Any] = {"TableName": self.table_name}
            if prefix:
                kwargs["FilterExpression"] = "begins_with(pk, :pfx)"
                kwargs["ExpressionAttributeValues"] = {":pfx": {"S": prefix}}
            while True:
                resp = client.scan(**kwargs)
                for item in resp.get("Items", []):
                    pk = item["pk"]["S"]
                    val = json.loads(item["value"]["S"])
                    items.append((pk, val))
                last = resp.get("LastEvaluatedKey")
                if not last:
                    break
                kwargs["ExclusiveStartKey"] = last
            return items

        return await self._run(_scan)

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
