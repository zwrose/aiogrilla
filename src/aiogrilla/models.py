# src/aiogrilla/models.py
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .const import PROBE_UNPLUGGED, TEMP_MAX_PLAUSIBLE_F


class Mode(StrEnum):
    OFF = "off"
    STANDBY = "standby"  # controller powered, idle (observed live when not cooking)
    IGNITING = "igniting"
    RUNNING = "running"
    STABLE = "stable"  # holding steady at target temp (observed live mid-cook)
    HOLD = "hold"
    FEED = "feed"
    MANUAL = "manual"
    SHUTDOWN = "shutdown"
    PAUSED = "paused"
    DONE = "done"
    IDLE = "idle"
    ERROR = "error"
    UNKNOWN = "unknown"

    @classmethod
    def from_raw(cls, value: object) -> Mode:
        try:
            return cls(str(value).lower())
        except ValueError:
            return cls.UNKNOWN


class CookMode(StrEnum):
    PID = "pid"
    PRO = "pro"
    UNKNOWN = "unknown"

    @classmethod
    def from_raw(cls, value: object) -> CookMode:
        try:
            return cls(str(value).lower())
        except ValueError:
            return cls.UNKNOWN


class TemperatureUnit(StrEnum):
    FAHRENHEIT = "F"
    CELSIUS = "C"

    @classmethod
    def from_raw(cls, value: object) -> TemperatureUnit:
        try:
            return cls(str(value).upper())
        except ValueError:
            return cls.FAHRENHEIT  # default when missing/unknown


@dataclass(frozen=True, slots=True)
class Grill:
    id: str  # serial == IoT thingName == MQTT device-id
    name: str
    model: str  # controller code


@dataclass(frozen=True, slots=True)
class GrillState:
    grill_temp: float | None
    target_grill_temp: float | None
    probe_temp: float | None
    target_probe_temp: float | None
    probe2_temp: float | None
    target_probe2_temp: float | None
    mode: Mode
    cook_mode: CookMode
    error: str
    units: TemperatureUnit
    timer_total_s: int
    timer_remaining_s: int
    turntable: bool | None
    fw_version: str | None
    alarm_low: float | None
    alarm_high: float | None
    alarm_on: bool
    unrecognized: bool  # True when payload had neither a "mode" nor "current_cook_temp" key
    raw: Mapping[str, Any]  # diagnostics/forward-compat ONLY

    @property
    def is_on(self) -> bool:
        return self.mode not in (Mode.OFF, Mode.STANDBY, Mode.UNKNOWN)

    @property
    def has_error(self) -> bool:
        """True when the controller reports an active error (i.e. not the 'none' sentinel)."""
        return self.error != "none"


_LOGGER = logging.getLogger(__name__)


def _temp(value: object) -> float | None:
    """Decode a temperature: None for missing, the unplugged sentinel, or out-of-range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value == PROBE_UNPLUGGED:  # explicit unplugged sentinel
        return None
    if not (-40 <= value <= TEMP_MAX_PLAUSIBLE_F):  # positive range also rejects NaN/inf
        return None
    return float(value)


def _int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return int(value)
    except (ValueError, OverflowError):  # NaN / inf
        return default


def _as_mapping(value: object) -> Mapping[str, Any]:
    """Return value if it is a mapping, else an empty mapping (defends wrong-typed fields)."""
    return value if isinstance(value, Mapping) else {}


def parse_grill_state(payload: Mapping[str, Any]) -> GrillState:
    """Total, never-raising parser from a raw grill_state dict to GrillState."""
    settings = _as_mapping(payload.get("settings"))
    alarm = _as_mapping(settings.get("temp_alarm_range"))
    timer = _as_mapping(payload.get("cook_timer"))

    probe_temp = _temp(payload.get("current_probe_temp"))
    probe2_temp = _temp(payload.get("current_probe2_temp"))
    # When a probe is unplugged its target is meaningless (observed desired==0) -> None.
    target_probe = _temp(payload.get("desired_probe_temp")) if probe_temp is not None else None
    target_probe2 = _temp(payload.get("desired_probe2_temp")) if probe2_temp is not None else None

    grill_temp = _temp(payload.get("current_cook_temp"))
    mode = Mode.from_raw(payload.get("mode"))
    _raw_mode = payload.get("mode")
    if mode is Mode.UNKNOWN and _raw_mode not in (None, "", "unknown"):
        _LOGGER.warning("unrecognized grill mode %r; consider adding it to Mode", _raw_mode)
    # "unrecognized" = payload with no known keys (empty dict or entirely foreign schema).
    # A payload that has the expected keys but bogus values (out-of-range temp, unknown mode)
    # is still "recognized" — the schema is known, just the data is odd.
    recognized = "mode" in payload or "current_cook_temp" in payload
    unrecognized = not recognized
    if unrecognized:
        _LOGGER.warning("Unrecognized grill_state schema; keys=%s", sorted(payload))

    fw = payload.get("fw_version")
    return GrillState(
        grill_temp=grill_temp,
        target_grill_temp=_temp(payload.get("desired_temp")),
        probe_temp=probe_temp,
        target_probe_temp=target_probe,
        probe2_temp=probe2_temp,
        target_probe2_temp=target_probe2,
        mode=mode,
        cook_mode=CookMode.from_raw(settings.get("cook_mode")),
        error=str(payload.get("error") or "none"),
        units=TemperatureUnit.from_raw(settings.get("units_pref")),
        # NOTE: despite the "_seconds" key name, the controller reports the cook timer in
        # MINUTES (confirmed live: a 30-minute timer reports total=remaining=30). ×60 → seconds.
        timer_total_s=_int(timer.get("total_seconds")) * 60,
        timer_remaining_s=_int(timer.get("remaining_seconds")) * 60,
        turntable=payload.get("turntable") if isinstance(payload.get("turntable"), bool) else None,
        fw_version=str(fw) if fw is not None else None,
        alarm_low=_temp(alarm.get("low")) if alarm.get("on") else None,
        alarm_high=_temp(alarm.get("high")) if alarm.get("on") else None,
        alarm_on=bool(alarm.get("on", False)),
        unrecognized=unrecognized,
        raw=MappingProxyType(dict(payload)),
    )
