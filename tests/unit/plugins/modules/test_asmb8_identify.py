# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_identify.

Every test here replaces `asmb8_identify.build_ipmi_client` with a fake -- no
test constructs a real `IpmiClient`, so nothing here can reach a socket, let
alone any real BMC. `TestCheckMode` additionally asserts that
`build_ipmi_client` itself is never even called in check mode -- see the
module's own DOCUMENTATION: standard IPMI Chassis Identify has no read-back,
so check mode never opens a connection at all, not merely "opens one but
skips the mutation" the way asmb8_power/asmb8_boot's check mode does.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import RemoteOperationError, TimeoutError_, UnsupportedCapabilityError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_identify

PASSWORD = "Sup3rSecret!"

BASE_ARGS = {
    "host": "10.0.0.5",
    "username": "admin",
    "password": PASSWORD,
    "state": "on",
}


def _set_module_args(args: dict) -> None:
    basic._ANSIBLE_ARGS = to_bytes(json.dumps({"ANSIBLE_MODULE_ARGS": args}))
    basic._ANSIBLE_PROFILE = "legacy"


class AnsibleExitJson(Exception):
    pass


class AnsibleFailJson(Exception):
    pass


def _exit_json(*_args, **kwargs):
    raise AnsibleExitJson(kwargs)


def _fail_json(*_args, **kwargs):
    raise AnsibleFailJson(kwargs)


@pytest.fixture(autouse=True)
def _patch_module_exit(monkeypatch):
    monkeypatch.setattr(basic.AnsibleModule, "exit_json", _exit_json)
    monkeypatch.setattr(basic.AnsibleModule, "fail_json", _fail_json)


def _fake_client() -> Mock:
    client = Mock()
    client.endpoint = "10.0.0.5:623"
    client.set_identify.return_value = None
    return client


def _wire_fake_client(monkeypatch, fake_client) -> None:
    monkeypatch.setattr(asmb8_identify, "build_ipmi_client", lambda params: fake_client)


def _run_ok(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleExitJson) as excinfo:
        asmb8_identify.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_identify.main()
    return excinfo.value.args[0]


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_identify.argument_spec()["password"]["no_log"] is True

    def test_state_is_required_with_no_default(self):
        spec = asmb8_identify.argument_spec()["state"]
        assert spec["required"] is True
        assert "default" not in spec

    def test_state_choices_are_exactly_on_and_off(self):
        assert set(asmb8_identify.argument_spec()["state"]["choices"]) == {"on", "off"}

    def test_duration_has_no_default(self):
        spec = asmb8_identify.argument_spec()["duration"]
        assert spec["type"] == "int"
        assert "default" not in spec

    def test_ipmi_port_defaults_to_623(self):
        assert asmb8_identify.argument_spec()["ipmi_port"]["default"] == 623

    def test_missing_state_is_rejected_by_argument_spec(self, monkeypatch):
        _wire_fake_client(monkeypatch, _fake_client())
        args = {k: v for k, v in BASE_ARGS.items() if k != "state"}
        result = _run_fail(args)
        assert "state" in result["msg"]

    def test_invalid_state_choice_is_rejected_by_argument_spec(self, monkeypatch):
        _wire_fake_client(monkeypatch, _fake_client())
        result = _run_fail(dict(BASE_ARGS, state="blink"))
        assert "blink" in result["msg"]


class TestValidateDuration:
    def test_off_with_no_duration_is_valid(self):
        assert asmb8_identify.validate_duration("off", None) is None

    def test_on_with_no_duration_is_valid(self):
        assert asmb8_identify.validate_duration("on", None) is None

    def test_on_with_positive_duration_is_valid(self):
        assert asmb8_identify.validate_duration("on", 30) is None

    def test_off_with_duration_is_invalid(self):
        message = asmb8_identify.validate_duration("off", 10)
        assert message is not None
        assert "state=on" in message

    def test_on_with_zero_duration_is_invalid(self):
        # The exact footgun this module refuses to pass through silently --
        # pyghmi's own underlying command treats duration=0 as "turn off"
        # regardless of state=on.
        message = asmb8_identify.validate_duration("on", 0)
        assert message is not None
        assert "state=on" in message

    def test_on_with_negative_duration_is_invalid(self):
        assert asmb8_identify.validate_duration("on", -5) is not None


