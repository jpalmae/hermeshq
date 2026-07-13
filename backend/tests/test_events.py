"""Tests for hermeshq.core.events – EventBroker and EventSubscription."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from hermeshq.core.events import EventBroker, EventSubscription


class TestEventSubscription(unittest.TestCase):
    """Tests for the EventSubscription dataclass."""

    def test_fields(self):
        ws = MagicMock()
        sub = EventSubscription(websocket=ws, is_admin=True, agent_ids={"a1", "a2"})
        self.assertIs(sub.websocket, ws)
        self.assertTrue(sub.is_admin)
        self.assertEqual(sub.agent_ids, {"a1", "a2"})

    def test_agent_ids_is_set(self):
        sub = EventSubscription(websocket=MagicMock(), is_admin=False, agent_ids=set())
        self.assertIsInstance(sub.agent_ids, set)

    def test_user_id_default_none(self):
        sub = EventSubscription(websocket=MagicMock(), is_admin=False, agent_ids=set())
        self.assertIsNone(sub.user_id)

    def test_user_id_set(self):
        sub = EventSubscription(websocket=MagicMock(), is_admin=False, agent_ids=set(), user_id="user-42")
        self.assertEqual(sub.user_id, "user-42")


# ---------------------------------------------------------------------------
# Helper to create mock WebSockets
# ---------------------------------------------------------------------------

def _make_ws(*, send_side_effect=None):
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock(side_effect=send_side_effect)
    return ws


# ===================================================================
# connect / disconnect
# ===================================================================

class TestConnectDisconnect(unittest.IsolatedAsyncioTestCase):
    """EventBroker.connect and EventBroker.disconnect."""

    async def asyncSetUp(self):
        self.broker = EventBroker()

    async def test_connect_adds_to_connections(self):
        ws = _make_ws()
        await self.broker.connect(ws, is_admin=False, agent_ids={"agent-1"})
        self.assertIn(ws, self.broker._connections)
        sub = self.broker._connections[ws]
        self.assertIsInstance(sub, EventSubscription)
        self.assertIs(sub.websocket, ws)
        self.assertFalse(sub.is_admin)
        self.assertEqual(sub.agent_ids, {"agent-1"})

    async def test_connect_calls_accept(self):
        ws = _make_ws()
        await self.broker.connect(ws, is_admin=False, agent_ids=set())
        ws.accept.assert_awaited_once()

    async def test_connect_stores_user_id(self):
        ws = _make_ws()
        await self.broker.connect(ws, is_admin=False, agent_ids=set(), user_id="user-7")
        sub = self.broker._connections[ws]
        self.assertEqual(sub.user_id, "user-7")

    async def test_connect_user_id_default_none(self):
        ws = _make_ws()
        await self.broker.connect(ws, is_admin=False, agent_ids=set())
        sub = self.broker._connections[ws]
        self.assertIsNone(sub.user_id)

    async def test_disconnect_removes_from_connections(self):
        ws = _make_ws()
        await self.broker.connect(ws, is_admin=False, agent_ids=set())
        self.broker.disconnect(ws)
        self.assertNotIn(ws, self.broker._connections)

    async def test_disconnect_nonexistent_does_nothing(self):
        """Disconnecting a websocket that was never connected must not raise."""
        ws = _make_ws()
        # Should not raise
        self.broker.disconnect(ws)
        self.assertNotIn(ws, self.broker._connections)

    async def test_disconnect_already_removed_does_nothing(self):
        ws = _make_ws()
        await self.broker.connect(ws, is_admin=False, agent_ids=set())
        self.broker.disconnect(ws)
        # Second disconnect should be safe
        self.broker.disconnect(ws)
        self.assertNotIn(ws, self.broker._connections)

    async def test_connect_multiple_websockets(self):
        ws1 = _make_ws()
        ws2 = _make_ws()
        await self.broker.connect(ws1, is_admin=True, agent_ids=set())
        await self.broker.connect(ws2, is_admin=False, agent_ids={"x"})
        self.assertEqual(len(self.broker._connections), 2)
        self.assertTrue(self.broker._connections[ws1].is_admin)
        self.assertFalse(self.broker._connections[ws2].is_admin)


# ===================================================================
# subscribe / unsubscribe
# ===================================================================

class TestSubscribeUnsubscribe(unittest.TestCase):
    """EventBroker.subscribe and EventBroker.unsubscribe (sync callbacks)."""

    def setUp(self):
        self.broker = EventBroker()

    def test_subscribe_adds_callback(self):
        cb = MagicMock()
        self.broker.subscribe(cb)
        self.assertIn(cb, self.broker._internal_subscribers)

    def test_subscribe_ignores_duplicates(self):
        cb = MagicMock()
        self.broker.subscribe(cb)
        self.broker.subscribe(cb)
        self.assertEqual(self.broker._internal_subscribers.count(cb), 1)

    def test_unsubscribe_removes_callback(self):
        cb = MagicMock()
        self.broker.subscribe(cb)
        self.broker.unsubscribe(cb)
        self.assertNotIn(cb, self.broker._internal_subscribers)

    def test_unsubscribe_non_subscriber_does_nothing(self):
        """Unsubscribing a callback that was never registered must not raise."""
        cb = MagicMock()
        # Should not raise
        self.broker.unsubscribe(cb)

    def test_subscribe_multiple_different_callbacks(self):
        cb1 = MagicMock()
        cb2 = MagicMock()
        self.broker.subscribe(cb1)
        self.broker.subscribe(cb2)
        self.assertEqual(len(self.broker._internal_subscribers), 2)


# ===================================================================
# publish – WebSocket delivery
# ===================================================================

class TestPublishWebSocketDelivery(unittest.IsolatedAsyncioTestCase):
    """EventBroker.publish routing to WebSocket connections."""

    async def asyncSetUp(self):
        self.broker = EventBroker()

    async def test_admin_receives_all_events(self):
        """Admin connections receive events regardless of agent_id."""
        admin_ws = _make_ws()
        await self.broker.connect(admin_ws, is_admin=True, agent_ids=set())

        event = {"type": "status", "agent_id": "agent-99"}
        await self.broker.publish(event)

        admin_ws.send_json.assert_awaited_once_with(event)

    async def test_admin_receives_events_without_agent_id(self):
        admin_ws = _make_ws()
        await self.broker.connect(admin_ws, is_admin=True, agent_ids=set())

        event = {"type": "system_notice"}
        await self.broker.publish(event)

        admin_ws.send_json.assert_awaited_once_with(event)

    async def test_non_admin_receives_matching_agent_id(self):
        """Non-admin receives events whose agent_id is in their subscription."""
        ws = _make_ws()
        await self.broker.connect(ws, is_admin=False, agent_ids={"agent-1"})

        event = {"type": "log", "agent_id": "agent-1"}
        await self.broker.publish(event)

        ws.send_json.assert_awaited_once_with(event)

    async def test_non_admin_does_not_receive_non_matching_agent_id(self):
        """Non-admin does NOT receive events for agent_ids they are not subscribed to."""
        ws = _make_ws()
        await self.broker.connect(ws, is_admin=False, agent_ids={"agent-1"})

        event = {"type": "log", "agent_id": "agent-2"}
        await self.broker.publish(event)

        ws.send_json.assert_not_awaited()

    async def test_event_without_agent_id_goes_to_all(self):
        """Events with no agent_id field are delivered to every connection."""
        ws1 = _make_ws()
        ws2 = _make_ws()
        await self.broker.connect(ws1, is_admin=False, agent_ids={"a"})
        await self.broker.connect(ws2, is_admin=False, agent_ids={"b"})

        event = {"type": "broadcast"}
        await self.broker.publish(event)

        ws1.send_json.assert_awaited_once_with(event)
        ws2.send_json.assert_awaited_once_with(event)

    async def test_stale_connection_gets_disconnected(self):
        """A connection whose send_json raises is removed from _connections."""
        stale_ws = _make_ws(send_side_effect=RuntimeError("disconnected"))
        await self.broker.connect(stale_ws, is_admin=True, agent_ids=set())

        event = {"type": "ping"}
        await self.broker.publish(event)

        self.assertNotIn(stale_ws, self.broker._connections)

    async def test_stale_connection_does_not_affect_healthy_ones(self):
        """A stale connection being removed doesn't prevent delivery to healthy ones."""
        stale_ws = _make_ws(send_side_effect=RuntimeError("gone"))
        healthy_ws = _make_ws()
        await self.broker.connect(stale_ws, is_admin=True, agent_ids=set())
        await self.broker.connect(healthy_ws, is_admin=True, agent_ids=set())

        event = {"type": "ping"}
        await self.broker.publish(event)

        self.assertNotIn(stale_ws, self.broker._connections)
        self.assertIn(healthy_ws, self.broker._connections)
        healthy_ws.send_json.assert_awaited_once_with(event)

    async def test_no_connections_no_error(self):
        """Publishing with zero connections must not raise."""
        await self.broker.publish({"type": "orphan"})

    async def test_admin_receives_multiple_events_for_different_agents(self):
        admin_ws = _make_ws()
        await self.broker.connect(admin_ws, is_admin=True, agent_ids=set())

        e1 = {"type": "x", "agent_id": "a1"}
        e2 = {"type": "x", "agent_id": "a2"}
        await self.broker.publish(e1)
        await self.broker.publish(e2)

        self.assertEqual(admin_ws.send_json.await_count, 2)
        admin_ws.send_json.assert_any_call(e1)
        admin_ws.send_json.assert_any_call(e2)


