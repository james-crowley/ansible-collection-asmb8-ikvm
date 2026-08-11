# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_sessions.

Same discipline as `test_asmb8_users.py`/`test_asmb8_network.py`: every end-to-end test wires a
real `AspClient` to canned HTTP responses built from the real, redacted fixtures under
`tests/unit/fixtures/asp/`, mocking only `requests.Session.request`. Nothing here opens a socket or
talks to any BMC.

`getsessioninfo.asp` is read through `AspClient.post_webvar()` (POST, SERVICEBIT) only when
O(active_session_services) is given -- see `TestActiveSessionServices` below, and
`TestActiveSessionsNeverViaGetWebvar` for the structural pin that it is never reached through the
GET-only `get_webvar()`, no matter what is requested.
"""

from __future__ import annotations

import ast
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
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_sessions

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "asp"

PASSWORD = "Sup3rSecret!"

BASE_ARGS = {
    "host": "198.51.100.10",
    "username": "admin",
    "password": PASSWORD,
}

#: What a session-expired HTML page (this module's documented, unverified getremotesession.asp
#: failure mode) looks like to parse_webvar: not the WEBVAR_JSONVAR_ shape at all, so it raises
#: ProtocolError. Used to exercise the degrade-to-None path without needing a real such capture.
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


def _run_ok(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleExitJson) as excinfo:
        asmb8_sessions.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_sessions.main()
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
    client = AspClient(host="198.51.100.10", password="unused-in-tests", use_tls=False, allow_insecure_transport=True)
    client._http_session.request = Mock(side_effect=_dispatch(fixture_map, login_ok=login_ok))
    return client


DEFAULT_FIXTURES = {
    "getallservicescfg": lambda: _read_fixture("getallservicescfg.txt"),
    "getremotesession": lambda: _read_fixture("getremotesession.txt"),
}


def _default_client(*, login_ok: bool = True, **fixture_overrides) -> AspClient:
    fixture_map = {name: loader() for name, loader in DEFAULT_FIXTURES.items()}
    fixture_map.update(fixture_overrides)
    return build_client_with_fixtures(fixture_map, login_ok=login_ok)


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_sessions.argument_spec()["password"]["no_log"] is True

    def test_port_defaults_to_443(self):
        assert asmb8_sessions.argument_spec()["port"]["default"] == 443


class TestDecodeSessionCount:
    """The +128 offset, per this module's cited evidence: web 148->20, cd-media 129->1."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (148, 20),  # web MAXSESS, matches errors.py's documented 20-session cap.
            (129, 1),  # cd-media/fd-media/hd-media MAXSESS, matches asmb8_redirection's catalog.
            (132, 4),  # kvm MAXSESS, matches asmb8_redirection's catalog.
            (128, 0),  # CURSESS baseline: zero active sessions of that kind right now.
        ],
    )
    def test_offset_decoding(self, raw, expected):
        assert asmb8_sessions.decode_session_count(raw) == expected

    def test_255_is_a_not_applicable_sentinel_not_127(self):
        # ssh/telnet report MAXSESS=CURSESS=255 in the corpus. Decoding with the offset would give
        # 127, which asmb8_redirection's own catalog contradicts (no session cap at all for either
        # service) -- see the module description for why this is a distinct sentinel.
        assert asmb8_sessions.decode_session_count(255) is None

    def test_none_passes_through(self):
        assert asmb8_sessions.decode_session_count(None) is None


class TestDecodeUint32OrNone:
    def test_sentinel_decodes_to_none(self):
        assert asmb8_sessions.decode_uint32_or_none(4294967295) is None

    def test_ordinary_value_passes_through(self):
        assert asmb8_sessions.decode_uint32_or_none(443) == 443


