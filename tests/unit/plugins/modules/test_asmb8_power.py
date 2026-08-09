# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_power.

Every test here replaces `asmb8_power.build_ipmi_client` with a fake -- no test
in this file constructs a real `IpmiClient`, let alone a real
`pyghmi.ipmi.command.Command`, so nothing here can reach a socket, let alone
any real BMC.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import RemoteOperationError, TimeoutError_
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_power

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


def _fake_client_at(powerstate: str) -> Mock:
    client = Mock()
    client.endpoint = "10.0.0.5:623"
    client.get_power_state.return_value = {"powerstate": powerstate}
    return client


def _wire_fake_client(monkeypatch, fake_client) -> None:
    monkeypatch.setattr(asmb8_power, "build_ipmi_client", lambda params: fake_client)


def _run_ok(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleExitJson) as excinfo:
        asmb8_power.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_power.main()
    return excinfo.value.args[0]


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_power.argument_spec()["password"]["no_log"] is True

    def test_state_choices_match_the_sourced_power_states_table(self):
        # Not invented here -- see module_utils/models.py's POWER_STATES,
        # sourced from community.general.ipmi_power's own documentation.
        spec = asmb8_power.argument_spec()["state"]
        assert set(spec["choices"]) == {"on", "off", "shutdown", "reset", "boot"}
        assert spec["required"] is True

    def test_ipmi_port_defaults_to_623(self):
        assert asmb8_power.argument_spec()["ipmi_port"]["default"] == 623

    def test_invalid_state_fails_argument_validation(self, monkeypatch):
        _wire_fake_client(monkeypatch, Mock())
        args = dict(BASE_ARGS)
        args["state"] = "explode"
        result = _run_fail(args)
        assert "explode" in result["msg"]
        assert "reset" in result["msg"]


class TestPlan:
    @pytest.mark.parametrize("current", ["on", "off"])
    def test_on_is_convergent(self, current):
        assert asmb8_power.plan("on", current) == (current != "on")

    @pytest.mark.parametrize("current", ["on", "off"])
    def test_off_is_convergent(self, current):
        assert asmb8_power.plan("off", current) == (current != "off")

    @pytest.mark.parametrize("state", ["shutdown", "reset", "boot"])
    @pytest.mark.parametrize("current", ["on", "off"])
    def test_states_pyghmi_cannot_report_are_always_imperative(self, state, current):
        # get_power() can only ever report 'on'/'off' (see module_utils/ipmi.py),
        # so these three can never compare equal to it.
        assert asmb8_power.plan(state, current) is True


class TestConvergence:
    def test_on_when_already_on_is_a_noop(self, monkeypatch):
        fake_client = _fake_client_at("on")
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, state="on"))
        assert result["changed"] is False
        fake_client.set_power_state.assert_not_called()

    def test_off_when_already_off_is_a_noop(self, monkeypatch):
        fake_client = _fake_client_at("off")
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, state="off"))
        assert result["changed"] is False
        fake_client.set_power_state.assert_not_called()

    def test_second_run_of_the_same_task_is_idempotent(self, monkeypatch):
        fake_client = _fake_client_at("on")
        _wire_fake_client(monkeypatch, fake_client)
        args = dict(BASE_ARGS, state="on")
        first = _run_ok(args)
        second = _run_ok(args)
        assert first["changed"] is False
        assert second["changed"] is False
        fake_client.set_power_state.assert_not_called()

    def test_on_when_off_issues_the_request(self, monkeypatch):
        fake_client = _fake_client_at("off")
        fake_client.set_power_state.return_value = {"powerstate": "on"}
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, state="on"))
        assert result["changed"] is True
        assert result["desired_state"] == "on"
        assert result["observed"] == {"powerstate": "on"}
        fake_client.set_power_state.assert_called_once_with("on", wait=60)


