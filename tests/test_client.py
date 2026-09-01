# tests/test_client.py
import asyncio
import contextlib
import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from aiogrilla.auth import Credentials
from aiogrilla.client import (
    _BACKOFF_BASE_S,
    _BACKOFF_CAP_S,
    _REFRESH_FLOOR_S,
    GrillaClient,
)
from aiogrilla.exceptions import GrillaAuthError, GrillaError
from aiogrilla.models import CookMode, Grill, GrillState, Mode, TemperatureUnit


def _state():
    return GrillState(
        None,
        None,
        None,
        None,
        None,
        None,
        Mode.RUNNING,
        CookMode.PID,
        "none",
        TemperatureUnit.FAHRENHEIT,
        0,
        0,
        None,
        None,
        None,
        None,
        False,
        False,
        {},
    )


@pytest.fixture
def patched(monkeypatch):
    auth = MagicMock()
    auth.async_login_with_password = AsyncMock(return_value="RE")
    auth.async_refresh = AsyncMock()
    auth.async_iam_credentials = AsyncMock(
        return_value=Credentials("AK", "SK", "ST", dt.datetime(2030, 1, 1, tzinfo=dt.UTC))
    )
    auth.identity_id = "us-east-2:xyz"
    auth.id_token = "ID"
    auth.id_token_fresh = True
    monkeypatch.setattr("aiogrilla.client.GrillaAuth", lambda *a, **k: auth)
    monkeypatch.setattr(
        "aiogrilla.client.async_get_grills",
        AsyncMock(return_value=[Grill("sx1", "Zamily", "silverbacxl")]),
    )
    streams = []

    def make_stream(loop, client_id):
        s = MagicMock()
        s.client_id = client_id
        s.async_connect = AsyncMock()
        s.async_disconnect = AsyncMock()
        s.add_grill = MagicMock()
        s.update_grill = MagicMock()
        streams.append(s)
        return s

    monkeypatch.setattr("aiogrilla.client.IotStream", make_stream)
    return {"auth": auth, "streams": streams}


async def test_login_then_get_grills(patched):
    c = GrillaClient()
    assert await c.async_login_with_password("e@x.com", "pw") == "RE"
    grills = await c.async_get_grills()
    assert grills[0].id == "sx1"
    # Close the lazily-created session to avoid ResourceWarning.
    await c._get_session().close()


async def test_connect_subscribes_each_grill(patched):
    c = GrillaClient(refresh_token="RE")
    await c.async_get_grills()
    await c.async_connect()
    assert patched["streams"][0].add_grill.called
    patched["streams"][0].async_connect.assert_awaited_once()
    await c.async_disconnect()


async def test_client_id_is_exact_identity_uuid(patched):
    """The vendor IoT policy authorizes ONLY the UUID portion of the identity id as the
    MQTT client id — no region prefix, no library prefix, and client_suffix is ignored."""
    c = GrillaClient(refresh_token="RE", client_suffix="abcd")
    await c.async_get_grills()
    await c.async_connect()
    cid = patched["streams"][0].client_id
    assert cid == "xyz"  # identity "us-east-2:xyz" -> uuid part only
    await c.async_disconnect()


async def test_registered_state_callback_is_passed_to_add_grill(patched):
    c = GrillaClient(refresh_token="RE")
    await c.async_get_grills()
    seen = []
    c.on_state("sx1", lambda s: seen.append(s))
    await c.async_connect()
    # the cb registered for sx1 must be the on_state passed to add_grill
    _, kwargs = patched["streams"][0].add_grill.call_args
    kwargs["on_state"](_state())
    assert len(seen) == 1
    await c.async_disconnect()


async def test_disconnect_cancels_refresh_task_and_disconnects_stream(patched):
    """async_disconnect must cancel the refresh task and disconnect the active stream."""
    c = GrillaClient(refresh_token="RE")
    await c.async_get_grills()
    await c.async_connect()
    await c.async_disconnect()
    patched["streams"][0].async_disconnect.assert_awaited_once()


