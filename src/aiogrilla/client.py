# src/aiogrilla/client.py
"""GrillaClient: orchestration layer — auth, discovery, IoT connection, cred refresh."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import Callable

import aiohttp

from . import const
from .auth import Credentials, GrillaAuth
from .discovery import async_get_grills
from .exceptions import GrillaAuthError, GrillaError
from .models import Grill, GrillState
from .mqtt import AvailCb, IotStream, StateCb

_LOGGER = logging.getLogger(__name__)

VoidCb = Callable[[], None]

# Floor on the calculated sleep duration so the refresh loop never spin-waits.
_REFRESH_FLOOR_S = 60.0

# Capped exponential backoff for consecutive transient refresh failures.
_BACKOFF_BASE_S = 5.0
_BACKOFF_CAP_S = 300.0


def _noop_state(_state: GrillState) -> None:
    pass


def _noop_avail(_avail: bool) -> None:
    pass


class GrillaClient:
    """Single account connection to the Grilla cloud & IoT bus.

    Usage::

        client = GrillaClient(refresh_token="…")
        grills = await client.async_get_grills()
        client.on_state(grills[0].id, my_state_cb)
        await client.async_connect()
        …
        await client.async_disconnect()
    """

    def __init__(
        self,
        refresh_token: str | None = None,
        *,
        client_suffix: str = "0",
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._auth = GrillaAuth(refresh_token=refresh_token)
        self._client_suffix = client_suffix

        # aiohttp session — if we created it we own it and will close it.
        # A caller-supplied session is used as-is and never closed by us.
        self._owns_session: bool = session is None
        self._session: aiohttp.ClientSession | None = session  # None → lazy-created on first use

        # Discovered grills (populated by async_get_grills).
        self._grills: list[Grill] = []

        # Registered callbacks, keyed by grill_id.
        self._state_cbs: dict[str, StateCb] = {}
        self._avail_cbs: dict[str, AvailCb] = {}

        # Auth-failure callback (optional).
        self._auth_failed_cb: VoidCb | None = None

        # Active IoT stream; None when disconnected.
        self._stream: IotStream | None = None

        # Background credential-refresh task.
        self._refresh_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def id_token(self) -> str | None:
        """The current Cognito ID token (None until login/refresh); retained for diagnostics.

        Prefer `account_sub` for the account identity rather than decoding this token."""
        return self._auth.id_token

    @property
    def account_sub(self) -> str | None:
        """The account 'sub' (stable Cognito identity) derived from the current id_token, or
        None. Suitable as a consumer's unique id; no signature verification (identity only)."""
        return self._auth.account_sub

    async def async_login_with_password(self, email: str, password: str) -> str:
        """Interactive login. Returns the Cognito refresh token for storage."""
        return await self._auth.async_login_with_password(email, password)

    async def async_get_grills(self) -> list[Grill]:
        """Ensure valid tokens and return the list of grills for this account."""
        if self._auth.id_token is None:
            await self._auth.async_refresh()
        if self._auth.identity_id is None:
            await self._auth.async_iam_credentials()
        self._grills = await async_get_grills(
            self._get_session(),
            id_token=self._auth.id_token,  # type: ignore[arg-type]
            identity_id=self._auth.identity_id,  # type: ignore[arg-type]
        )
        return self._grills

    def on_state(self, grill_id: str, cb: StateCb) -> None:
        """Register a state-update callback for the given grill.

        Takes effect immediately on the live connection (if already connected) and
        survives reconnects.
        """
        self._state_cbs[grill_id] = cb
        if self._stream is not None:
            self._stream.update_grill(grill_id, on_state=cb)

    def on_availability(self, grill_id: str, cb: AvailCb) -> None:
        """Register an availability callback for the given grill.

        Takes effect immediately on the live connection (if already connected) and
        survives reconnects.
        """
        self._avail_cbs[grill_id] = cb
        if self._stream is not None:
            self._stream.update_grill(grill_id, on_availability=cb)

    def on_auth_failed(self, cb: VoidCb) -> None:
        """Register a callback fired when credential renewal fails with GrillaAuthError."""
        self._auth_failed_cb = cb

    async def async_connect(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Obtain IAM credentials and open the IoT connection for all discovered grills.

        Starts the background credential-refresh loop.
        """
        if self._stream is not None:
            _LOGGER.debug("async_connect called while already connected; ignoring")
            return
        if not self._grills:
            raise GrillaError(
                "no grills discovered; call async_get_grills() before async_connect()"
            )
        creds = await self._auth.async_iam_credentials()
        _loop = loop or asyncio.get_running_loop()

        # Build into a LOCAL stream and connect it before assigning self._stream, so a
        # failed connect leaves self._stream None (a later async_connect can retry) and
        # never leaks a half-open stream (mirrors _async_reconnect's make-before-break).
        stream = self._build_stream(_loop)
        try:
            await stream.async_connect(creds)
        except Exception:
            with contextlib.suppress(Exception):
                await stream.async_disconnect()
            raise
        self._stream = stream

        # Start the background refresh loop.
        self._refresh_task = asyncio.ensure_future(self._cred_refresh_loop(creds))

    async def async_disconnect(self) -> None:
        """Cancel the refresh task, disconnect the IoT stream, close the owned session."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None

        try:
            if self._stream is not None:
                await self._stream.async_disconnect()
                self._stream = None
        finally:
            # Close the owned session regardless of how the stream disconnect went,
            # so a raising stream-disconnect never leaks a client-owned session.
            if self._owns_session and self._session is not None:
                await self._session.close()
                self._session = None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_session(self) -> aiohttp.ClientSession:
        """Return the shared aiohttp session, creating it lazily on first call."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    def _build_stream(self, loop: asyncio.AbstractEventLoop) -> IotStream:
        """Create a fresh IotStream and register all stored callbacks on it."""
        client_id = f"aiogrilla-{self._auth.identity_id}-{self._client_suffix}"
        stream = IotStream(loop, client_id)
        for grill in self._grills:
            state_cb = self._state_cbs.get(grill.id, _noop_state)
            avail_cb = self._avail_cbs.get(grill.id, _noop_avail)
            stream.add_grill(grill.id, on_state=state_cb, on_availability=avail_cb)
        return stream

    async def _async_reconnect(self) -> Credentials:
        """Build a BRAND-NEW IotStream (never reuse a closed one), connect it, then
        disconnect the old stream (make-before-break where possible).

        Returns the fresh Credentials so callers can track the new expiration.
        This is the reconnect entry point used by both the refresh loop and tests.
        """
        loop = asyncio.get_running_loop()

        # Renew Cognito tokens and then fetch fresh IAM credentials.
        await self._auth.async_refresh()
        new_creds: Credentials = await self._auth.async_iam_credentials()

        # Build and connect the new stream BEFORE touching the old one (make-before-break).
        new_stream = self._build_stream(loop)
        try:
            await new_stream.async_connect(new_creds)
        except Exception:
            # Connect (or its in-flight subscribe) failed before the swap; don't leak
            # the half-open connection. Old stream stays in place for the caller to retry.
            with contextlib.suppress(Exception):
                await new_stream.async_disconnect()
            raise

        # Swap streams atomically from the perspective of the event loop.
        old_stream = self._stream
        self._stream = new_stream

        # Disconnect the old stream after the new one is up.
        if old_stream is not None:
            await old_stream.async_disconnect()

        return new_creds

    async def _cred_refresh_loop(
        self,
        initial_creds: Credentials,
    ) -> None:
        """Background task that proactively renews credentials before they expire."""
        creds = initial_creds
        # Backoff after consecutive transient failures; 0 means the schedule is healthy.
        backoff = 0.0
        while True:
            if backoff > 0.0:
                # A prior reconnect failed transiently; retry on a capped exponential
                # schedule instead of hammering the floor every cycle.
                _LOGGER.debug("cred-refresh: backing off %.0fs after transient failure", backoff)
                await asyncio.sleep(backoff)
            else:
                # Calculate how long to sleep before renewal.
                now = dt.datetime.now(dt.UTC)
                remaining = (creds.expiration - now).total_seconds()
                # Sleep for CRED_REFRESH_RATIO of the remaining lifetime, floored to avoid spin.
                sleep_for = max(_REFRESH_FLOOR_S, remaining * const.CRED_REFRESH_RATIO)
                _LOGGER.debug(
                    "cred-refresh: sleeping %.0fs (remaining=%.0fs)", sleep_for, remaining
                )
                await asyncio.sleep(sleep_for)

            try:
                # _async_reconnect fetches fresh creds internally and returns them.
                creds = await self._async_reconnect()
                backoff = 0.0  # success: return to the normal expiry-based schedule
            except GrillaAuthError as exc:
                _LOGGER.error("cred-refresh: auth failed, stopping refresh loop: %s", exc)
                if self._auth_failed_cb is not None:
                    self._auth_failed_cb()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # transient errors; back off and try again next cycle
                backoff = _BACKOFF_BASE_S if backoff == 0.0 else min(backoff * 2, _BACKOFF_CAP_S)
                _LOGGER.warning(
                    "cred-refresh: transient error, retrying in %.0fs: %s", backoff, exc
                )
