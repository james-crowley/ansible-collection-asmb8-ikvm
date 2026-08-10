# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_alerts.

Every test here replaces `asmb8_alerts.build_asp_client` with a fake client (`_FixtureAsp`
below) whose `get_webvar()` reads the real, checked-in fixture bytes under
`tests/unit/fixtures/asp/` and parses them with the real `webvar.parse_webvar` -- the same
parser the real `AspClient` uses. No test here constructs a real `AspClient`, opens a socket,
or talks to any BMC; `_FixtureAsp.login()` is a no-op that never leaves this process.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import AuthenticationError, RemoteOperationError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.webvar import parse_webvar
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_alerts

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "asp"

PASSWORD = "Sup3rSecret!"

BASE_ARGS = {
    "host": "10.0.0.5",
    "username": "admin",
    "password": PASSWORD,
}


def _read_fixture(endpoint: str) -> str:
    return (FIXTURES_DIR / f"{endpoint}.txt").read_text(encoding="utf-8")


class _FixtureAsp:
    """A stand-in for `AspClient` that parses real fixture bytes instead of opening a socket.

    `overrides` lets a test replace one endpoint's response (with an alternate body string) or
    make it raise (with an `IkvmError` instance), while every other endpoint still reads its
    real fixture file through the real parser.
    """

    def __init__(self, *, endpoint: str = "10.0.0.5:443", overrides: dict | None = None) -> None:
        self.endpoint = endpoint
        self._overrides = overrides or {}
        self.login_called = False

    def login(self) -> str:
        self.login_called = True
        return "fake-session-cookie"

    def get_webvar(self, endpoint: str):
        override = self._overrides.get(endpoint)
        if isinstance(override, Exception):
            raise override
        body = override if isinstance(override, str) else _read_fixture(endpoint)
        return parse_webvar(body, endpoint=endpoint, operation=f"get_webvar:{endpoint}")


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


def _wire_fake_asp(monkeypatch, fake_asp) -> None:
    monkeypatch.setattr(asmb8_alerts, "build_asp_client", lambda params: fake_asp)


def _run_ok(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleExitJson) as excinfo:
        asmb8_alerts.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_alerts.main()
    return excinfo.value.args[0]


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_alerts.argument_spec()["password"]["no_log"] is True

    def test_host_is_required(self):
        assert asmb8_alerts.argument_spec()["host"]["required"] is True


class TestNeverMutates:
    def test_a_successful_read_always_reports_changed_false(self, monkeypatch):
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        assert result["changed"] is False
        assert result["operation"]["changed"] is False

    def test_check_mode_behaves_identically_to_normal_mode(self, monkeypatch):
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        normal = _run_ok(dict(BASE_ARGS))
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        checked = _run_ok(dict(BASE_ARGS, _ansible_check_mode=True))
        assert normal["destinations"] == checked["destinations"]
        assert normal["filtering"] == checked["filtering"]
        assert normal["email_format"] == checked["email_format"]
        assert normal["adviser"] == checked["adviser"]


class TestRealFixtures:
    """End-to-end against the real, checked-in fixture corpus for all seven endpoints."""

    def test_smtp_channels_from_real_fixture(self, monkeypatch):
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        smtp = result["destinations"]["smtp"]
        assert len(smtp) == 2
        assert smtp[0]["channel"] == 1
        assert smtp[1]["channel"] == 8
        for channel in smtp:
            assert channel["sender_address"] == ""
            assert channel["machine_name"] == ""
            assert channel["primary"]["enabled"] is True
            assert channel["primary"]["port"] == 25
            assert channel["primary"]["server"] == ""
            assert channel["primary"]["auth_enabled"] is False
            assert channel["primary"]["username"] == ""
            assert channel["secondary"]["enabled"] is False

    def test_lan_destinations_from_real_fixture(self, monkeypatch):
        # Real corpus fixture: 30 rows, every DestAddr already redacted to an empty string
        # (no destination configured on the captured board). The exact value asserted here is
        # what the fixture actually contains -- see this module's DOCUMENTATION on why no shape
        # is assumed for a populated real address.
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        lan = result["destinations"]["lan"]
        assert len(lan) == 30
        addresses = {row["address"] for row in lan}
        assert addresses == {""}
        channels = {row["channel"] for row in lan}
        assert channels == {1, 8}

    def test_event_filters_from_real_fixture(self, monkeypatch):
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        event_filters = result["filtering"]["event_filters"]
        assert len(event_filters) == 40
        assert event_filters[0]["alert_policy_num"] == 1
        assert event_filters[0]["filter_config"] == 192
        assert event_filters[0]["sensor_name"] == "Any"

    def test_alert_policies_from_real_fixture(self, monkeypatch):
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        policies = result["filtering"]["policies"]
        assert len(policies) == 60
        assert policies[0]["entry_number"] == 1
        assert policies[0]["enabled"] is False
        assert policies[0]["channel_number"] == 1

    def test_triggers_from_real_fixture(self, monkeypatch):
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        triggers = result["filtering"]["triggers"]
        assert len(triggers) == 10
        assert triggers[8]["timestamp"] == 1786380766
        assert triggers[8]["enabled"] is False
        assert triggers[0]["timestamp"] == 0

    def test_email_format_from_real_fixture(self, monkeypatch):
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        assert result["email_format"]["available"] == ["AMI-Format", "FixedSubject-Format"]

    def test_adviser_from_real_fixture(self, monkeypatch):
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        adviser = result["adviser"]
        assert adviser["kvm_port"] == 7578
        assert adviser["keyboard_layout"] == "AD"
        assert adviser["web_port"] == 443
        assert adviser["singleport_status"] == 0


