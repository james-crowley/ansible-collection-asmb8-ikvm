# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_auditlog.

Every test here replaces `asmb8_auditlog.build_asp_client` with a fake client (`_FixtureAsp`
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
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_auditlog

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "asp"

PASSWORD = "Sup3rSecret!"

BASE_ARGS = {
    "host": "10.0.0.5",
    "username": "admin",
    "password": PASSWORD,
}

#: The exact literal from tests/unit/fixtures/asp/getauditlog.txt -- one real record whose
#: value contains `[2615 INFO]` *inside* its own quoted string. This is the corpus case
#: module_utils/webvar.py's quote-aware scanner exists for; see this test module's docstring
#: and asmb8_auditlog.py's DOCUMENTATION.
_REAL_AUDIT_ENTRY = "Aug 11 00:48:53 localhost webgo: [2615 INFO]WEBGUI user admin login successfully from 192.0.2.10"


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
    monkeypatch.setattr(asmb8_auditlog, "build_asp_client", lambda params: fake_asp)


def _run_ok(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleExitJson) as excinfo:
        asmb8_auditlog.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_auditlog.main()
    return excinfo.value.args[0]


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_auditlog.argument_spec()["password"]["no_log"] is True

    def test_host_is_required(self):
        assert asmb8_auditlog.argument_spec()["host"]["required"] is True


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
        assert normal["entries"] == checked["entries"]
        assert normal["remote_storage"] == checked["remote_storage"]
        assert normal["logging"] == checked["logging"]
        assert normal["sel_policy"] == checked["sel_policy"]


class TestRealFixtures:
    """End-to-end against the real, checked-in fixture corpus for all four endpoints."""

    def test_entries_from_real_fixture(self, monkeypatch):
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        assert result["entries"] == [_REAL_AUDIT_ENTRY]

    def test_the_embedded_brackets_inside_the_entry_survive_intact(self, monkeypatch):
        # The specific parsing hazard called out in this module's DOCUMENTATION: the real
        # fixture's one entry contains a literal `[2615 INFO]` *inside* its own quoted string
        # value. A parser that naively counted brackets while ignoring quote state would think
        # the surrounding array closed right there, mid-string. This asserts the substring
        # survives end to end through this module, not just through module_utils/webvar.py's
        # own unit tests.
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        entry = result["entries"][0]
        assert "[2615 INFO]" in entry
        assert entry.startswith("Aug 11 00:48:53 localhost webgo: ")
        assert entry.endswith("from 192.0.2.10")

    def test_remote_storage_from_real_fixture(self, monkeypatch):
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        remote_storage = result["remote_storage"]
        assert remote_storage["remote_enabled"] is False
        assert remote_storage["sd_card_enabled"] is False
        assert remote_storage["address"] == ""
        assert remote_storage["remote_path"] == "/home"
        assert remote_storage["username"] == ""
        assert remote_storage["password_configured"] is False
        assert remote_storage["domain_name"] == ""

    def test_logging_from_real_fixture(self, monkeypatch):
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        logging_cfg = result["logging"]
        assert logging_cfg["audit_enabled"] is True
        assert logging_cfg["syslog_enabled"] is True
        assert logging_cfg["file_size"] == 50000
        assert logging_cfg["rotate_count"] == 0
        assert logging_cfg["remote_syslog_address"] == ""

    def test_sel_policy_from_real_fixture(self, monkeypatch):
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        assert result["sel_policy"] == 0


class TestSecretHandling:
    """getauditlogcfg.asp carries a PWORD field -- prove it never appears, even when set."""

    def test_password_field_never_appears_in_output_even_when_blank(self, monkeypatch):
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        dumped = json.dumps(result).lower()
        assert "pword" not in dumped

    def test_a_real_configured_password_is_reduced_to_a_boolean_never_a_value(self, monkeypatch):
        injected_body = (
            "//Dynamic Data Begin\n"
            " WEBVAR_JSONVAR_GETAUDITLOGCFG = \n"
            " { \n"
            " WEBVAR_STRUCTNAME_GETAUDITLOGCFG : \n"
            " [ \n"
            " { 'RM_ENABLE' : 1,'SD_ENABLE' : 0,'REMOTE_STATUS' : 1,'SDCARD_STATUS' : 0,"
            "'IP_ADDR' : '198.51.100.7','REMOTE_PATH' : '/export/audit','SHR_TYPE' : 1,"
            "'UNAME' : 'auditsvc','PWORD' : 'do-not-leak-this-secret','DOMAIN_NAME' : 'EXAMPLE' },  {} ],  \n"
            " HAPI_STATUS:0 }; \n"
            "//Dynamic data end\n"
        )
        fake_asp = _FixtureAsp(overrides={"getauditlogcfg": injected_body})
        _wire_fake_asp(monkeypatch, fake_asp)
        result = _run_ok(dict(BASE_ARGS))
        remote_storage = result["remote_storage"]
        assert remote_storage["password_configured"] is True
        assert remote_storage["username"] == "auditsvc"
        assert remote_storage["address"] == "198.51.100.7"
        dumped = json.dumps(result)
        assert "do-not-leak-this-secret" not in dumped
        assert "PWORD" not in dumped


class TestEntriesAreNotSanitised:
    def test_entries_are_returned_verbatim_not_redacted(self, monkeypatch):
        # This module's DOCUMENTATION is explicit that entries are NOT sanitised -- the username
        # and source address in the real fixture's own entry must survive untouched, unlike a
        # credential, which this collection redacts elsewhere.
        _wire_fake_asp(monkeypatch, _FixtureAsp())
        result = _run_ok(dict(BASE_ARGS))
        entry = result["entries"][0]
        assert "admin" in entry
        assert "192.0.2.10" in entry
        assert "[REDACTED]" not in entry


class TestPerEndpointDegrade:
    def test_a_single_failed_endpoint_degrades_to_none_without_failing_the_module(self, monkeypatch):
        fake_asp = _FixtureAsp(overrides={"getselcfg": RemoteOperationError("boom", endpoint="10.0.0.5:443", operation="get_webvar:getselcfg")})
        _wire_fake_asp(monkeypatch, fake_asp)
        result = _run_ok(dict(BASE_ARGS))
        assert result["sel_policy"] is None
        assert result["entries"] is not None
        reads = result["operation"]["endpoint_reads"]
        assert reads["sel_policy"]["outcome"] == "failed"
        assert reads["sel_policy"]["error_class"] == "remote_operation"
        assert reads["entries"]["outcome"] == "read"

    def test_a_malformed_response_degrades_the_same_way(self, monkeypatch):
        fake_asp = _FixtureAsp(overrides={"getlogcfg": "not a valid webvar response at all"})
        _wire_fake_asp(monkeypatch, fake_asp)
        result = _run_ok(dict(BASE_ARGS))
        assert result["logging"] is None
        assert result["operation"]["endpoint_reads"]["logging"]["outcome"] == "failed"
        assert result["operation"]["endpoint_reads"]["logging"]["error_class"] == "protocol"


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
        monkeypatch.setattr(asmb8_auditlog, "HAS_REQUESTS", False)
        monkeypatch.setattr(asmb8_auditlog, "REQUESTS_IMPORT_ERROR", "No module named 'requests'")
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
        # can clear the audit log or change where it is mirrored to.
        spec = asmb8_auditlog.argument_spec()
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
