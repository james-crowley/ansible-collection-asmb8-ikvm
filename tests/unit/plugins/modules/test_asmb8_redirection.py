# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_redirection.

This module was rewritten to report (and, for now, always refuse to toggle) this BMC's own
service enablement -- see `test_asmb8_console.py` for the tests covering the IVTP console/KVM
session implementation that used to live under this module's name.

Every test that exercises the reachability probe injects a fake `connect` callable -- nothing
here ever opens a real socket, let alone reaches a real BMC.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_redirection

BASE_ARGS = {
    "host": "10.0.0.5",
    # Required by the shared connection fragment's argument spec (for
    # module_defaults group compatibility -- see the module's own
    # DOCUMENTATION), but never actually used by this module: it never
    # authenticates against anything.
    "password": "unused-by-this-module",
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


def _run_ok(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleExitJson) as excinfo:
        asmb8_redirection.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_redirection.main()
    return excinfo.value.args[0]


def _never_connect(_address, _timeout):
    raise AssertionError("no test should open a real socket -- inject a fake `connect`")


def _refusing_connect(_address, _timeout):
    """Simulates the on-demand ports' resting state: TCP RST, i.e. an immediate OSError."""
    raise OSError("connection refused")


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _accepting_connect(_address, _timeout):
    return _FakeConnection()


class TestArgumentSpec:
    def test_services_default_is_every_known_service(self):
        spec = asmb8_redirection.argument_spec()
        assert spec["services"]["default"] == list(asmb8_redirection.SERVICE_NAMES)
        assert set(spec["services"]["choices"]) == set(asmb8_redirection.SERVICE_NAMES)

    def test_service_choices_match_the_catalog(self):
        assert set(asmb8_redirection.argument_spec()["service"]["choices"]) == set(asmb8_redirection.SERVICE_NAMES)

    def test_state_choices_are_enabled_and_disabled(self):
        assert set(asmb8_redirection.argument_spec()["state"]["choices"]) == {"enabled", "disabled"}

    def test_probe_timeout_default(self):
        assert asmb8_redirection.argument_spec()["probe_timeout"]["default"] == 2.0

    def test_password_is_no_log(self):
        assert asmb8_redirection.argument_spec()["password"]["no_log"] is True

    def test_catalog_names_match_the_hardware_evidence_table(self):
        assert asmb8_redirection.SERVICE_NAMES == ("web", "kvm", "cd-media", "fd-media", "hd-media", "ssh", "telnet")


class TestServiceCatalog:
    """Pins docs/hardware-evidence-2026-08-08.md's Services-page table exactly."""

    def test_web_capacity(self):
        capacity = asmb8_redirection.SERVICE_CATALOG["web"]
        assert (capacity.nonsecure_port, capacity.secure_port, capacity.timeout_seconds, capacity.max_sessions) == (80, 443, 1800, 20)
        assert capacity.on_demand is False
        assert capacity.known_active is True

    def test_kvm_capacity(self):
        capacity = asmb8_redirection.SERVICE_CATALOG["kvm"]
        assert (capacity.nonsecure_port, capacity.secure_port, capacity.timeout_seconds, capacity.max_sessions) == (7578, 7582, 1800, 4)
        assert capacity.on_demand is True
        assert capacity.known_active is True

    @pytest.mark.parametrize(
        ("name", "nonsecure", "secure"),
        [("cd-media", 5120, 5124), ("fd-media", 5122, 5126), ("hd-media", 5123, 5127)],
    )
    def test_media_service_capacities_are_on_demand_with_no_timeout_and_one_session(self, name, nonsecure, secure):
        capacity = asmb8_redirection.SERVICE_CATALOG[name]
        assert (capacity.nonsecure_port, capacity.secure_port) == (nonsecure, secure)
        assert capacity.timeout_seconds is None
        assert capacity.max_sessions == 1
        assert capacity.on_demand is True
        assert capacity.known_active is True

    def test_ssh_has_no_nonsecure_port(self):
        capacity = asmb8_redirection.SERVICE_CATALOG["ssh"]
        assert capacity.nonsecure_port is None
        assert capacity.secure_port == 22
        assert capacity.timeout_seconds == 600
        assert capacity.on_demand is False
        assert capacity.known_active is True

    def test_telnet_is_reported_inactive_and_has_no_secure_port(self):
        capacity = asmb8_redirection.SERVICE_CATALOG["telnet"]
        assert capacity.nonsecure_port == 23
        assert capacity.secure_port is None
        assert capacity.on_demand is False
        assert capacity.known_active is False  # the one service the vendor self-report showed Inactive.


class TestProbePort:
    def test_refused_connection_is_reported_unreachable_not_raised(self):
        assert asmb8_redirection.probe_port("10.0.0.5", 7578, timeout=2.0, connect=_refusing_connect) is False

    def test_accepted_connection_is_reported_reachable_and_closed(self):
        connections: list[_FakeConnection] = []

        def _connect(address, _timeout):
            conn = _FakeConnection()
            connections.append(conn)
            return conn

        assert asmb8_redirection.probe_port("10.0.0.5", 7578, timeout=2.0, connect=_connect) is True
        assert connections[0].closed is True

    def test_timeout_and_host_are_passed_through_to_connect(self):
        seen = {}

        def _connect(address, timeout):
            seen["address"] = address
            seen["timeout"] = timeout
            return _FakeConnection()

        asmb8_redirection.probe_port("10.0.0.5", 443, timeout=3.5, connect=_connect)
        assert seen == {"address": ("10.0.0.5", 443), "timeout": 3.5}


class TestBuildServiceReport:
    def test_known_and_enabled_come_from_the_static_catalog_not_a_live_query(self):
        report = asmb8_redirection.build_service_report("web", "10.0.0.5", timeout=1.0, connect=_refusing_connect)
        assert report["known"] is True
        assert report["enabled"] is True
        assert report["on_demand"] is False

    def test_telnet_reports_enabled_false_per_vendor_self_report(self):
        report = asmb8_redirection.build_service_report("telnet", "10.0.0.5", timeout=1.0, connect=_refusing_connect)
        assert report["enabled"] is False

    def test_capacity_dict_matches_the_catalog(self):
        report = asmb8_redirection.build_service_report("kvm", "10.0.0.5", timeout=1.0, connect=_refusing_connect)
        assert report["capacity"] == {"nonsecure_port": 7578, "secure_port": 7582, "timeout_seconds": 1800, "max_sessions": 4}

    def test_reachable_is_none_for_a_port_role_the_service_does_not_have(self):
        ssh_report = asmb8_redirection.build_service_report("ssh", "10.0.0.5", timeout=1.0, connect=_refusing_connect)
        assert ssh_report["reachable"]["nonsecure"] is None  # ssh has no nonsecure port at all.
        assert ssh_report["reachable"]["secure"] is not None

        telnet_report = asmb8_redirection.build_service_report("telnet", "10.0.0.5", timeout=1.0, connect=_refusing_connect)
        assert telnet_report["reachable"]["secure"] is None  # telnet has no secure port at all.
        assert telnet_report["reachable"]["nonsecure"] is not None

    def test_reachable_entry_shape(self):
        report = asmb8_redirection.build_service_report("web", "10.0.0.5", timeout=1.0, connect=_accepting_connect)
        assert report["reachable"]["nonsecure"] == {"port": 80, "reachable": True}
        assert report["reachable"]["secure"] == {"port": 443, "reachable": True}


class TestOnDemandSemantics:
    """The single most important behaviour this module must get right: an on-demand service
    reporting enabled=true and reachable=false, with no session open, is healthy -- not a fault."""

    @pytest.mark.parametrize("name", ["kvm", "cd-media", "fd-media", "hd-media"])
    def test_on_demand_service_with_no_session_open_is_enabled_but_unreachable(self, name):
        report = asmb8_redirection.build_service_report(name, "10.0.0.5", timeout=1.0, connect=_refusing_connect)
        assert report["on_demand"] is True
        assert report["enabled"] is True
        assert report["reachable"]["nonsecure"]["reachable"] is False
        # Building this report must not raise just because the on-demand port refused -- that is
        # exactly the normal resting state this module's own documentation describes.

    def test_standing_service_reachability_is_independent_of_on_demand_flag(self):
        report = asmb8_redirection.build_service_report("web", "10.0.0.5", timeout=1.0, connect=_accepting_connect)
        assert report["on_demand"] is False
        assert report["reachable"]["nonsecure"]["reachable"] is True


class TestReadOnlyDefault:
    def test_default_run_reports_every_known_service_and_never_changes_anything(self, monkeypatch):
        monkeypatch.setattr(asmb8_redirection, "probe_port", lambda *a, **k: False)
        result = _run_ok(dict(BASE_ARGS))

        assert result["changed"] is False
        assert set(result["services"]) == set(asmb8_redirection.SERVICE_NAMES)
        assert result["operation"]["schema"] == "asmb8-ikvm-operation/v1"
        assert result["operation"]["action"] == "asmb8_redirection.report"
        assert result["operation"]["changed"] is False
        assert result["operation"]["error_class"] is None
        assert result["operation"]["observed"] == result["services"]

    def test_services_option_narrows_the_report(self, monkeypatch):
        monkeypatch.setattr(asmb8_redirection, "probe_port", lambda *a, **k: False)
        result = _run_ok(dict(BASE_ARGS, services=["kvm", "ssh"]))
        assert set(result["services"]) == {"kvm", "ssh"}

    def test_endpoint_is_the_bare_host(self, monkeypatch):
        monkeypatch.setattr(asmb8_redirection, "probe_port", lambda *a, **k: False)
        result = _run_ok(dict(BASE_ARGS))
        assert result["operation"]["endpoint"] == "10.0.0.5"

    def test_check_mode_behaves_identically_to_normal_mode(self, monkeypatch):
        monkeypatch.setattr(asmb8_redirection, "probe_port", lambda *a, **k: False)
        result = _run_ok(dict(BASE_ARGS, _ansible_check_mode=True))
        assert result["changed"] is False
        assert set(result["services"]) == set(asmb8_redirection.SERVICE_NAMES)

    def test_probe_timeout_is_threaded_through_to_every_probe(self, monkeypatch):
        spy = Mock(return_value=False)
        monkeypatch.setattr(asmb8_redirection, "probe_port", spy)
        _run_ok(dict(BASE_ARGS, services=["web"], probe_timeout=7.5))
        assert spy.call_count == 2  # web has both a nonsecure and a secure port.
        for call in spy.call_args_list:
            assert call.kwargs["timeout"] == 7.5


class TestUnsupportedState:
    def test_state_without_service_fails_required_by(self):
        result = _run_fail(dict(BASE_ARGS, state="enabled"))
        assert "service" in result["msg"]

    def test_state_with_service_fails_unsupported_capability(self):
        result = _run_fail(dict(BASE_ARGS, service="kvm", state="enabled"))
        assert result["error_class"] == "unsupported_capability"

    def test_state_failure_message_points_at_the_bmc_web_ui(self):
        result = _run_fail(dict(BASE_ARGS, service="telnet", state="disabled"))
        assert "web UI" in result["msg"]

    def test_state_fails_even_in_check_mode(self):
        result = _run_fail(dict(BASE_ARGS, service="kvm", state="enabled", _ansible_check_mode=True))
        assert result["error_class"] == "unsupported_capability"

    def test_state_never_probes_any_port(self, monkeypatch):
        spy = Mock(side_effect=AssertionError("state must fail before any network access is attempted"))
        monkeypatch.setattr(asmb8_redirection, "build_services_report", spy)
        _run_fail(dict(BASE_ARGS, service="kvm", state="enabled"))
        spy.assert_not_called()

    def test_invalid_state_choice_is_rejected_by_the_argument_spec(self):
        result = _run_fail(dict(BASE_ARGS, service="kvm", state="on"))
        assert "msg" in result
