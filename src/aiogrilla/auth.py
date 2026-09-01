# src/aiogrilla/auth.py
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
from dataclasses import dataclass, field

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pycognito import Cognito  # type: ignore[import-untyped]

from . import const
from .exceptions import GrillaAuthError, GrillaConnectionError, GrillaError

# Cognito error codes that mean the account/tokens themselves were rejected. Only these
# justify sending the consumer back through reauth; anything else (network failures,
# timeouts, throttling, 5xx) is transient and must surface as GrillaConnectionError so
# callers retry instead of discarding a perfectly valid refresh token.
_AUTH_REJECTION_CODES = frozenset(
    {
        "NotAuthorizedException",
        "UserNotFoundException",
        "UserNotConfirmedException",
        "PasswordResetRequiredException",
    }
)

# Treat an id_token within this many seconds of its 'exp' claim as already stale, so a
# token is never handed to AWS right as it expires.
_TOKEN_EXPIRY_SKEW_S = 300


def _classified(err: Exception, message: str) -> GrillaError:
    """Map a raw pycognito/botocore exception to GrillaAuthError (credential rejection)
    or GrillaConnectionError (anything transient)."""
    code = None
    if isinstance(err, ClientError):
        code = err.response.get("Error", {}).get("Code")
    if code in _AUTH_REJECTION_CODES:
        return GrillaAuthError(f"{message}: {code}")
    return GrillaConnectionError(f"{message}: {type(err).__name__}")


def _jwt_payload(token: str) -> dict | None:
    """Decode a JWT payload WITHOUT signature verification (the token comes from our own
    TLS exchange with Cognito; used only for local claims like 'sub' and 'exp')."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


@dataclass(frozen=True, slots=True)
class Credentials:
    access_key: str = field(repr=False)  # IAM key id; suppressed for repr symmetry
    secret_key: str = field(repr=False)  # live IAM secret; keep out of repr/logs
    session_token: str = field(repr=False)  # live IAM secret; keep out of repr/logs
    expiration: dt.datetime


class GrillaAuth:
    """Owns Cognito tokens and Identity-Pool IAM credentials."""

    def __init__(self, refresh_token: str | None = None) -> None:
        self._refresh_token = refresh_token
        self._cognito: Cognito | None = None
        self.id_token: str | None = None
        self.identity_id: str | None = None

    async def async_login_with_password(self, email: str, password: str) -> str:
        """Interactive login (config/reauth only). Returns the Cognito refresh token."""

        def _login() -> Cognito:
            c = Cognito(const.USER_POOL_ID, const.USER_POOL_CLIENT_ID, username=email)
            c.authenticate(password=password)
            return c

        try:
            self._cognito = await asyncio.to_thread(_login)
        except Exception as err:  # pycognito raises botocore ClientError subclasses
            raise _classified(err, "login failed") from err
        refresh_token: str = self._cognito.refresh_token  # type: ignore[assignment]
        self.id_token = self._cognito.id_token
        self._refresh_token = refresh_token
        return refresh_token

    async def async_refresh(self) -> None:
        """Refresh the id/access tokens from the stored refresh token."""
        if not self._refresh_token:
            raise GrillaAuthError("no refresh token; reauth required")

        def _refresh() -> Cognito:
            c = Cognito(
                const.USER_POOL_ID,
                const.USER_POOL_CLIENT_ID,
                refresh_token=self._refresh_token,
            )
            c.renew_access_token()
            return c

        try:
            self._cognito = await asyncio.to_thread(_refresh)
        except Exception as err:  # pycognito/botocore raise varied types
            raise _classified(err, "token refresh failed") from err
        self.id_token = self._cognito.id_token

    @property
    def id_token_fresh(self) -> bool:
        """True when an id_token is present and not within skew of its 'exp' claim.

        Cognito id tokens live ~1 hour; callers that hold a client across that boundary
        (e.g. a connect-retry loop) must re-refresh before exchanging the token for IAM
        credentials, or the exchange fails in a way that looks like an auth rejection."""
        token = self.id_token
        if not token:
            return False
        payload = _jwt_payload(token)
        if payload is None or not isinstance(payload.get("exp"), int):
            return False  # undecodable/clamped token: treat as stale so callers refresh
        exp: int = payload["exp"]
        return dt.datetime.now(dt.UTC).timestamp() < exp - _TOKEN_EXPIRY_SKEW_S

    async def async_iam_credentials(self) -> Credentials:
        """Exchange the id token for temporary Identity-Pool IAM credentials."""
        if not self.id_token:
            raise GrillaAuthError("not authenticated")
        logins = {const.COGNITO_LOGIN_KEY: self.id_token}

        def _fetch() -> dict:
            ci = boto3.client("cognito-identity", region_name=const.REGION)
            ident = ci.get_id(IdentityPoolId=const.IDENTITY_POOL_ID, Logins=logins)["IdentityId"]
            creds = ci.get_credentials_for_identity(IdentityId=ident, Logins=logins)["Credentials"]
            return {"ident": ident, "creds": creds}

        try:
            result = await asyncio.to_thread(_fetch)
        except Exception as err:
            raise _classified(err, "identity-pool credential fetch failed") from err
        self.identity_id = result["ident"]
        c = result["creds"]
        # boto3 returns a tz-aware Expiration; normalize defensively so a (hypothetical) naive
        # value can't make the refresh loop's (expiration - now) raise and back off forever.
        expiration = c["Expiration"]
        if isinstance(expiration, dt.datetime) and expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=dt.UTC)
        return Credentials(c["AccessKeyId"], c["SecretKey"], c["SessionToken"], expiration)

    @property
    def account_sub(self) -> str | None:
        """The Cognito account 'sub' decoded from the current id_token (None if absent or
        undecodable). Identity only -- NO signature verification (the token comes from our own
        TLS login; 'sub' is used solely as a stable account identifier)."""
        token = self.id_token
        if not token:
            return None
        payload = _jwt_payload(token)
        if payload is None or "sub" not in payload:
            return None
        return str(payload["sub"])
