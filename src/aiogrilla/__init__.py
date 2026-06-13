from importlib.metadata import PackageNotFoundError, version

from .client import GrillaClient
from .const import ERROR_CODE_NAMES, MODEL_NAMES
from .exceptions import GrillaAuthError, GrillaConnectionError, GrillaError
from .models import CookMode, Grill, GrillState, Mode, TemperatureUnit

try:
    __version__ = version("aiogrilla")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without metadata
    __version__ = "0.2.3"

__all__ = [
    "GrillaClient",
    "Grill",
    "GrillState",
    "Mode",
    "CookMode",
    "TemperatureUnit",
    "GrillaError",
    "GrillaAuthError",
    "GrillaConnectionError",
    "MODEL_NAMES",
    "ERROR_CODE_NAMES",
    "__version__",
]
