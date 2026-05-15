"""Tests for sp_rtk_base.services.event_bridge — thread-to-async event forwarding."""

# pyright: reportPrivateUsage=false
# Tests need to access internal state for unit testing.

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from sp_rtk_base.services.event_bridge import EventBridge
from sp_rtk_base.services.relay_service import RelayService
from sp_rtk_base_relay import RelayEvent


def _make_event(event_type: str = "engine.started", msg: str = "Test") -> RelayEvent:
    """Create a test RelayEvent."""
    return RelayEvent(
        event_type=event_type,
        message=msg,
        timestamp=time.time(),
        payload={},
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestEventBridgeConstruction:
    """Tests for EventBridge initialization."""

    def test_initial_state(self) -> None:
        """New EventBridge is not running."""
        bridge = EventBridge()
        assert bridge.is_running is False

    def test_event_queue_exists(self) -> None:
        """Event queue is available on construction."""
        bridge = EventBridge()
        assert bridge.event_queue is not None

    def test_custom_queue_size(self) -> None:
        """EventBridge respects custom max_queue_size."""
        bridge = EventBridge(max_queue_size=10)
        assert bridge.event_queue.maxsize == 10


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestEventBridgeLifecycle:
    """Tests for start/stop lifecycle."""

    def test_start_with_subscription(self) -> None:
        """start() begins the event bridge thread."""
        mock_sub = MagicMock()
        mock_sub.is_closed = False
        mock_sub.get_event.return_value = None

        mock_relay = MagicMock(spec=RelayService)
        mock_relay.subscribe_events.return_value = mock_sub

        bridge = EventBridge()
        bridge.start(mock_relay)

        assert bridge.is_running is True
        assert bridge._thread is not None
        assert bridge._thread.is_alive()

        bridge.stop()
        assert bridge.is_running is False

    def test_start_when_already_running_raises(self) -> None:
        """start() raises RuntimeError if already running."""
        mock_sub = MagicMock()
        mock_sub.is_closed = False
        mock_sub.get_event.return_value = None

        mock_relay = MagicMock(spec=RelayService)
        mock_relay.subscribe_events.return_value = mock_sub

        bridge = EventBridge()
        bridge.start(mock_relay)

        with pytest.raises(RuntimeError, match="already running"):
            bridge.start(mock_relay)

        bridge.stop()

    def test_start_without_engine_raises(self) -> None:
        """start() raises RuntimeError if relay has no engine."""
        mock_relay = MagicMock(spec=RelayService)
        mock_relay.subscribe_events.return_value = None

        bridge = EventBridge()
        with pytest.raises(RuntimeError, match="not available"):
            bridge.start(mock_relay)

    def test_stop_when_not_running_is_noop(self) -> None:
        """stop() is a no-op when not running."""
        bridge = EventBridge()
        bridge.stop()  # Should not raise
        assert bridge.is_running is False

    def test_stop_closes_subscription(self) -> None:
        """stop() closes the event subscription."""
        mock_sub = MagicMock()
        mock_sub.is_closed = False
        mock_sub.get_event.return_value = None

        mock_relay = MagicMock(spec=RelayService)
        mock_relay.subscribe_events.return_value = mock_sub

        bridge = EventBridge()
        bridge.start(mock_relay)
        bridge.stop()

        mock_sub.close.assert_called_once()


# ---------------------------------------------------------------------------
# Event delivery
# ---------------------------------------------------------------------------


class TestEventBridgeDelivery:
    """Tests for event delivery to async queue."""

    def test_event_forwarded_to_queue(self) -> None:
        """Events from subscription are pushed to the async queue."""
        test_event = _make_event("engine.started", "Engine started")

        mock_sub = MagicMock()
        mock_sub.is_closed = False
        # Return one event then None (will loop, then we stop)
        call_count = 0

        def get_event_side_effect(timeout: float = 1.0) -> RelayEvent | None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return test_event
            # After delivering the event, simulate closing
            mock_sub.is_closed = True
            return None

        mock_sub.get_event.side_effect = get_event_side_effect

        mock_relay = MagicMock(spec=RelayService)
        mock_relay.subscribe_events.return_value = mock_sub

        bridge = EventBridge()
        bridge.start(mock_relay)

        # Wait a moment for the thread to process the event
        time.sleep(0.5)
        bridge.stop()

        # Check queue received the event
        assert not bridge.event_queue.empty()
        event_dict = bridge.event_queue.get_nowait()
        assert event_dict["event_type"] == "engine.started"
        assert event_dict["message"] == "Engine started"

    def test_serialize_event(self) -> None:
        """_serialize_event converts RelayEvent to dict."""
        bridge = EventBridge()
        event = _make_event("destination.connected", "Connected")

        result = bridge._serialize_event(event)
        assert isinstance(result, dict)
        assert result["event_type"] == "destination.connected"
        assert result["message"] == "Connected"
        assert "timestamp" in result
        assert "payload" in result

    def test_enqueue_nowait_on_full_queue(self) -> None:
        """_enqueue_nowait discards oldest on full queue."""
        bridge = EventBridge(max_queue_size=2)

        # Fill the queue
        bridge._enqueue_nowait({"event_type": "first"})
        bridge._enqueue_nowait({"event_type": "second"})
        assert bridge.event_queue.qsize() == 2

        # Push a third — should evict the oldest
        bridge._enqueue_nowait({"event_type": "third"})
        assert bridge.event_queue.qsize() == 2

        # Verify "first" was evicted
        item1 = bridge.event_queue.get_nowait()
        assert item1["event_type"] == "second"
        item2 = bridge.event_queue.get_nowait()
        assert item2["event_type"] == "third"

    def test_push_to_queue_without_loop(self) -> None:
        """_push_to_queue works without an event loop (fallback path)."""
        bridge = EventBridge()
        bridge._loop = None

        bridge._push_to_queue({"event_type": "test"})
        assert not bridge.event_queue.empty()
        event = bridge.event_queue.get_nowait()
        assert event["event_type"] == "test"
