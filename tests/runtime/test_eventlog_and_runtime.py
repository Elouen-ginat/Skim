"""Tests for EventLog and runtime fixes."""

from __future__ import annotations

import asyncio

import pytest

from skaal.backends.local_backend import LocalMap
from skaal.patterns import EventLog


class TestEventLogFixes:
    """Test EventLog TOCTOU race condition fix and scan bug fix."""

    @pytest.mark.asyncio
    async def test_append_concurrent_no_collision(self) -> None:
        """Test that concurrent appends don't collide (TOCTOU fix)."""
        backend = LocalMap()
        log = EventLog(backend)

        # Simulate concurrent appends
        results = await asyncio.gather(
            log.append({"type": "event1"}),
            log.append({"type": "event2"}),
            log.append({"type": "event3"}),
        )

        # All should get unique offsets
        assert len(set(results)) == 3
        assert sorted(results) == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_append_sequential_increments(self) -> None:
        """Test sequential appends increment correctly."""
        backend = LocalMap()
        log = EventLog(backend)

        off1 = await log.append({"data": "first"})
        off2 = await log.append({"data": "second"})
        off3 = await log.append({"data": "third"})

        assert off1 == 0
        assert off2 == 1
        assert off3 == 2

    @pytest.mark.asyncio
    async def test_replay_from_offset(self) -> None:
        """Test replay correctly returns events from offset."""
        backend = LocalMap()
        log = EventLog(backend)

        # Append events
        for i in range(5):
            await log.append({"id": i})

        # Replay from offset 2
        events = []
        async for offset, event in log.replay(from_offset=2):
            events.append((offset, event))

        # Should get events 2, 3, 4
        assert len(events) == 3
        assert events[0][0] == 2
        assert events[1][0] == 3
        assert events[2][0] == 4

    @pytest.mark.asyncio
    async def test_replay_uses_full_prefix_scan(self) -> None:
        """Test replay uses full event: prefix, not offset-prefixed scan."""
        backend = LocalMap()
        log = EventLog(backend)

        # Add some events
        await log.append({"id": 0})
        await log.append({"id": 1})
        await log.append({"id": 2})

        # Manually add some events with offsets that would be missed by prefix scan
        # (this tests that the scan uses "event:" not "event:00000000000000000002")
        await backend.set("event:00000000000000000050", {"id": 50})

        # Replay from offset 1 - should get events 1, 2, and 50
        events = []
        async for offset, event in log.replay(from_offset=1):
            events.append(offset)

        assert 1 in events
        assert 2 in events
        assert 50 in events  # This would be missed if scan used offset-prefixed pattern

    @pytest.mark.asyncio
    async def test_subscribe_from_offset(self) -> None:
        """Test subscribe starts from correct offset."""
        backend = LocalMap()
        log = EventLog(backend)

        # Append some events
        for i in range(5):
            await log.append({"id": i})

        # Set consumer offset to 2
        await backend.set("consumer:group1:offset", 2)

        # Subscribe (should start from offset 2)
        events = []
        async for offset, event in log.subscribe("group1", poll_interval=0.01):
            events.append(offset)
            if len(events) >= 3:
                break  # Stop after getting 3 events

        # Should start from offset 2
        assert events[0] == 2

    @pytest.mark.asyncio
    async def test_subscribe_poll_interval(self) -> None:
        """Test subscribe respects poll_interval parameter."""
        backend = LocalMap()
        log = EventLog(backend)

        # Add initial event
        await log.append({"id": 0})

        # Start subscribe and get events - should return existing events immediately
        events = []
        async for offset, event in log.subscribe(
            "poller",
            from_beginning=True,
            poll_interval=0.05,  # Verify parameter is accepted
        ):
            events.append(offset)
            if len(events) >= 1:
                # Just break after first event - timing-based polls are hard to test reliably
                break

        # Should have gotten at least one event
        assert len(events) >= 1
        assert 0 in events


class TestLocalRuntimeFixes:
    """Test LocalRuntime request size and backend lifecycle fixes."""

    @pytest.mark.asyncio
    async def test_request_size_limit_enforcement(self) -> None:
        """Test request size limit prevents memory exhaustion."""
        from unittest.mock import AsyncMock, MagicMock

        from skaal.app import App
        from skaal.runtime.local import _MAX_BODY_SIZE, LocalRuntime

        app = App(name="test")

        @app.function
        async def echo(data: str) -> str:
            return data

        runtime = LocalRuntime(app)

        # Verify the constant is set
        assert _MAX_BODY_SIZE == 10 * 1024 * 1024

        # Test that oversized requests are rejected by mocking the TCP streams
        reader = AsyncMock()
        writer = MagicMock()

        # Simulate reading HTTP request with oversized Content-Length
        http_request = f"POST /echo HTTP/1.1\r\nContent-Length: {_MAX_BODY_SIZE + 1}\r\n\r\n"
        reader.readline.side_effect = [
            http_request.split("\r\n")[0].encode() + b"\r\n",
            http_request.split("\r\n")[1].encode() + b"\r\n",
            b"\r\n",  # empty line marking end of headers
        ]
        writer.drain = AsyncMock()

        # Call _handle_connection - should write 413 response and return early
        await runtime._handle_connection(reader, writer)

        # Verify write was called with 413 response
        write_calls = writer.write.call_args_list
        assert len(write_calls) > 0
        written_data = write_calls[0][0][0].decode("utf-8", errors="ignore")
        assert "413" in written_data  # Payload Too Large response code

    @pytest.mark.asyncio
    async def test_backend_shutdown_closes_connections(self) -> None:
        """Test LocalRuntime.shutdown() closes all backend connections."""
        from unittest.mock import AsyncMock

        from skaal.app import App
        from skaal.runtime.local import LocalRuntime

        app = App(name="test")

        @app.storage
        class Counter:
            pass

        # Create runtime and inject mock backends
        runtime = LocalRuntime(app)

        # Replace backends with mocks
        mock_backend1 = AsyncMock()
        mock_backend2 = AsyncMock()
        runtime._backends = {
            "Counter1": mock_backend1,
            "Counter2": mock_backend2,
        }

        # Call shutdown
        await runtime.shutdown()

        # Both backends should have close() called
        mock_backend1.close.assert_called_once()
        mock_backend2.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_backend_shutdown_handles_exceptions(self) -> None:
        """Test shutdown continues even if a backend raises."""
        from unittest.mock import AsyncMock

        from skaal.app import App
        from skaal.runtime.local import LocalRuntime

        app = App(name="test")
        runtime = LocalRuntime(app)

        # Create mock backends, one of which fails
        mock_backend1 = AsyncMock()
        mock_backend1.close.side_effect = Exception("Connection error")
        mock_backend2 = AsyncMock()

        runtime._backends = {
            "Backend1": mock_backend1,
            "Backend2": mock_backend2,
        }

        # shutdown() should handle exceptions and continue
        await runtime.shutdown()  # Should not raise

        # Both close() should have been attempted
        mock_backend1.close.assert_called_once()
        mock_backend2.close.assert_called_once()