# ===================================================================
# publish – user_id filtering (cross-user privacy)
# ===================================================================

class TestPublishUserIdFiltering(unittest.IsolatedAsyncioTestCase):
    """EventBroker.publish filters events by created_by_user_id to prevent cross-user leaks."""

    async def asyncSetUp(self):
        self.broker = EventBroker()

    async def test_user_receives_own_events(self):
        """User receives events created by themselves."""
        ws = _make_ws()
        await self.broker.connect(ws, is_admin=False, agent_ids={"agent-1"}, user_id="user-A")

        event = {"type": "task.progress", "agent_id": "agent-1", "created_by_user_id": "user-A"}
        await self.broker.publish(event)

        ws.send_json.assert_awaited_once_with(event)

    async def test_user_does_not_receive_other_users_events(self):
        """User does NOT receive events created by a different user, even for the same agent."""
        ws = _make_ws()
        await self.broker.connect(ws, is_admin=False, agent_ids={"agent-1"}, user_id="user-A")

        event = {"type": "task.progress", "agent_id": "agent-1", "created_by_user_id": "user-B"}
        await self.broker.publish(event)

        ws.send_json.assert_not_awaited()

    async def test_two_users_same_agent_isolated(self):
        """Two users sharing the same agent only receive their own events."""
        ws_a = _make_ws()
        ws_b = _make_ws()
        await self.broker.connect(ws_a, is_admin=False, agent_ids={"agent-1"}, user_id="user-A")
        await self.broker.connect(ws_b, is_admin=False, agent_ids={"agent-1"}, user_id="user-B")

        event_a = {"type": "task.progress", "agent_id": "agent-1", "created_by_user_id": "user-A"}
        event_b = {"type": "task.progress", "agent_id": "agent-1", "created_by_user_id": "user-B"}

        await self.broker.publish(event_a)
        await self.broker.publish(event_b)

        ws_a.send_json.assert_awaited_once_with(event_a)
        ws_b.send_json.assert_awaited_once_with(event_b)

    async def test_admin_receives_all_users_events(self):
        """Admin receives events from all users."""
        admin_ws = _make_ws()
        await self.broker.connect(admin_ws, is_admin=True, agent_ids=set(), user_id="admin-1")

        event = {"type": "task.progress", "agent_id": "agent-1", "created_by_user_id": "user-B"}
        await self.broker.publish(event)

        admin_ws.send_json.assert_awaited_once_with(event)

    async def test_event_without_user_id_goes_to_all_matching_agent(self):
        """Events without created_by_user_id are delivered to all users with matching agent."""
        ws_a = _make_ws()
        ws_b = _make_ws()
        await self.broker.connect(ws_a, is_admin=False, agent_ids={"agent-1"}, user_id="user-A")
        await self.broker.connect(ws_b, is_admin=False, agent_ids={"agent-1"}, user_id="user-B")

        event = {"type": "agent.status_changed", "agent_id": "agent-1"}
        await self.broker.publish(event)

        ws_a.send_json.assert_awaited_once_with(event)
        ws_b.send_json.assert_awaited_once_with(event)

    async def test_subscription_without_user_id_receives_all(self):
        """Connections without user_id (legacy) receive all events for their agents."""
        ws = _make_ws()
        await self.broker.connect(ws, is_admin=False, agent_ids={"agent-1"})

        event = {"type": "task.progress", "agent_id": "agent-1", "created_by_user_id": "user-B"}
        await self.broker.publish(event)

        ws.send_json.assert_awaited_once_with(event)


