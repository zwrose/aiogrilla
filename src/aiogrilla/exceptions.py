# src/aiogrilla/exceptions.py
class GrillaError(Exception):
    """Base error."""


class GrillaAuthError(GrillaError):
    """Login/token/credential failure that needs user reauth."""


class GrillaConnectionError(GrillaError):
    """Transient/cloud/network failure that is worth retrying."""
