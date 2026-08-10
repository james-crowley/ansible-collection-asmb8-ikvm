# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_users.

Every test that exercises the module end-to-end wires a real `AspClient` to canned HTTP responses
built from the real, redacted fixtures under `tests/unit/fixtures/asp/` -- only
`requests.Session.request` is mocked (the same boundary `test_asp.py` mocks at), so the real
`parse_webvar`/`AspClient.get_webvar`/login logic all run for real against real captured bytes.
Nothing here opens a socket or talks to any BMC.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import AspClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import AuthenticationError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.webvar import parse_webvar
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_users

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "asp"

PASSWORD = "Sup3rSecret!"

BASE_ARGS = {
    "host": "198.51.100.10",
    "username": "admin",
    "password": PASSWORD,
}

#: A synthetic getalluserinfo.asp-shaped body carrying values shaped like real secrets/PII in the
#: EmailID and SSHKeyInfo fields -- both empty or "Not Available" in every real corpus sample, so
#: this is the only way to prove those fields are stripped rather than merely happening to be
#: empty in the one fixture on disk. Clearly synthetic, not a real capture.
_SECRET_EMAIL = "admin@example.com"
_SECRET_SSH_KEY = "ssh-rsa AAAAB3NzaC1yc2EA-SUPER-SECRET-KEY-MATERIAL"
SYNTHETIC_USERINFO_WITH_SECRETS = (
    "\n//Dynamic Data Begin\n"
    " WEBVAR_JSONVAR_HL_GETALLUSERINFO = \n"
    " { \n"
    " WEBVAR_STRUCTNAME_HL_GETALLUSERINFO : \n"
    " [ \n"
    " { 'UserName' : 'admin','UserStatus' : 1,'PrivLimit_Network' : 84,'KVMPriv' : 1,'VMediaPriv' : 1,"
    "'PrivLimit_Serial' : 84,'FixedUserCount' : 2,'SNMPStatus' : 0,'SNMPAccess' : 0,'AUTHProtocol' : 0,"
    "'PrivProtocol' : 0,'EmailID' : '" + _SECRET_EMAIL + "','EmailFormat' : 'AMI-Format','SOL_Status' : 2,"
    "'SSHKeyStatus' : 1,'SSHKeyInfo' : '" + _SECRET_SSH_KEY + "' },  {} ],  \n"
    " HAPI_STATUS:0 }; \n"
    "//Dynamic data end\n"
)


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


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
        asmb8_users.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_users.main()
    return excinfo.value.args[0]


def mock_response(text: str) -> Mock:
    response = Mock(spec=requests.Response)
    response.text = text
    return response


def _dispatch(fixture_map: dict[str, str], login_ok: bool = True):
    def _request(method, url, **_kwargs):
        if url.endswith("/rpc/WEBSES/create.asp"):
            if login_ok:
                return mock_response("{'SESSION_COOKIE':'test-session-cookie'}")
            return mock_response("{'SESSION_COOKIE':'Failure_Login_Bad_Password'}")
        for endpoint, text in fixture_map.items():
            if url.endswith(f"/rpc/{endpoint}.asp"):
                return mock_response(text)
        raise AssertionError(f"unexpected request: {method} {url}")

    return _request


def build_client_with_fixtures(fixture_map: dict[str, str], *, login_ok: bool = True) -> AspClient:
    """A real `AspClient` whose transport is mocked at `requests.Session.request` only.

    Every field this module reports therefore passes through the real `parse_webvar` and
    `AspClient.get_webvar`/`login` code paths against real (or, where noted, clearly synthetic)
    `.asp` response bytes.
    """
    client = AspClient(host="198.51.100.10", password="unused-in-tests", use_tls=False, allow_insecure_transport=True)
    client._http_session.request = Mock(side_effect=_dispatch(fixture_map, login_ok=login_ok))
    return client


DEFAULT_FIXTURES = {
    "getalluserinfo": lambda: _read_fixture("getalluserinfo.txt"),
    "getrole": lambda: _read_fixture("getrole.txt"),
    "getallrolegroupcfg": lambda: _read_fixture("getallrolegroupcfg.txt"),
}


def _default_client(*, login_ok: bool = True, **fixture_overrides) -> AspClient:
    fixture_map = {name: loader() for name, loader in DEFAULT_FIXTURES.items()}
    fixture_map.update(fixture_overrides)
    return build_client_with_fixtures(fixture_map, login_ok=login_ok)


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_users.argument_spec()["password"]["no_log"] is True

    def test_port_defaults_to_443(self):
        assert asmb8_users.argument_spec()["port"]["default"] == 443

    def test_no_ipmi_port_option(self):
        # This module never touches IPMI -- unlike asmb8_info, there is no ipmi_port option.
        assert "ipmi_port" not in asmb8_users.argument_spec()


