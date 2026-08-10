# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_reset.

Every test here replaces `asmb8_reset.build_ipmi_client` with a fake -- no
test constructs a real `IpmiClient`, so nothing here can reach a socket, let
alone any real BMC. `TestCheckMode` additionally asserts that
`build_ipmi_client` itself is never even called in check mode -- see the
module's own DOCUMENTATION: a self-reset has no idempotency concept, so check
mode never opens a connection at all, not merely "opens one but skips the
mutation" the way asmb8_power/asmb8_boot's check mode does.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import RemoteOperationError, TimeoutError_
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_reset

PASSWORD = "Sup3rSecret!"

BASE_ARGS = {
    "host": "10.0.0.5",
    "username": "admin",
    "password": PASSWORD,
    "mode": "cold",
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


def _fake_client(mode_result: dict | None = None) -> Mock:
    client = Mock()
    client.endpoint = "10.0.0.5:623"
    client.reset_bmc.return_value = mode_result if mode_result is not None else {"mode": "cold"}
    return client


def _wire_fake_client(monkeypatch, fake_client) -> None:
    monkeypatch.setattr(asmb8_reset, "build_ipmi_client", lambda params: fake_client)


def _run_ok(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleExitJson) as excinfo:
        asmb8_reset.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_reset.main()
    return excinfo.value.args[0]


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_reset.argument_spec()["password"]["no_log"] is True

    def test_mode_is_required_with_no_default(self):
        spec = asmb8_reset.argument_spec()["mode"]
        assert spec["required"] is True
        assert "default" not in spec

    def test_mode_choices_are_exactly_cold_and_warm(self):
        assert set(asmb8_reset.argument_spec()["mode"]["choices"]) == {"cold", "warm"}

    def test_ipmi_port_defaults_to_623(self):
        assert asmb8_reset.argument_spec()["ipmi_port"]["default"] == 623

    def test_missing_mode_is_rejected_by_argument_spec(self, monkeypatch):
        _wire_fake_client(monkeypatch, _fake_client())
        args = {k: v for k, v in BASE_ARGS.items() if k != "mode"}
        result = _run_fail(args)
        assert "mode" in result["msg"]

    def test_invalid_mode_choice_is_rejected_by_argument_spec(self, monkeypatch):
        _wire_fake_client(monkeypatch, _fake_client())
        result = _run_fail(dict(BASE_ARGS, mode="hot"))
        assert "hot" in result["msg"]


class TestWarmReset:
    def test_warm_reset_is_issued_and_reported(self, monkeypatch):
        fake_client = _fake_client({"mode": "warm"})
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, mode="warm"))
        assert result["mode"] == "warm"
        assert result["operation"]["changed"] is True
        assert result["operation"]["action"] == "asmb8_reset.warm"
        fake_client.reset_bmc.assert_called_once_with("warm")


class TestColdReset:
    def test_cold_reset_is_issued_and_reported(self, monkeypatch):
        fake_client = _fake_client({"mode": "cold"})
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, mode="cold"))
        assert result["mode"] == "cold"
        assert result["operation"]["changed"] is True
        assert result["operation"]["action"] == "asmb8_reset.cold"
        fake_client.reset_bmc.assert_called_once_with("cold")

    def test_changed_is_always_true_on_a_real_run(self, monkeypatch):
        # A self-reset is never idempotent -- there is no "already reset"
        # state to converge against, unlike asmb8_power's on/off.
        fake_client = _fake_client()
        _wire_fake_client(monkeypatch, fake_client)
        first = _run_ok(dict(BASE_ARGS))
        second = _run_ok(dict(BASE_ARGS))
        assert first["changed"] is True
        assert second["changed"] is True
        assert fake_client.reset_bmc.call_count == 2


