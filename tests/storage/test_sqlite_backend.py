"""Tests for SqliteBackend."""

from __future__ import annotations

import pytest

from skaal.backends.sqlite_backend import SqliteBackend
from skaal.types.storage import SecondaryIndex


@pytest.mark.asyncio
async def test_get_set_delete(tmp_path):
    db_file = tmp_path / "test.db"
    backend = SqliteBackend(db_file, namespace="ns1")
    try:
        assert await backend.get("key1") is None

        await backend.set("key1", "hello")
        assert await backend.get("key1") == "hello"

        await backend.set("key1", 42)
        assert await backend.get("key1") == 42

        await backend.delete("key1")
        assert await backend.get("key1") is None

        # delete non-existent key is no-op
        await backend.delete("nonexistent")
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_list(tmp_path):
    db_file = tmp_path / "test.db"
    backend = SqliteBackend(db_file, namespace="ns1")
    try:
        await backend.set("a", 1)
        await backend.set("b", 2)
        await backend.set("c", {"nested": True})

        items = await backend.list()
        assert len(items) == 3
        keys = {k for k, _ in items}
        assert keys == {"a", "b", "c"}
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_scan(tmp_path):
    db_file = tmp_path / "test.db"
    backend = SqliteBackend(db_file, namespace="ns1")
    try:
        await backend.set("event:00001", "e1")
        await backend.set("event:00002", "e2")
        await backend.set("meta:count", 2)

        results = await backend.scan("event:")
        assert len(results) == 2
        keys = {k for k, _ in results}
        assert keys == {"event:00001", "event:00002"}

        all_results = await backend.scan("")
        assert len(all_results) == 3

        empty = await backend.scan("nomatch:")
        assert empty == []
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_persistence(tmp_path):
    """Data written in one backend instance should be readable after reopen."""
    db_file = tmp_path / "persist.db"

    backend1 = SqliteBackend(db_file, namespace="ns1")
    try:
        await backend1.set("persistent_key", {"value": 99})
        await backend1.set("another", [1, 2, 3])
    finally:
        await backend1.close()

    # Reopen same file
    backend2 = SqliteBackend(db_file, namespace="ns1")
    try:
        assert await backend2.get("persistent_key") == {"value": 99}
        assert await backend2.get("another") == [1, 2, 3]
        items = await backend2.list()
        assert len(items) == 2
    finally:
        await backend2.close()


@pytest.mark.asyncio
async def test_namespace_isolation(tmp_path):
    """Two backends sharing a file but with different namespaces must not interfere."""
    db_file = tmp_path / "shared.db"

    ns_a = SqliteBackend(db_file, namespace="A")
    ns_b = SqliteBackend(db_file, namespace="B")
    try:
        await ns_a.set("shared_key", "from_A")
        await ns_b.set("shared_key", "from_B")
        await ns_b.set("only_in_b", True)

        assert await ns_a.get("shared_key") == "from_A"
        assert await ns_b.get("shared_key") == "from_B"
        assert await ns_a.get("only_in_b") is None

        a_items = await ns_a.list()
        assert len(a_items) == 1

        b_items = await ns_b.list()
        assert len(b_items) == 2
    finally:
        await ns_a.close()
        await ns_b.close()


@pytest.mark.asyncio
async def test_lazy_connect(tmp_path):
    """Backend should work without explicit connect() call."""
    db_file = tmp_path / "lazy.db"
    backend = SqliteBackend(db_file, namespace="lazy")
    # Do NOT call connect() explicitly
    try:
        await backend.set("auto", "connected")
        assert await backend.get("auto") == "connected"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_json_types(tmp_path):
    """Various JSON-serializable types should round-trip correctly."""
    db_file = tmp_path / "types.db"
    backend = SqliteBackend(db_file, namespace="types")
    try:
        cases = {
            "int": 42,
            "float": 3.14,
            "bool_true": True,
            "bool_false": False,
            "none_val": None,
            "list": [1, "two", 3.0],
            "dict": {"a": 1, "b": [2, 3]},
            "str": "hello world",
        }
        for key, value in cases.items():
            await backend.set(key, value)

        for key, expected in cases.items():
            assert await backend.get(key) == expected, f"Mismatch for {key}"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_list_page_scan_page_and_query_index(tmp_path):
    db_file = tmp_path / "paged.db"
    backend = SqliteBackend(db_file, namespace="paged")
    setattr(
        backend,
        "_skaal_secondary_indexes",
        {
            "by_team": SecondaryIndex(name="by_team", partition_key="team", sort_key="score"),
        },
    )
    try:
        await backend.set("m1", {"team": "alpha", "score": 10})
        await backend.set("m2", {"team": "alpha", "score": 2})
        await backend.set("m3", {"team": "beta", "score": 5})

        list_page = await backend.list_page(limit=2, cursor=None)
        assert [key for key, _ in list_page.items] == ["m1", "m2"]
        assert list_page.has_more is True
        assert list_page.next_cursor is not None

        next_list_page = await backend.list_page(limit=2, cursor=list_page.next_cursor)
        assert [key for key, _ in next_list_page.items] == ["m3"]
        assert next_list_page.has_more is False

        scan_page = await backend.scan_page("m", limit=2, cursor=None)
        assert [key for key, _ in scan_page.items] == ["m1", "m2"]
        assert scan_page.has_more is True
        assert scan_page.next_cursor is not None

        next_scan_page = await backend.scan_page("m", limit=2, cursor=scan_page.next_cursor)
        assert [key for key, _ in next_scan_page.items] == ["m3"]
        assert next_scan_page.has_more is False

        index_page = await backend.query_index("by_team", "alpha", limit=2, cursor=None)
        assert [item["score"] for item in index_page.items] == [2, 10]
        assert index_page.has_more is False
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_query_index_pages_sorted_results(tmp_path):
    db_file = tmp_path / "indexed.db"
    backend = SqliteBackend(db_file, namespace="indexed")
    setattr(
        backend,
        "_skaal_secondary_indexes",
        {
            "by_team": SecondaryIndex(name="by_team", partition_key="team", sort_key="score"),
        },
    )
    try:
        await backend.set("m1", {"team": "alpha", "score": 10})
        await backend.set("m2", {"team": "alpha", "score": 2})
        await backend.set("m3", {"team": "alpha", "score": 30})

        first_page = await backend.query_index("by_team", "alpha", limit=2, cursor=None)
        assert [item["score"] for item in first_page.items] == [2, 10]
        assert first_page.has_more is True
        assert first_page.next_cursor is not None

        second_page = await backend.query_index(
            "by_team",
            "alpha",
            limit=2,
            cursor=first_page.next_cursor,
        )
        assert [item["score"] for item in second_page.items] == [30]
        assert second_page.has_more is False
    finally:
        await backend.close()
