# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_boot.

Every test here replaces `asmb8_boot.build_ipmi_client` with a fake -- no test
constructs a real `IpmiClient`, so nothing here can reach a socket, let alone
any real BMC.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import RemoteOperationError, UnsupportedCapabilityError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_boot

PASSWORD = "Sup3rSecret!"

BASE_ARGS = {
    "host": "10.0.0.5",
    "username": "admin",
    "password": PASSWORD,
    "device": "network",
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


def _fake_client_with(bootdev: str, *, persistent: bool = False, uefimode: bool | None = False) -> Mock:
    client = Mock()
    client.endpoint = "10.0.0.5:623"
    current = {"bootdev": bootdev, "persistent": persistent}
    if uefimode is not None:
        current["uefimode"] = uefimode
    client.get_boot_device.return_value = current
    return client


def _wire_fake_client(monkeypatch, fake_client) -> None:
    monkeypatch.setattr(asmb8_boot, "build_ipmi_client", lambda params: fake_client)


def _run_ok(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleExitJson) as excinfo:
        asmb8_boot.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_boot.main()
    return excinfo.value.args[0]


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_boot.argument_spec()["password"]["no_log"] is True

    def test_device_choices_match_the_sourced_boot_devices_table(self):
        # Not invented here -- see module_utils/models.py's BOOT_DEVICES,
        # sourced from community.general.ipmi_boot's own documentation.
        spec = asmb8_boot.argument_spec()["device"]
        assert set(spec["choices"]) == {"network", "floppy", "hd", "safe", "optical", "setup", "default"}
        assert spec["required"] is True

    def test_persistent_defaults_to_false(self):
        assert asmb8_boot.argument_spec()["persistent"]["default"] is False

    def test_ipmi_port_defaults_to_623(self):
        assert asmb8_boot.argument_spec()["ipmi_port"]["default"] == 623

    def test_invalid_device_choice_is_rejected_by_argument_spec(self, monkeypatch):
        _wire_fake_client(monkeypatch, Mock())
        result = _run_fail(dict(BASE_ARGS, device="floppydisk"))
        assert "floppydisk" in result["msg"]


class TestPersistentIsRejectedOutright:
    def test_persistent_true_fails_with_unsupported_capability(self, monkeypatch):
        fake_client = _fake_client_with("network")
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS, persistent=True))
        assert result["error_class"] == "unsupported_capability"
        # The refusal happens before any connection is attempted.
        fake_client.get_boot_device.assert_not_called()
        fake_client.set_boot_device.assert_not_called()

    def test_persistent_true_refuses_even_in_check_mode(self, monkeypatch):
        fake_client = _fake_client_with("network")
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS, persistent=True, _ansible_check_mode=True))
        assert result["error_class"] == "unsupported_capability"
        fake_client.get_boot_device.assert_not_called()

    def test_reject_persistent_helper_raises_only_when_true(self):
        asmb8_boot.reject_persistent(False)  # must not raise
        with pytest.raises(UnsupportedCapabilityError):
            asmb8_boot.reject_persistent(True)


class TestPlan:
    def test_matching_device_and_uefi_is_not_changed(self):
        previous = {"bootdev": "hd", "persistent": False, "uefimode": True}
        assert asmb8_boot.plan("hd", True, previous) is False

    def test_different_device_is_changed(self):
        previous = {"bootdev": "hd", "persistent": False, "uefimode": False}
        assert asmb8_boot.plan("network", False, previous) is True

    def test_different_uefi_flag_is_changed(self):
        previous = {"bootdev": "hd", "persistent": False, "uefimode": False}
        assert asmb8_boot.plan("hd", True, previous) is True

    def test_missing_uefimode_is_normalized_to_the_requested_value(self):
        # The 'default' branch of get_bootdev() carries no uefimode key at all
        # (see module_utils/ipmi.py) -- treated as "no opinion", matching
        # community.general.ipmi_boot's own current.setdefault('uefimode', ...).
        previous = {"bootdev": "default", "persistent": True}
        assert asmb8_boot.plan("default", True, previous) is False
        assert asmb8_boot.plan("default", False, previous) is False


