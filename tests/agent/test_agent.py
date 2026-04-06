"""Tests for Agent class and persistent field marking."""

from __future__ import annotations

import weakref

from skaal.agent import Agent, AgentMeta
from skaal.types import Persistent


class TestAgentClass:
    """Test Agent base class and metaclass."""

    def test_agent_subclass_registration(self) -> None:
        """Agent subclasses should be registered in AgentMeta._registry."""

        class MyAgent(Agent):
            pass

        # WeakValueDictionary should contain the class
        assert "MyAgent" in AgentMeta._registry
        assert AgentMeta._registry["MyAgent"] is MyAgent

    def test_agent_registry_uses_weakref(self) -> None:
        """Agent registry should use WeakValueDictionary."""
        # Verify the registry is a WeakValueDictionary
        assert isinstance(AgentMeta._registry, weakref.WeakValueDictionary)

    def test_agent_registry_cleanup_on_gc(self) -> None:
        """Agent classes should be removed from registry when garbage collected."""

        # Create a temporary agent class
        class TemporaryAgent(Agent):
            pass

        # Verify it's in the registry
        assert "TemporaryAgent" in AgentMeta._registry

        # Delete all references
        del TemporaryAgent
        import gc

        gc.collect()

        # Should be gone from registry
        assert "TemporaryAgent" not in AgentMeta._registry

    def test_persistent_fields_explicit_marking(self) -> None:
        """Only fields marked with Persistent[T] should be persistent."""

        class MyAgent(Agent):
            persistent_score: Persistent[float] = 0.0
            transient_state: dict = {}

        # Only persistent_score should be in __skim_persistent_fields__
        assert "persistent_score" in MyAgent.__skim_persistent_fields__
        assert "transient_state" not in MyAgent.__skim_persistent_fields__

    def test_persistent_fields_without_wrapper_ignored(self) -> None:
        """Fields without Persistent[T] wrapper should not be marked persistent."""

        class MyAgent(Agent):
            score: float = 0.0  # Not wrapped in Persistent
            _internal: int = 0  # Private field
            name: Persistent[str] = "default"  # Explicitly persistent

        # Only name should be persistent
        assert "name" in MyAgent.__skim_persistent_fields__
        assert "score" not in MyAgent.__skim_persistent_fields__
        assert "_internal" not in MyAgent.__skim_persistent_fields__

    def test_persistent_type_annotation_works(self) -> None:
        """Persistent[T] should work as a type annotation."""

        class MyAgent(Agent):
            balance: Persistent[float] = 100.0
            name: Persistent[str] = "anonymous"

        assert len(MyAgent.__skim_persistent_fields__) == 2
        assert "balance" in MyAgent.__skim_persistent_fields__
        assert "name" in MyAgent.__skim_persistent_fields__

    def test_no_persistent_fields_by_default(self) -> None:
        """Agent without explicit Persistent marks should have no persistent fields."""

        class SimpleAgent(Agent):
            value: int = 0
            state: dict = {}

        assert len(SimpleAgent.__skim_persistent_fields__) == 0

    def test_agent_skim_agent_attribute(self) -> None:
        """Agent subclasses should have __skim_agent__ attribute."""

        class MyAgent(Agent):
            pass

        assert hasattr(MyAgent, "__skim_agent__")
        assert isinstance(MyAgent.__skim_agent__, dict)

    def test_agent_can_subclass_multiple_times(self) -> None:
        """Multiple Agent subclasses should all work correctly."""

        class Agent1(Agent):
            field1: Persistent[int] = 0

        class Agent2(Agent):
            field2: Persistent[str] = ""

        # Both should be registered
        assert "Agent1" in AgentMeta._registry
        assert "Agent2" in AgentMeta._registry

        # Each should have correct persistent fields
        assert "field1" in Agent1.__skim_persistent_fields__
        assert "field2" in Agent2.__skim_persistent_fields__
        assert "field2" not in Agent1.__skim_persistent_fields__
        assert "field1" not in Agent2.__skim_persistent_fields__


class TestPersistentTypeAnnotation:
    """Test the Persistent[T] type annotation."""

    def test_persistent_generic_annotation(self) -> None:
        """Persistent[T] should be instantiable with any type."""
        annotation1 = Persistent[int]
        annotation2 = Persistent[str]
        annotation3 = Persistent[dict]

        # All should be different instances but same origin
        assert annotation1 is not annotation2
        assert annotation1 is not annotation3

    def test_persistent_in_agent(self) -> None:
        """Persistent should work in Agent type annotations."""

        class TestAgent(Agent):
            score: Persistent[float] = 0.0

        assert "score" in TestAgent.__skim_persistent_fields__

    def test_persistent_mixed_fields(self) -> None:
        """Agent with mix of persistent and transient fields."""

        class MixedAgent(Agent):
            # Persistent
            persistent_count: Persistent[int] = 0
            persistent_name: Persistent[str] = "unknown"

            # Transient (not wrapped)
            cache: dict = {}
            temp_value: int = 0

            # Private
            _internal: str = ""

        persistent = MixedAgent.__skim_persistent_fields__
        assert "persistent_count" in persistent
        assert "persistent_name" in persistent
        assert "cache" not in persistent
        assert "temp_value" not in persistent
        assert "_internal" not in persistent
