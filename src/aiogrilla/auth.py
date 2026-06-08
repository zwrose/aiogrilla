# src/aiogrilla/auth.py
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
from dataclasses import dataclass, field

import boto3  # type: ignore[import-untyped]
from pycognito import Cognito  # type: ignore[import-untyped]

from . import const
from .exceptions import GrillaAuthError


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
            raise GrillaAuthError("login failed") from err
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
            raise GrillaAuthError("token refresh failed") from err
        self.id_token = self._cognito.id_token

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
            raise GrillaAuthError("identity-pool credential fetch failed") from err
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
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return str(json.loads(base64.urlsafe_b64decode(payload))["sub"])
        except (ValueError, KeyError, IndexError, TypeError):
            return None
