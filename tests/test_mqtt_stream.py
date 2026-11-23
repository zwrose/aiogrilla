# tests/test_mqtt_stream.py
"""Coverage for IotStream's connect/subscribe/availability/resubscribe state machine,
using an owned connection-factory seam so no live awscrt connection is required."""

import asyncio
import datetime as dt
import json
import time
from concurrent.futures import Future

import pytest
from awscrt import mqtt

from aiogrilla import const
from aiogrilla.auth import Credentials
from aiogrilla.exceptions import GrillaConnectionError
from aiogrilla.models import GrillState
from aiogrilla.mqtt import IotStream

CREDS = Credentials("AK", "SK", "ST", dt.datetime(2030, 1, 1, tzinfo=dt.UTC))


def _resolved(value=None):
    f: Future = Future()
    f.set_result(value)
    return f


def _failed(exc: Exception):
    f: Future = Future()
    f.set_exception(exc)
    return f


class FakeConnection:
    """Stand-in for awscrt mqtt.Connection whose futures resolve immediately."""

    def __init__(self, *, subscribe_future_factory=None):
        self.subscribed: list[str] = []
        self.callbacks: dict[str, object] = {}
        self.connect_called = False
        self.disconnect_called = False
        self.published: list[tuple[str, str, object]] = []
        self._subscribe_future_factory = subscribe_future_factory or (lambda: _resolved())
        self.publish_future_factory = lambda: _resolved()

    def connect(self):
        self.connect_called = True
        return _resolved()

    def subscribe(self, *, topic, qos, callback):
        self.subscribed.append(topic)
        self.callbacks[topic] = callback
        return self._subscribe_future_factory(), None

    def publish(self, *, topic, payload, qos):
        self.published.append((topic, payload, qos))
        return self.publish_future_factory(), 1

    def disconnect(self):
        self.disconnect_called = True
        return _resolved()


class FakeFactory:
    """Captures the interruption/resume callbacks and hands back a FakeConnection."""

    def __init__(self, conn: FakeConnection):
        self.conn = conn
        self.on_interrupted = None
        self.on_resumed = None
        self.call_count = 0

    def __call__(self, *, creds, client_id, on_interrupted, on_resumed):
        self.call_count += 1
        self.on_interrupted = on_interrupted
        self.on_resumed = on_resumed
        return self.conn


def _make_stream(conn=None, **kwargs):
    conn = conn or FakeConnection()
    factory = FakeFactory(conn)
    loop = asyncio.get_running_loop()
    stream = IotStream(loop, "client-1", connection_factory=factory, **kwargs)
    return stream, factory, conn


async def test_async_connect_subscribes_each_grill_and_marks_available():
    stream, factory, conn = _make_stream()
    avail_a: list[bool] = []
    avail_b: list[bool] = []
    stream.add_grill("sx1", on_state=lambda s: None, on_availability=avail_a.append)
    stream.add_grill("sx2", on_state=lambda s: None, on_availability=avail_b.append)

    await stream.async_connect(CREDS)

    assert factory.call_count == 1
    assert conn.connect_called
    assert conn.subscribed == [
        const.TOPIC_GRILL_STATE.format(device_id="sx1"),
        const.TOPIC_GRILL_STATE.format(device_id="sx2"),
    ]
    # Each dispatcher was marked available after its subscribe resolved.
    assert avail_a == [True]
    assert avail_b == [True]


async def test_on_interrupted_marks_all_dispatchers_unavailable():
    stream, factory, conn = _make_stream()
    avail: list[bool] = []
    stream.add_grill("sx1", on_state=lambda s: None, on_availability=avail.append)
    await stream.async_connect(CREDS)
    assert avail == [True]

    # Simulate the awscrt native-thread interruption callback.
    assert factory.on_interrupted is not None
    factory.on_interrupted(connection=conn, error="boom")
    await asyncio.sleep(0)  # let call_soon_threadsafe run set_available(False)

    assert avail == [True, False]