class TestCheckMode:
    def test_check_mode_reports_changed_true_but_never_builds_a_client(self, monkeypatch):
        build_calls = []
        monkeypatch.setattr(asmb8_reset, "build_ipmi_client", lambda params: build_calls.append(params) or _fake_client())
        args = dict(BASE_ARGS, _ansible_check_mode=True)
        result = _run_ok(args)
        assert result["changed"] is True
        assert result["operation"]["changed"] is True
        assert result["observed"] is None
        assert build_calls == []  # no IPMI connection was ever opened

    def test_check_mode_never_calls_reset_bmc(self, monkeypatch):
        fake_client = _fake_client()
        _wire_fake_client(monkeypatch, fake_client)
        _run_ok(dict(BASE_ARGS, _ansible_check_mode=True))
        fake_client.reset_bmc.assert_not_called()

    def test_check_mode_reports_the_requested_mode(self, monkeypatch):
        _wire_fake_client(monkeypatch, _fake_client())
        result = _run_ok(dict(BASE_ARGS, mode="warm", _ansible_check_mode=True))
        assert result["mode"] == "warm"
        assert result["operation"]["desired"] == "warm"
        assert result["operation"]["action"] == "asmb8_reset.warm"

    def test_check_mode_endpoint_reflects_host_and_ipmi_port_without_a_client(self, monkeypatch):
        called = []
        monkeypatch.setattr(asmb8_reset, "build_ipmi_client", lambda params: called.append(True))
        result = _run_ok(dict(BASE_ARGS, ipmi_port=6230, _ansible_check_mode=True))
        assert result["operation"]["endpoint"] == "10.0.0.5:6230"
        assert called == []


class TestReceiptShape:
    def test_receipt_is_nested_under_operation_not_spread_at_top_level(self, monkeypatch):
        fake_client = _fake_client({"mode": "cold"})
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        for moved_field in ("schema", "action", "endpoint", "desired"):
            assert moved_field not in result, f"{moved_field!r} must not be spread at the top level"
        operation = result["operation"]
        assert operation["schema"] == "asmb8-ikvm-operation/v1"
        assert operation["previous"] is None
        assert operation["desired"] == "cold"
        assert operation["observed"] == {"mode": "cold"}

    def test_endpoint_uses_the_clients_own_endpoint_on_a_real_run(self, monkeypatch):
        fake_client = _fake_client()
        fake_client.endpoint = "10.0.0.5:9999"
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        assert result["operation"]["endpoint"] == "10.0.0.5:9999"


class TestErrorHandling:
    def test_remote_operation_failure_propagates_as_a_failure(self, monkeypatch):
        fake_client = _fake_client()
        fake_client.reset_bmc.side_effect = RemoteOperationError("IPMI cold reset failed: bad completion code", endpoint="10.0.0.5:623", operation="reset_bmc")
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS))
        assert result["error_class"] == "remote_operation"

    def test_timeout_failure_maps_to_the_timeout_error_class(self, monkeypatch):
        fake_client = _fake_client()
        fake_client.reset_bmc.side_effect = TimeoutError_("IPMI cold reset timed out", endpoint="10.0.0.5:623", operation="reset_bmc")
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS))
        assert result["error_class"] == "timeout"

    def test_missing_pyghmi_dependency_is_an_actionable_failure(self, monkeypatch):
        monkeypatch.setattr(asmb8_reset, "HAS_PYGHMI", False)
        monkeypatch.setattr(asmb8_reset, "PYGHMI_IMPORT_ERROR", "No module named 'pyghmi'")
        result = _run_fail(dict(BASE_ARGS))
        assert "pyghmi" in result["msg"]

    def test_missing_pyghmi_dependency_is_checked_even_in_check_mode(self, monkeypatch):
        monkeypatch.setattr(asmb8_reset, "HAS_PYGHMI", False)
        monkeypatch.setattr(asmb8_reset, "PYGHMI_IMPORT_ERROR", "No module named 'pyghmi'")
        result = _run_fail(dict(BASE_ARGS, _ansible_check_mode=True))
        assert "pyghmi" in result["msg"]


class TestNoCredentialLeakage:
    def test_no_credential_in_a_failure_result(self, monkeypatch):
        fake_client = _fake_client()
        fake_client.reset_bmc.side_effect = RemoteOperationError(
            f"rejected password={PASSWORD}", endpoint="10.0.0.5:623", operation="reset_bmc", secrets=PASSWORD
        )
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS))
        assert PASSWORD not in json.dumps(result)
        assert "[REDACTED]" in result["msg"]