# ===================================================================
# publish – internal subscribers
# ===================================================================

class TestPublishInternalSubscribers(unittest.IsolatedAsyncioTestCase):
    """EventBroker.publish notifies internal subscribers."""

    async def asyncSetUp(self):
        self.broker = EventBroker()

    async def test_internal_subscriber_receives_all_events(self):
        cb = AsyncMock()
        self.broker.subscribe(cb)

        event = {"type": "test", "agent_id": "z"}
        await self.broker.publish(event)

        cb.assert_awaited_once_with(event)

    async def test_multiple_subscribers_all_get_called(self):
        cb1 = AsyncMock()
        cb2 = AsyncMock()
        self.broker.subscribe(cb1)
        self.broker.subscribe(cb2)

        event = {"type": "multi"}
        await self.broker.publish(event)

        cb1.assert_awaited_once_with(event)
        cb2.assert_awaited_once_with(event)

    async def test_failing_subscriber_does_not_block_others(self):
        """If one subscriber raises, others still receive the event."""
        bad_cb = AsyncMock(side_effect=ValueError("boom"))
        good_cb = AsyncMock()
        self.broker.subscribe(bad_cb)
        self.broker.subscribe(good_cb)

        event = {"type": "resilient"}
        await self.broker.publish(event)

        bad_cb.assert_awaited_once_with(event)
        good_cb.assert_awaited_once_with(event)

    async def test_failing_subscriber_does_not_block_websocket_delivery(self):
        """A failing internal subscriber must not prevent WebSocket sends."""
        bad_cb = AsyncMock(side_effect=RuntimeError("subscriber died"))
        self.broker.subscribe(bad_cb)

        ws = _make_ws()
        await self.broker.connect(ws, is_admin=True, agent_ids=set())

        event = {"type": "still-delivered"}
        await self.broker.publish(event)

        ws.send_json.assert_awaited_once_with(event)

    async def test_internal_subscribers_called_before_websocket(self):
        """Internal subscribers are notified before WebSocket connections."""
        order = []
        cb = AsyncMock(side_effect=lambda e: order.append("subscriber"))
        self.broker.subscribe(cb)

        ws = _make_ws()
        ws.send_json.side_effect = lambda e: order.append("websocket")
        await self.broker.connect(ws, is_admin=True, agent_ids=set())

        await self.broker.publish({"type": "order-test"})

        self.assertEqual(order, ["subscriber", "websocket"])

    async def test_no_internal_subscribers_no_error(self):
        """Publishing with zero internal subscribers must not raise."""
        await self.broker.publish({"type": "no-subs"})


