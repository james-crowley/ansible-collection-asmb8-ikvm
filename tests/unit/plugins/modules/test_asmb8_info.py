# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_info.

Every test here replaces `asmb8_info.build_ipmi_client`/`build_asp_client` with
fakes -- no test constructs a real `IpmiClient` or `AspClient`, so nothing here
can reach a socket, let alone any real BMC.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import AuthenticationError, ProtocolError, RemoteOperationError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.webvar import parse_webvar
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_info

PASSWORD = "Sup3rSecret!"

BASE_ARGS = {
    "host": "10.0.0.5",
    "username": "admin",
    "password": PASSWORD,
}

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "asp"

#: What a session-expired HTML page (getremotesession.asp's documented, unverified failure mode
#: against a programmatic client -- see asmb8_info's own DOCUMENTATION and
#: asmb8_sessions.py's identical, independently observed gap) looks like to parse_webvar: not the
#: WEBVAR_JSONVAR_ shape at all, so it raises ProtocolError. Used to exercise the degrade-to-None
#: path without needing a real such capture.
SESSION_EXPIRED_HTML = "<html><body>Your session has expired. Please log in again.</body></html>"


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


def _fake_ipmi_client() -> Mock:
    client = Mock()
    client.endpoint = "10.0.0.5:623"
    client.get_power_state.return_value = {"powerstate": "on"}
    client.get_boot_device.return_value = {"bootdev": "default", "persistent": True}
    client.get_mc_info.return_value = "some-mc-identifier"
    return client


def _wire_fake_ipmi_client(monkeypatch, fake_client) -> None:
    monkeypatch.setattr(asmb8_info, "build_ipmi_client", lambda params: fake_client)


def _fake_asp_client_with_fixtures(**fixture_overrides: str) -> Mock:
    """A fake AspClient whose ``get_webvar()`` replays real fixtures through the real parser.

    Defaults to the real, checked-in ``getremotesession.txt``/``getvmediacfg.txt`` fixtures --
    per this collection's rule of testing against captured hardware responses, never invented
    payloads. Pass e.g. ``getremotesession=SESSION_EXPIRED_HTML`` to exercise a specific
    endpoint's degrade/failure path instead.
    """
    fixtures = {
        "getremotesession": _read_fixture("getremotesession.txt"),
        "getvmediacfg": _read_fixture("getvmediacfg.txt"),
    }
    fixtures.update(fixture_overrides)

    client = Mock()
    client.endpoint = "10.0.0.5:443"
    client.login.return_value = "session-cookie-not-real"
    client.get_host_status.return_value = "raw hoststatus.asp body"
    client.get_webvar = Mock(side_effect=lambda endpoint, **_kwargs: parse_webvar(fixtures[endpoint], endpoint=endpoint))
    return client


def _run_ok(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleExitJson) as excinfo:
        asmb8_info.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_info.main()
    return excinfo.value.args[0]


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_info.argument_spec()["password"]["no_log"] is True

    def test_include_web_session_defaults_to_false(self):
        assert asmb8_info.argument_spec()["include_web_session"]["default"] is False

    def test_ipmi_port_defaults_to_623(self):
        assert asmb8_info.argument_spec()["ipmi_port"]["default"] == 623


class TestNeverMutates:
    def test_a_successful_read_always_reports_changed_false(self, monkeypatch):
        fake_client = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        assert result["changed"] is False
        assert result["operation"]["changed"] is False

    def test_check_mode_behaves_identically_to_normal_mode(self, monkeypatch):
        fake_client = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_client)
        normal = _run_ok(dict(BASE_ARGS))
        checked = _run_ok(dict(BASE_ARGS, _ansible_check_mode=True))
        assert normal["asmb8"] == checked["asmb8"]

    def test_never_fetches_a_jnlp(self, monkeypatch):
        # Fetching the KVM/media JNLP allocates a BMC-side session as a side
        # effect (see module_utils/asp.py) -- a read-only module must never do
        # that implicitly. There is no AspClient.allocate_media_session call
        # anywhere in this module; this test pins that down structurally.
        source = inspect.getsource(asmb8_info)
        assert "allocate_media_session" not in source