async def test_on_resumed_resubscribes_and_remarks_available():
    stream, factory, conn = _make_stream()
    avail: list[bool] = []
    stream.add_grill("sx1", on_state=lambda s: None, on_availability=avail.append)
    await stream.async_connect(CREDS)

    # Drop, then resume.
    factory.on_interrupted(connection=conn, error="boom")
    await asyncio.sleep(0)
    assert avail == [True, False]

    assert factory.on_resumed is not None
    factory.on_resumed(connection=conn, return_code=0, session_present=False)
    # _on_resumed schedules a coroutine via run_coroutine_threadsafe; give it cycles to run.
    for _ in range(5):
        await asyncio.sleep(0)

    # Re-subscribed the topic again and re-marked available.
    topic = const.TOPIC_GRILL_STATE.format(device_id="sx1")
    assert conn.subscribed.count(topic) == 2
    assert avail[-1] is True


async def test_subscribe_failure_leaves_dispatchers_unavailable_no_crash():
    failing = FakeConnection(subscribe_future_factory=lambda: _failed(RuntimeError("sub failed")))
    stream, factory, conn = _make_stream(failing)
    avail: list[bool] = []
    stream.add_grill("sx1", on_state=lambda s: None, on_availability=avail.append)

    # async_connect surfaces the subscribe failure (caller decides how to react).
    raised = False
    try:
        await stream.async_connect(CREDS)
    except RuntimeError:
        raised = True
    assert raised
    # Never marked available because the subscribe future failed before set_available(True).
    assert avail == []


async def test_update_grill_swaps_live_callback_on_real_dispatcher():
    """update_grill/set_callbacks must swap the on_state callback on the live dispatcher:
    after the swap, a delivered message reaches the NEW callback and not the old one.
    Exercises the real IotStream + StateDispatcher path (no MagicMock stream)."""
    stream, factory, conn = _make_stream()
    old_seen: list[GrillState] = []
    new_seen: list[GrillState] = []

    stream.add_grill("sx1", on_state=old_seen.append, on_availability=lambda a: None)
    await stream.async_connect(CREDS)

    # Swap to a NEW on_state callback on the live connection.
    stream.update_grill("sx1", on_state=new_seen.append)

    # Deliver a message via the real subscribe callback captured by FakeConnection.
    topic = const.TOPIC_GRILL_STATE.format(device_id="sx1")
    payload = json.dumps({"mode": "running", "current_cook_temp": 247}).encode()
    conn.callbacks[topic](topic=topic, payload=payload)
    # handle_message_threadsafe schedules _on_loop via call_soon_threadsafe.
    await asyncio.sleep(0)

    assert len(new_seen) == 1, "new callback must receive the parsed state"
    assert new_seen[0].mode.value == "running"
    assert old_seen == [], "old callback must not receive anything after the swap"


async def test_resubscribe_failure_is_logged_not_raised():
    # Connect successfully first (so self._conn is set and the initial subscribe runs),
    # then make the NEXT subscribe fail so _resubscribe genuinely hits the
    # subscribe-failure path rather than tripping an assert before reaching conn.subscribe.
    conn = FakeConnection()
    stream, factory, conn = _make_stream(conn)
    avail: list[bool] = []
    stream.add_grill("sx1", on_state=lambda s: None, on_availability=avail.append)

    await stream.async_connect(CREDS)
    assert avail == [True]  # initial subscribe succeeded and marked available

    # Now point the connection's subscribe at a failed future so re-subscribe fails.
    conn._subscribe_future_factory = lambda: _failed(RuntimeError("sub failed"))

    # _resubscribe swallows subscribe failures (stays unavailable, no crash).
    await stream._resubscribe()

    # The real subscribe error was swallowed: availability was NOT re-marked True
    # (no new True appended). If _resubscribe stops swallowing, this raises instead.
    assert avail == [True]


async def test_publish_sends_compact_json_at_least_once():
    stream, factory, conn = _make_stream()
    stream.add_grill("sx1", on_state=lambda s: None, on_availability=lambda a: None)
    await stream.async_connect(CREDS)
    assert stream._online

    await stream._async_publish("topic/client/sx1/heartbeat", {"timestamp": 5})

    assert conn.published == [
        ("topic/client/sx1/heartbeat", '{"timestamp":5}', mqtt.QoS.AT_LEAST_ONCE)
    ]


