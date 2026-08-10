# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_inventory.

Every test here drives the module against the real, checked-in
``tests/unit/fixtures/asp/getfwinfo.txt``, ``getprojectcfg.txt``, and
``getfruinfo.txt`` captures, parsed by the real ``webvar.parse_webvar()`` --
nothing here fabricates a record. The only thing replaced with a fake is
`asmb8_inventory.build_asp_client`, so no test constructs a real `AspClient`
or opens a socket.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import AuthenticationError, ProtocolError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.webvar import parse_webvar
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_inventory

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "asp"

PASSWORD = "Sup3rSecret!"

BASE_ARGS = {
    "host": "10.0.0.5",
    "username": "admin",
    "password": PASSWORD,
}

_FIXTURE_BY_ENDPOINT = {
    "getfruinfo": "getfruinfo.txt",
    "getfwinfo": "getfwinfo.txt",
    "getprojectcfg": "getprojectcfg.txt",
}


def _load_fixture(name: str) -> str:
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


def _fake_asp_client() -> Mock:
    client = Mock()
    client.endpoint = "10.0.0.5:443"
    client.login.return_value = "<cookie>"
    client.get_webvar.side_effect = lambda endpoint, operation=None: parse_webvar(
        _load_fixture(_FIXTURE_BY_ENDPOINT[endpoint]), endpoint=endpoint, operation=operation
    )
    return client


def _wire_fake_asp_client(monkeypatch, fake_client) -> None:
    monkeypatch.setattr(asmb8_inventory, "build_asp_client", lambda params: fake_client)


def _run_ok(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleExitJson) as excinfo:
        asmb8_inventory.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_inventory.main()
    return excinfo.value.args[0]


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_inventory.argument_spec()["password"]["no_log"] is True

    def test_sections_defaults_to_all_three(self):
        assert sorted(asmb8_inventory.argument_spec()["sections"]["default"]) == ["firmware", "fru", "project_features"]


