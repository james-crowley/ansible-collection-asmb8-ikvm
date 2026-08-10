# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_sel.

Every test here replaces `asmb8_sel.build_asp_client` with a fake -- no test
constructs a real `AspClient`, so nothing here can reach a socket, let alone
any real BMC. The fake's `get_webvar()` parses the real
`tests/unit/fixtures/asp/{getallselentries,getmaxselentries,getselcfg}.txt`
captures with the real `webvar.parse_webvar` -- only the HTTP transport is
faked, not the parsing, so assertions below are checked against this
project's actual captured values (24 real SEL entries, COUNT 3000,
SEL_POLICY 0), not invented ones.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import ProtocolError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.webvar import WebVarResponse, parse_webvar
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_sel

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "asp"

PASSWORD = "Sup3rSecret!"

BASE_ARGS = {
    "host": "10.0.0.5",
    "username": "admin",
    "password": PASSWORD,
}

#: The real getallselentries.txt fixture's newest record, sourced directly from that file, for
#: assertions to check against rather than inventing expected values.
_EXPECTED_FIRST_RECORD = {
    "RecordID": 24,
    "RecordType": 2,
    "TimeStamp": 1608171458,
    "GenID1": 32,
    "GenID2": 0,
    "EvmRev": 4,
    "SensorType": 4,
    "SensorName": "REAR_FAN2",
    "EventDirType": 1,
    "EventData1": 82,
    "EventData2": 6,
    "EventData3": 6,
}


def _fixture_text(name: str) -> str:
    return (FIXTURES_DIR / f"{name}.txt").read_text(encoding="utf-8")


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
        asmb8_sel.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_sel.main()
    return excinfo.value.args[0]


def _fake_client_from_real_fixtures(endpoint: str = "10.0.0.5:443") -> Mock:
    """A fake AspClient whose get_webvar() parses the real fixture for whichever endpoint is asked for."""
    client = Mock()
    client.endpoint = endpoint
    client.login.return_value = "session-cookie-not-real"
    client.get_webvar.side_effect = lambda name, operation=None: parse_webvar(_fixture_text(name), endpoint=endpoint, operation=operation)
    return client


def _wire_fake_client(monkeypatch, fake_client) -> None:
    monkeypatch.setattr(asmb8_sel, "build_asp_client", lambda params: fake_client)


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_sel.argument_spec()["password"]["no_log"] is True

    def test_limit_has_no_default(self):
        assert asmb8_sel.argument_spec()["limit"].get("default") is None

    def test_there_is_no_clear_option(self):
        # This module is strictly read-only -- see its DOCUMENTATION's "no clear option, and there
        # will not be one" note. Pin that down structurally.
        assert "clear" not in asmb8_sel.argument_spec()


class TestReadAgainstRealFixtures:
    def test_reads_all_24_real_entries_by_default(self, monkeypatch):
        fake_client = _fake_client_from_real_fixtures()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        assert result["entries_available"] == 24
        assert result["entries_returned"] == 24
        assert len(result["entries"]) == 24
        assert result["changed"] is False
        assert result["operation"]["action"] == "asmb8_sel.read"

    def test_max_entries_matches_the_real_fixture(self, monkeypatch):
        fake_client = _fake_client_from_real_fixtures()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        assert result["max_entries"] == 3000

    def test_sel_policy_matches_the_real_fixture(self, monkeypatch):
        fake_client = _fake_client_from_real_fixtures()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        assert result["sel_policy"] == 0

    def test_the_newest_entry_is_mapped_field_for_field(self, monkeypatch):
        fake_client = _fake_client_from_real_fixtures()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        first = result["entries"][0]
        assert first["record_id"] == _EXPECTED_FIRST_RECORD["RecordID"]
        assert first["record_type"] == _EXPECTED_FIRST_RECORD["RecordType"]
        assert first["timestamp_epoch"] == _EXPECTED_FIRST_RECORD["TimeStamp"]
        assert first["timestamp"] == datetime.fromtimestamp(_EXPECTED_FIRST_RECORD["TimeStamp"], tz=timezone.utc).isoformat()
        assert first["generator_id_1"] == _EXPECTED_FIRST_RECORD["GenID1"]
        assert first["generator_id_2"] == _EXPECTED_FIRST_RECORD["GenID2"]
        assert first["event_message_format_version"] == _EXPECTED_FIRST_RECORD["EvmRev"]
        assert first["sensor_type"] == _EXPECTED_FIRST_RECORD["SensorType"]
        assert first["sensor_name"] == _EXPECTED_FIRST_RECORD["SensorName"]
        assert first["event_dir_type"] == _EXPECTED_FIRST_RECORD["EventDirType"]
        assert first["event_data_1"] == _EXPECTED_FIRST_RECORD["EventData1"]
        assert first["event_data_2"] == _EXPECTED_FIRST_RECORD["EventData2"]
        assert first["event_data_3"] == _EXPECTED_FIRST_RECORD["EventData3"]
        assert first["extra"] == {}

    def test_entries_are_kept_in_the_order_the_bmc_returned_them(self, monkeypatch):
        fake_client = _fake_client_from_real_fixtures()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        record_ids = [entry["record_id"] for entry in result["entries"]]
        assert record_ids == list(range(24, 0, -1))

    def test_logs_in_before_reading(self, monkeypatch):
        fake_client = _fake_client_from_real_fixtures()
        _wire_fake_client(monkeypatch, fake_client)
        _run_ok(dict(BASE_ARGS))
        fake_client.login.assert_called_once()

    def test_check_mode_behaves_identically_to_normal_mode(self, monkeypatch):
        fake_client = _fake_client_from_real_fixtures()
        _wire_fake_client(monkeypatch, fake_client)
        normal = _run_ok(dict(BASE_ARGS))
        checked = _run_ok(dict(BASE_ARGS, _ansible_check_mode=True))
        assert normal["entries"] == checked["entries"]
        assert normal["max_entries"] == checked["max_entries"]
        assert normal["sel_policy"] == checked["sel_policy"]

    def test_no_credential_leakage(self, monkeypatch):
        fake_client = _fake_client_from_real_fixtures()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        assert PASSWORD not in json.dumps(result)


