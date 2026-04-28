"""Tests for schema-aware Store storage."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from skaal.backends.kv.local_map import LocalMap
from skaal.storage import Store, _deserialize, _primary_key_field, _schema_hints, _serialize

# ── Domain models for tests ────────────────────────────────────────────────────


class Address(BaseModel):
    street: str
    city: str
    country: str = "US"


class User(BaseModel):
    id: str
    name: str
    address: Address
    scores: list[float] = []


class Product(BaseModel):
    sku: str
    name: str
    price: float


# ── Store[T] type extraction ──────────────────────────────────────────────────


def test_store_extracts_value_type():
    class UserStore(Store[User]):
        pass

    assert UserStore.__skaal_value_type__ is User
    assert UserStore.__skaal_key_type__ is str


def test_store_nested_subclass_inherits():
    class Base(Store[User]):
        pass

    class Sub(Base):
        pass

    # Sub doesn't re-declare, inherits from Base
    assert Sub.__skaal_value_type__ is User


def test_store_primitive_value_type():
    class Counts(Store[int]):
        pass

    assert Counts.__skaal_value_type__ is int


# ── Store[T] key-field extraction ─────────────────────────────────────────────


def test_store_extracts_value_type_for_model_collection():
    class Products(Store[Product]):
        pass

    assert Products.__skaal_value_type__ is Product


def test_store_infers_key_field_from_id():
    class Users(Store[User]):
        pass

    assert Users.__skaal_key_field__ == "id"


def test_store_infers_key_field_first_when_no_id():
    class NoIdModel(BaseModel):
        sku: str
        name: str

    class Things(Store[NoIdModel]):
        pass

    assert Things.__skaal_key_field__ == "sku"


def test_store_respects_explicit_key_field():
    class ProductStore(Store[Product]):
        __skaal_key_field__ = "sku"

    assert ProductStore.__skaal_key_field__ == "sku"


# ── _primary_key_field helper ──────────────────────────────────────────────────


def test_primary_key_field_id():
    assert _primary_key_field(User) == "id"


def test_primary_key_field_first_fallback():
    class Thing(BaseModel):
        code: str
        label: str

    assert _primary_key_field(Thing) == "code"


# ── _schema_hints ──────────────────────────────────────────────────────────────


def test_schema_hints_empty_for_plain_class():
    class Plain:
        pass

    assert _schema_hints(Plain) == {}


def test_schema_hints_for_store_with_nested_model():
    class UserStore(Store[User]):
        pass

    hints = _schema_hints(UserStore)
    assert hints["model"] == "User"
    assert hints["field_count"] == 4  # id, name, address, scores
    assert hints["nested_models"] == 1  # Address
    assert hints["list_fields"] == 1  # scores
    assert hints["prefers_sql"] is True  # has nested model


def test_schema_hints_flat_model():
    class PStore(Store[Product]):
        pass

    hints = _schema_hints(PStore)
    assert hints["nested_models"] == 0
    assert hints["list_fields"] == 0
    assert hints["prefers_sql"] is False


# ── _serialize / _deserialize ──────────────────────────────────────────────────


def test_serialize_pydantic_model():
    user = User(id="u1", name="Alice", address=Address(street="1 Main", city="NYC"))
    result = _serialize(user, User)
    assert isinstance(result, dict)
    assert result["id"] == "u1"
    assert result["address"] == {"street": "1 Main", "city": "NYC", "country": "US"}


def test_serialize_dict_validates_and_converts():
    raw = {"id": "u1", "name": "Bob", "address": {"street": "2 Elm", "city": "LA"}}
    result = _serialize(raw, User)
    assert isinstance(result, dict)
    assert result["address"]["country"] == "US"  # default filled in


def test_serialize_no_type_passthrough():
    assert _serialize(42, None) == 42
    assert _serialize({"x": 1}, None) == {"x": 1}


def test_deserialize_dict_to_model():
    raw = {"id": "u1", "name": "Carol", "address": {"street": "3 Oak", "city": "SF"}}
    result = _deserialize(raw, User)
    assert isinstance(result, User)
    assert isinstance(result.address, Address)
    assert result.address.city == "SF"


def test_deserialize_already_correct_type():
    user = User(id="u1", name="Dan", address=Address(street="4 Pine", city="BOS"))
    result = _deserialize(user, User)
    assert result is user


def test_deserialize_none():
    assert _deserialize(None, User) is None
    assert _deserialize(None, None) is None


def test_deserialize_no_type_passthrough():
    assert _deserialize(42, None) == 42


def test_deserialize_json_string():
    user = User(id="u1", name="Eve", address=Address(street="5 Maple", city="CHI"))
    json_str = user.model_dump_json()
    result = _deserialize(json_str, User)
    assert isinstance(result, User)
    assert result.name == "Eve"


# ── Store.wire() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_get_set_returns_model():
    class UserStore(Store[User]):
        pass

    UserStore.wire(LocalMap())

    user = User(id="u1", name="Frank", address=Address(street="6 Birch", city="PDX"))
    await UserStore.set("u1", user)
    result = await UserStore.get("u1")

    assert isinstance(result, User)
    assert isinstance(result.address, Address)
    assert result.name == "Frank"
    assert result.address.city == "PDX"


@pytest.mark.asyncio
async def test_store_accepts_dict_on_set():
    class UserStore(Store[User]):
        pass

    UserStore.wire(LocalMap())

    await UserStore.set(
        "u2", {"id": "u2", "name": "Grace", "address": {"street": "7 Cedar", "city": "SEA"}}
    )
    result = await UserStore.get("u2")
    assert isinstance(result, User)
    assert result.address.country == "US"  # default applied


@pytest.mark.asyncio
async def test_store_list_returns_models():
    class UserStore(Store[User]):
        pass

    UserStore.wire(LocalMap())

    for i, name in enumerate(["Alice", "Bob", "Carol"]):
        await UserStore.set(
            f"u{i}", User(id=f"u{i}", name=name, address=Address(street=f"{i} St", city="NYC"))
        )

    entries = await UserStore.list()
    assert len(entries) == 3
    assert all(isinstance(v, User) for _, v in entries)


@pytest.mark.asyncio
async def test_store_nested_model_roundtrip():
    class UserStore(Store[User]):
        pass

    UserStore.wire(LocalMap())

    user = User(
        id="u1",
        name="Heidi",
        address=Address(street="8 Walnut", city="Austin", country="US"),
        scores=[1.5, 2.7, 3.9],
    )
    await UserStore.set("u1", user)
    result = await UserStore.get("u1")

    assert result.scores == [1.5, 2.7, 3.9]
    assert result.address.country == "US"


# ── Store model-centric helpers ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_add_and_get():
    class UserCol(Store[User]):
        pass

    UserCol.wire(LocalMap())

    user = User(id="u1", name="Ivan", address=Address(street="9 Elm", city="DEN"))
    await UserCol.add(user)

    result = await UserCol.get("u1")
    assert isinstance(result, User)
    assert result.name == "Ivan"


@pytest.mark.asyncio
async def test_store_all():
    class UserCol(Store[User]):
        pass

    UserCol.wire(LocalMap())

    for i in range(3):
        await UserCol.add(
            User(id=f"u{i}", name=f"User{i}", address=Address(street=f"{i} Rd", city="NYC"))
        )

    results = await UserCol.all()
    assert len(results) == 3
    assert all(isinstance(u, User) for u in results)


@pytest.mark.asyncio
async def test_store_remove():
    class UserCol(Store[User]):
        pass

    UserCol.wire(LocalMap())

    user = User(id="u1", name="Judy", address=Address(street="1 Ave", city="MIA"))
    await UserCol.add(user)
    await UserCol.remove("u1")
    assert await UserCol.get("u1") is None


@pytest.mark.asyncio
async def test_store_update_replaces_value():
    class UserCol(Store[User]):
        pass

    UserCol.wire(LocalMap())

    user = User(id="u1", name="Karl", address=Address(street="2 Ave", city="MIA"))
    await UserCol.add(user)

    updated = User(id="u1", name="Karl Updated", address=Address(street="2 Ave", city="MIA"))
    result = await UserCol.update("u1", updated)

    assert result.name == "Karl Updated"
    stored = await UserCol.get("u1")
    assert stored.name == "Karl Updated"


@pytest.mark.asyncio
async def test_store_find_by_prefix():
    class UserCol(Store[User]):
        pass

    UserCol.wire(LocalMap())

    for prefix, name in [("admin_u1", "Admin1"), ("user_u2", "User2"), ("admin_u3", "Admin3")]:
        await UserCol.add(User(id=prefix, name=name, address=Address(street="X St", city="NYC")))

    admins = await UserCol.find("admin_")
    assert len(admins) == 2
    assert all("Admin" in u.name for u in admins)


@pytest.mark.asyncio
async def test_store_update_accepts_atomic_function():
    class UserStore(Store[User]):
        pass

    UserStore.wire(LocalMap())
    await UserStore.add(User(id="u1", name="Lena", address=Address(street="3 Ave", city="MIA")))

    def rename(current: User | None) -> User:
        assert current is not None
        return current.model_copy(update={"name": "Lena Updated"})

    result = await UserStore.update("u1", rename)
    assert result.name == "Lena Updated"
    stored = await UserStore.get("u1")
    assert stored.name == "Lena Updated"


# ── todo_api.py integration ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_todo_api_end_to_end(tmp_path):
    """Full integration test of examples/todo_api.py with typed Store storage."""
    import json

    from examples.todo_api import app as todo_app
    from skaal.runtime.local import LocalRuntime

    runtime = LocalRuntime.from_sqlite(todo_app, db_path=tmp_path / "todo_api.db")

    try:
        # Create
        body = json.dumps(
            {
                "id": "t1",
                "title": "Test todo",
                "description": "Prepare the integration spec for the grocery workflow",
                "tags": ["test", "groceries"],
            }
        ).encode()
        data, status = await runtime._dispatch("POST", "/create_todo", body)
        assert status == 200
        assert data["id"] == "t1"
        assert data["tags"] == ["test", "groceries"]
        assert data["done"] is False

        # Get (returns full Pydantic model serialized)
        data, status = await runtime._dispatch("POST", "/get_todo", b'{"id":"t1"}')
        assert status == 200
        assert data["title"] == "Test todo"

        # Add a relational comment
        body = json.dumps(
            {"todo_id": "t1", "author": "alex", "body": "Remember oat milk too"}
        ).encode()
        data, status = await runtime._dispatch("POST", "/add_comment", body)
        assert status == 200
        assert data["todo_id"] == "t1"
        assert data["author"] == "alex"

        data, status = await runtime._dispatch("POST", "/list_comments", b'{"todo_id":"t1"}')
        assert status == 200
        assert data["count"] == 1
        assert data["comments"][0]["body"] == "Remember oat milk too"

        # Add attachment (nested model mutation)
        body = json.dumps(
            {"id": "t1", "url": "https://example.com/file.pdf", "name": "spec.pdf"}
        ).encode()
        data, status = await runtime._dispatch("POST", "/add_attachment", body)
        assert status == 200
        assert len(data["attachments"]) == 1
        assert data["attachments"][0]["name"] == "spec.pdf"
        assert data["attachments"][0]["mime_type"] == "application/octet-stream"

        # Complete
        data, status = await runtime._dispatch("POST", "/complete_todo", b'{"id":"t1"}')
        assert status == 200
        assert data["done"] is True
        assert data["completed_at"] is not None

        # Semantic search should use the vector index and resolve back to the stored todo
        body = json.dumps({"query": "grocery integration spec pdf", "done": True}).encode()
        data, status = await runtime._dispatch("POST", "/search_todos", body)
        assert status == 200
        assert data["count"] == 1
        assert data["todos"][0]["id"] == "t1"

        # List — attachment and done status preserved
        data, status = await runtime._dispatch("POST", "/list_todos", b"")
        assert status == 200
        assert data["count"] == 1
        todo = data["todos"][0]
        assert len(todo["attachments"]) == 1

        # Delete
        data, status = await runtime._dispatch("POST", "/delete_todo", b'{"id":"t1"}')
        assert status == 200

        data, status = await runtime._dispatch("POST", "/list_todos", b"")
        assert data["count"] == 0

        data, status = await runtime._dispatch(
            "POST", "/search_todos", b'{"query":"grocery integration spec"}'
        )
        assert status == 200
        assert data["count"] == 0
    finally:
        for backend in runtime._backends.values():
            await backend.close()
