# tests/test_models.py
import json
import logging
import pathlib

import pytest

from aiogrilla.models import (
    CookMode,
    GrillState,
    Mode,
    TemperatureUnit,
    parse_grill_state,
)

FIX = pathlib.Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / name).read_text())


def test_mode_unknown_fallback():
    assert Mode.from_raw("running") is Mode.RUNNING
    assert Mode.from_raw("some_future_value") is Mode.UNKNOWN
    assert Mode.from_raw(None) is Mode.UNKNOWN


def test_standby_mode_recognized_and_not_on():
    assert Mode.from_raw("standby") is Mode.STANDBY
    s = parse_grill_state({"mode": "standby", "current_cook_temp": 90})
    assert s.mode is Mode.STANDBY
    assert s.is_on is False  # idle, not cooking


def test_stable_mode_recognized_and_on(caplog):
    assert Mode.from_raw("stable") is Mode.STABLE
    with caplog.at_level(logging.WARNING, logger="aiogrilla.models"):
        s = parse_grill_state({"mode": "stable", "current_cook_temp": 225})
    assert s.mode is Mode.STABLE
    assert s.is_on is True  # holding at target temp == actively cooking
    assert "unrecognized grill mode" not in caplog.text


def test_cook_timer_minutes_to_seconds():
    s = parse_grill_state(
        {"mode": "running", "cook_timer": {"total_seconds": 30, "remaining_seconds": 30}}
    )
    assert s.timer_total_s == 1800 and s.timer_remaining_s == 1800  # 30 min -> 1800 s


def test_cook_timer_missing_and_truncates_before_scaling():
    # Missing cook_timer -> 0; whole minutes -> x60; truncation happens BEFORE x60.
    assert parse_grill_state({"mode": "running"}).timer_remaining_s == 0
    s = parse_grill_state(
        {"mode": "running", "cook_timer": {"total_seconds": 5, "remaining_seconds": 5}}
    )
    assert s.timer_total_s == 300 and s.timer_remaining_s == 300
    # _int truncates 30.9 -> 30, THEN x60 = 1800 (not int(30.9*60) == 1854).
    frac = parse_grill_state({"mode": "running", "cook_timer": {"remaining_seconds": 30.9}})
    assert frac.timer_remaining_s == 1800


def test_has_error_property():
    assert parse_grill_state({"mode": "running"}).has_error is False  # default "none"
    assert parse_grill_state({"mode": "running", "error": "none"}).has_error is False
    assert parse_grill_state({"mode": "shutdown", "error": "FHI"}).has_error is True


def test_cookmode_and_units():
    assert CookMode.from_raw("pid") is CookMode.PID
    assert CookMode.from_raw("PRO") is CookMode.PRO
    assert TemperatureUnit.from_raw("C") is TemperatureUnit.CELSIUS
    assert TemperatureUnit.from_raw(None) is TemperatureUnit.FAHRENHEIT  # default


def test_parse_running():
    s = parse_grill_state(_load("grill_state_running.json"))
    assert s.grill_temp == 247 and s.target_grill_temp == 250
    assert s.probe_temp == 156 and s.target_probe_temp == 195
    assert s.probe2_temp is None  # 65535 sentinel
    assert s.mode is Mode.RUNNING and s.cook_mode is CookMode.PID
    assert s.units is TemperatureUnit.FAHRENHEIT
    assert s.timer_remaining_s == 509 * 60 and s.turntable is True  # field is minutes -> seconds
    assert s.alarm_on is False and s.unrecognized is False


def test_parse_celsius_and_pro():
    s = parse_grill_state(_load("grill_state_celsius.json"))
    assert s.units is TemperatureUnit.CELSIUS and s.cook_mode is CookMode.PRO


def test_parse_probe1_unplugged_and_alarm():
    s = parse_grill_state(_load("grill_state_probe1_unplugged.json"))
    assert s.probe_temp is None and s.target_probe_temp is None  # unplugged -> target None too
    assert s.error == "FHI" and s.alarm_on is True
    assert s.alarm_low == 150 and s.alarm_high == 250
    assert s.mode is Mode.SHUTDOWN


def test_parse_out_of_range_and_missing():
    s = parse_grill_state({"current_cook_temp": 5000, "mode": "weird", "settings": {}})
    assert s.grill_temp is None  # >TEMP_MAX_PLAUSIBLE_F -> None
    assert s.mode is Mode.UNKNOWN
    assert s.unrecognized is False  # had a (bogus) temp+mode key present


def test_parse_malformed_never_raises():
    s = parse_grill_state({})  # empty
    assert s.grill_temp is None and s.mode is Mode.UNKNOWN
    assert s.unrecognized is True  # nothing recognizable
    assert dict(s.raw) == {}


def test_raw_is_immutable():
    s = parse_grill_state(_load("grill_state_running.json"))
    with pytest.raises(TypeError):
        s.raw["x"] = 1  # MappingProxyType is read-only


@pytest.mark.parametrize(
    "bad",
    [
        {"settings": ["pid"]},
        {"settings": "pid"},
        {"settings": 5},
        {"settings": {"temp_alarm_range": [1, 2]}},
        {"cook_timer": "12:00"},
        {"cook_timer": {"total_seconds": float("inf"), "remaining_seconds": float("nan")}},
        {"current_cook_temp": float("nan"), "mode": "running"},
        {"current_cook_temp": float("inf")},
        {"turntable": "yes"},
        {"error": 123},
        {"current_probe_temp": True},
    ],
)
def test_parser_is_total(bad):
    assert parse_grill_state(bad) is not None  # must never raise


def test_nan_and_inf_temp_become_none():
    s = parse_grill_state({"current_cook_temp": float("nan"), "mode": "running"})
    assert s.grill_temp is None
    s2 = parse_grill_state({"current_cook_temp": float("inf"), "mode": "running"})
    assert s2.grill_temp is None


def _state_with_mode(mode: Mode) -> GrillState:
    return GrillState(
        grill_temp=None,
        target_grill_temp=None,
        probe_temp=None,
        target_probe_temp=None,
        probe2_temp=None,
        target_probe2_temp=None,
        mode=mode,
        cook_mode=CookMode.PID,
        error="none",
        units=TemperatureUnit.FAHRENHEIT,
        timer_total_s=0,
        timer_remaining_s=0,
        turntable=None,
        fw_version=None,
        alarm_low=None,
        alarm_high=None,
        alarm_on=False,
        unrecognized=False,
        raw={},
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        # is_on is False only for the two "not running" modes.
        (Mode.OFF, False),
        (Mode.UNKNOWN, False),
        # Every other mode is "on".
        (Mode.IGNITING, True),
        (Mode.RUNNING, True),
        (Mode.STABLE, True),
        (Mode.HOLD, True),
        (Mode.FEED, True),
        (Mode.MANUAL, True),
        (Mode.SHUTDOWN, True),
        (Mode.PAUSED, True),
        (Mode.DONE, True),
        (Mode.IDLE, True),
        (Mode.ERROR, True),
    ],
)
def test_is_on(mode, expected):
    assert _state_with_mode(mode).is_on is expected


def test_exceptions_hierarchy():
    from aiogrilla.exceptions import GrillaAuthError, GrillaConnectionError, GrillaError

    assert issubclass(GrillaAuthError, GrillaError)
    assert issubclass(GrillaConnectionError, GrillaError)