class TestBuildServicesReport:
    def test_matches_the_fixture_for_every_service(self):
        response = parse_webvar(_read_fixture("getallservicescfg.txt"))
        services = asmb8_sessions.build_services_report(response.records)

        assert set(services) == {"web", "kvm", "cd-media", "fd-media", "hd-media", "ssh", "telnet"}

        assert services["web"]["sessions"] == {"max": 20, "current": 0}
        assert services["web"]["port"] == {"plain": 80, "secure": 443}
        assert services["web"]["timeout_seconds"] == 1800
        assert services["web"]["enabled"] is True

        assert services["kvm"]["sessions"] == {"max": 4, "current": 0}

        for name in ("cd-media", "fd-media", "hd-media"):
            assert services[name]["sessions"] == {"max": 1, "current": 0}
            assert services[name]["timeout_seconds"] is None  # 4294967295 sentinel -> no timeout.

        assert services["ssh"]["sessions"] == {"max": None, "current": None}  # 255 sentinel.
        assert services["ssh"]["port"] == {"plain": None, "secure": 22}  # NSPORT sentinel -> no plaintext port.
        assert services["ssh"]["enabled"] is True

        assert services["telnet"]["sessions"] == {"max": None, "current": None}
        assert services["telnet"]["port"] == {"plain": 23, "secure": None}  # SECPORT sentinel -> no secure port.
        assert services["telnet"]["enabled"] is False  # STATE=0 in the corpus.

    def test_web_session_cap_matches_the_independently_measured_20_session_limit(self):
        # Cross-check against errors.py's ErrorClass.BMC_BUSY docstring, which cites 20 as this
        # board's independently-measured concurrent web-session cap.
        response = parse_webvar(_read_fixture("getallservicescfg.txt"))
        services = asmb8_sessions.build_services_report(response.records)
        assert services["web"]["sessions"]["max"] == 20


class TestDecodeRemoteSessionConfig:
    def test_matches_the_fixture(self):
        response = parse_webvar(_read_fixture("getremotesession.txt"))
        decoded = asmb8_sessions.decode_remote_session_config(response.records[0])
        assert decoded == {
            "kvm_encryption_enabled": False,
            "media_encryption_enabled": False,
            "single_port_enabled": False,
            "keyboard_language": "AD",
            "local_media_enabled": False,
            "remote_media_enabled": False,
            "vmedia_attach_raw": 0,
            "host_lock_enabled": True,
            "host_lock_auto_enabled": False,
            "sd_card_status_raw": 0,
        }


class TestFetchRemoteSessionConfig:
    """The documented, unverified getremotesession.asp failure mode: a session-expired-looking
    response even after a fresh login. This must degrade, never raise or fail the module."""

    def test_a_parseable_fixture_reads_normally(self):
        client = build_client_with_fixtures({"getremotesession": _read_fixture("getremotesession.txt")})
        client.login()
        remote_session, read = asmb8_sessions.fetch_remote_session_config(client)
        assert remote_session is not None
        assert read == {"outcome": "read", "error_class": None}

    def test_a_session_expired_html_page_degrades_to_none_without_raising(self):
        client = build_client_with_fixtures({"getremotesession": SESSION_EXPIRED_HTML})
        client.login()
        remote_session, read = asmb8_sessions.fetch_remote_session_config(client)
        assert remote_session is None
        assert read == {"outcome": "failed", "error_class": "protocol"}

    def test_a_non_protocol_error_still_propagates(self):
        client = build_client_with_fixtures({})
        client.login()

        def _raise_something_else(_endpoint):
            raise AuthenticationError("session expired for real", endpoint=client.endpoint, operation="get_webvar:getremotesession")

        client.get_webvar = _raise_something_else
        with pytest.raises(AuthenticationError):
            asmb8_sessions.fetch_remote_session_config(client)

    def test_the_real_reconstructed_session_expired_fixture_degrades_the_same_way(self):
        # tests/unit/fixtures/asp/session_expired.html (see that directory's README) is this
        # collection's own reconstruction of GitHub issue #5's documented HTML shape -- confirms
        # this degrade path handles it exactly like the inline SESSION_EXPIRED_HTML constant above,
        # not just that ad hoc string, and that the reason recorded is still "protocol" (accurate:
        # module_utils/asp.py's looks_like_session_expired_html() now gives that ProtocolError a
        # specific message naming the shape, rather than leaving the degrade reason vague).
        client = build_client_with_fixtures({"getremotesession": _read_fixture("session_expired.html")})
        client.login()
        remote_session, read = asmb8_sessions.fetch_remote_session_config(client)
        assert remote_session is None
        assert read == {"outcome": "failed", "error_class": "protocol"}


