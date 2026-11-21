"""T5 (midterm Table 1). The dependency client forwards reservation keys and the orchestrator sets the idempotency flag."""
import json

from orchestrate import fault_config
from test_dep_client import FakeResponse, make_client


def test_client_sends_key_in_reservation_json():
    """A keyed call forwards the exact client key."""
    client, http = make_client(FakeResponse(200, {}))
    client.reserve(17, 1, timeout_s=1.0, idempotency_key="order-key")
    assert json.loads(http.calls[0][2]["body"]) == {
        "product_id": 17, "quantity": 1, "idempotency_key": "order-key"}


def test_client_omits_key_for_legacy_reservation():
    """The default request body contains only product and quantity."""
    client, http = make_client(FakeResponse(200, {}))
    client.reserve(17, 1, timeout_s=1.0)
    assert json.loads(http.calls[0][2]["body"]) == {"product_id": 17, "quantity": 1}


def test_flag_enables_idempotency():
    """The orchestrator encodes the enabled flag as a string."""
    assert fault_config.mechanism_variables({}, False, False, idempotency=True)["MECH_IDEMPOTENCY"] == "true"


def test_default_clears_previous_idempotency_flag():
    """An ordinary run explicitly disables any previous deduplication setting."""
    current = {"MECH_IDEMPOTENCY": "true"}
    assert fault_config.mechanism_variables(current, False, False)["MECH_IDEMPOTENCY"] == "false"


def test_set_mechanisms_forwards_idempotency(monkeypatch):
    """Capture the environment update locally without creating a Lambda client."""
    written = {}

    def capture(function_name, region, change):
        written.update(change({}))

    monkeypatch.setattr(fault_config, "update_variables", capture)
    fault_config.set_mechanisms("catalog", False, False, idempotency=True)
    assert written["MECH_IDEMPOTENCY"] == "true"