async def test_on_auth_failed_callback_registered(patched):
    """on_auth_failed stores the registered callable on the client."""
    c = GrillaClient(refresh_token="RE")

    def cb() -> None:
        pass

    c.on_auth_failed(cb)
    # Registration must actually store the callback (mutant dropping the assign dies here).
    assert c._auth_failed_cb is cb


async def test_availability_callback_is_passed_to_add_grill(patched):
    """on_availability callback is forwarded to add_grill."""
    c = GrillaClient(refresh_token="RE")
    await c.async_get_grills()
    avail_seen = []
    c.on_availability("sx1", lambda a: avail_seen.append(a))
    await c.async_connect()
    _, kwargs = patched["streams"][0].add_grill.call_args
    kwargs["on_availability"](True)
    assert avail_seen == [True]
    await c.async_disconnect()


async def test_reconnect_builds_fresh_stream_not_old_one(patched):
    """Credential renewal must build a NEW IotStream, never reuse the closed one."""
    c = GrillaClient(refresh_token="RE")
    await c.async_get_grills()
    await c.async_connect()

    # Force a reconnect by calling the private method directly.
    await c._async_reconnect()

    # Two streams must exist; the second is a fresh object.
    assert len(patched["streams"]) == 2
    s1, s2 = patched["streams"]
    # Old stream was disconnected.
    s1.async_disconnect.assert_awaited_once()
    # New stream was connected.
    s2.async_connect.assert_awaited_once()
    # New stream also had add_grill called (callbacks re-registered).
    assert s2.add_grill.called
    await c.async_disconnect()


async def test_reconnect_reregisters_callbacks(patched):
    """Stored callbacks must be re-applied on the new IotStream after reconnect."""
    c = GrillaClient(refresh_token="RE")
    await c.async_get_grills()
    seen = []
    c.on_state("sx1", lambda s: seen.append(s))
    await c.async_connect()

    await c._async_reconnect()

    # New stream's add_grill must have received the same on_state callback.
    _, kwargs = patched["streams"][1].add_grill.call_args
    kwargs["on_state"](_state())
    assert len(seen) == 1
    await c.async_disconnect()


# ---------------------------------------------------------------------------
# New tests added by review fixes
# ---------------------------------------------------------------------------


async def test_auth_failure_fires_callback_and_stops_loop(patched):
    """When credential renewal raises GrillaAuthError the on_auth_failed callback fires
    exactly once and the refresh loop coroutine returns (task finishes)."""
    c = GrillaClient(refresh_token="RE")
    await c.async_get_grills()

    auth_failed_calls: list[None] = []
    c.on_auth_failed(lambda: auth_failed_calls.append(None))

    await c.async_connect()

    # Patch asyncio.sleep to a no-op so the loop doesn't really wait,
    # then make _async_reconnect raise GrillaAuthError on the first call.
    with (
        patch("aiogrilla.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        patch.object(
            c, "_async_reconnect", new_callable=AsyncMock, side_effect=GrillaAuthError("bad token")
        ),
    ):
        # Allow the background task to run until it returns naturally.
        mock_sleep.return_value = None
        assert c._refresh_task is not None
        # The task should complete (not be cancelled) after the auth error.
        await asyncio.wait_for(c._refresh_task, timeout=2.0)

    assert auth_failed_calls == [None], "on_auth_failed must fire exactly once"
    # Task should have completed (not still pending).
    assert c._refresh_task.done()

    # Clean up stream without the now-finished refresh task causing issues.
    c._refresh_task = None
    await c.async_disconnect()