class TestLimit:
    def test_limit_caps_the_returned_entries(self, monkeypatch):
        fake_client = _fake_client_from_real_fixtures()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, limit=5))
        assert result["entries_returned"] == 5
        assert len(result["entries"]) == 5
        assert [entry["record_id"] for entry in result["entries"]] == [24, 23, 22, 21, 20]

    def test_limit_does_not_change_entries_available(self, monkeypatch):
        fake_client = _fake_client_from_real_fixtures()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, limit=5))
        assert result["entries_available"] == 24

    def test_limit_larger_than_the_log_returns_everything(self, monkeypatch):
        fake_client = _fake_client_from_real_fixtures()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, limit=1000))
        assert result["entries_returned"] == 24

    def test_negative_limit_is_rejected(self, monkeypatch):
        fake_client = _fake_client_from_real_fixtures()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS, limit=-1))
        assert "limit" in result["msg"]
        fake_client.login.assert_not_called()

    def test_limit_zero_returns_no_entries(self, monkeypatch):
        fake_client = _fake_client_from_real_fixtures()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, limit=0))
        assert result["entries_returned"] == 0
        assert result["entries_available"] == 24


class TestNeverUsesThePagedPostEndpoint:
    def test_only_the_three_get_endpoints_are_ever_requested(self, monkeypatch):
        fake_client = _fake_client_from_real_fixtures()
        _wire_fake_client(monkeypatch, fake_client)
        _run_ok(dict(BASE_ARGS))
        requested = {call.args[0] for call in fake_client.get_webvar.call_args_list}
        assert requested == {"getallselentries", "getmaxselentries", "getselcfg"}
        assert "getselentries" not in requested


class TestFieldLevelHelpers:
    def test_parse_entry_collects_unknown_fields_into_extra(self):
        record = dict(_EXPECTED_FIRST_RECORD, SomeFutureField="unseen-in-the-corpus")
        entry = asmb8_sel.parse_entry(record)
        assert entry["extra"] == {"SomeFutureField": "unseen-in-the-corpus"}

    def test_parse_entry_degrades_missing_fields_to_none_rather_than_raising(self):
        entry = asmb8_sel.parse_entry({})
        assert entry["record_id"] is None
        assert entry["timestamp_epoch"] is None
        assert entry["timestamp"] is None
        assert entry["extra"] == {}

    def test_read_max_entries_raises_protocol_error_when_count_missing(self):
        client = Mock()
        client.endpoint = "10.0.0.5:443"
        client.get_webvar.return_value = WebVarResponse(variable_name="MAXSELENTRIES", struct_name="MAXSELENTRIES", records=[], hapi_status=0)
        with pytest.raises(ProtocolError):
            asmb8_sel.read_max_entries(client)

    def test_read_sel_policy_raises_protocol_error_when_field_missing(self):
        client = Mock()
        client.endpoint = "10.0.0.5:443"
        client.get_webvar.return_value = WebVarResponse(variable_name="GETSELCFG", struct_name="GETSELCFG", records=[], hapi_status=0)
        with pytest.raises(ProtocolError):
            asmb8_sel.read_sel_policy(client)


class TestErrorHandling:
    def test_missing_requests_dependency_is_fatal(self, monkeypatch):
        monkeypatch.setattr(asmb8_sel, "HAS_REQUESTS", False)
        monkeypatch.setattr(asmb8_sel, "REQUESTS_IMPORT_ERROR", "No module named 'requests'")
        result = _run_fail(dict(BASE_ARGS))
        assert "requests" in result["msg"]

    def test_entries_read_failure_fails_the_whole_module(self, monkeypatch):
        fake_client = Mock()
        fake_client.endpoint = "10.0.0.5:443"
        fake_client.login.return_value = "cookie"
        fake_client.get_webvar.side_effect = ProtocolError("getallselentries.asp is unparseable", endpoint="10.0.0.5:443", operation="asmb8_sel.read")
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS))
        assert result["error_class"] == "protocol"