class TestFixtureCorpusSanity:
    """Pins this module's understanding of the real corpus shape before testing decode logic against it."""

    def test_getalluserinfo_has_three_configured_and_seven_empty_slots(self):
        response = parse_webvar(_read_fixture("getalluserinfo.txt"))
        configured = [r for r in response.records if r.get("UserName")]
        assert len(response.records) == 10
        assert len(configured) == 3
        assert {r["UserName"] for r in configured} == {"anonymous", "admin", "root"}

    def test_getrole_has_one_record(self):
        response = parse_webvar(_read_fixture("getrole.txt"))
        assert response.records == [{"CURUSERNAME": "admin", "CURPRIV": 4, "EXTENDED_PRIV": 259}]

    def test_getallrolegroupcfg_has_five_unconfigured_slots(self):
        response = parse_webvar(_read_fixture("getallrolegroupcfg.txt"))
        assert len(response.records) == 5
        assert all(r["ROLEGROUP_NAME"] == "" for r in response.records)


class TestDecodeUserSlot:
    def _record(self, username: str) -> dict:
        response = parse_webvar(_read_fixture("getalluserinfo.txt"))
        return next(r for r in response.records if r.get("UserName") == username)

    def test_admin_is_enabled_with_raw_priv_limits_preserved(self):
        decoded = asmb8_users.decode_user_slot(self._record("admin"))
        assert decoded["username"] == "admin"
        assert decoded["enabled"] is True
        assert decoded["user_status_raw"] == 1
        assert decoded["network_privilege_limit_raw"] == 84
        assert decoded["serial_privilege_limit_raw"] == 84

    def test_anonymous_is_disabled(self):
        decoded = asmb8_users.decode_user_slot(self._record("anonymous"))
        assert decoded["enabled"] is False
        assert decoded["user_status_raw"] == 0

    def test_root_has_a_distinct_priv_limit(self):
        decoded = asmb8_users.decode_user_slot(self._record("root"))
        assert decoded["network_privilege_limit_raw"] == 52
        assert decoded["serial_privilege_limit_raw"] == 52

    def test_ssh_key_not_available_decodes_to_not_configured(self):
        decoded = asmb8_users.decode_user_slot(self._record("admin"))
        assert decoded["ssh_key_configured"] is False
        assert "SSHKeyInfo" not in decoded
        assert "Not Available" not in json.dumps(decoded)

    def test_no_email_decodes_to_not_configured(self):
        decoded = asmb8_users.decode_user_slot(self._record("admin"))
        assert decoded["email_configured"] is False
        assert "EmailID" not in decoded

    def test_kvm_and_vmedia_privilege_are_booleans(self):
        decoded = asmb8_users.decode_user_slot(self._record("admin"))
        assert decoded["kvm_privilege"] is True
        assert decoded["vmedia_privilege"] is True


class TestSecretFieldsAreNeverReturnedRaw:
    """The most important guarantee this module makes: EmailID and SSHKeyInfo text never survives decoding."""

    def _decoded_admin_with_secrets(self) -> dict:
        response = parse_webvar(SYNTHETIC_USERINFO_WITH_SECRETS)
        record = next(r for r in response.records if r.get("UserName") == "admin")
        return asmb8_users.decode_user_slot(record)

    def test_email_address_value_is_absent_from_the_decoded_dict(self):
        decoded = self._decoded_admin_with_secrets()
        assert decoded["email_configured"] is True
        assert _SECRET_EMAIL not in json.dumps(decoded)

    def test_ssh_key_material_is_absent_from_the_decoded_dict(self):
        decoded = self._decoded_admin_with_secrets()
        assert decoded["ssh_key_configured"] is True
        assert _SECRET_SSH_KEY not in json.dumps(decoded)

    def test_neither_secret_survives_a_full_module_run(self, monkeypatch):
        client = _default_client(getalluserinfo=SYNTHETIC_USERINFO_WITH_SECRETS)
        monkeypatch.setattr(asmb8_users, "build_asp_client", lambda params: client)
        result = _run_ok(dict(BASE_ARGS))
        rendered = json.dumps(result)
        assert _SECRET_EMAIL not in rendered
        assert _SECRET_SSH_KEY not in rendered
        assert result["users"][0]["ssh_key_configured"] is True
        assert result["users"][0]["email_configured"] is True


class TestBuildUsersReport:
    def test_empty_slots_are_excluded_from_users_but_counted_in_slots(self):
        response = parse_webvar(_read_fixture("getalluserinfo.txt"))
        report = asmb8_users.build_users_report(response.records)
        assert {u["username"] for u in report["users"]} == {"anonymous", "admin", "root"}
        assert report["slots"] == {"total": 10, "configured": 3, "empty": 7}