class TestNeverMutates:
    def test_a_successful_read_always_reports_changed_false(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        assert result["changed"] is False
        assert result["operation"]["changed"] is False

    def test_check_mode_behaves_identically_to_normal_mode(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        normal = _run_ok(dict(BASE_ARGS))
        checked = _run_ok(dict(BASE_ARGS, _ansible_check_mode=True))
        assert normal["firmware"] == checked["firmware"]
        assert normal["project_features"] == checked["project_features"]
        assert normal["fru"] == checked["fru"]

    def test_logs_in_exactly_once_even_though_three_endpoints_are_read(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        _run_ok(dict(BASE_ARGS))
        fake_client.login.assert_called_once()


class TestFirmwareBcdDecoding:
    """Pins the one specific encoding fact this module must get right: FirmwareRevision2 is
    BCD, and 20 decimal (0x14) must render as "1.14", never "1.20". Cross-checked against
    docs/protocol-notes.md and README.md, both of which independently record this board as
    "firmware 1.14, aux 1.14.2" from real hardware.
    """

    def test_decode_bcd_byte(self):
        assert asmb8_inventory.decode_bcd_byte(20) == "14"
        assert asmb8_inventory.decode_bcd_byte(0) == "00"
        assert asmb8_inventory.decode_bcd_byte(0x99) == "99"

    def test_decode_bcd_byte_rejects_invalid_nibbles(self):
        # 0xFA -> nibbles (15, 10), neither a valid decimal digit.
        assert asmb8_inventory.decode_bcd_byte(0xFA) is None

    def test_firmware_revision_2_raw_is_kept_verbatim_and_not_reported_as_the_version(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        firmware = result["firmware"]
        assert firmware["firmware_revision_2_raw"] == 20
        assert firmware["firmware_revision_2_bcd"] == "14"
        assert firmware["firmware_version"] == "1.14"
        assert firmware["firmware_version"] != "1.20"

    def test_firmware_version_full_matches_the_independently_recorded_aux_version(self, monkeypatch):
        # docs/protocol-notes.md and README.md both record this board, from real hardware, as
        # "firmware 1.14, aux 1.14.2" -- this is the cross-check the BCD decode above is held to.
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        firmware = result["firmware"]
        assert firmware["aux_firmware_revision"] == 2
        assert firmware["firmware_version_full"] == "1.14.2"


class TestFirmwareOtherFields:
    def test_plain_decimal_fields_pass_through_unmodified(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        firmware = result["firmware"]
        assert firmware["device_id"] == 32
        assert firmware["device_revision"] == 1
        assert firmware["ipmi_version"] == 2
        assert firmware["device_support"] == 191
        assert firmware["firmware_revision_1"] == 1
        assert firmware["product_id"] == 3699
        assert firmware["completion_code"] == 0
        assert firmware["firmware_build_date"] == "Jan 25 2018"
        assert firmware["firmware_build_time"] == "17:49:02 CST"

    def test_manufacturer_id_is_combined_but_not_resolved_to_a_vendor_name(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        mfg = result["firmware"]["manufacturer_id"]
        assert mfg["byte_0"] == 63
        assert mfg["byte_1"] == 10
        assert mfg["byte_2"] == 0
        assert mfg["combined"] == 63 | (10 << 8) | (0 << 16)
        assert "name" not in mfg
        assert "vendor" not in mfg


class TestProjectFeatures:
    """Pins the exact shape of tests/unit/fixtures/asp/getprojectcfg.txt: 42 FEATURES records,
    40 unique after dedup (IMG_REDIRECTION and CAPTURE_BSOD_RAW each appear twice), verified
    directly against the fixture -- not the single-feature example in the original task brief,
    which this fixture does not actually match.
    """

    def test_features_preserves_every_record_including_duplicates(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        features = result["project_features"]["features"]
        assert len(features) == 42
        assert features.count("IMG_REDIRECTION") == 2
        assert features.count("CAPTURE_BSOD_RAW") == 2
        assert result["project_features"]["feature_count"] == 42

    def test_feature_set_is_sorted_and_deduplicated(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        feature_set = result["project_features"]["feature_set"]
        assert len(feature_set) == 40
        assert feature_set == sorted(feature_set)
        assert "NWLINK" in feature_set
        assert "SYSTEM_FIREWALL" in feature_set


class TestFru:
    def test_this_boards_fru_table_is_empty(self, monkeypatch):
        # tests/unit/fixtures/asp/getfruinfo.txt is one of the corpus's 5 sentinel-only
        # fixtures -- its array is just [ {} ], with zero real records.
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        assert result["fru"]["populated"] is False
        assert result["fru"]["entries"] == []


class TestSectionsOption:
    def test_only_requested_sections_are_read_and_returned(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, sections=["project_features"]))
        assert result["project_features"] is not None
        assert result["firmware"] is None
        assert result["fru"] is None
        fake_client.get_webvar.assert_called_once()
        assert fake_client.get_webvar.call_args.args[0] == "getprojectcfg"

    def test_all_three_endpoints_are_read_when_sections_defaults(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        _run_ok(dict(BASE_ARGS))
        called_endpoints = {call.args[0] for call in fake_client.get_webvar.call_args_list}
        assert called_endpoints == {"getfruinfo", "getfwinfo", "getprojectcfg"}


class TestFailureHandling:
    def test_login_failure_fails_the_module_with_its_error_class(self, monkeypatch):
        fake_client = _fake_asp_client()
        fake_client.login.side_effect = AuthenticationError("bad credentials", endpoint="10.0.0.5:443", operation="login")
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS))
        assert result["error_class"] == "authentication"

    def test_malformed_response_fails_the_module_with_protocol_error_class(self, monkeypatch):
        fake_client = _fake_asp_client()
        fake_client.get_webvar.side_effect = ProtocolError("could not parse", endpoint="10.0.0.5:443", operation="get_inventory")
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS))
        assert result["error_class"] == "protocol"