class TestMainReadOnlyDefault:
    def test_default_run_reports_services_and_remote_session(self, monkeypatch):
        client = _default_client()
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS))

        assert result["changed"] is False
        assert set(result["services"]) == {"web", "kvm", "cd-media", "fd-media", "hd-media", "ssh", "telnet"}
        assert result["remote_session"]["keyboard_language"] == "AD"
        assert result["remote_session_read"] == {"outcome": "read", "error_class": None}
        assert result["active_sessions"] is None
        assert result["operation"]["action"] == "asmb8_sessions.report"
        assert result["operation"]["error_class"] is None

    def test_session_expired_remote_session_read_does_not_fail_the_module(self, monkeypatch):
        client = _default_client(getremotesession=SESSION_EXPIRED_HTML)
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS))

        assert result["changed"] is False
        assert result["remote_session"] is None
        assert result["remote_session_read"]["outcome"] == "failed"
        # Services must still be reported even though the remote-session read failed.
        assert set(result["services"]) == {"web", "kvm", "cd-media", "fd-media", "hd-media", "ssh", "telnet"}

    def test_endpoint_is_host_and_port(self, monkeypatch):
        client = _default_client()
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", lambda params: client)
        result = _run_ok(dict(BASE_ARGS))
        assert result["operation"]["endpoint"] == client.endpoint


class TestCheckMode:
    def test_check_mode_never_logs_in_or_reads(self, monkeypatch):
        build_asp = Mock(side_effect=AssertionError("check mode must never build a client"))
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", build_asp)

        result = _run_ok(dict(BASE_ARGS, _ansible_check_mode=True))

        assert result["changed"] is False
        assert result["services"] is None
        assert result["remote_session"] is None
        assert result["active_sessions"] is None
        build_asp.assert_not_called()


class TestErrorHandling:
    def test_login_failure_fails_the_whole_module(self, monkeypatch):
        client = _default_client(login_ok=False)
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", lambda params: client)
        result = _run_fail(dict(BASE_ARGS))
        assert result["error_class"] == "authentication"

    def test_missing_requests_dependency_is_fatal(self, monkeypatch):
        monkeypatch.setattr(asmb8_sessions, "HAS_REQUESTS", False)
        monkeypatch.setattr(asmb8_sessions, "REQUESTS_IMPORT_ERROR", "No module named 'requests'")
        result = _run_fail(dict(BASE_ARGS))
        assert "requests" in result["msg"]


class TestNoCredentialLeakage:
    def test_password_never_appears_in_a_failure_result(self, monkeypatch):
        def _raise(_params):
            raise AuthenticationError(f"rejected password={PASSWORD}", endpoint="198.51.100.10:443", operation="login", secrets=PASSWORD)

        monkeypatch.setattr(asmb8_sessions, "build_asp_client", _raise)
        result = _run_fail(dict(BASE_ARGS))
        assert PASSWORD not in json.dumps(result)
        assert "[REDACTED]" in result["msg"]

    def test_password_never_appears_in_a_successful_result(self, monkeypatch):
        client = _default_client()
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", lambda params: client)
        result = _run_ok(dict(BASE_ARGS))
        assert PASSWORD not in json.dumps(result)


#: The real getsessioninfo_post_servicebit4.txt capture's one session record, decoded -- see
#: tests/unit/fixtures/asp/README.md's "POST-parameterized reads" section for provenance.
_EXPECTED_CD_MEDIA_SESSION = {
    "service": "cd-media",
    "session_id_raw": 24,
    "session_type_raw": 7,
    "user_id_raw": 2,
    "username": "admin",
    "ip_address": "192.0.2.10",
    "privilege_raw": 4,
}


def _client_with_active_sessions(**fixture_overrides) -> AspClient:
    fixture_map = {name: loader() for name, loader in DEFAULT_FIXTURES.items()}
    fixture_map["getsessioninfo"] = _read_fixture("getsessioninfo_post_servicebit4.txt")
    fixture_map.update(fixture_overrides)
    return build_client_with_fixtures(fixture_map)


