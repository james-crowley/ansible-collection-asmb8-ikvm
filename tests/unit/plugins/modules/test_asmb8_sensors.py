# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_sensors.

Every test here drives the module against the real, checked-in
``tests/unit/fixtures/asp/getallsensors.txt`` capture, parsed by the real
``webvar.parse_webvar()`` -- nothing here fabricates a sensor record. The only
thing replaced with a fake is `asmb8_sensors.build_asp_client`, so no test
constructs a real `AspClient` or opens a socket.
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
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_sensors

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "asp"

PASSWORD = "Sup3rSecret!"

BASE_ARGS = {
    "host": "10.0.0.5",
    "username": "admin",
    "password": PASSWORD,
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


def _fake_asp_client(fixture_name: str = "getallsensors.txt") -> Mock:
    client = Mock()
    client.endpoint = "10.0.0.5:443"
    client.login.return_value = "<cookie>"
    client.get_webvar.side_effect = lambda endpoint, operation=None: parse_webvar(_load_fixture(fixture_name), endpoint=endpoint, operation=operation)
    return client


def _wire_fake_asp_client(monkeypatch, fake_client) -> None:
    monkeypatch.setattr(asmb8_sensors, "build_asp_client", lambda params: fake_client)


def _run_ok(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleExitJson) as excinfo:
        asmb8_sensors.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_sensors.main()
    return excinfo.value.args[0]


def _by_name(sensors: list[dict], name: str) -> dict:
    matches = [s for s in sensors if s["name"] == name]
    assert len(matches) == 1, f"expected exactly one sensor named {name!r}, found {len(matches)}"
    return matches[0]


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_sensors.argument_spec()["password"]["no_log"] is True

    def test_sensor_names_and_sensor_types_are_unset_by_default(self):
        spec = asmb8_sensors.argument_spec()
        assert "default" not in spec["sensor_names"]
        assert "default" not in spec["sensor_types"]


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
        assert normal["sensors"] == checked["sensors"]

    def test_logs_in_exactly_once(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        _run_ok(dict(BASE_ARGS))
        fake_client.login.assert_called_once()


class TestRealFixtureCounts:
    """Pins the exact shape of tests/unit/fixtures/asp/getallsensors.txt -- 48 real records
    (49 array elements minus the trailing sentinel), 41 threshold-kind and 7 discrete-kind
    (SettableReadableFlags == 0), verified directly against the fixture, not assumed.
    """

    def test_returns_every_sensor_by_default(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        assert result["sensor_count"] == 48
        assert len(result["sensors"]) == 48

    def test_seven_sensors_are_discrete_kind(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        discrete = [s for s in result["sensors"] if s["kind"] == "discrete"]
        assert {s["name"] for s in discrete} == {
            "NM Capabilities",
            "ChassisIntrusion",
            "CPU1_ECC1",
            "CPU2_ECC1",
            "CPU_CATERR",
            "Memory_Train_ERR",
            "Watchdog2",
        }

    def test_forty_one_sensors_are_threshold_kind(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        threshold = [s for s in result["sensors"] if s["kind"] == "threshold"]
        assert len(threshold) == 41


class TestScaling:
    """Pins the /1000 scaling arithmetic against real values from the fixture -- the exact
    failure mode this module exists to avoid is presenting RawReading as if it already were
    the physical value.
    """

    def test_cpu1_temperature_scales_to_a_plausible_celsius_value(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        sensor = _by_name(result["sensors"], "CPU1 Temperature")
        assert sensor["kind"] == "threshold"
        assert sensor["reading"]["raw"] == 69
        assert sensor["reading"]["scaled_raw"] == 69000
        assert sensor["reading"]["value"] == 69.0
        assert sensor["reading"]["unit"] == "degrees C"
        assert sensor["reading"]["unit_code"] == 1

    def test_plus_12v_scales_to_a_plausible_rail_voltage_not_its_raw_byte(self, monkeypatch):
        # RawReading here is 62 -- not a voltage. Only /1000 of SensorReading is.
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        sensor = _by_name(result["sensors"], "+12V")
        assert sensor["reading"]["raw"] == 62
        assert sensor["reading"]["scaled_raw"] == 11904
        assert sensor["reading"]["value"] == pytest.approx(11.904)
        assert sensor["reading"]["unit"] == "Volts"

    def test_cpu_fan1_scales_to_a_plausible_rpm_value(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        sensor = _by_name(result["sensors"], "CPU_FAN1")
        assert sensor["reading"]["scaled_raw"] == 1000000
        assert sensor["reading"]["value"] == 1000.0
        assert sensor["reading"]["unit"] == "RPM"

    def test_vcore1_thresholds_are_scaled_the_same_way_as_the_reading(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        sensor = _by_name(result["sensors"], "+VCORE1")
        assert sensor["reading"]["value"] == pytest.approx(1.776)
        thresholds = sensor["thresholds"]
        assert thresholds["low_non_recoverable"] == {"raw": 1280, "scaled": pytest.approx(1.28)}
        assert thresholds["high_non_recoverable"] == {"raw": 2400, "scaled": pytest.approx(2.4)}

    def test_discrete_sensor_reading_value_is_null_not_a_divided_placeholder(self, monkeypatch):
        # Watchdog2's SensorReading is 32768000 (0x8000 * 1000) -- a firmware placeholder, not a
        # real reading. Dividing it by 1000 and calling it a value would be exactly the kind of
        # silently-wrong presentation this module refuses to produce.
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        sensor = _by_name(result["sensors"], "Watchdog2")
        assert sensor["kind"] == "discrete"
        assert sensor["reading"]["scaled_raw"] == 32768000
        assert sensor["reading"]["value"] is None
        assert sensor["thresholds"]["low_non_recoverable"]["scaled"] is None
        assert sensor["state"]["discrete_state"] == 111

    def test_discrete_sensor_thresholds_raw_values_are_still_reported(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        sensor = _by_name(result["sensors"], "ChassisIntrusion")
        assert sensor["thresholds"]["low_non_recoverable"]["raw"] == 0


class TestSensorTypeNames:
    def test_confirmed_ipmi_spec_types_are_named(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        assert _by_name(result["sensors"], "CPU1 Temperature")["sensor_type_name"] == "Temperature"
        assert _by_name(result["sensors"], "+VCORE1")["sensor_type_name"] == "Voltage"
        assert _by_name(result["sensors"], "CPU_FAN1")["sensor_type_name"] == "Fan"
        assert _by_name(result["sensors"], "ChassisIntrusion")["sensor_type_name"] == "Physical Security"
        assert _by_name(result["sensors"], "CPU_CATERR")["sensor_type_name"] == "Processor"
        assert _by_name(result["sensors"], "PMBPower")["sensor_type_name"] == "Power Supply"
        assert _by_name(result["sensors"], "CPU1_ECC1")["sensor_type_name"] == "Memory"
        assert _by_name(result["sensors"], "Watchdog2")["sensor_type_name"] == "Watchdog 2"

    def test_vendor_oem_range_codes_are_left_unmapped(self, monkeypatch):
        # SensorType 220 and 197 both fall in IPMI's OEM range (0xC0-0xFF); AMI has not published
        # what they mean, and this module must not invent a name for them.
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        assert _by_name(result["sensors"], "NM Capabilities")["sensor_type_name"] == "unknown(220)"
        assert _by_name(result["sensors"], "Memory_Train_ERR")["sensor_type_name"] == "unknown(197)"


class TestGroupingAndFiltering:
    def test_by_type_groups_every_sensor_by_decoded_type_name(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        by_type = result["by_type"]
        assert len(by_type["Temperature"]) == 18
        assert len(by_type["Voltage"]) == 13
        assert len(by_type["Fan"]) == 9
        assert len(by_type["Memory"]) == 2
        assert sum(len(v) for v in by_type.values()) == 48

    def test_sensor_names_filters_to_exactly_the_requested_sensors(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, sensor_names=["CPU1 Temperature", "CPU2 Temperature"]))
        assert result["sensor_count"] == 2
        assert {s["name"] for s in result["sensors"]} == {"CPU1 Temperature", "CPU2 Temperature"}

    def test_sensor_types_filters_to_exactly_the_requested_type(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, sensor_types=[4]))
        assert result["sensor_count"] == 9
        assert all(s["sensor_type"] == 4 for s in result["sensors"])

    def test_by_type_is_computed_from_the_filtered_set(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, sensor_types=[4]))
        assert list(result["by_type"].keys()) == ["Fan"]
        assert len(result["by_type"]["Fan"]) == 9

    def test_combined_filters_use_and_semantics(self, monkeypatch):
        fake_client = _fake_asp_client()
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, sensor_names=["CPU1 Temperature"], sensor_types=[4]))
        # CPU1 Temperature is type 1, not 4 -- the AND of these two filters matches nothing.
        assert result["sensors"] == []
        assert result["by_type"] == {}


class TestFailureHandling:
    def test_login_failure_fails_the_module_with_its_error_class(self, monkeypatch):
        fake_client = _fake_asp_client()
        fake_client.login.side_effect = AuthenticationError("bad credentials", endpoint="10.0.0.5:443", operation="login")
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS))
        assert result["error_class"] == "authentication"

    def test_malformed_response_fails_the_module_with_protocol_error_class(self, monkeypatch):
        fake_client = _fake_asp_client()
        fake_client.get_webvar.side_effect = ProtocolError("could not parse", endpoint="10.0.0.5:443", operation="get_sensors")
        _wire_fake_asp_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS))
        assert result["error_class"] == "protocol"
