# tests/test_mqtt_bridge.py
import asyncio
import concurrent.futures
import datetime as dt
import pathlib
import threading

from aiogrilla.auth import Credentials
from aiogrilla.models import Mode
from aiogrilla.mqtt import IotStream, StateDispatcher

FIX = pathlib.Path(__file__).parent / "fixtures"


async def test_dispatch_parses_and_calls_back_on_loop():
    got = []
    d = StateDispatcher(
        "sx1",
        on_state=lambda s: got.append(s),
        on_availability=lambda a: None,
        loop=asyncio.get_running_loop(),
    )
    payload = (FIX / "grill_state_running.json").read_bytes()
    d.handle_message_threadsafe(payload)  # simulates awscrt native-thread callback
    await asyncio.sleep(0.01)  # let call_soon_threadsafe run
    assert len(got) == 1 and got[0].mode is Mode.RUNNING


async def test_malformed_payload_does_not_raise_or_dispatch():
    got = []
    d = StateDispatcher(
        "sx1",
        on_state=lambda s: got.append(s),
        on_availability=lambda a: None,
        loop=asyncio.get_running_loop(),
    )
    d.handle_message_threadsafe(b"not json{")  # must not raise
    await asyncio.sleep(0.01)
    assert got == []  # bad payload dropped


async def test_unsub_stops_further_dispatch():
    got = []
    d = StateDispatcher(
        "sx1",
        on_state=lambda s: got.append(s),
        on_availability=lambda a: None,
        loop=asyncio.get_running_loop(),
    )
    d.close()
    d.handle_message_threadsafe((FIX / "grill_state_running.json").read_bytes())
    await asyncio.sleep(0.01)
    assert got == []  # no dispatch after close()


async def test_watchdog_marks_unavailable_after_staleness_then_recovers():
    # Silence while connected => UNAVAILABLE (the grill likely powered off). The dispatcher
    # must NOT synthesize a mode; a later message recovers availability.
    states, avail = [], []
    d = StateDispatcher(
        "sx1",
        on_state=states.append,
        on_availability=avail.append,
        loop=asyncio.get_running_loop(),
    )
    payload = (FIX / "grill_state_running.json").read_bytes()
    d.handle_message_threadsafe(payload)
    await asyncio.sleep(0.01)
    assert states[-1].mode is Mode.RUNNING
    assert avail[-1] is True  # fresh telemetry -> available
    n_states = len(states)

    d._on_silent()  # fire the staleness timer callback as the event loop would
    assert avail[-1] is False  # silence -> unavailable
    assert len(states) == n_states  # no synthetic state emitted

    d.handle_message_threadsafe(payload)  # a fresh message recovers availability
    await asyncio.sleep(0.01)
    assert avail[-1] is True and states[-1].mode is Mode.RUNNING


async def test_watchdog_does_not_infer_off_while_messages_keep_arriving():
    # Re-arm path, driven deterministically (no wall-clock wait against the staleness
    # window). Each handled message arms a TimerHandle; the next message must cancel the
    # prior handle and install a fresh one, proving re-arm without any real sleep.
    states: list = []
    d = StateDispatcher(
        "sx1",
        on_state=states.append,
        on_availability=lambda a: None,
        loop=asyncio.get_running_loop(),
        # Large staleness so the real call_later timer cannot fire within the test;
        # the re-arm proof below is cancel-based and does not depend on it firing.
        staleness=1000,
    )
    payload = (FIX / "grill_state_running.json").read_bytes()

    d.handle_message_threadsafe(payload)
    await asyncio.sleep(0)  # let call_soon_threadsafe deliver the first message
    first_handle = d._watchdog
    assert first_handle is not None
    assert not first_handle.cancelled()

    d.handle_message_threadsafe(payload)
    await asyncio.sleep(0)  # deliver the second message, which re-arms the watchdog
    second_handle = d._watchdog
    # The first watchdog was cancelled and a brand-new handle installed (re-armed).
    assert first_handle.cancelled()
    assert second_handle is not None and second_handle is not first_handle
    # No OFF was synthesized while messages kept arriving.
    assert all(s.mode is Mode.RUNNING for s in states)
    assert not any(s.mode is Mode.OFF for s in states)


def test_handle_message_after_loop_closed_does_not_raise():
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()
    d = StateDispatcher(
        "sx1", on_state=lambda s: None, on_availability=lambda a: None, loop=closed_loop
    )
    # Must not raise even though the loop is closed (simulates teardown race).
    d.handle_message_threadsafe(b'{"mode":"running"}')


def _done_future(result: object = None) -> concurrent.futures.Future:
    fut: concurrent.futures.Future = concurrent.futures.Future()
    fut.set_result(result)
    return fut


class _FakeConn:
    """Minimal stand-in for an awscrt mqtt.Connection (futures already resolved)."""

    def connect(self) -> concurrent.futures.Future:
        return _done_future()

    def subscribe(self, topic: str, qos: object, callback: object):  # noqa: ANN201
        return _done_future(), None

    def disconnect(self) -> concurrent.futures.Future:
        return _done_future()


async def test_connection_built_off_the_event_loop():
    # awscrt's builder does blocking I/O; async_connect must build the connection in an
    # executor thread, not on the event loop. Capture the thread the factory runs on.
    loop_thread = threading.get_ident()
    factory_thread: dict[str, int] = {}

    def factory(**_kwargs: object) -> _FakeConn:
        factory_thread["id"] = threading.get_ident()
        return _FakeConn()

    stream = IotStream(asyncio.get_running_loop(), "client-1", connection_factory=factory)
    stream.add_grill("sx1", on_state=lambda s: None, on_availability=lambda a: None)
    creds = Credentials("ak", "sk", "tok", dt.datetime(2030, 1, 1, tzinfo=dt.UTC))

    await stream.async_connect(creds)

    assert factory_thread["id"] != loop_thread  # built off the loop, in an executor thread


async def test_real_watchdog_timer_marks_unavailable_after_staleness():
    # Drive the REAL loop.call_later -> _on_silent timer (not the private method) with a tiny
    # staleness, proving the watchdog is actually armed with the right callback and delay.
    avail: list[bool] = []
    d = StateDispatcher(
        "sx1",
        on_state=lambda s: None,
        on_availability=avail.append,
        loop=asyncio.get_running_loop(),
        staleness=0.02,
    )
    d.handle_message_threadsafe((FIX / "grill_state_running.json").read_bytes())
    await asyncio.sleep(0)  # deliver the message, arm the real watchdog
    assert avail[-1] is True
    await asyncio.sleep(0.05)  # let the REAL call_later fire _on_silent
    assert avail[-1] is False  # silence -> unavailable, via the real timer


async def test_repeated_fresh_messages_do_not_refire_available():
    # set_available is idempotent: a second fresh message while already available must NOT
    # emit another True (consumers may treat each availability callback as an event).
    avail: list[bool] = []
    d = StateDispatcher(
        "sx1",
        on_state=lambda s: None,
        on_availability=avail.append,
        loop=asyncio.get_running_loop(),
    )
    payload = (FIX / "grill_state_running.json").read_bytes()
    d.handle_message_threadsafe(payload)
    await asyncio.sleep(0)
    d.handle_message_threadsafe(payload)
    await asyncio.sleep(0)
    assert avail == [True]  # exactly one True, not [True, True]