class TestIpmiFacts:
    def test_gathers_power_boot_device_and_mc_info(self, monkeypatch):
        fake_client = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        asmb8 = result["asmb8"]
        assert asmb8["reachable"] is True
        assert asmb8["ipmi"]["power_state"] == {"powerstate": "on"}
        assert asmb8["ipmi"]["boot_device"] == {"bootdev": "default", "persistent": True}
        assert asmb8["ipmi"]["mc_info"] == "some-mc-identifier"

    def test_mc_info_is_a_bare_string_not_a_dict(self, monkeypatch):
        # The regression this guards: get_mci() is not shaped like
        # get_power()/get_bootdev() -- see module_utils/ipmi.py.
        fake_client = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        assert isinstance(result["asmb8"]["ipmi"]["mc_info"], str)

    def test_a_single_failed_field_degrades_to_none_without_failing_the_module(self, monkeypatch):
        fake_client = _fake_ipmi_client()
        fake_client.get_mc_info.side_effect = RemoteOperationError(
            "IPMI get-management-controller-info failed: bad completion code", endpoint="10.0.0.5:623", operation="get_mci"
        )
        _wire_fake_ipmi_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        asmb8 = result["asmb8"]
        assert asmb8["ipmi"]["mc_info"] is None
        assert asmb8["ipmi"]["power_state"] == {"powerstate": "on"}
        ipmi_reads = result["operation"]["ipmi_reads"]
        assert ipmi_reads["mc_info"]["outcome"] == "failed"
        assert ipmi_reads["mc_info"]["error_class"] == "remote_operation"
        assert ipmi_reads["power_state"]["outcome"] == "read"

    def test_connect_failure_fails_the_whole_module(self, monkeypatch):
        def _raise(_params):
            raise RemoteOperationError("could not connect", endpoint="10.0.0.5:623", operation="ipmi_connect")

        monkeypatch.setattr(asmb8_info, "build_ipmi_client", _raise)
        result = _run_fail(dict(BASE_ARGS))
        assert result["error_class"] == "remote_operation"


class TestCapabilities:
    def test_ipmi_capabilities_are_reported_as_proven(self, monkeypatch):
        fake_client = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        capabilities = result["asmb8"]["capabilities"]
        assert capabilities["ipmi_power"]["supported"] is True
        assert capabilities["ipmi_power"]["proven"] is True
        assert capabilities["ipmi_boot_device"]["proven"] is True
        assert capabilities["ipmi_mc_info"]["proven"] is True

    def test_virtual_media_and_remote_console_are_not_reported_as_available(self, monkeypatch):
        # The requirement this guards: this hardware has NOT been proven to
        # support virtual media or remote console/KVM redirection. Reporting
        # `supported: true` here would be inventing evidence this collection
        # does not have.
        fake_client = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        capabilities = result["asmb8"]["capabilities"]
        assert capabilities["virtual_media"]["supported"] is not True
        assert capabilities["virtual_media"]["proven"] is False
        assert capabilities["remote_console"]["supported"] is not True
        assert capabilities["remote_console"]["proven"] is False

    def test_redfish_is_reported_as_unsupported_and_proven(self, monkeypatch):
        # A confirmed hardware-generation fact (ASPEED AST2400 predates
        # Redfish), not a live probe -- no Redfish endpoint is ever contacted.
        fake_client = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        redfish = result["asmb8"]["capabilities"]["redfish"]
        assert redfish["supported"] is False
        assert redfish["proven"] is True

    def test_port_mode_is_always_unknown(self, monkeypatch):
        fake_client = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        assert result["asmb8"]["media"]["port_mode"] == "unknown"

    def test_web_management_capability_is_unproven_when_not_requested(self, monkeypatch):
        fake_client = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        assert result["asmb8"]["web_management"] is None
        assert result["asmb8"]["capabilities"]["web_management"]["proven"] is False