class TestConvergence:
    def test_already_matching_device_is_a_noop(self, monkeypatch):
        fake_client = _fake_client_with("network", uefimode=False)
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, device="network"))
        assert result["operation"]["changed"] is False
        fake_client.set_boot_device.assert_not_called()

    def test_second_run_of_the_same_task_is_idempotent(self, monkeypatch):
        fake_client = _fake_client_with("network", uefimode=False)
        _wire_fake_client(monkeypatch, fake_client)
        args = dict(BASE_ARGS, device="network")
        first = _run_ok(args)
        second = _run_ok(args)
        assert first["operation"]["changed"] is False
        assert second["operation"]["changed"] is False
        fake_client.set_boot_device.assert_not_called()

    def test_different_device_issues_the_request(self, monkeypatch):
        fake_client = _fake_client_with("hd", uefimode=False)
        fake_client.set_boot_device.return_value = {"bootdev": "network"}
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, device="network"))
        assert result["operation"]["changed"] is True
        assert result["device"] == "network"
        fake_client.set_boot_device.assert_called_once_with("network", persist=False, uefiboot=False)

    def test_persist_argument_sent_to_ipmi_is_always_false(self, monkeypatch):
        fake_client = _fake_client_with("hd", uefimode=False)
        fake_client.set_boot_device.return_value = {"bootdev": "network"}
        _wire_fake_client(monkeypatch, fake_client)
        _run_ok(dict(BASE_ARGS, device="network"))
        kwargs = fake_client.set_boot_device.call_args.kwargs
        assert kwargs["persist"] is False


class TestCheckMode:
    def test_check_mode_never_mutates_when_a_change_would_be_needed(self, monkeypatch):
        fake_client = _fake_client_with("hd", uefimode=False)
        _wire_fake_client(monkeypatch, fake_client)
        args = dict(BASE_ARGS, device="network", _ansible_check_mode=True)
        result = _run_ok(args)
        assert result["operation"]["changed"] is True
        fake_client.set_boot_device.assert_not_called()

    def test_check_mode_on_an_already_converged_device_reports_no_change(self, monkeypatch):
        fake_client = _fake_client_with("network", uefimode=False)
        _wire_fake_client(monkeypatch, fake_client)
        args = dict(BASE_ARGS, device="network", _ansible_check_mode=True)
        result = _run_ok(args)
        assert result["operation"]["changed"] is False
        fake_client.set_boot_device.assert_not_called()


class TestReceiptShape:
    def test_receipt_is_nested_under_operation_not_spread_at_top_level(self, monkeypatch):
        fake_client = _fake_client_with("hd", uefimode=False)
        fake_client.set_boot_device.return_value = {"bootdev": "network"}
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, device="network"))
        # `previous` is deliberately ALSO returned at the top level here (see
        # this module's RETURN docs) -- unlike the sibling intel_amt
        # collection's amt_boot, which nests it only under `operation`. Only
        # the receipt's own bookkeeping fields must not leak to the top level.
        for moved_field in ("schema", "action", "endpoint", "desired", "observed"):
            assert moved_field not in result, f"{moved_field!r} must not be spread at the top level"
        operation = result["operation"]
        assert operation["schema"] == "asmb8-ikvm-operation/v1"
        assert operation["action"] == "asmb8_boot"
        assert operation["desired"] == {"bootdev": "network", "uefiboot": False, "persist": False}
        assert operation["observed"] == {"bootdev": "network", "persistent": False, "uefimode": False}


class TestErrorHandling:
    def test_remote_operation_failure_propagates_as_a_failure(self, monkeypatch):
        fake_client = _fake_client_with("hd", uefimode=False)
        fake_client.set_boot_device.side_effect = RemoteOperationError(
            "IPMI set-boot-device to 'network' failed: bad completion code", endpoint="10.0.0.5:623", operation="set_bootdev"
        )
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS, device="network"))
        assert result["error_class"] == "remote_operation"

    def test_missing_pyghmi_dependency_is_an_actionable_failure(self, monkeypatch):
        monkeypatch.setattr(asmb8_boot, "HAS_PYGHMI", False)
        monkeypatch.setattr(asmb8_boot, "PYGHMI_IMPORT_ERROR", "No module named 'pyghmi'")
        result = _run_fail(dict(BASE_ARGS))
        assert "pyghmi" in result["msg"]


class TestNoCredentialLeakage:
    def test_no_credential_in_a_failure_result(self, monkeypatch):
        fake_client = _fake_client_with("hd", uefimode=False)
        fake_client.set_boot_device.side_effect = RemoteOperationError(
            f"rejected password={PASSWORD}", endpoint="10.0.0.5:623", operation="set_bootdev", secrets=PASSWORD
        )
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS, device="network"))
        assert PASSWORD not in json.dumps(result)
        assert "[REDACTED]" in result["msg"]