class TestImperativeStates:
    @pytest.mark.parametrize("state", ["shutdown", "reset", "boot"])
    def test_always_issues_the_request_regardless_of_current_state(self, monkeypatch, state):
        fake_client = _fake_client_at("on")
        fake_client.set_power_state.return_value = {"pendingpowerstate": state}
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, state=state))
        assert result["changed"] is True
        fake_client.set_power_state.assert_called_once()

    def test_reset_never_asks_pyghmi_to_wait(self, monkeypatch):
        # reset/boot are never confirmed by pyghmi's own wait loop regardless
        # of wait_timeout (see module_utils/ipmi.py) -- this module passes
        # wait=False for them rather than pretending otherwise.
        fake_client = _fake_client_at("on")
        fake_client.set_power_state.return_value = {"pendingpowerstate": "reset"}
        _wire_fake_client(monkeypatch, fake_client)
        _run_ok(dict(BASE_ARGS, state="reset", wait_timeout=120))
        fake_client.set_power_state.assert_called_once_with("reset", wait=False)

    def test_custom_wait_timeout_is_passed_through_for_confirmable_states(self, monkeypatch):
        fake_client = _fake_client_at("off")
        fake_client.set_power_state.return_value = {"powerstate": "on"}
        _wire_fake_client(monkeypatch, fake_client)
        _run_ok(dict(BASE_ARGS, state="on", wait_timeout=120))
        fake_client.set_power_state.assert_called_once_with("on", wait=120)


class TestCheckMode:
    @pytest.mark.parametrize("state", ["shutdown", "reset", "boot"])
    def test_check_mode_reports_the_plan_without_sending_it(self, monkeypatch, state):
        fake_client = _fake_client_at("off")
        _wire_fake_client(monkeypatch, fake_client)
        args = dict(BASE_ARGS, state=state, _ansible_check_mode=True)
        result = _run_ok(args)
        assert result["changed"] is True
        fake_client.set_power_state.assert_not_called()

    def test_check_mode_on_an_already_converged_state_reports_no_change(self, monkeypatch):
        fake_client = _fake_client_at("on")
        _wire_fake_client(monkeypatch, fake_client)
        args = dict(BASE_ARGS, state="on", _ansible_check_mode=True)
        result = _run_ok(args)
        assert result["changed"] is False
        fake_client.set_power_state.assert_not_called()

    def test_check_mode_never_mutates_when_a_change_would_be_needed(self, monkeypatch):
        fake_client = _fake_client_at("off")
        _wire_fake_client(monkeypatch, fake_client)
        args = dict(BASE_ARGS, state="on", _ansible_check_mode=True)
        result = _run_ok(args)
        assert result["changed"] is True
        fake_client.set_power_state.assert_not_called()


class TestErrorHandling:
    def test_remote_operation_failure_propagates_as_a_failure(self, monkeypatch):
        fake_client = _fake_client_at("off")
        fake_client.set_power_state.side_effect = RemoteOperationError(
            "IPMI set-power-state to 'on' failed: bad completion code", endpoint="10.0.0.5:623", operation="set_power"
        )
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS, state="on"))
        assert result["error_class"] == "remote_operation"

    def test_confirmation_timeout_is_reported_as_indeterminate(self, monkeypatch):
        fake_client = _fake_client_at("off")
        fake_client.set_power_state.side_effect = TimeoutError_("confirmation timed out", endpoint="10.0.0.5:623", operation="set_power", indeterminate=True)
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS, state="on"))
        assert result["error_class"] == "timeout"
        assert result["indeterminate"] is True

    def test_missing_pyghmi_dependency_is_an_actionable_failure(self, monkeypatch):
        monkeypatch.setattr(asmb8_power, "HAS_PYGHMI", False)
        monkeypatch.setattr(asmb8_power, "PYGHMI_IMPORT_ERROR", "No module named 'pyghmi'")
        result = _run_fail(dict(BASE_ARGS))
        assert "pyghmi" in result["msg"]


class TestNoCredentialLeakage:
    def test_no_credential_in_a_failure_result(self, monkeypatch):
        fake_client = _fake_client_at("off")
        fake_client.set_power_state.side_effect = RemoteOperationError(
            f"rejected password={PASSWORD}", endpoint="10.0.0.5:623", operation="set_power", secrets=PASSWORD
        )
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS, state="on"))
        assert PASSWORD not in json.dumps(result)
        assert "[REDACTED]" in result["msg"]
