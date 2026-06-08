"""The public decode tables are exported and stable.

These tables are part of aiogrilla's public API (consumed by the Home Assistant
integration). This test documents that contract so the tables are not removed as
"unused" again.
"""

from aiogrilla import ERROR_CODE_NAMES, MODEL_NAMES, const


def test_model_names_public_and_known() -> None:
    assert const.MODEL_NAMES is MODEL_NAMES
    assert MODEL_NAMES["silverbacxl"] == "Silverbac XL"
    assert MODEL_NAMES["grilla"] == "Grilla"


def test_error_code_names_public_and_traceable() -> None:
    assert const.ERROR_CODE_NAMES is ERROR_CODE_NAMES
    # The friendly string keeps the raw code so it stays traceable.
    assert "FHI" in ERROR_CODE_NAMES["FHI"]
    assert "C15" in ERROR_CODE_NAMES["C15"]
