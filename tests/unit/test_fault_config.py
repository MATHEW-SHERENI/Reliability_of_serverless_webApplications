import pytest

from orchestrate.fault_config import HEALTHY, merged_variables

CURRENT = {"PRODUCTS_TABLE": "b10-products", "FAULT_MODE": "delay", "FAULT_DELAY_MS": "300"}


def test_keeps_unrelated_variables():
    assert merged_variables(CURRENT, {})["PRODUCTS_TABLE"] == "b10-products"


def test_previous_fault_settings_do_not_linger():
    result = merged_variables(CURRENT, {"FAULT_MODE": "transient", "FAULT_RATE": 0.1})
    assert result["FAULT_MODE"] == "transient" and result["FAULT_RATE"] == "0.1"
    assert result["FAULT_DELAY_MS"] == HEALTHY["FAULT_DELAY_MS"]


def test_unknown_setting_is_rejected():
    with pytest.raises(ValueError):
        merged_variables(CURRENT, {"FAULT_RAT": "0.1"})