async def test_refresh_interval_math(patched):
    """sleep duration is 0.8 * remaining for long TTL, or the floor for near-expired TTL."""
    from aiogrilla import const

    # --- Case 1: long remaining lifetime (e.g. 3600 s) → sleep = 0.8 * 3600 = 2880 s ---
    long_expiry = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=3600)
    patched["auth"].async_iam_credentials = AsyncMock(
        return_value=Credentials("AK", "SK", "ST", long_expiry)
    )

    c1 = GrillaClient(refresh_token="RE")
    await c1.async_get_grills()

    sleep_args: list[float] = []

    async def capturing_sleep(duration: float) -> None:
        sleep_args.append(duration)
        # Raise CancelledError after capturing the first sleep to stop the loop cleanly.
        raise asyncio.CancelledError

    with patch("aiogrilla.client.asyncio.sleep", side_effect=capturing_sleep):
        await c1.async_connect()
        assert c1._refresh_task is not None
        # Wait for the task to finish (cancelled by our sleep side-effect).
        with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError)):
            await asyncio.wait_for(asyncio.shield(c1._refresh_task), timeout=2.0)

    assert len(sleep_args) >= 1
    expected_long = 3600 * const.CRED_REFRESH_RATIO
    assert abs(sleep_args[0] - expected_long) < 5.0, (
        f"Expected ~{expected_long}s sleep for long TTL, got {sleep_args[0]}s"
    )

    c1._refresh_task = None
    await c1.async_disconnect()

    # --- Case 2: nearly-expired (remaining < floor / 0.8) → sleep is floored ---
    # Remaining of 10 s → 0.8 * 10 = 8 s < 60 s floor → expect 60 s.
    near_expiry = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=10)
    patched["auth"].async_iam_credentials = AsyncMock(
        return_value=Credentials("AK", "SK", "ST", near_expiry)
    )

    c2 = GrillaClient(refresh_token="RE")
    await c2.async_get_grills()

    floor_sleep_args: list[float] = []

    async def capturing_sleep_floor(duration: float) -> None:
        floor_sleep_args.append(duration)
        raise asyncio.CancelledError

    with patch("aiogrilla.client.asyncio.sleep", side_effect=capturing_sleep_floor):
        await c2.async_connect()
        assert c2._refresh_task is not None
        with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError)):
            await asyncio.wait_for(asyncio.shield(c2._refresh_task), timeout=2.0)

    assert len(floor_sleep_args) >= 1
    assert floor_sleep_args[0] == _REFRESH_FLOOR_S, (
        f"Expected floor {_REFRESH_FLOOR_S}s for near-expired TTL, got {floor_sleep_args[0]}s"
    )

    c2._refresh_task = None
    await c2.async_disconnect()


async def test_on_state_after_connect_updates_live_dispatcher(patched):
    """Calling on_state AFTER async_connect must push the callback to the live stream
    via update_grill, so it takes effect without a reconnect."""
    c = GrillaClient(refresh_token="RE")
    await c.async_get_grills()
    await c.async_connect()

    stream = patched["streams"][0]
    stream.update_grill.reset_mock()

    seen: list[GrillState] = []

    def cb(s: GrillState) -> None:
        seen.append(s)

    c.on_state("sx1", cb)

    # update_grill must be called on the live stream with the new callback.
    stream.update_grill.assert_called_once_with("sx1", on_state=cb)

    await c.async_disconnect()


async def test_login_then_get_grills_does_not_call_async_refresh(patched):
    """After async_login_with_password sets id_token, async_get_grills must NOT
    call async_refresh (the token is already valid)."""
    auth = patched["auth"]
    # Simulate login having set id_token (already done by the fixture default).
    auth.id_token = "ID"
    auth.identity_id = "us-east-2:xyz"

    c = GrillaClient()
    await c.async_login_with_password("e@x.com", "pw")
    await c.async_get_grills()

    auth.async_refresh.assert_not_called()
    await c._get_session().close()


async def test_persisted_refresh_token_calls_async_refresh(patched):
    """A client built from a persisted refresh_token (id_token starts as None)
    must call async_refresh on the first async_get_grills call."""
    auth = patched["auth"]
    # Start with no id_token (persisted-refresh-token scenario).
    auth.id_token = None
    auth.id_token_fresh = False
    auth.identity_id = None

    # Simulate async_refresh setting id_token and async_iam_credentials setting identity_id.
    async def _fake_refresh() -> None:
        auth.id_token = "ID"
        auth.id_token_fresh = True

    async def _fake_iam() -> Credentials:
        auth.identity_id = "us-east-2:xyz"
        return Credentials("AK", "SK", "ST", dt.datetime(2030, 1, 1, tzinfo=dt.UTC))

    auth.async_refresh = AsyncMock(side_effect=_fake_refresh)
    auth.async_iam_credentials = AsyncMock(side_effect=_fake_iam)

    c = GrillaClient(refresh_token="RE")
    await c.async_get_grills()

    auth.async_refresh.assert_called_once()
    auth.async_iam_credentials.assert_called_once()
    await c._get_session().close()