class TestWebSession:
    def test_not_attempted_by_default(self, monkeypatch):
        fake_client = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_client)
        build_asp = Mock()
        monkeypatch.setattr(asmb8_info, "build_asp_client", build_asp)
        _run_ok(dict(BASE_ARGS))
        build_asp.assert_not_called()

    def test_include_web_session_logs_in_and_reads_host_status(self, monkeypatch):
        fake_ipmi = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_ipmi)
        fake_asp = Mock()
        fake_asp.login.return_value = "session-cookie-not-real"
        fake_asp.get_host_status.return_value = "raw hoststatus.asp body"
        monkeypatch.setattr(asmb8_info, "build_asp_client", lambda params: fake_asp)

        result = _run_ok(dict(BASE_ARGS, include_web_session=True))

        fake_asp.login.assert_called_once()
        web_management = result["asmb8"]["web_management"]
        assert web_management["logged_in"] is True
        assert web_management["host_status_raw"] == "raw hoststatus.asp body"
        assert result["asmb8"]["capabilities"]["web_management"]["proven"] is True

    def test_the_session_cookie_itself_never_appears_in_the_result(self, monkeypatch):
        fake_ipmi = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_ipmi)
        fake_asp = Mock()
        fake_asp.login.return_value = "very-secret-session-cookie-value"
        fake_asp.get_host_status.return_value = "raw body"
        monkeypatch.setattr(asmb8_info, "build_asp_client", lambda params: fake_asp)

        result = _run_ok(dict(BASE_ARGS, include_web_session=True))
        assert "very-secret-session-cookie-value" not in json.dumps(result)

    def test_login_failure_fails_the_whole_module(self, monkeypatch):
        fake_ipmi = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_ipmi)
        fake_asp = Mock()
        fake_asp.login.side_effect = AuthenticationError("rejected", endpoint="10.0.0.5:443", operation="login")
        monkeypatch.setattr(asmb8_info, "build_asp_client", lambda params: fake_asp)

        result = _run_fail(dict(BASE_ARGS, include_web_session=True))
        assert result["error_class"] == "authentication"


class TestErrorHandling:
    def test_missing_pyghmi_dependency_is_an_actionable_failure(self, monkeypatch):
        monkeypatch.setattr(asmb8_info, "HAS_PYGHMI", False)
        monkeypatch.setattr(asmb8_info, "PYGHMI_IMPORT_ERROR", "No module named 'pyghmi'")
        result = _run_fail(dict(BASE_ARGS))
        assert "pyghmi" in result["msg"]

    def test_missing_requests_dependency_is_only_fatal_when_web_session_is_requested(self, monkeypatch):
        monkeypatch.setattr(asmb8_info, "HAS_REQUESTS", False)
        monkeypatch.setattr(asmb8_info, "REQUESTS_IMPORT_ERROR", "No module named 'requests'")
        fake_client = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_client)

        # Not requested: requests being unavailable must not fail this run.
        result = _run_ok(dict(BASE_ARGS))
        assert result["changed"] is False

        # Requested: now it must fail, with an actionable message.
        result = _run_fail(dict(BASE_ARGS, include_web_session=True))
        assert "requests" in result["msg"]


class TestNoCredentialLeakage:
    def test_no_credential_in_a_failure_result(self, monkeypatch):
        def _raise(_params):
            raise RemoteOperationError(f"rejected password={PASSWORD}", endpoint="10.0.0.5:623", operation="ipmi_connect", secrets=PASSWORD)

        monkeypatch.setattr(asmb8_info, "build_ipmi_client", _raise)
        result = _run_fail(dict(BASE_ARGS))
        assert PASSWORD not in json.dumps(result)
        assert "[REDACTED]" in result["msg"]