# ===================================================================
# publish_many
# ===================================================================

class TestPublishMany(unittest.IsolatedAsyncioTestCase):
    """EventBroker.publish_many iterates and publishes each event."""

    async def asyncSetUp(self):
        self.broker = EventBroker()

    async def test_publishes_each_event_in_order(self):
        ws = _make_ws()
        await self.broker.connect(ws, is_admin=True, agent_ids=set())

        events = [
            {"type": "a", "agent_id": "1"},
            {"type": "b", "agent_id": "2"},
            {"type": "c"},
        ]
        await self.broker.publish_many(events)

        self.assertEqual(ws.send_json.await_count, 3)
        calls = [c.args[0] for c in ws.send_json.await_args_list]
        self.assertEqual(calls, events)

    async def test_publish_many_with_internal_subscribers(self):
        cb = AsyncMock()
        self.broker.subscribe(cb)

        events = [{"type": "x"}, {"type": "y"}]
        await self.broker.publish_many(events)

        self.assertEqual(cb.await_count, 2)
        cb.assert_any_call(events[0])
        cb.assert_any_call(events[1])

    async def test_publish_many_empty_iterable(self):
        """publish_many with an empty iterable should complete without error."""
        await self.broker.publish_many([])

    async def test_publish_many_respects_filtering(self):
        admin_ws = _make_ws()
        user_ws = _make_ws()
        await self.broker.connect(admin_ws, is_admin=True, agent_ids=set())
        await self.broker.connect(user_ws, is_admin=False, agent_ids={"agent-1"})

        events = [
            {"type": "t1", "agent_id": "agent-1"},
            {"type": "t2", "agent_id": "agent-2"},
            {"type": "t3", "agent_id": "agent-1"},
        ]
        await self.broker.publish_many(events)

        # Admin sees all 3
        self.assertEqual(admin_ws.send_json.await_count, 3)
        # User only sees the 2 matching agent-1
        self.assertEqual(user_ws.send_json.await_count, 2)

    async def test_publish_many_respects_user_filtering(self):
        """publish_many respects user_id filtering across multiple events."""
        ws_a = _make_ws()
        ws_b = _make_ws()
        await self.broker.connect(ws_a, is_admin=False, agent_ids={"agent-1"}, user_id="user-A")
        await self.broker.connect(ws_b, is_admin=False, agent_ids={"agent-1"}, user_id="user-B")

        events = [
            {"type": "task.progress", "agent_id": "agent-1", "created_by_user_id": "user-A"},
            {"type": "task.progress", "agent_id": "agent-1", "created_by_user_id": "user-B"},
            {"type": "task.progress", "agent_id": "agent-1", "created_by_user_id": "user-A"},
        ]
        await self.broker.publish_many(events)

        self.assertEqual(ws_a.send_json.await_count, 2)
        self.assertEqual(ws_b.send_json.await_count, 1)


if __name__ == "__main__":
    unittest.main()