class TestSecretHandling:
    """SMTP configuration frequently carries credentials -- prove none ever appears."""

    def test_no_password_shaped_field_in_smtp_output(self, monkeypatch):
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        dumped = json.dumps(result).lower()
        assert "password" not in dumped
        assert "pword" not in dumped

    def test_an_unrecognised_password_shaped_key_is_dropped_not_passed_through(self, monkeypatch):
        # Defends the claim in this module's DOCUMENTATION: this module only ever extracts the
        # keys it names explicitly, so a hypothetical firmware revision that reports an SMTP
        # password under a key this module has never seen still cannot leak it -- the key is
        # simply not read, whatever it is called.
        injected_body = (
            "//Dynamic Data Begin\n"
            " WEBVAR_JSONVAR_GETSMTPCFG = \n"
            " { \n"
            " WEBVAR_STRUCTNAME_GETSMTPCFG : \n"
            " [ \n"
            " { 'CHANNEL_NUM' : 1,'SENDERADDR' : '','MACHINENAME' : '','SMTPENABLE1' : 1,"
            "'SMTPPORT1' : 25,'SMTPSERVER1' : 'mail.example.com','SMTPAUTHENABLE1' : 1,"
            "'USERNAME1' : 'alerts','SMTPPASSWORD1' : 'hunter2-super-secret',"
            "'SMTPENABLE2' : 0,'SMTPPORT2' : 25,'SMTPSERVER2' : '','SMTPAUTHENABLE2' : 0,"
            "'USERNAME2' : '' },  {} ],  \n"
            " HAPI_STATUS:0 }; \n"
            "//Dynamic data end\n"
        )
        fake_asp = _FixtureAsp(overrides={"getsmtpcfg": injected_body})
        _wire_fake_asp(monkeypatch, fake_asp)
        result = _run_ok(dict(BASE_ARGS))
        smtp = result["destinations"]["smtp"]
        assert smtp[0]["primary"]["server"] == "mail.example.com"
        assert smtp[0]["primary"]["username"] == "alerts"
        assert "hunter2-super-secret" not in json.dumps(result)
        assert "SMTPPASSWORD1" not in json.dumps(result)


class TestPerEndpointDegrade:
    def test_a_single_failed_endpoint_degrades_to_none_without_failing_the_module(self, monkeypatch):
        fake_asp = _FixtureAsp(overrides={"getadvisercfg": RemoteOperationError("boom", endpoint="10.0.0.5:443", operation="get_webvar:getadvisercfg")})
        _wire_fake_asp(monkeypatch, fake_asp)
        result = _run_ok(dict(BASE_ARGS))
        assert result["adviser"] is None
        assert result["destinations"]["smtp"] is not None
        reads = result["operation"]["endpoint_reads"]
        assert reads["adviser"]["outcome"] == "failed"
        assert reads["adviser"]["error_class"] == "remote_operation"
        assert reads["smtp"]["outcome"] == "read"

    def test_a_malformed_response_degrades_the_same_way(self, monkeypatch):
        fake_asp = _FixtureAsp(overrides={"gettriggercfg": "not a valid webvar response at all"})
        _wire_fake_asp(monkeypatch, fake_asp)
        result = _run_ok(dict(BASE_ARGS))
        assert result["filtering"]["triggers"] is None
        assert result["operation"]["endpoint_reads"]["triggers"]["outcome"] == "failed"
        assert result["operation"]["endpoint_reads"]["triggers"]["error_class"] == "protocol"


class TestErrorHandling:
    def test_login_failure_fails_the_whole_module(self, monkeypatch):
        fake_asp = _FixtureAsp()

        def _raise_login():
            raise AuthenticationError("rejected", endpoint="10.0.0.5:443", operation="login")

        fake_asp.login = _raise_login
        _wire_fake_asp(monkeypatch, fake_asp)
        result = _run_fail(dict(BASE_ARGS))
        assert result["error_class"] == "authentication"

    def test_missing_requests_dependency_is_an_actionable_failure(self, monkeypatch):
        monkeypatch.setattr(asmb8_alerts, "HAS_REQUESTS", False)
        monkeypatch.setattr(asmb8_alerts, "REQUESTS_IMPORT_ERROR", "No module named 'requests'")
        result = _run_fail(dict(BASE_ARGS))
        assert "requests" in result["msg"]


class TestNoCredentialLeakage:
    def test_no_credential_in_a_failure_result(self, monkeypatch):
        fake_asp = _FixtureAsp()

        def _raise_login():
            raise RemoteOperationError(f"rejected password={PASSWORD}", endpoint="10.0.0.5:443", operation="login", secrets=PASSWORD)

        fake_asp.login = _raise_login
        _wire_fake_asp(monkeypatch, fake_asp)
        result = _run_fail(dict(BASE_ARGS))
        assert PASSWORD not in json.dumps(result)
        assert "[REDACTED]" in result["msg"]


class TestWriteNotImplemented:
    def test_no_write_capable_option_exists(self):
        # Frames the module's own documented promise: nothing in this module's argument spec
        # can request a mutation. There is no `state`, no destination/policy-editing option.
        spec = asmb8_alerts.argument_spec()
        assert "state" not in spec
        connection_only = {
            "host",
            "port",
            "username",
            "password",
            "use_tls",
            "allow_insecure_transport",
            "validate_certs",
            "ca_path",
            "tls_fingerprint",
            "timeout",
            "connect_timeout",
        }
        assert set(spec) == connection_only
