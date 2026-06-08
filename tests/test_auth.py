# tests/test_auth.py
import base64
import datetime as dt
import json
from unittest.mock import patch

import pytest

from aiogrilla.auth import Credentials, GrillaAuth
from aiogrilla.exceptions import GrillaAuthError


@pytest.fixture
def fake_cognito():
    with patch("aiogrilla.auth.Cognito") as C:
        inst = C.return_value
        inst.id_token = "ID"
        inst.access_token = "AC"
        inst.refresh_token = "RE"
        yield inst


@pytest.fixture
def fake_boto():
    with patch("aiogrilla.auth.boto3") as b:
        client = b.client.return_value
        client.get_id.return_value = {"IdentityId": "us-east-2:abc"}
        client.get_credentials_for_identity.return_value = {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretKey": "SK",
                "SessionToken": "ST",
                "Expiration": dt.datetime(2030, 1, 1, tzinfo=dt.UTC),
            }
        }
        yield client


async def test_login_returns_refresh_token(fake_cognito, fake_boto):
    auth = GrillaAuth()
    refresh = await auth.async_login_with_password("e@x.com", "pw")
    assert refresh == "RE"
    fake_cognito.authenticate.assert_called_once_with(password="pw")


async def test_iam_credentials_and_identity(fake_cognito, fake_boto):
    auth = GrillaAuth(refresh_token="RE")
    await auth.async_refresh()
    # Must use renew_access_token (REFRESH_TOKEN_AUTH flow), never check_token.
    fake_cognito.renew_access_token.assert_called_once()
    fake_cognito.check_token.assert_not_called()
    creds = await auth.async_iam_credentials()
    assert isinstance(creds, Credentials)
    assert creds.access_key == "AK" and auth.identity_id == "us-east-2:abc"


async def test_login_failure_raises_auth_error(fake_boto):
    with patch("aiogrilla.auth.Cognito") as C:
        C.return_value.authenticate.side_effect = Exception("bad password")
        with pytest.raises(GrillaAuthError):
            await GrillaAuth().async_login_with_password("e@x.com", "wrong")


async def test_refresh_without_token_raises_auth_error():
    # No stored refresh token -> guard fires before any network/thread work.
    with pytest.raises(GrillaAuthError):
        await GrillaAuth().async_refresh()


async def test_refresh_failure_raises_auth_error():
    with patch("aiogrilla.auth.Cognito") as C:
        C.return_value.renew_access_token.side_effect = Exception("expired refresh")
        with pytest.raises(GrillaAuthError):
            await GrillaAuth(refresh_token="RE").async_refresh()


async def test_iam_credentials_without_id_token_raises_auth_error():
    # async_iam_credentials before any login/refresh -> not authenticated guard.
    with pytest.raises(GrillaAuthError):
        await GrillaAuth().async_iam_credentials()


async def test_iam_credentials_fetch_failure_raises_auth_error(fake_cognito, fake_boto):
    auth = GrillaAuth(refresh_token="RE")
    await auth.async_refresh()
    fake_boto.get_id.side_effect = Exception("boto failure")
    with pytest.raises(GrillaAuthError):
        await auth.async_iam_credentials()


def _jwt(sub: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=").decode()
    return f"h.{payload}.s"


def test_account_sub_decodes_and_handles_bad_tokens():
    auth = GrillaAuth()
    assert auth.account_sub is None  # no token yet
    auth.id_token = _jwt("sub-xyz")
    assert auth.account_sub == "sub-xyz"
    auth.id_token = "not-a-jwt"  # undecodable -> None, not a crash
    assert auth.account_sub is None


async def test_iam_credentials_normalizes_naive_expiration(fake_cognito, fake_boto):
    # A (hypothetical) naive Expiration from boto is coerced to UTC-aware so the refresh
    # loop's (expiration - utcnow) subtraction can't raise and back off forever.
    fake_boto.get_credentials_for_identity.return_value = {
        "Credentials": {
            "AccessKeyId": "AK",
            "SecretKey": "SK",
            "SessionToken": "ST",
            "Expiration": dt.datetime(2030, 1, 1),  # NAIVE (no tzinfo)
        }
    }
    auth = GrillaAuth(refresh_token="RE")
    await auth.async_refresh()
    creds = await auth.async_iam_credentials()
    assert creds.expiration.tzinfo is not None  # normalized to tz-aware