class TestStateOn:
    def test_on_indefinitely_is_issued_and_reported(self, monkeypatch):
        fake_client = _fake_client()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, state="on"))
        assert result["state"] == "on"
        assert result["duration"] is None
        assert result["operation"]["changed"] is True
        assert result["operation"]["action"] == "asmb8_identify.on"
        fake_client.set_identify.assert_called_once_with(on=True, duration=None)

    def test_on_with_bounded_duration_is_issued_and_reported(self, monkeypatch):
        fake_client = _fake_client()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, state="on", duration=300))
        assert result["duration"] == 300
        fake_client.set_identify.assert_called_once_with(on=True, duration=300)

    def test_changed_is_always_true_on_a_real_run(self, monkeypatch):
        # Chassis identify is never idempotent -- there is no read-back to
        # compare against, unlike asmb8_power's on/off.
        fake_client = _fake_client()
        _wire_fake_client(monkeypatch, fake_client)
        first = _run_ok(dict(BASE_ARGS, state="on"))
        second = _run_ok(dict(BASE_ARGS, state="on"))
        assert first["changed"] is True
        assert second["changed"] is True
        assert fake_client.set_identify.call_count == 2


class TestStateOff:
    def test_off_is_issued_and_reported(self, monkeypatch):
        fake_client = _fake_client()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, state="off"))
        assert result["state"] == "off"
        assert result["duration"] is None
        assert result["operation"]["action"] == "asmb8_identify.off"
        fake_client.set_identify.assert_called_once_with(on=False, duration=None)

    def test_off_with_duration_fails_before_any_connection_is_opened(self, monkeypatch):
        build_calls = []
        monkeypatch.setattr(asmb8_identify, "build_ipmi_client", lambda params: build_calls.append(params) or _fake_client())
        result = _run_fail(dict(BASE_ARGS, state="off", duration=10))
        assert "state=on" in result["msg"]
        assert build_calls == []


class TestCheckMode:
    def test_check_mode_reports_changed_true_but_never_builds_a_client(self, monkeypatch):
        build_calls = []
        monkeypatch.setattr(asmb8_identify, "build_ipmi_client", lambda params: build_calls.append(params) or _fake_client())
        args = dict(BASE_ARGS, _ansible_check_mode=True)
        result = _run_ok(args)
        assert result["changed"] is True
        assert result["operation"]["changed"] is True
        assert result["observed"] is None
        assert build_calls == []  # no IPMI connection was ever opened

    def test_check_mode_never_calls_set_identify(self, monkeypatch):
        fake_client = _fake_client()
        _wire_fake_client(monkeypatch, fake_client)
        _run_ok(dict(BASE_ARGS, _ansible_check_mode=True))
        fake_client.set_identify.assert_not_called()

    def test_check_mode_reports_the_requested_state_and_duration(self, monkeypatch):
        _wire_fake_client(monkeypatch, _fake_client())
        result = _run_ok(dict(BASE_ARGS, state="on", duration=60, _ansible_check_mode=True))
        assert result["state"] == "on"
        assert result["duration"] == 60
        assert result["operation"]["desired"] == {"state": "on", "duration": 60}
        assert result["operation"]["action"] == "asmb8_identify.on"

    def test_check_mode_endpoint_reflects_host_and_ipmi_port_without_a_client(self, monkeypatch):
        called = []
        monkeypatch.setattr(asmb8_identify, "build_ipmi_client", lambda params: called.append(True))
        result = _run_ok(dict(BASE_ARGS, ipmi_port=6230, _ansible_check_mode=True))
        assert result["operation"]["endpoint"] == "10.0.0.5:6230"
        assert called == []

    def test_check_mode_still_validates_duration_combination(self, monkeypatch):
        build_calls = []
        monkeypatch.setattr(asmb8_identify, "build_ipmi_client", lambda params: build_calls.append(params) or _fake_client())
        result = _run_fail(dict(BASE_ARGS, state="off", duration=10, _ansible_check_mode=True))
        assert "state=on" in result["msg"]
        assert build_calls == []


