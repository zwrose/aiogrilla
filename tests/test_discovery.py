# tests/test_discovery.py
import aiohttp
import pytest

from aiogrilla.discovery import async_get_grills
from aiogrilla.exceptions import GrillaAuthError, GrillaConnectionError
from aiogrilla.models import Grill


class _FakeResp:
    """Minimal async-context-manager stand-in for aiohttp's response."""

    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def json(self, content_type: object = None) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeSession:
    """Stand-in for aiohttp.ClientSession that returns a canned response from .get()."""

    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp
        self.last_url: str | None = None
        self.last_kwargs: dict | None = None

    def get(self, url: str, **kwargs: object) -> _FakeResp:
        self.last_url = url
        self.last_kwargs = kwargs
        return self._resp


class _RaisingSession:
    """Stand-in for aiohttp.ClientSession whose .get() raises (connection drop/timeout)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get(self, url: str, **kwargs: object) -> _FakeResp:
        raise self._exc


async def test_discovery_ok():
    body = {
        "ident": "sub",
        "grills": [{"sn": "sx1", "model": "silverbacxl", "name": "Zamily"}],
        "state": [],
    }
    session = _FakeSession(_FakeResp(200, body))
    grills = await async_get_grills(session, id_token="ID", identity_id="ID1")
    assert grills == [Grill(id="sx1", name="Zamily", model="silverbacxl")]
    # sanity: the Bearer token and identity param were passed
    assert session.last_kwargs is not None
    assert session.last_kwargs["headers"]["Authorization"] == "Bearer ID"
    assert session.last_kwargs["params"] == {"identity": "ID1"}


async def test_discovery_401_is_auth_error():
    session = _FakeSession(_FakeResp(401, {}))
    with pytest.raises(GrillaAuthError):
        await async_get_grills(session, id_token="ID", identity_id="ID1")


async def test_discovery_502_is_connection_error():
    session = _FakeSession(_FakeResp(502, {}))
    with pytest.raises(GrillaConnectionError):
        await async_get_grills(session, id_token="ID", identity_id="ID1")


async def test_discovery_bad_shape_is_connection_error():
    session = _FakeSession(_FakeResp(200, {"unexpected": True}))
    with pytest.raises(GrillaConnectionError):
        await async_get_grills(session, id_token="ID", identity_id="ID1")


async def test_discovery_non_json_body_is_connection_error():
    # A 200 with a non-JSON body: json(content_type=None) raises JSONDecodeError (ValueError).
    session = _FakeSession(_FakeResp(200, ValueError("not json")))
    with pytest.raises(GrillaConnectionError):
        await async_get_grills(session, id_token="ID", identity_id="ID1")


async def test_discovery_client_error_is_connection_error():
    # A dropped connection/timeout surfaces as aiohttp.ClientError from session.get.
    session = _RaisingSession(aiohttp.ClientError("connection reset"))
    with pytest.raises(GrillaConnectionError):
        await async_get_grills(session, id_token="ID", identity_id="ID1")