async def test_id_token_property_delegates(patched):
    """GrillaClient.id_token delegates to the auth object's id_token."""
    # Before login: id_token reflects the auth mock's initial value (already "ID" in fixture).
    # Confirm the fixture baseline is "ID" (set statically on the mock).
    auth = patched["auth"]
    auth.id_token = None  # explicitly simulate pre-login state
    c = GrillaClient()
    assert c.id_token is None

    # After login: fixture's async_login_with_password leaves auth.id_token as "ID".
    auth.id_token = "ID"
    await c.async_login_with_password("e@x.com", "pw")
    assert c.id_token == "ID"


async def test_cred_refresh_transient_backoff_grows_and_resets(patched):
    """Consecutive transient _async_reconnect failures back off on a capped exponential
    schedule (5, 10, 20, ...) and reset to the normal expiry-based schedule on success."""
    c = GrillaClient(refresh_token="RE")
    await c.async_get_grills()
    await c.async_connect()
    # Stop the real refresh task started by async_connect; we drive the loop manually.
    assert c._refresh_task is not None
    c._refresh_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await c._refresh_task
    c._refresh_task = None

    sleeps: list[float] = []
    # Near-expired creds so each expiry-based sleep is floored to _REFRESH_FLOOR_S.
    near = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=10)
    initial = Credentials("AK", "SK", "ST", near)
    fresh = Credentials("AK", "SK", "ST", near)
    # First 3 reconnects fail transiently, the 4th succeeds, then stop the loop.
    results: list = [
        RuntimeError("transient 1"),
        RuntimeError("transient 2"),
        RuntimeError("transient 3"),
        fresh,
    ]

    async def fake_reconnect() -> Credentials:
        outcome = results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def capturing_sleep(duration: float) -> None:
        sleeps.append(duration)
        if not results:  # last outcome consumed; stop the loop after the next reconnect
            raise asyncio.CancelledError

    with (
        patch.object(c, "_async_reconnect", side_effect=fake_reconnect),
        patch("aiogrilla.client.asyncio.sleep", side_effect=capturing_sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            await c._cred_refresh_loop(initial)

    # sleeps[0] is the initial expiry-based sleep (floored), then the backoff schedule.
    assert sleeps[0] == _REFRESH_FLOOR_S
    assert sleeps[1] == _BACKOFF_BASE_S  # 5 after first transient failure
    assert sleeps[2] == _BACKOFF_BASE_S * 2  # 10 after second
    assert sleeps[3] == _BACKOFF_BASE_S * 4  # 20 after third
    # After the 4th (successful) reconnect, backoff resets to the normal expiry schedule.
    assert sleeps[4] == _REFRESH_FLOOR_S

    c._refresh_task = None
    await c.async_disconnect()


async def test_cred_refresh_backoff_caps(patched):
    """Backoff doubles but never exceeds _BACKOFF_CAP_S across many transient failures."""
    c = GrillaClient(refresh_token="RE")
    await c.async_get_grills()

    sleeps: list[float] = []

    async def always_fail() -> Credentials:
        raise RuntimeError("persistent transient")

    async def capturing_sleep(duration: float) -> None:
        sleeps.append(duration)
        if len(sleeps) >= 12:  # collect enough cycles to hit the cap
            raise asyncio.CancelledError

    creds = Credentials("AK", "SK", "ST", dt.datetime(2030, 1, 1, tzinfo=dt.UTC))
    with (
        patch.object(c, "_async_reconnect", side_effect=always_fail),
        patch("aiogrilla.client.asyncio.sleep", side_effect=capturing_sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            await c._cred_refresh_loop(creds)

    # Backoff values (sleeps[1:]) never exceed the cap and reach it.
    backoffs = sleeps[1:]
    assert all(b <= _BACKOFF_CAP_S for b in backoffs)
    assert max(backoffs) == _BACKOFF_CAP_S

    c._refresh_task = None
    await c.async_disconnect()


async def test_connect_failure_leaves_stream_none_and_disconnects_half_open(patched):
    """If the stream's async_connect raises, self._stream must be left None so a
    subsequent async_connect retries (instead of no-opping on the already-connected
    guard), and the half-open stream must be disconnected (no leak)."""
    c = GrillaClient(refresh_token="RE")
    await c.async_get_grills()

    # First connect: the stream's async_connect raises.
    def make_failing_stream(loop, client_id):
        s = MagicMock()
        s.client_id = client_id
        s.async_connect = AsyncMock(side_effect=RuntimeError("connect boom"))
        s.async_disconnect = AsyncMock()
        s.add_grill = MagicMock()
        s.update_grill = MagicMock()
        patched["streams"].append(s)
        return s

    with patch("aiogrilla.client.IotStream", make_failing_stream):
        with pytest.raises(RuntimeError):
            await c.async_connect()

    failed_stream = patched["streams"][-1]
    # self._stream must be left None (no wedge), and the half-open stream disconnected.
    assert c._stream is None
    failed_stream.async_disconnect.assert_awaited_once()
    # No refresh task should have been started on the failed path.
    assert c._refresh_task is None

    # A subsequent async_connect must retry (the fixture's healthy make_stream is back).
    await c.async_connect()
    assert c._stream is not None
    patched["streams"][-1].async_connect.assert_awaited_once()
    await c.async_disconnect()


async def test_connect_before_discovery_raises(patched):
    """async_connect must raise GrillaError when called before any grills are discovered."""
    c = GrillaClient(refresh_token="RE")
    with pytest.raises(GrillaError):
        await c.async_connect()
    # No stream or refresh task should have been created.
    assert c._stream is None
    assert c._refresh_task is None


async def test_session_ownership_disconnect(patched):
    """async_disconnect closes a client-created session but NOT a caller-supplied session."""
    # --- Case 1: client-owned session (lazy-created) ---
    c_owned = GrillaClient(refresh_token="RE")
    await c_owned.async_get_grills()  # triggers lazy session creation
    assert c_owned._session is not None
    owned_session = c_owned._session
    assert not owned_session.closed

    await c_owned.async_connect()
    await c_owned.async_disconnect()

    assert owned_session.closed, "Client-owned session must be closed on disconnect"

    # --- Case 2: caller-supplied session must NOT be closed ---
    supplied = aiohttp.ClientSession()
    try:
        c_supplied = GrillaClient(refresh_token="RE", session=supplied)
        await c_supplied.async_get_grills()
        await c_supplied.async_connect()
        await c_supplied.async_disconnect()
        assert not supplied.closed, "Caller-supplied session must not be closed by the client"
    finally:
        await supplied.close()


async def test_reconnect_disconnects_old_before_connecting_new(patched):
    """Break-before-make: old and new streams share the one policy-allowed client id, so
    the OLD stream must be fully disconnected BEFORE the new one connects (connecting
    first would trigger a broker duplicate-id kick and an auto-reconnect war with
    ourselves). Asserts ORDER, not just call counts."""
    events: list[str] = []

    def make_ordered_stream(loop, client_id):
        idx = len(patched["streams"])  # 0 = first/old, 1 = second/new

        async def _connect(*_a, _i=idx, **_k):
            events.append(f"connect:{_i}")

        async def _disconnect(*_a, _i=idx, **_k):
            events.append(f"disconnect:{_i}")

        s = MagicMock()
        s.client_id = client_id
        s.add_grill = MagicMock()
        s.update_grill = MagicMock()
        s.async_connect = AsyncMock(side_effect=_connect)
        s.async_disconnect = AsyncMock(side_effect=_disconnect)
        patched["streams"].append(s)
        return s

    with patch("aiogrilla.client.IotStream", make_ordered_stream):
        c = GrillaClient(refresh_token="RE")
        await c.async_get_grills()
        await c.async_connect()  # stream 0 connects
        await c._async_reconnect()  # stream 0 disconnects, THEN stream 1 connects
        # Old (0) disconnected before new (1) connected — the break-before-make invariant.
        assert events == ["connect:0", "disconnect:0", "connect:1"]
        await c.async_disconnect()


async def test_connect_refreshes_stale_id_token(patched):
    """async_connect must re-refresh a stale id_token before exchanging it for IAM
    credentials — the connect-retry loop routinely outlives the ~1h token, and a stale
    token used to surface as a bogus auth failure (forcing pointless reauth)."""
    auth = patched["auth"]
    c = GrillaClient(refresh_token="RE")
    await c.async_get_grills()
    auth.async_refresh.reset_mock()

    auth.id_token_fresh = False  # token went stale while connect was being retried
    await c.async_connect()

    auth.async_refresh.assert_awaited_once()
    await c.async_disconnect()


async def test_connect_skips_refresh_when_token_fresh(patched):
    auth = patched["auth"]
    c = GrillaClient(refresh_token="RE")
    await c.async_get_grills()
    auth.async_refresh.reset_mock()

    await c.async_connect()  # id_token_fresh is True in the fixture

    auth.async_refresh.assert_not_called()
    await c.async_disconnect()


async def test_account_sub_delegates_to_auth(patched):
    """GrillaClient.account_sub delegates to the auth object's account_sub."""
    patched["auth"].account_sub = "sub-123"
    c = GrillaClient()
    assert c.account_sub == "sub-123"


async def test_async_reconnect_real_stream_uses_fresh_creds(monkeypatch):
    """Drive _async_reconnect through the REAL IotStream + a fake awscrt connection so the
    refresh -> iam -> build -> connect -> swap wiring runs end-to-end (the path a live bug once
    hid in), and confirm the NEW connection is built with the freshly-fetched creds."""
    from concurrent.futures import Future

    from aiogrilla.mqtt import IotStream as RealIotStream

    def _resolved(value=None):
        f: Future = Future()
        f.set_result(value)
        return f

    creds_seen: list[Credentials] = []

    class _FakeConn:
        def connect(self):
            return _resolved()

        def subscribe(self, *, topic, qos, callback):
            return _resolved(), None

        def disconnect(self):
            return _resolved()

    def fake_factory(*, creds, client_id, on_interrupted, on_resumed):
        creds_seen.append(creds)
        return _FakeConn()

    def real_stream_factory(loop, client_id):
        return RealIotStream(loop, client_id, connection_factory=fake_factory)

    auth = MagicMock()
    auth.async_refresh = AsyncMock()
    initial = Credentials("AK0", "SK0", "ST0", dt.datetime(2030, 1, 1, tzinfo=dt.UTC))
    refreshed = Credentials("AK1", "SK1", "ST1", dt.datetime(2031, 1, 1, tzinfo=dt.UTC))
    auth.async_iam_credentials = AsyncMock(side_effect=[initial, refreshed])
    auth.identity_id = "us-east-2:xyz"
    auth.id_token = "ID"
    auth.id_token_fresh = True
    monkeypatch.setattr("aiogrilla.client.GrillaAuth", lambda *a, **k: auth)
    monkeypatch.setattr(
        "aiogrilla.client.async_get_grills",
        AsyncMock(return_value=[Grill("sx1", "Zamily", "silverbacxl")]),
    )
    monkeypatch.setattr("aiogrilla.client.IotStream", real_stream_factory)

    c = GrillaClient(refresh_token="RE")
    await c.async_get_grills()
    await c.async_connect()  # builds the REAL stream 1 with `initial` creds
    await c._async_reconnect()  # refresh -> builds the REAL stream 2 with `refreshed` creds

    auth.async_refresh.assert_awaited()  # the real reconnect path refreshed tokens
    assert creds_seen[0] is initial  # first connection used the initial creds
    assert creds_seen[-1] is refreshed  # the NEW connection used the FRESH creds
    await c.async_disconnect()