class TestKnownServiceIds:
    def test_builds_the_mapping_straight_from_getallservicescfg_records(self):
        response = parse_webvar(_read_fixture("getallservicescfg.txt"))
        ids = asmb8_sessions.known_service_ids(response.records)
        assert ids["cd-media"] == 4  # the one SERVICEBIT this collection has independently confirmed
        assert ids["web"] == 1
        assert set(ids) == {"web", "kvm", "cd-media", "fd-media", "hd-media", "ssh", "telnet"}


class TestResolveRequestedSessionServices:
    KNOWN = {"web": 1, "kvm": 2, "cd-media": 4}

    def test_none_or_empty_returns_no_services(self):
        assert asmb8_sessions.resolve_requested_session_services(None, self.KNOWN) == []
        assert asmb8_sessions.resolve_requested_session_services([], self.KNOWN) == []

    def test_specific_names_are_returned_deduplicated_preserving_order(self):
        resolved = asmb8_sessions.resolve_requested_session_services(["cd-media", "web", "cd-media"], self.KNOWN)
        assert resolved == ["cd-media", "web"]

    def test_all_resolves_to_every_known_name(self):
        resolved = asmb8_sessions.resolve_requested_session_services(["all"], self.KNOWN)
        assert set(resolved) == set(self.KNOWN)

    def test_all_short_circuits_even_when_combined_with_other_names(self):
        resolved = asmb8_sessions.resolve_requested_session_services(["web", "all"], self.KNOWN)
        assert set(resolved) == set(self.KNOWN)

    def test_unknown_name_raises_value_error_naming_it(self):
        with pytest.raises(ValueError, match="typo-service"):
            asmb8_sessions.resolve_requested_session_services(["typo-service"], self.KNOWN)


class TestDecodeSessionRecord:
    """IPADDRESS/UNAME are returned deliberately here -- see the module description for why that
    is the opposite of asmb8_users' EmailID/SSHKeyInfo choice and is not an inconsistency."""

    def test_matches_the_real_capture_field_for_field(self):
        record = {"SID": 24, "STYPE": 7, "IPADDRESS": "192.0.2.10", "UID": 2, "UNAME": "admin", "UPRIV": 4}
        assert asmb8_sessions.decode_session_record(record, service_name="cd-media") == _EXPECTED_CD_MEDIA_SESSION

    def test_empty_ip_address_or_username_decodes_to_none(self):
        decoded = asmb8_sessions.decode_session_record({"IPADDRESS": "", "UNAME": ""}, service_name="web")
        assert decoded["ip_address"] is None
        assert decoded["username"] is None

    def test_upriv_is_returned_raw_undecoded(self):
        decoded = asmb8_sessions.decode_session_record({"UPRIV": 4}, service_name="web")
        assert decoded["privilege_raw"] == 4


class TestFetchActiveSessions:
    def test_posts_servicebit_derived_from_the_live_service_id_mapping(self):
        client = Mock()
        client.post_webvar.return_value = parse_webvar(_read_fixture("getsessioninfo_post_servicebit4.txt"))

        sessions = asmb8_sessions.fetch_active_sessions(client, ["cd-media"], {"cd-media": 4})

        client.post_webvar.assert_called_once_with("getsessioninfo", data={"SERVICEBIT": "4"})
        assert sessions == [_EXPECTED_CD_MEDIA_SESSION]

    def test_source_uses_post_webvar_and_never_get_webvar(self):
        # AST-based (Attribute nodes only), not a raw substring search over the function's text:
        # its own docstring names get_webvar() in prose to explain why it is *not* used, which a
        # plain `"get_webvar" not in source` check would wrongly trip on.
        tree = ast.parse(inspect.getsource(asmb8_sessions.fetch_active_sessions))
        called_attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert "post_webvar" in called_attrs
        assert "get_webvar" not in called_attrs


