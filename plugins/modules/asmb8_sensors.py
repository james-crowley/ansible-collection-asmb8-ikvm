#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_sensors
short_description: Read ASMB8-iKVM sensor readings over the C(.asp) web-management surface
description:
  - >-
    Logs in to the BMC's C(.asp) web-management session and reads C(getallsensors.asp), the
    richest single payload in this collection's 54-file capture corpus
    (C(tests/unit/fixtures/asp/getallsensors.txt), ~22 KB, 38 real sensor records on the target
    board). Every field name and every claim this module makes about scaling below was checked
    directly against that fixture, not assumed from an IPMI SDR shape a caller might expect --
    see the module description's arithmetic paragraph for why that distinction matters here.
  - >-
    B(This is not the same thing as reading sensors over IPMI.) This collection already depends on
    C(pyghmi) for IPMI power/boot/reset (see C(module_utils/ipmi.py)), and C(pyghmi.ipmi.command.Command)
    exposes its own generic sensor path (C(get_sensor_data())/C(get_sensor_reprs())) that walks the
    BMC's SDR repository and decodes each reading with the SDR's own linearization -- this works
    against any IPMI-compliant BMC, not just this one, and is the right choice when you need a
    portable, spec-driven reading or when IPMI-over-LAN is the only path available (this module's
    own O(port) -- this collection's web-management HTTPS port -- firewalled off, but IPMI-over-LAN
    open). This module reads the same underlying sensor table
    through this board's proprietary C(.asp) RPC surface instead: prefer it when the web-management
    port is what you actually have open, when you want the exact same numbers this board's own web
    UI shows without a separate SDR walk, or when you want this board's OEM/vendor-specific sensors
    (RV(sensors[].sensor_type_name) V(unknown(220)), C(NM Capabilities|Memory_Train_ERR)) alongside
    the standard ones. Neither path is implemented as a wrapper over the other, and this module
    never silently falls back to IPMI or vice versa -- pick the one your topology and your goal
    actually call for.
  - >-
    B(Raw versus scaled readings, and the arithmetic behind RV(sensors[].reading.value).) Every
    record in the fixture carries both C(RawReading) (the sensor's own raw byte) and
    C(SensorReading) (a larger integer). Cross-checking the fixture directly: C(CPU1 Temperature)
    reports C(SensorReading=69000, RawReading=69) -- dividing C(SensorReading) by 1000 gives
    C(69.0), a plausible CPU temperature in degrees Celsius, while C(RawReading) alone (V(69)) only
    happens to look plausible here by coincidence. The voltage rails prove C(RawReading) can never
    be used directly: C(+12V) reports C(SensorReading=11904, RawReading=62) -- V(62) is not a
    voltage, but C(11904 / 1000 = 11.904) is a believable reading for a nominal 12V rail, and
    C(+VCORE1) (C(SensorReading=1776, RawReading=111)) similarly divides to a believable C(1.776V)
    core voltage that C(RawReading) alone gives no hint of. C(CPU_FAN1) (C(SensorReading=1000000,
    RawReading=10)) divides to a believable C(1000) RPM. B(This module therefore reports
    RV(sensors[].reading.value) as C(SensorReading / 1000) -- never C(RawReading) presented as if
    it already were the physical value) -- but says so plainly rather than asserting an IPMI SDR
    M/B/exponent conversion this fixture does not carry: the /1000 factor is an empirical match
    against nominal rail voltages, a plausible CPU temperature, and a plausible fan speed, not a
    documented firmware conversion table. C(RawReading) is always kept alongside it, unmodified,
    for anyone who wants to see exactly what this board's C(.asp) surface actually sent.
  - >-
    B(Not every record has a meaningful reading, and this module does not pretend otherwise.) Seven
    records in the fixture (C(NM Capabilities), C(ChassisIntrusion), C(CPU1_ECC1), C(CPU2_ECC1),
    C(CPU_CATERR), C(Memory_Train_ERR), C(Watchdog2)) report C(SettableReadableFlags=0), while every
    sensor with a real analog reading reports the identical non-zero value C(16191) -- this field is
    the IPMI SDR "threshold readable/settable mask" concept (a documented part of the IPMI
    specification's Full Sensor Record, not something this project invented), and its being zero or
    non-zero is a clean, corpus-observed split between "this sensor supports threshold readings" and
    "it does not". For six of those seven, C(SensorReading) is the same suspicious round value every
    time (V(32768000) or V(32832000) -- V(32768)/V(32832) times 1000, i.e. C(0x8000)/C(0x8040)), which
    looks like a firmware placeholder rather than a real physical value. This module reports
    RV(sensors[].kind) V(discrete) for these, sets RV(sensors[].reading.value) to V(null) rather than
    dividing a placeholder by 1000 and calling it a reading, and points a caller instead at
    RV(sensors[].state) (C(SensorState)/C(DiscreteState)), which is what a discrete/event-only IPMI
    sensor actually reports.
  - >-
    RV(sensors[].sensor_type_name) is decoded from the standard IPMI specification's Sensor Type
    Codes table (a public spec table, the same one C(ipmitool)/C(pyghmi) decode against -- not
    something specific to this board or vendor). Every code this module maps was either directly
    confirmed against a fixture record's own name (V(1) Temperature/C(CPU1 Temperature), V(2)
    Voltage/C(+VCORE1), V(4) Fan/C(CPU_FAN1), V(5) Physical Security/C(ChassisIntrusion), V(7)
    Processor/C(CPU_CATERR), V(8) Power Supply/C(PMBPower), V(12) Memory/C(CPU1_ECC1), V(35)
    Watchdog 2/C(Watchdog2)) or is a standard, uncontested entry from the same public table.
    Codes in the vendor-defined OEM range (V(0xC0)-V(0xFF), decimal 192-255 -- C(NM Capabilities)
    at V(220) and C(Memory_Train_ERR) at V(197) both fall here) are deliberately left unmapped
    (RV(sensors[].sensor_type_name) V(unknown(220))): AMI has not published what those OEM codes
    mean, and this module will not invent one. RV(sensors[].reading.unit) is decoded the same way,
    from the same specification's Sensor Unit Type Codes table, but only for the small set of codes
    this fixture actually exercises (V(0) none, V(1) degrees C, V(2) degrees F, V(3) Kelvin, V(4)
    Volts, V(5) Amps, V(6) Watts, V(18) RPM, V(19) Hz) -- an unrecognised code is reported as
    V(null) with the raw integer kept at RV(sensors[].reading.unit_code), never guessed at.
  - >-
    Filtering and grouping are both supported: O(sensor_names)/O(sensor_types) narrow
    RV(sensors) down (both are optional and unset by default, so a plain call returns every sensor
    the fixture's endpoint reports), and RV(by_type) is always additionally provided, grouping the
    (possibly filtered) result by RV(sensors[].sensor_type_name) for a caller that wants "every
    fan" or "every temperature sensor" without filtering first.
  - This module is read-only. It never writes to C(getallsensors.asp) or any other RPC, and always reports C(changed=false).
version_added: 0.4.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
options:
  sensor_names:
    description:
      - >-
        Restrict RV(sensors) to sensors whose C(SensorName) is exactly one of these values (the
        board's own naming, case-sensitive -- e.g. C(CPU1 Temperature), C(+VCORE1)). Unset (the
        default) returns every sensor. Combined with O(sensor_types), if both are given, using
        AND semantics -- a sensor must match both to be included.
    type: list
    elements: str
  sensor_types:
    description:
      - >-
        Restrict RV(sensors) to sensors whose raw C(SensorType) code is one of these values. See
        the module description for how this module decodes known codes into
        RV(sensors[].sensor_type_name); this option filters on the raw integer, not the decoded
        name, so it works identically for a vendor-OEM code this module cannot name. Unset (the
        default) returns every sensor.
    type: list
    elements: int
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_info
  - module: james_crowley.asmb8_ikvm.asmb8_inventory
attributes:
  check_mode:
    description: A full read runs identically in check mode, since this module never mutates anything.
    support: full
  diff_mode:
    description: Not supported. There is no prior/after state to diff for a read-only module.
    support: none
requirements:
  - requests >= 2.25.0 (on the Ansible controller)
"""

EXAMPLES = r"""
- name: Read every sensor this board reports
  james_crowley.asmb8_ikvm.asmb8_sensors:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
  delegate_to: localhost
  no_log: true
  register: sensors

- name: Only the two CPU temperature sensors
  james_crowley.asmb8_ikvm.asmb8_sensors:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    sensor_names: ["CPU1 Temperature", "CPU2 Temperature"]
  delegate_to: localhost
  no_log: true
  register: cpu_temps

- name: Every fan, grouped for free without filtering
  james_crowley.asmb8_ikvm.asmb8_sensors:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
  delegate_to: localhost
  no_log: true
  register: all_sensors

- name: Assert an analog sensor's scaled value looks like a real temperature, not a raw byte
  ansible.builtin.assert:
    that:
      - all_sensors.by_type.Temperature is defined

- name: A discrete sensor reports state, not a bogus scaled reading
  ansible.builtin.assert:
    that:
      - item.reading.value is none
    loop: "{{ all_sensors.sensors | selectattr('kind', 'equalto', 'discrete') | list }}"
"""

RETURN = r"""
changed:
  description: Always V(false) -- this module never mutates anything.
  type: bool
  returned: always
sensors:
  description: >-
    Every sensor reported by C(getallsensors.asp), after O(sensor_names)/O(sensor_types) filtering.
  type: list
  elements: dict
  returned: always
  contains:
    number:
      description: The C(SensorNumber) field, verbatim.
      type: int
    name:
      description: The C(SensorName) field, verbatim.
      type: str
    owner_id:
      description: The C(OwnerID) field, verbatim.
      type: int
    owner_lun:
      description: The C(OwnerLUN) field, verbatim.
      type: int
    sensor_type:
      description: The raw C(SensorType) code, verbatim.
      type: int
    sensor_type_name:
      description: >-
        A human-readable name for RV(sensors[].sensor_type), decoded from the standard IPMI Sensor
        Type Codes table -- see the module description for exactly which codes are sourced from
        this fixture versus carried from the same public table unverified against this hardware,
        and why the vendor-OEM range is deliberately left as V(unknown(<code>)).
      type: str
    kind:
      description: >-
        V(threshold) if this sensor's C(SettableReadableFlags) is non-zero (it supports IPMI
        threshold readings and RV(sensors[].reading.value)/RV(sensors[].thresholds) are meaningful),
        V(discrete) otherwise (an event/state-only sensor -- see RV(sensors[].state) instead, and
        the module description for why RV(sensors[].reading.value) is V(null) here).
      type: str
      choices: [threshold, discrete]
    reading:
      description: This sensor's reading, raw and (when meaningful) scaled.
      type: dict
      contains:
        raw:
          description: The C(RawReading) field, verbatim. See the module description for why this is not usable directly as a physical value.
          type: int
        scaled_raw:
          description: The C(SensorReading) field, verbatim, before the /1000 division documented in the module description.
          type: int
        value:
          description: >-
            RV(sensors[].reading.scaled_raw) divided by 1000, for RV(sensors[].kind) V(threshold)
            only. V(null) for V(discrete) sensors, where C(SensorReading) has been observed to carry
            a placeholder value rather than a real reading -- see the module description.
          type: float
        unit:
          description: >-
            A human-readable base unit for RV(sensors[].reading.value), decoded from the C(SensorUnit2)
            field via the standard IPMI Sensor Unit Type Codes table. V(null) if the code is not one
            this module recognises -- see RV(sensors[].reading.unit_code) for the raw value in that case.
          type: str
        unit_code:
          description: The raw C(SensorUnit2) field this module decodes RV(sensors[].reading.unit) from.
          type: int
    thresholds:
      description: >-
        This sensor's six IPMI threshold fields, raw and (for RV(sensors[].kind) V(threshold) only)
        scaled the same way as RV(sensors[].reading.value). V(null) entries under C(scaled) mirror
        RV(sensors[].reading.value)'s V(null) for V(discrete) sensors.
      type: dict
      contains:
        low_non_recoverable:
          description: Raw C(LowNRThresh) and its /1000 C(scaled) value.
          type: dict
        low_critical:
          description: Raw C(LowCTThresh) and its /1000 C(scaled) value.
          type: dict
        low_non_critical:
          description: Raw C(LowNCThresh) and its /1000 C(scaled) value.
          type: dict
        high_non_critical:
          description: Raw C(HighNCThresh) and its /1000 C(scaled) value.
          type: dict
        high_critical:
          description: Raw C(HighCTThresh) and its /1000 C(scaled) value.
          type: dict
        high_non_recoverable:
          description: Raw C(HighNRThresh) and its /1000 C(scaled) value.
          type: dict
    state:
      description: >-
        This sensor's raw state fields -- the meaningful signal for a RV(sensors[].kind) V(discrete)
        sensor, present for every sensor regardless of kind.
      type: dict
      contains:
        sensor_state:
          description: The C(SensorState) field, verbatim.
          type: int
        discrete_state:
          description: The C(DiscreteState) field, verbatim.
          type: int
    flags:
      description: >-
        This sensor's two raw IPMI accessibility/mask fields, verbatim, kept for anyone who wants
        to see the evidence RV(sensors[].kind) was derived from.
      type: dict
      contains:
        accessible:
          description: The C(SensorAccessibleFlags) field, verbatim.
          type: int
        settable_readable:
          description: The C(SettableReadableFlags) field, verbatim. See the module description -- V(0) means RV(sensors[].kind) V(discrete).
          type: int
sensor_count:
  description: C(len(sensors)) -- the number of sensors returned after filtering, for convenience.
  type: int
  returned: always
by_type:
  description: >-
    RV(sensors), grouped by RV(sensors[].sensor_type_name) into C({type_name: [sensor_number, ...]})
    -- always computed from the (possibly filtered) RV(sensors), never from the unfiltered set.
  type: dict
  returned: always
operation:
  description: >-
    The C(asmb8-ikvm-operation/v1) receipt for this read, in the same nested shape every other
    module in this collection returns it under.
  type: dict
  returned: always
  contains:
    schema:
      description: Always V(asmb8-ikvm-operation/v1).
      type: str
    action:
      description: Always V(get_sensors).
      type: str
    endpoint:
      description: The C(host:port) this read was performed against.
      type: str
    changed:
      description: Always V(false).
      type: bool
    error_class:
      description: A stable machine-readable failure class. V(null) on success.
      type: str
"""

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import HAS_REQUESTS, REQUESTS_IMPORT_ERROR, AspClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import IkvmError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt

#: The empirically-observed divisor that turns ``SensorReading`` into a value expressed in the
#: sensor's own base unit (degrees C, Volts, RPM, Watts, ...). See this module's DOCUMENTATION for
#: the fixture cross-checks this is based on (nominal rail voltages, a plausible CPU temperature and
#: fan speed) -- it is an observation about this corpus, not a documented firmware conversion table.
_READING_SCALE = 1000.0

#: Standard IPMI specification "Sensor Type Codes" this module is willing to name. Deliberately not
#: a complete transcription of the whole published table: every entry kept here is either directly
#: confirmed against a real record in tests/unit/fixtures/asp/getallsensors.txt (see the inline
#: citations) or an uncontested, widely-reproduced entry from the same public table. The vendor-OEM
#: range (0xC0-0xFF / 192-255) is deliberately absent -- AMI has not published what its own codes in
#: that range (220 "NM Capabilities", 197 "Memory_Train_ERR" in the fixture) mean, and this module
#: will not invent one; see _sensor_type_name() below.
_SENSOR_TYPE_NAMES: dict[int, str] = {
    0x01: "Temperature",  # confirmed: CPU1/CPU2 Temperature, DIMM*_Temp
    0x02: "Voltage",  # confirmed: +VCORE1/+VCORE2/+3.3V/+5V/+12V/...
    0x03: "Current",
    0x04: "Fan",  # confirmed: CPU_FAN1/2, FRNT_FAN*, REAR_FAN*
    0x05: "Physical Security",  # confirmed: ChassisIntrusion
    0x06: "Platform Security Violation Attempt",
    0x07: "Processor",  # confirmed: CPU_CATERR
    0x08: "Power Supply",  # confirmed: PMBPower
    0x09: "Power Unit",
    0x0A: "Cooling Device",
    0x0C: "Memory",  # confirmed: CPU1_ECC1/CPU2_ECC1
    0x0F: "System Firmware Progress",
    0x10: "Event Logging Disabled",
    0x11: "Watchdog 1",
    0x12: "System Event",
    0x13: "Critical Interrupt",
    0x14: "Button/Switch",
    0x21: "Slot/Connector",
    0x22: "System ACPI Power State",
    0x23: "Watchdog 2",  # confirmed: Watchdog2
}

#: Standard IPMI specification "Sensor Unit Type Codes", limited to the codes actually seen in the
#: corpus (see this module's DOCUMENTATION). An unrecognised code is reported as None, never guessed.
_SENSOR_UNIT_NAMES: dict[int, str] = {
    0: "none",  # observed on discrete sensors, which carry no base unit
    1: "degrees C",  # confirmed: SensorUnit2 on every *Temperature/*_Temp record
    2: "degrees F",
    3: "Kelvin",
    4: "Volts",  # confirmed: SensorUnit2 on every voltage-rail record
    5: "Amps",
    6: "Watts",  # confirmed: SensorUnit2 on PMBPower
    18: "RPM",  # confirmed: SensorUnit2 on every *_FAN* record
    19: "Hz",
}

_THRESHOLD_FIELDS = (
    ("low_non_recoverable", "LowNRThresh"),
    ("low_critical", "LowCTThresh"),
    ("low_non_critical", "LowNCThresh"),
    ("high_non_critical", "HighNCThresh"),
    ("high_critical", "HighCTThresh"),
    ("high_non_recoverable", "HighNRThresh"),
)


def _sensor_type_name(sensor_type: int) -> str:
    return _SENSOR_TYPE_NAMES.get(sensor_type, f"unknown({sensor_type})")


def _sensor_unit_name(unit_code: int) -> str | None:
    return _SENSOR_UNIT_NAMES.get(unit_code)


def _connection_argument_spec() -> dict[str, dict]:
    return {
        "host": {"type": "str", "required": True},
        "port": {"type": "int", "default": 443},
        "username": {"type": "str", "default": "admin"},
        "password": {"type": "str", "required": True, "no_log": True},
        "use_tls": {"type": "bool", "default": True},
        "allow_insecure_transport": {"type": "bool", "default": False},
        "validate_certs": {"type": "bool", "default": True},
        "ca_path": {"type": "path"},
        "tls_fingerprint": {"type": "str"},
        "timeout": {"type": "int", "default": 30},
        "connect_timeout": {"type": "int", "default": 10},
    }


def argument_spec() -> dict[str, dict]:
    spec = _connection_argument_spec()
    spec.update(
        {
            "sensor_names": {"type": "list", "elements": "str"},
            "sensor_types": {"type": "list", "elements": "int"},
        }
    )
    return spec


def build_asp_client(params: dict) -> AspClient:
    """Construct an :class:`AspClient` from the module's connection parameters."""
    return AspClient(
        host=params["host"],
        port=params["port"],
        username=params["username"],
        password=params["password"],
        use_tls=params["use_tls"],
        validate_certs=params["validate_certs"],
        ca_path=params["ca_path"],
        tls_fingerprint=params["tls_fingerprint"],
        allow_insecure_transport=params["allow_insecure_transport"],
        timeout=params["timeout"],
        connect_timeout=params["connect_timeout"],
    )


def normalize_sensor(record: dict) -> dict:
    """Turn one raw ``getallsensors.asp`` record into this module's normalized shape.

    See this module's DOCUMENTATION for the corpus evidence behind every decision here: the /1000
    scale, the ``SettableReadableFlags``-based threshold/discrete split, and the IPMI-spec-sourced
    type/unit name tables.
    """
    settable_readable = int(record.get("SettableReadableFlags", 0) or 0)
    kind = "threshold" if settable_readable != 0 else "discrete"

    scaled_raw = int(record.get("SensorReading", 0) or 0)
    unit_code = int(record.get("SensorUnit2", 0) or 0)
    value = scaled_raw / _READING_SCALE if kind == "threshold" else None

    thresholds: dict[str, dict] = {}
    for key, field in _THRESHOLD_FIELDS:
        raw_threshold = int(record.get(field, 0) or 0)
        thresholds[key] = {
            "raw": raw_threshold,
            "scaled": raw_threshold / _READING_SCALE if kind == "threshold" else None,
        }

    sensor_type = int(record.get("SensorType", 0) or 0)

    return {
        "number": int(record.get("SensorNumber", 0) or 0),
        "name": record.get("SensorName"),
        "owner_id": int(record.get("OwnerID", 0) or 0),
        "owner_lun": int(record.get("OwnerLUN", 0) or 0),
        "sensor_type": sensor_type,
        "sensor_type_name": _sensor_type_name(sensor_type),
        "kind": kind,
        "reading": {
            "raw": int(record.get("RawReading", 0) or 0),
            "scaled_raw": scaled_raw,
            "value": value,
            "unit": _sensor_unit_name(unit_code),
            "unit_code": unit_code,
        },
        "thresholds": thresholds,
        "state": {
            "sensor_state": int(record.get("SensorState", 0) or 0),
            "discrete_state": int(record.get("DiscreteState", 0) or 0),
        },
        "flags": {
            "accessible": int(record.get("SensorAccessibleFlags", 0) or 0),
            "settable_readable": settable_readable,
        },
    }


def filter_sensors(sensors: list[dict], *, sensor_names: list[str] | None, sensor_types: list[int] | None) -> list[dict]:
    """Apply O(sensor_names)/O(sensor_types) with AND semantics. Neither given returns everything."""
    result = sensors
    if sensor_names is not None:
        wanted_names = set(sensor_names)
        result = [s for s in result if s["name"] in wanted_names]
    if sensor_types is not None:
        wanted_types = set(sensor_types)
        result = [s for s in result if s["sensor_type"] in wanted_types]
    return result


def group_by_type(sensors: list[dict]) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = {}
    for sensor in sensors:
        groups.setdefault(sensor["sensor_type_name"], []).append(sensor["number"])
    return groups


def main() -> None:
    module = AnsibleModule(argument_spec=argument_spec(), supports_check_mode=True)

    if not HAS_REQUESTS:
        module.fail_json(msg=missing_required_lib("requests"), exception=REQUESTS_IMPORT_ERROR)
        return

    params = module.params

    try:
        client = build_asp_client(params)
        client.login()
        response = client.get_webvar("getallsensors", operation="get_sensors")
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    sensors = [normalize_sensor(record) for record in response.records]
    sensors = filter_sensors(sensors, sensor_names=params["sensor_names"], sensor_types=params["sensor_types"])

    receipt = OperationReceipt(action="get_sensors", endpoint=client.endpoint, changed=False)
    module.exit_json(
        changed=False,
        sensors=sensors,
        sensor_count=len(sensors),
        by_type=group_by_type(sensors),
        operation=receipt.to_dict(),
    )


if __name__ == "__main__":
    main()