class TestReceiptShape:
    def test_receipt_is_nested_under_operation_not_spread_at_top_level(self, monkeypatch):
        fake_client = _fake_client()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, state="on"))
        for moved_field in ("schema", "action", "endpoint", "desired"):
            assert moved_field not in result, f"{moved_field!r} must not be spread at the top level"
        operation = result["operation"]
        assert operation["schema"] == "asmb8-ikvm-operation/v1"
        assert operation["previous"] is None
        assert operation["desired"] == {"state": "on", "duration": None}
        assert operation["observed"] == {"state": "on", "duration": None}

    def test_endpoint_uses_the_clients_own_endpoint_on_a_real_run(self, monkeypatch):
        fake_client = _fake_client()
        fake_client.endpoint = "10.0.0.5:9999"
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, state="on"))
        assert result["operation"]["endpoint"] == "10.0.0.5:9999"


class TestErrorHandling:
    def test_remote_operation_failure_propagates_as_a_failure(self, monkeypatch):
        fake_client = _fake_client()
        fake_client.set_identify.side_effect = RemoteOperationError(
            "IPMI set-identify failed: bad completion code", endpoint="10.0.0.5:623", operation="set_identify"
        )
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS, state="on"))
        assert result["error_class"] == "remote_operation"

    def test_unsupported_capability_maps_to_the_unsupported_capability_error_class(self, monkeypatch):
        fake_client = _fake_client()
        fake_client.set_identify.side_effect = UnsupportedCapabilityError(
            "IPMI set-identify failed: Blink not supported with generic IPMI", endpoint="10.0.0.5:623", operation="set_identify"
        )
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS, state="on"))
        assert result["error_class"] == "unsupported_capability"

    def test_timeout_failure_maps_to_the_timeout_error_class(self, monkeypatch):
        fake_client = _fake_client()
        fake_client.set_identify.side_effect = TimeoutError_("IPMI session timed out", endpoint="10.0.0.5:623", operation="ipmi_connect")
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS, state="on"))
        assert result["error_class"] == "timeout"

    def test_missing_pyghmi_dependency_is_an_actionable_failure(self, monkeypatch):
        monkeypatch.setattr(asmb8_identify, "HAS_PYGHMI", False)
        monkeypatch.setattr(asmb8_identify, "PYGHMI_IMPORT_ERROR", "No module named 'pyghmi'")
        result = _run_fail(dict(BASE_ARGS, state="on"))
        assert "pyghmi" in result["msg"]

    def test_missing_pyghmi_dependency_is_checked_even_in_check_mode(self, monkeypatch):
        monkeypatch.setattr(asmb8_identify, "HAS_PYGHMI", False)
        monkeypatch.setattr(asmb8_identify, "PYGHMI_IMPORT_ERROR", "No module named 'pyghmi'")
        result = _run_fail(dict(BASE_ARGS, state="on", _ansible_check_mode=True))
        assert "pyghmi" in result["msg"]


class TestNoCredentialLeakage:
    def test_no_credential_in_a_failure_result(self, monkeypatch):
        fake_client = _fake_client()
        fake_client.set_identify.side_effect = RemoteOperationError(
            f"rejected password={PASSWORD}", endpoint="10.0.0.5:623", operation="set_identify", secrets=PASSWORD
        )
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS, state="on"))
        assert PASSWORD not in json.dumps(result)
        assert "[REDACTED]" in result["msg"]