class TestActiveSessionServices:
    """End-to-end O(active_session_services), against the real, redacted
    getsessioninfo_post_servicebit4.txt capture."""

    def test_omitted_leaves_active_sessions_null_unchanged_from_before(self, monkeypatch):
        client = _default_client()
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS))

        assert result["active_sessions"] is None
        assert result["active_sessions_queried"] == []

    def test_a_specific_service_reads_via_post_webvar(self, monkeypatch):
        client = _client_with_active_sessions()
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS, active_session_services=["cd-media"]))

        assert result["active_sessions_queried"] == ["cd-media"]
        assert result["active_sessions"] == [_EXPECTED_CD_MEDIA_SESSION]

    def test_all_queries_every_service_getallservicescfg_reported(self, monkeypatch):
        client = _client_with_active_sessions()
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS, active_session_services=["all"]))

        assert set(result["active_sessions_queried"]) == {"web", "kvm", "cd-media", "fd-media", "hd-media", "ssh", "telnet"}
        # Every service's getsessioninfo.asp read is answered by the same canned fixture here, so
        # every queried service contributes exactly one session record, tagged with its own name.
        assert len(result["active_sessions"]) == len(result["active_sessions_queried"])
        assert {entry["service"] for entry in result["active_sessions"]} == set(result["active_sessions_queried"])

    def test_unknown_service_name_fails_the_module_after_login_not_before(self, monkeypatch):
        client = _default_client()
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", lambda params: client)

        result = _run_fail(dict(BASE_ARGS, active_session_services=["not-a-real-service"]))

        assert "not-a-real-service" in result["msg"]
        assert "error_class" not in result  # a plain ValueError, not an IkvmError -- see main()

    def test_unknown_service_name_still_performs_a_real_login_first(self, monkeypatch):
        # Companion to the test above: uses the real, fixture-backed client (not a bare Mock) to
        # prove the failure genuinely happens after login + getallservicescfg.asp, not before.
        client = _default_client()
        login_spy = Mock(wraps=client.login)
        client.login = login_spy
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", lambda params: client)

        _run_fail(dict(BASE_ARGS, active_session_services=["not-a-real-service"]))

        login_spy.assert_called_once()

    def test_check_mode_never_reads_active_sessions_either(self, monkeypatch):
        build_asp = Mock(side_effect=AssertionError("check mode must never build a client"))
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", build_asp)

        result = _run_ok(dict(BASE_ARGS, active_session_services=["all"], _ansible_check_mode=True))

        assert result["active_sessions"] is None
        assert result["active_sessions_queried"] is None
        build_asp.assert_not_called()

    def test_no_credential_leakage_with_active_sessions_populated(self, monkeypatch):
        client = _client_with_active_sessions()
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", lambda params: client)
        result = _run_ok(dict(BASE_ARGS, active_session_services=["cd-media"]))
        assert PASSWORD not in json.dumps(result)


class TestActiveSessionsNeverViaGetWebvar:
    """Structural pin: no matter what O(active_session_services) requests, getsessioninfo.asp must
    only ever be reached through post_webvar(), never through the GET-only get_webvar() -- see
    AspClient.get_webvar()'s own docstring on why that method must never be widened."""

    def test_get_webvar_is_never_called_with_getsessioninfo(self, monkeypatch):
        client = _client_with_active_sessions()
        real_get_webvar = client.get_webvar

        def _spy(endpoint, **kwargs):
            assert endpoint != "getsessioninfo"
            return real_get_webvar(endpoint, **kwargs)

        client.get_webvar = _spy
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", lambda params: client)
        _run_ok(dict(BASE_ARGS, active_session_services=["all"]))

    def test_post_webvar_is_called_with_getsessioninfo_when_requested(self, monkeypatch):
        client = _client_with_active_sessions()
        real_post_webvar = client.post_webvar
        seen_endpoints = []

        def _spy(endpoint, **kwargs):
            seen_endpoints.append(endpoint)
            return real_post_webvar(endpoint, **kwargs)

        client.post_webvar = _spy
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", lambda params: client)
        _run_ok(dict(BASE_ARGS, active_session_services=["cd-media"]))

        assert seen_endpoints == ["getsessioninfo"]

    def test_post_webvar_is_never_called_at_all_when_active_session_services_is_omitted(self, monkeypatch):
        client = _default_client()
        client.post_webvar = Mock(side_effect=AssertionError("post_webvar must not be called when active_session_services is omitted"))
        monkeypatch.setattr(asmb8_sessions, "build_asp_client", lambda params: client)

        _run_ok(dict(BASE_ARGS))

        client.post_webvar.assert_not_called()
