# src/aiogrilla/discovery.py
from __future__ import annotations

import aiohttp

from . import const
from .exceptions import GrillaAuthError, GrillaConnectionError
from .models import Grill


async def async_get_grills(
    session: aiohttp.ClientSession, *, id_token: str, identity_id: str
) -> list[Grill]:
    """GET the owner's grills. 401/403 -> auth error; 5xx/bad-shape -> connection error."""
    headers = {"Authorization": f"Bearer {id_token}"}
    params = {"identity": identity_id}
    try:
        async with session.get(
            f"{const.API_GRILLS_BY_OWNER}/",
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status in (401, 403):
                raise GrillaAuthError(f"discovery unauthorized ({resp.status})")
            if resp.status >= 400:
                raise GrillaConnectionError(
                    f"grill-discovery returned {resp.status}; the cloud API may have changed"
                )
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, ValueError) as err:
        # ValueError covers json.JSONDecodeError from a non-JSON body (content_type=None
        # bypasses aiohttp's content-type check and calls json.loads directly).
        raise GrillaConnectionError("grill-discovery request failed") from err

    grills_raw = data.get("grills") if isinstance(data, dict) else None
    if not isinstance(grills_raw, list):
        raise GrillaConnectionError("grill-discovery response missing 'grills' list")
    grills: list[Grill] = []
    for g in grills_raw:
        if isinstance(g, dict) and "sn" in g:
            grills.append(
                Grill(
                    id=str(g["sn"]),
                    name=str(g.get("name") or g["sn"]),
                    model=str(g.get("model") or ""),
                )
            )
    return grills