class TestDecodeMediaSessionCount:
    """The +128 offset, applied to getvmediacfg.asp's V_MAX_CD_SESSIONS/V_ACTIVE_CD_SESSIONS on the
    strength of asmb8_sessions.py's independently-confirmed evidence for the same offset -- see
    that module's DOCUMENTATION and this module's own description."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (129, 1),  # getvmediacfg.asp's V_MAX_CD_SESSIONS -- the same raw value getallservicescfg.asp's own cd-media MAXSESS reports.
            (128, 0),  # V_ACTIVE_CD_SESSIONS baseline: zero active sessions right now.
        ],
    )
    def test_offset_decoding(self, raw, expected):
        assert asmb8_info.decode_media_session_count(raw) == expected

    def test_255_is_a_not_applicable_sentinel_not_127(self):
        assert asmb8_info.decode_media_session_count(255) is None

    def test_none_passes_through(self):
        assert asmb8_info.decode_media_session_count(None) is None


class TestFetchRemoteSessionPreconditions:
    """getremotesession.asp's documented, unverified failure mode: a session-expired-looking
    response even after a fresh login. Must degrade, never raise."""

    def test_a_parseable_fixture_reads_normally(self):
        client = _fake_asp_client_with_fixtures()
        fields, read = asmb8_info.fetch_remote_session_preconditions(client)
        assert fields == {"media_encryption_enabled": False, "attach_raw": 0}
        assert read == {"outcome": "read", "error_class": None}

    def test_a_session_expired_html_page_degrades_to_none_without_raising(self):
        client = _fake_asp_client_with_fixtures(getremotesession=SESSION_EXPIRED_HTML)
        fields, read = asmb8_info.fetch_remote_session_preconditions(client)
        assert fields is None
        assert read == {"outcome": "failed", "error_class": "protocol"}

    def test_a_non_protocol_error_still_propagates(self):
        client = _fake_asp_client_with_fixtures()
        client.get_webvar = Mock(side_effect=AuthenticationError("session expired for real", endpoint="10.0.0.5:443", operation="get_webvar"))
        with pytest.raises(AuthenticationError):
            asmb8_info.fetch_remote_session_preconditions(client)


class TestFetchVmediacfgPreconditions:
    def test_a_parseable_fixture_reads_normally(self):
        client = _fake_asp_client_with_fixtures()
        fields = asmb8_info.fetch_vmediacfg_preconditions(client)
        assert fields["secure_channel_enabled"] is False
        assert fields["license_status_raw"] == 1
        assert fields["device_counts"] == {"cd": 1, "fd": 1, "hd": 1}
        assert fields["sessions"] == {"cd": {"max": 1, "current": 0}}
        assert fields["status_raw"] == 1

    def test_a_session_expired_html_page_raises_rather_than_degrading(self):
        # Unlike getremotesession.asp, getvmediacfg.asp has shown no equivalent quirk in this
        # project's testing -- a parse failure here is a real problem and must not be swallowed.
        client = _fake_asp_client_with_fixtures(getvmediacfg=SESSION_EXPIRED_HTML)
        with pytest.raises(ProtocolError):
            asmb8_info.fetch_vmediacfg_preconditions(client)


class TestMediaPreconditionsOption:
    def test_defaults_to_false(self):
        assert asmb8_info.argument_spec()["include_media_preconditions"]["default"] is False

    def test_requires_include_web_session(self, monkeypatch):
        fake_ipmi = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_ipmi)
        result = _run_fail(dict(BASE_ARGS, include_media_preconditions=True))
        assert "include_web_session" in result["msg"]

    def test_not_read_when_not_requested(self, monkeypatch):
        fake_ipmi = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_ipmi)
        fake_asp = _fake_asp_client_with_fixtures()
        monkeypatch.setattr(asmb8_info, "build_asp_client", lambda params: fake_asp)

        result = _run_ok(dict(BASE_ARGS, include_web_session=True))

        assert result["asmb8"]["media"]["preconditions"] is None
        fake_asp.get_webvar.assert_not_called()

    def test_reads_and_decodes_session_counts_not_raw(self, monkeypatch):
        fake_ipmi = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_ipmi)
        fake_asp = _fake_asp_client_with_fixtures()
        monkeypatch.setattr(asmb8_info, "build_asp_client", lambda params: fake_asp)

        result = _run_ok(dict(BASE_ARGS, include_web_session=True, include_media_preconditions=True))

        preconditions = result["asmb8"]["media"]["preconditions"]
        # The fixture's raw V_MAX_CD_SESSIONS/V_ACTIVE_CD_SESSIONS are 129/128 -- never reported.
        assert preconditions["sessions"]["cd"] == {"max": 1, "current": 0}
        assert "129" not in json.dumps(preconditions)
        assert preconditions["encryption"] == {"media_encryption_enabled": False, "secure_channel_enabled": False}
        assert preconditions["licensing"] == {"license_status_raw": 1}
        assert preconditions["attach"] == {"attach_raw": 0}
        assert preconditions["device_counts"] == {"cd": 1, "fd": 1, "hd": 1}
        assert preconditions["status_raw"] == 1
        assert preconditions["remote_session_read"] == {"outcome": "read", "error_class": None}
        fake_asp.login.assert_called_once()  # One session shared for web_management + preconditions, not two.

    def test_session_expired_getremotesession_degrades_without_failing_the_module(self, monkeypatch):
        fake_ipmi = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_ipmi)
        fake_asp = _fake_asp_client_with_fixtures(getremotesession=SESSION_EXPIRED_HTML)
        monkeypatch.setattr(asmb8_info, "build_asp_client", lambda params: fake_asp)

        result = _run_ok(dict(BASE_ARGS, include_web_session=True, include_media_preconditions=True))

        preconditions = result["asmb8"]["media"]["preconditions"]
        assert preconditions["encryption"]["media_encryption_enabled"] is None
        assert preconditions["attach"]["attach_raw"] is None
        assert preconditions["remote_session_read"]["outcome"] == "failed"
        # getvmediacfg.asp-sourced fields are unaffected by getremotesession.asp's failure.
        assert preconditions["encryption"]["secure_channel_enabled"] is False
        assert preconditions["sessions"]["cd"] == {"max": 1, "current": 0}

    def test_getvmediacfg_failure_fails_the_whole_module(self, monkeypatch):
        fake_ipmi = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_ipmi)
        fake_asp = _fake_asp_client_with_fixtures(getvmediacfg=SESSION_EXPIRED_HTML)
        monkeypatch.setattr(asmb8_info, "build_asp_client", lambda params: fake_asp)

        result = _run_fail(dict(BASE_ARGS, include_web_session=True, include_media_preconditions=True))
        assert result["error_class"] == "protocol"


class TestBackwardCompatibleOutputSurface:
    """Pins the published surface: every key that existed before include_media_preconditions must
    still be present with the same meaning. New data is additive only."""

    def test_default_run_output_is_unchanged(self, monkeypatch):
        fake_client = _fake_ipmi_client()
        _wire_fake_ipmi_client(monkeypatch, fake_client)

        result = _run_ok(dict(BASE_ARGS))
        asmb8 = result["asmb8"]

        assert asmb8["reachable"] is True
        assert asmb8["ipmi"] == {
            "power_state": {"powerstate": "on"},
            "boot_device": {"bootdev": "default", "persistent": True},
            "mc_info": "some-mc-identifier",
        }
        assert asmb8["web_management"] is None
        assert asmb8["media"]["port_mode"] == "unknown"
        assert asmb8["capabilities"]["ipmi_power"]["proven"] is True
        assert asmb8["capabilities"]["virtual_media"]["supported"] is None
        assert asmb8["capabilities"]["virtual_media"]["proven"] is False
        assert asmb8["capabilities"]["redfish"] == {
            "supported": False,
            "proven": True,
            "note": asmb8_info.build_capabilities(web_management=None, include_web_session=False)["redfish"]["note"],
        }
        assert result["operation"]["schema"] == "asmb8-ikvm-operation/v1"
        assert result["operation"]["action"] == "get_facts"
        assert result["operation"]["changed"] is False
        assert result["operation"]["error_class"] is None
        assert set(result["operation"]["ipmi_reads"]) == {"power_state", "boot_device", "mc_info"}

        # The only change: an additive, always-None-by-default key.
        assert "preconditions" in asmb8["media"]
        assert asmb8["media"]["preconditions"] is None
