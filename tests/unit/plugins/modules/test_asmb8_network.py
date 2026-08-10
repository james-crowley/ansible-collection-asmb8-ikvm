# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_network.

Same discipline as `test_asmb8_users.py`: every end-to-end test wires a real `AspClient` to canned
HTTP responses built from the real, redacted fixtures under `tests/unit/fixtures/asp/`, mocking
only `requests.Session.request`. Nothing here opens a socket or talks to any BMC.
"""

from __future__ import annotations

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
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_network

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "asp"

PASSWORD = "Sup3rSecret!"

BASE_ARGS = {
    "host": "198.51.100.10",
    "username": "admin",
    "password": PASSWORD,
}

#: A synthetic getdnscfg.asp-shaped body carrying a real-looking TSIG key value in TSIG_PRIVATE --
#: "Not Available" in every real corpus sample, so this is the only way to prove the field is
#: stripped rather than merely happening to be empty in the one fixture on disk.
_SECRET_TSIG_KEY = "hmac-sha256:SUPER-SECRET-TSIG-SHARED-KEY=="
SYNTHETIC_DNSCFG_WITH_SECRET = (
    "\n//Dynamic Data Begin\n"
    " WEBVAR_JSONVAR_GETDNSCFG = \n"
    " { \n"
    " WEBVAR_STRUCTNAME_GETDNSCFG : \n"
    " [ \n"
    " { 'DNS_ENABLE' : 1,'MDNS' : 0,'HOST_CFG' : 1,'HOST_NAME' : 'AMI14DDA9D4ED4A','REG_BMC' : 1,"
    "'REG_DHCP' : 0,'TSIG_ENABLE' : 1,'TSIG_PRIVATE' : '" + _SECRET_TSIG_KEY + "','TSIG_EXISTS' : 1,"
    "'DOMAIN_CFG' : 'Manual','DOMAIN_NAME' : 'house.com','DNS_CFG' : 0,'DNS_PRIORITY' : 0,"
    "'DNS_IP' : '192.0.2.10' },  {} ],  \n"
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
        asmb8_network.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_network.main()
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
    "getalllancfg": lambda: _read_fixture("getalllancfg.txt"),
    "getlanchannelinfo": lambda: _read_fixture("getlanchannelinfo.txt"),
    "getdnscfg": lambda: _read_fixture("getdnscfg.txt"),
    "getnwbondcfg": lambda: _read_fixture("getnwbondcfg.txt"),
    "checknwbond": lambda: _read_fixture("checknwbond.txt"),
}


def _default_client(*, login_ok: bool = True, **fixture_overrides) -> AspClient:
    fixture_map = {name: loader() for name, loader in DEFAULT_FIXTURES.items()}
    fixture_map.update(fixture_overrides)
    return build_client_with_fixtures(fixture_map, login_ok=login_ok)


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_network.argument_spec()["password"]["no_log"] is True

    def test_port_defaults_to_443(self):
        assert asmb8_network.argument_spec()["port"]["default"] == 443


class TestFixtureCorpusSanity:
    def test_getalllancfg_has_two_channels(self):
        response = parse_webvar(_read_fixture("getalllancfg.txt"))
        assert [r["channelNum"] for r in response.records] == [1, 8]

    def test_getdnscfg_has_four_entries_with_no_channel_link(self):
        response = parse_webvar(_read_fixture("getdnscfg.txt"))
        assert len(response.records) == 4
        assert all("channelNum" not in r and "CHANNEL_NUM" not in r for r in response.records)


class TestDecodeLanChannel:
    def test_redacted_addresses_and_mac_are_passed_through_as_is(self):
        response = parse_webvar(_read_fixture("getalllancfg.txt"))
        decoded = asmb8_network.decode_lan_channel(response.records[0])
        assert decoded["channel"] == 1
        assert decoded["enabled"] is True
        assert decoded["mac_address"] == "00:00:5E:00:53:00"
        assert decoded["ipv4"]["address"] == "192.0.2.10"
        assert decoded["ipv6"]["address"] == "::"

    def test_vlan_fields_are_passed_through(self):
        response = parse_webvar(_read_fixture("getalllancfg.txt"))
        decoded = asmb8_network.decode_lan_channel(response.records[0])
        assert decoded["vlan"] == {"enabled": False, "id": 0, "priority": 0}


class TestDecodeInterface:
    def test_matches_the_fixture(self):
        response = parse_webvar(_read_fixture("getlanchannelinfo.txt"))
        decoded = [asmb8_network.decode_interface(r) for r in response.records]
        assert decoded == [
            {"index": 0, "name": "eth0", "channel": 1, "enabled": True},
            {"index": 1, "name": "eth1", "channel": 8, "enabled": True},
        ]


class TestDecodeDnsEntry:
    def test_tsig_not_available_decodes_to_not_configured(self):
        response = parse_webvar(_read_fixture("getdnscfg.txt"))
        decoded = asmb8_network.decode_dns_entry(response.records[0])
        assert decoded["tsig_key_configured"] is False
        assert "TSIG_PRIVATE" not in decoded
        assert "Not Available" not in json.dumps(decoded)

    def test_other_fields_pass_through(self):
        response = parse_webvar(_read_fixture("getdnscfg.txt"))
        decoded = asmb8_network.decode_dns_entry(response.records[0])
        assert decoded["hostname"] == "AMI14DDA9D4ED4A"
        assert decoded["domain_name"] == "house.com"
        assert decoded["register_with_bmc"] is True


class TestTsigSecretIsNeverReturnedRaw:
    def test_synthetic_tsig_key_is_absent_from_decoded_entry(self):
        response = parse_webvar(SYNTHETIC_DNSCFG_WITH_SECRET)
        decoded = asmb8_network.decode_dns_entry(response.records[0])
        assert decoded["tsig_key_configured"] is True
        assert _SECRET_TSIG_KEY not in json.dumps(decoded)

    def test_synthetic_tsig_key_never_survives_a_full_module_run(self, monkeypatch):
        client = _default_client(getdnscfg=SYNTHETIC_DNSCFG_WITH_SECRET)
        monkeypatch.setattr(asmb8_network, "build_asp_client", lambda params: client)
        result = _run_ok(dict(BASE_ARGS))
        rendered = json.dumps(result)
        assert _SECRET_TSIG_KEY not in rendered
        assert result["dns_entries"][0]["tsig_key_configured"] is True


class TestDecodeBonding:
    def test_matches_the_fixture(self):
        response = parse_webvar(_read_fixture("getnwbondcfg.txt"))
        decoded = asmb8_network.decode_bonding(response.records[0])
        assert decoded == {"enabled": False, "mode_raw": 0, "interface_raw": 1, "vlan_enabled": False, "auto_configured": True}


class TestDecodeBondSupport:
    def test_matches_the_fixture(self):
        response = parse_webvar(_read_fixture("checknwbond.txt"))
        decoded = asmb8_network.decode_bond_support(response.records[0])
        assert decoded == {"nic_count": 2}


class TestMainReadOnlyDefault:
    def test_default_run_reports_everything(self, monkeypatch):
        client = _default_client()
        monkeypatch.setattr(asmb8_network, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS))

        assert result["changed"] is False
        assert len(result["lan_channels"]) == 2
        assert len(result["interfaces"]) == 2
        assert len(result["dns_entries"]) == 4
        assert result["bonding"]["enabled"] is False
        assert result["bond_support"] == {"nic_count": 2}
        assert result["operation"]["action"] == "asmb8_network.report"
        assert result["operation"]["error_class"] is None

    def test_endpoint_is_host_and_port(self, monkeypatch):
        client = _default_client()
        monkeypatch.setattr(asmb8_network, "build_asp_client", lambda params: client)
        result = _run_ok(dict(BASE_ARGS))
        assert result["operation"]["endpoint"] == client.endpoint


class TestCheckMode:
    def test_check_mode_never_logs_in_or_reads(self, monkeypatch):
        build_asp = Mock(side_effect=AssertionError("check mode must never build a client"))
        monkeypatch.setattr(asmb8_network, "build_asp_client", build_asp)

        result = _run_ok(dict(BASE_ARGS, _ansible_check_mode=True))

        assert result["changed"] is False
        assert result["lan_channels"] is None
        assert result["interfaces"] is None
        assert result["dns_entries"] is None
        assert result["bonding"] is None
        assert result["bond_support"] is None
        build_asp.assert_not_called()


class TestErrorHandling:
    def test_login_failure_fails_the_whole_module(self, monkeypatch):
        client = _default_client(login_ok=False)
        monkeypatch.setattr(asmb8_network, "build_asp_client", lambda params: client)
        result = _run_fail(dict(BASE_ARGS))
        assert result["error_class"] == "authentication"

    def test_missing_requests_dependency_is_fatal(self, monkeypatch):
        monkeypatch.setattr(asmb8_network, "HAS_REQUESTS", False)
        monkeypatch.setattr(asmb8_network, "REQUESTS_IMPORT_ERROR", "No module named 'requests'")
        result = _run_fail(dict(BASE_ARGS))
        assert "requests" in result["msg"]


class TestNoCredentialLeakage:
    def test_password_never_appears_in_a_failure_result(self, monkeypatch):
        def _raise(_params):
            raise AuthenticationError(f"rejected password={PASSWORD}", endpoint="198.51.100.10:443", operation="login", secrets=PASSWORD)

        monkeypatch.setattr(asmb8_network, "build_asp_client", _raise)
        result = _run_fail(dict(BASE_ARGS))
        assert PASSWORD not in json.dumps(result)
        assert "[REDACTED]" in result["msg"]

    def test_password_never_appears_in_a_successful_result(self, monkeypatch):
        client = _default_client()
        monkeypatch.setattr(asmb8_network, "build_asp_client", lambda params: client)
        result = _run_ok(dict(BASE_ARGS))
        assert PASSWORD not in json.dumps(result)