async def test_publish_before_connect_raises_connection_error():
    stream, factory, conn = _make_stream()
    with pytest.raises(GrillaConnectionError):
        await stream._async_publish("t", {})
    assert conn.published == []


async def test_publish_while_interrupted_raises_then_works_after_resume():
    stream, factory, conn = _make_stream()
    stream.add_grill("sx1", on_state=lambda s: None, on_availability=lambda a: None)
    await stream.async_connect(CREDS)

    factory.on_interrupted(connection=conn, error="boom")
    assert not stream._online
    with pytest.raises(GrillaConnectionError):
        await stream._async_publish("t", {})

    factory.on_resumed(connection=conn, return_code=0, session_present=False)
    for _ in range(5):
        await asyncio.sleep(0)
    assert stream._online
    await stream._async_publish("t", {})
    assert [p[0] for p in conn.published] == ["t"]


async def test_publish_unacknowledged_raises_connection_error():
    stream, factory, conn = _make_stream()
    await stream.async_connect(CREDS)
    conn.publish_future_factory = lambda: _failed(RuntimeError("nack"))
    with pytest.raises(GrillaConnectionError):
        await stream._async_publish("t", {})


async def test_no_heartbeat_by_default():
    stream, factory, conn = _make_stream()
    stream.add_grill("sx1", on_state=lambda s: None, on_availability=lambda a: None)
    await stream.async_connect(CREDS)
    await asyncio.sleep(0.02)
    assert conn.published == []
    await stream.async_disconnect()


async def test_heartbeat_published_for_each_grill_until_disconnect():
    stream, factory, conn = _make_stream(heartbeat_interval=0.01)
    stream.add_grill("sx1", on_state=lambda s: None, on_availability=lambda a: None)
    stream.add_grill("sx2", on_state=lambda s: None, on_availability=lambda a: None)
    before = int(time.time() * 1000)
    await stream.async_connect(CREDS)
    await asyncio.sleep(0.035)
    await stream.async_disconnect()
    after = int(time.time() * 1000)

    topics = [p[0] for p in conn.published]
    assert topics[:2] == [
        const.TOPIC_HEARTBEAT.format(device_id="sx1"),
        const.TOPIC_HEARTBEAT.format(device_id="sx2"),
    ]
    assert len(topics) >= 4  # the first beat is immediate, then one per interval
    for _topic, payload, qos in conn.published:
        assert qos == mqtt.QoS.AT_LEAST_ONCE
        body = json.loads(payload)
        assert set(body) == {"timestamp"}
        assert before <= body["timestamp"] <= after

    # Cancelled on disconnect: nothing more is published afterwards.
    count = len(conn.published)
    await asyncio.sleep(0.03)
    assert len(conn.published) == count


async def test_heartbeat_paused_while_interrupted():
    stream, factory, conn = _make_stream(heartbeat_interval=0.01)
    stream.add_grill("sx1", on_state=lambda s: None, on_availability=lambda a: None)
    await stream.async_connect(CREDS)
    await asyncio.sleep(0)

    factory.on_interrupted(connection=conn, error="boom")
    count = len(conn.published)
    await asyncio.sleep(0.035)
    assert len(conn.published) == count  # no beats while the link is down

    factory.on_resumed(connection=conn, return_code=0, session_present=False)
    await asyncio.sleep(0.035)
    assert len(conn.published) > count  # beats resume once re-subscribed
    await stream.async_disconnect()


async def test_heartbeat_publish_failure_is_swallowed():
    stream, factory, conn = _make_stream(heartbeat_interval=0.01)
    stream.add_grill("sx1", on_state=lambda s: None, on_availability=lambda a: None)
    conn.publish_future_factory = lambda: _failed(RuntimeError("nack"))
    await stream.async_connect(CREDS)
    await asyncio.sleep(0.03)
    assert len(conn.published) >= 2  # kept trying; the loop did not die
    await stream.async_disconnect()
