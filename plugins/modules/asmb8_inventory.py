#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_inventory
short_description: Read ASMB8-iKVM firmware, FRU, and project-feature inventory over C(.asp)
description:
  - >-
    Logs in to the BMC's C(.asp) web-management session and reads up to three RPC endpoints, all
    present in this collection's 54-file capture corpus (C(tests/unit/fixtures/asp/)): C(getfwinfo.asp)
    (management-controller firmware identity), C(getprojectcfg.asp) (this firmware build's compiled-in
    feature list), and C(getfruinfo.asp) (Field Replaceable Unit inventory). O(sections) selects
    which of the three to read; all three are read by default.
  - >-
    B(C(getfwinfo.asp)'s C(FirmwareRevision2) is BCD-encoded, and this module decodes it rather
    than presenting the raw byte as a version number.) The fixture reports
    C(FirmwareRevision1: 1, FirmwareRevision2: 20). 20 decimal is C(0x14) hex -- read as two BCD
    digits (C(1), C(4)), B(not) as the decimal number twenty, that byte is how this board arrives
    at reporting itself as firmware "1.14", exactly as C(docs/protocol-notes.md) and this
    collection's own C(README.md) (both independently, from a real capture and from the
    maintainer's own hardware) already record it. This module builds RV(firmware.firmware_version)
    (V(1.14)) from that BCD decode, keeps the raw byte at RV(firmware.firmware_revision_2_raw) so
    nothing is lost, and never reports V(1.20) -- printing the raw decimal value as if it already
    were the minor version is exactly the mistake C(docs/protocol-notes.md) warns against. B(This
    is a fact about this one field's own encoding, not a general BCD rule for the response
    format) -- every other integer field in the same C(getfwinfo.asp) record (C(DeviceID),
    C(DevRevision), C(IPMIVersion), C(CompletionCode), C(FirmwareRevision1), C(AuxFirmwareRevision))
    is a plain decimal integer and is passed through unmodified.
  - >-
    RV(firmware.firmware_version_full) (V(1.14.2)) additionally appends C(AuxFirmwareRevision)
    (plain decimal, not BCD) after RV(firmware.firmware_version) -- this matches, digit for digit,
    the "firmware 1.14 (aux 1.14.2)" this collection's own C(docs/protocol-notes.md) and
    C(README.md) already record for the target board from direct hardware observation, which is
    this module's cross-check that the BCD decode above is being applied the right way, not merely
    a plausible-looking one.
  - >-
    RV(firmware.manufacturer_id) combines the three C(MfgID_0)/C(MfgID_1)/C(MfgID_2) bytes as a
    little-endian 24-bit integer (C(MfgID_0 | MfgID_1 << 8 | MfgID_2 << 16)) because that is the
    standard IPMI Get Device ID byte order for this field. B(It is deliberately not resolved to an
    organisation name.) This project's own rule (see C(README.md)/C(CONTRIBUTING.md)) is to never
    claim a protocol fact it cannot source, and no IANA-enterprise-number lookup for this specific
    combined value is checked into this collection -- inventing one here would be exactly that kind
    of unsourced claim. The three raw bytes and the combined integer are all reported; the name is
    not.
  - >-
    RV(project_features.features) is C(getprojectcfg.asp)'s C(FEATURES) list, verbatim and in
    order, including any duplicate this firmware happens to report (the fixture reports both
    C(IMG_REDIRECTION) and C(CAPTURE_BSOD_RAW) twice) -- this module does not silently deduplicate
    what the BMC actually said. RV(project_features.feature_set) is provided alongside it as a
    sorted, deduplicated convenience view for a caller that only wants a yes/no membership check.
  - >-
    RV(fru) is a generic, unopinionated pass-through of whatever C(getfruinfo.asp) returns.
    B(This board's own capture has no populated FRU record) -- C(tests/unit/fixtures/asp/getfruinfo.txt)
    is one of the five fixtures in the corpus whose array is only the empty-object sentinel, with
    zero real records -- so this module has no evidence for what a populated record's field names
    look like, and does not invent a normalized shape for one. RV(fru.populated) tells a caller
    whether RV(fru.entries) (a list of raw C(dict) records, whatever fields a populated board
    happens to report) is real data or, as observed on the target board, empty.
  - This module is read-only. It never writes to any of these three RPCs, and always reports C(changed=false).
version_added: 0.4.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
options:
  sections:
    description:
      - Which of the three inventory sections to read. Defaults to all three.
    type: list
    elements: str
    choices: [fru, firmware, project_features]
    default: [fru, firmware, project_features]
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_info
  - module: james_crowley.asmb8_ikvm.asmb8_sensors
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
- name: Read the full inventory
  james_crowley.asmb8_ikvm.asmb8_inventory:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
  delegate_to: localhost
  no_log: true
  register: inventory

- name: This board reports itself as firmware 1.14, decoded from a BCD byte, not 1.20
  ansible.builtin.assert:
    that:
      - inventory.firmware.firmware_version == "1.14"
      - inventory.firmware.firmware_version_full == "1.14.2"
      - inventory.firmware.firmware_revision_2_raw == 20

- name: Only the compiled-in feature list
  james_crowley.asmb8_ikvm.asmb8_inventory:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    sections: [project_features]
  delegate_to: localhost
  no_log: true
  register: features_only

- name: Check whether a specific compiled-in feature is present
  ansible.builtin.assert:
    that:
      - "'NWLINK' in features_only.project_features.feature_set"
"""

RETURN = r"""
changed:
  description: Always V(false) -- this module never mutates anything.
  type: bool
  returned: always
firmware:
  description: The decoded C(getfwinfo.asp) record. V(null) unless O(sections) includes V(firmware).
  type: dict
  returned: when O(sections) includes V(firmware)
  contains:
    device_id:
      description: The C(DeviceID) field, verbatim.
      type: int
    device_revision:
      description: The C(DevRevision) field, verbatim.
      type: int
    ipmi_version:
      description: The C(IPMIVersion) field, verbatim.
      type: int
    device_support:
      description: The C(DevSupport) field, verbatim.
      type: int
    firmware_revision_1:
      description: The C(FirmwareRevision1) field, verbatim -- the major version, plain decimal.
      type: int
    firmware_revision_2_raw:
      description: The C(FirmwareRevision2) field, verbatim, before BCD decoding. See the module description.
      type: int
    firmware_revision_2_bcd:
      description: >-
        RV(firmware.firmware_revision_2_raw) decoded as two BCD digits (e.g. V(20) -> V("14")).
        V(null) if either nibble is out of the valid C(0)-C(9) BCD digit range, in which case
        RV(firmware.firmware_version) is also V(null) rather than guessed at.
      type: str
    firmware_version:
      description: >-
        RV(firmware.firmware_revision_1) joined with RV(firmware.firmware_revision_2_bcd), e.g.
        V(1.14). V(null) if the BCD decode failed.
      type: str
    aux_firmware_revision:
      description: The C(AuxFirmwareRevision) field, verbatim -- plain decimal, not BCD.
      type: int
    firmware_version_full:
      description: >-
        RV(firmware.firmware_version) with RV(firmware.aux_firmware_revision) appended, e.g.
        V(1.14.2). V(null) if RV(firmware.firmware_version) is V(null).
      type: str
    firmware_build_date:
      description: The C(FirmwareBuildDate) field, verbatim.
      type: str
    firmware_build_time:
      description: The C(FirmwareBuildTime) field, verbatim.
      type: str
    manufacturer_id:
      description: >-
        The C(MfgID_0)/C(MfgID_1)/C(MfgID_2) bytes, raw and combined. See the module description
        for why the combined value is not resolved to an organisation name.
      type: dict
      contains:
        byte_0:
          description: The C(MfgID_0) field, verbatim (least-significant byte).
          type: int
        byte_1:
          description: The C(MfgID_1) field, verbatim.
          type: int
        byte_2:
          description: The C(MfgID_2) field, verbatim (most-significant byte).
          type: int
        combined:
          description: C(byte_0 | byte_1 << 8 | byte_2 << 16), per the standard IPMI Get Device ID byte order.
          type: int
    product_id:
      description: The C(ProdID) field, verbatim. Not decoded further -- no sourced product catalog exists for it.
      type: int
    completion_code:
      description: The C(CompletionCode) field, verbatim. V(0) means success, per ordinary IPMI completion-code semantics.
      type: int
project_features:
  description: The decoded C(getprojectcfg.asp) record. V(null) unless O(sections) includes V(project_features).
  type: dict
  returned: when O(sections) includes V(project_features)
  contains:
    features:
      description: Every C(FEATURES) value, verbatim and in order, including any duplicate this firmware reports.
      type: list
      elements: str
    feature_set:
      description: RV(project_features.features), sorted and deduplicated, for a simple membership check.
      type: list
      elements: str
    feature_count:
      description: C(len(project_features.features)) (before deduplication).
      type: int
fru:
  description: The C(getfruinfo.asp) record. V(null) unless O(sections) includes V(fru).
  type: dict
  returned: when O(sections) includes V(fru)
  contains:
    populated:
      description: >-
        Whether RV(fru.entries) contains any real record. V(false) on the target board -- see the
        module description for why this module has no normalized shape to offer for a populated one.
      type: bool
    entries:
      description: Raw C(getfruinfo.asp) records, verbatim, whatever fields a populated board happens to report.
      type: list
      elements: dict
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
      description: Always V(get_inventory).
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
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import IkvmError, ProtocolError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt

SECTION_FRU = "fru"
SECTION_FIRMWARE = "firmware"
SECTION_PROJECT_FEATURES = "project_features"
ALL_SECTIONS = (SECTION_FRU, SECTION_FIRMWARE, SECTION_PROJECT_FEATURES)

#: Maps each O(sections) choice to the bare endpoint name AspClient.get_webvar() expects.
_SECTION_ENDPOINTS = {
    SECTION_FRU: "getfruinfo",
    SECTION_FIRMWARE: "getfwinfo",
    SECTION_PROJECT_FEATURES: "getprojectcfg",
}

#: Valid BCD digit range. A nibble outside this range is not a valid BCD digit -- see
#: decode_bcd_byte() below.
_BCD_DIGIT_MAX = 9


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
    spec["sections"] = {"type": "list", "elements": "str", "choices": list(ALL_SECTIONS), "default": list(ALL_SECTIONS)}
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


def decode_bcd_byte(value: int) -> str | None:
    """Decode one byte as two packed BCD digits, e.g. ``20`` (``0x14``) -> ``"14"``.

    Returns ``None`` -- rather than a best-effort guess -- if either nibble is not a valid decimal
    digit (0-9), which is not a shape this module's one sourced example (``FirmwareRevision2: 20``)
    exercises but that a different firmware revision's byte could produce.

    Deliberately returns the two digits as a concatenated *string*, not ``hi * 10 + lo``: both give
    the same result for this fixture's value (``1``, ``4`` -> ``"14"`` == ``14``), but only the
    string form preserves a leading zero a future BCD byte like ``0x05`` would need (``"05"``, not
    ``"5"``) -- see this module's DOCUMENTATION for why this matters for RV(firmware.firmware_version).
    """
    hi, lo = (value >> 4) & 0xF, value & 0xF
    if hi > _BCD_DIGIT_MAX or lo > _BCD_DIGIT_MAX:
        return None
    return f"{hi}{lo}"


def parse_firmware(records: list[dict]) -> dict:
    """Build RV(firmware) from ``getfwinfo.asp``'s single record. See this module's DOCUMENTATION."""
    if not records:
        raise ProtocolError("getfwinfo.asp returned no records", operation="get_inventory:firmware")
    record = records[0]

    firmware_revision_1 = int(record.get("FirmwareRevision1", 0) or 0)
    firmware_revision_2_raw = int(record.get("FirmwareRevision2", 0) or 0)
    aux_firmware_revision = int(record.get("AuxFirmwareRevision", 0) or 0)

    bcd = decode_bcd_byte(firmware_revision_2_raw)
    firmware_version = f"{firmware_revision_1}.{bcd}" if bcd is not None else None
    firmware_version_full = f"{firmware_version}.{aux_firmware_revision}" if firmware_version is not None else None

    return {
        "device_id": int(record.get("DeviceID", 0) or 0),
        "device_revision": int(record.get("DevRevision", 0) or 0),
        "ipmi_version": int(record.get("IPMIVersion", 0) or 0),
        "device_support": int(record.get("DevSupport", 0) or 0),
        "firmware_revision_1": firmware_revision_1,
        "firmware_revision_2_raw": firmware_revision_2_raw,
        "firmware_revision_2_bcd": bcd,
        "firmware_version": firmware_version,
        "aux_firmware_revision": aux_firmware_revision,
        "firmware_version_full": firmware_version_full,
        "firmware_build_date": record.get("FirmwareBuildDate"),
        "firmware_build_time": record.get("FirmwareBuildTime"),
        "manufacturer_id": {
            "byte_0": int(record.get("MfgID_0", 0) or 0),
            "byte_1": int(record.get("MfgID_1", 0) or 0),
            "byte_2": int(record.get("MfgID_2", 0) or 0),
            "combined": int(record.get("MfgID_0", 0) or 0) | (int(record.get("MfgID_1", 0) or 0) << 8) | (int(record.get("MfgID_2", 0) or 0) << 16),
        },
        "product_id": int(record.get("ProdID", 0) or 0),
        "completion_code": int(record.get("CompletionCode", 0) or 0),
    }


def parse_project_features(records: list[dict]) -> dict:
    """Build RV(project_features) from ``getprojectcfg.asp``'s C(FEATURES) list. See this module's DOCUMENTATION."""
    features = [record["FEATURES"] for record in records if "FEATURES" in record]
    return {
        "features": features,
        "feature_set": sorted(set(features)),
        "feature_count": len(features),
    }


def parse_fru(records: list[dict]) -> dict:
    """Build RV(fru) from ``getfruinfo.asp``. See this module's DOCUMENTATION for why this is a raw pass-through."""
    return {
        "populated": bool(records),
        "entries": records,
    }


_SECTION_PARSERS = {
    SECTION_FRU: parse_fru,
    SECTION_FIRMWARE: parse_firmware,
    SECTION_PROJECT_FEATURES: parse_project_features,
}


def main() -> None:
    module = AnsibleModule(argument_spec=argument_spec(), supports_check_mode=True)

    if not HAS_REQUESTS:
        module.fail_json(msg=missing_required_lib("requests"), exception=REQUESTS_IMPORT_ERROR)
        return

    params = module.params
    requested_sections = params["sections"]

    result: dict[str, dict | None] = dict.fromkeys(ALL_SECTIONS)

    try:
        client = build_asp_client(params)
        client.login()
        for section in requested_sections:
            response = client.get_webvar(_SECTION_ENDPOINTS[section], operation=f"get_inventory:{section}")
            result[section] = _SECTION_PARSERS[section](response.records)
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    receipt = OperationReceipt(action="get_inventory", endpoint=client.endpoint, changed=False)
    module.exit_json(
        changed=False,
        fru=result[SECTION_FRU],
        firmware=result[SECTION_FIRMWARE],
        project_features=result[SECTION_PROJECT_FEATURES],
        operation=receipt.to_dict(),
    )


if __name__ == "__main__":
    main()