class TestBuildCurrentSessionReport:
    def test_matches_the_getrole_fixture(self):
        response = parse_webvar(_read_fixture("getrole.txt"))
        report = asmb8_users.build_current_session_report(response.records)
        assert report == {"username": "admin", "privilege_raw": 4, "extended_privilege_raw": 259}

    def test_no_records_yields_none(self):
        assert asmb8_users.build_current_session_report([]) is None


class TestBuildRoleGroupsReport:
    def test_every_group_in_the_fixture_is_unconfigured(self):
        response = parse_webvar(_read_fixture("getallrolegroupcfg.txt"))
        report = asmb8_users.build_role_groups_report(response.records)
        assert len(report) == 5
        assert all(g["configured"] is False for g in report)
        assert all(g["name"] is None for g in report)
        assert [g["id"] for g in report] == [1, 2, 3, 4, 5]


class TestMainReadOnlyDefault:
    def test_default_run_reports_users_slots_session_and_role_groups(self, monkeypatch):
        client = _default_client()
        monkeypatch.setattr(asmb8_users, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS))

        assert result["changed"] is False
        assert {u["username"] for u in result["users"]} == {"anonymous", "admin", "root"}
        assert result["slots"] == {"total": 10, "configured": 3, "empty": 7}
        assert result["current_session"] == {"username": "admin", "privilege_raw": 4, "extended_privilege_raw": 259}
        assert len(result["role_groups"]) == 5
        assert result["operation"]["schema"] == "asmb8-ikvm-operation/v1"
        assert result["operation"]["action"] == "asmb8_users.report"
        assert result["operation"]["changed"] is False
        assert result["operation"]["error_class"] is None

    def test_login_is_actually_called(self, monkeypatch):
        client = _default_client()
        login_spy = Mock(wraps=client.login)
        monkeypatch.setattr(client, "login", login_spy)
        monkeypatch.setattr(asmb8_users, "build_asp_client", lambda params: client)

        _run_ok(dict(BASE_ARGS))
        login_spy.assert_called_once()

    def test_endpoint_is_host_and_port(self, monkeypatch):
        client = _default_client()
        monkeypatch.setattr(asmb8_users, "build_asp_client", lambda params: client)
        result = _run_ok(dict(BASE_ARGS))
        assert result["operation"]["endpoint"] == client.endpoint


class TestCheckMode:
    def test_check_mode_never_logs_in_or_reads(self, monkeypatch):
        build_asp = Mock(side_effect=AssertionError("check mode must never build a client"))
        monkeypatch.setattr(asmb8_users, "build_asp_client", build_asp)

        result = _run_ok(dict(BASE_ARGS, _ansible_check_mode=True))

        assert result["changed"] is False
        assert result["users"] is None
        assert result["slots"] is None
        assert result["current_session"] is None
        assert result["role_groups"] is None
        build_asp.assert_not_called()


class TestErrorHandling:
    def test_login_failure_fails_the_whole_module(self, monkeypatch):
        client = _default_client(login_ok=False)
        monkeypatch.setattr(asmb8_users, "build_asp_client", lambda params: client)
        result = _run_fail(dict(BASE_ARGS))
        assert result["error_class"] == "authentication"

    def test_missing_requests_dependency_is_fatal(self, monkeypatch):
        monkeypatch.setattr(asmb8_users, "HAS_REQUESTS", False)
        monkeypatch.setattr(asmb8_users, "REQUESTS_IMPORT_ERROR", "No module named 'requests'")
        result = _run_fail(dict(BASE_ARGS))
        assert "requests" in result["msg"]


class TestNoCredentialLeakage:
    def test_password_never_appears_in_a_failure_result(self, monkeypatch):
        def _raise(_params):
            raise AuthenticationError(f"rejected password={PASSWORD}", endpoint="198.51.100.10:443", operation="login", secrets=PASSWORD)

        monkeypatch.setattr(asmb8_users, "build_asp_client", _raise)
        result = _run_fail(dict(BASE_ARGS))
        assert PASSWORD not in json.dumps(result)
        assert "[REDACTED]" in result["msg"]

    def test_password_never_appears_in_a_successful_result(self, monkeypatch):
        client = _default_client()
        monkeypatch.setattr(asmb8_users, "build_asp_client", lambda params: client)
        result = _run_ok(dict(BASE_ARGS))
        assert PASSWORD not in json.dumps(result)


class TestNeverCallsUnimplementedEndpoints:
    def test_source_never_names_an_endpoint_outside_this_modules_documented_three(self):
        # A structural guard against silently growing extra endpoint calls that were never
        # reviewed for the same secret-handling care the documented three received.
        source = inspect.getsource(asmb8_users)
        for endpoint in ("getalluserinfo", "getrole", "getallrolegroupcfg"):
            assert f'"{endpoint}"' in source
