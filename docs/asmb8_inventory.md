<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_inventory`

Read ASMB8-iKVM firmware, FRU, and project-feature inventory over `.asp`.
Read-only.

## Synopsis

Logs in to the BMC's `.asp` web-management session and reads up to three RPC
endpoints, all present in this collection's 54-file capture corpus:
`getfwinfo.asp` (management-controller firmware identity), `getprojectcfg.asp`
(this firmware build's compiled-in feature list), and `getfruinfo.asp` (Field
Replaceable Unit inventory). `sections` selects which of the three to read;
all three are read by default.

**`getfwinfo.asp`'s `FirmwareRevision2` is BCD-encoded, and this module
decodes it rather than presenting the raw byte as a version number.** The
fixture reports `FirmwareRevision1: 1, FirmwareRevision2: 20`. 20 decimal is
`0x14` hex — read as two BCD digits (`1`, `4`), **not** as the decimal number
twenty, that byte is how this board arrives at reporting itself as firmware
"1.14". This module builds `firmware.firmware_version` (`"1.14"`) from that
BCD decode, keeps the raw byte at `firmware.firmware_revision_2_raw` so
nothing is lost, and never reports `"1.20"`. **This is a fact about this one
field's own encoding, not a general BCD rule for the response format** —
every other integer field in the same record (`DeviceID`, `DevRevision`,
`IPMIVersion`, `CompletionCode`, `FirmwareRevision1`, `AuxFirmwareRevision`)
is a plain decimal integer and is passed through unmodified.

`firmware.firmware_version_full` (`"1.14.2"`) additionally appends
`AuxFirmwareRevision` (plain decimal, not BCD) after `firmware_version` —
matching, digit for digit, "firmware 1.14 (aux 1.14.2)" as recorded elsewhere
in this collection from direct hardware observation, which is this module's
cross-check that the BCD decode is being applied the right way.

`firmware.manufacturer_id` combines the three `MfgID_0`/`MfgID_1`/`MfgID_2`
bytes as a little-endian 24-bit integer (`MfgID_0 | MfgID_1 << 8 | MfgID_2 <<
16`), per the standard IPMI Get Device ID byte order. **It is deliberately
not resolved to an organisation name.** No IANA-enterprise-number lookup for
this specific combined value is checked into this collection — inventing one
would be exactly the kind of unsourced claim this project's policy refuses.
The three raw bytes and the combined integer are all reported; the name is
not.

`project_features.features` is `getprojectcfg.asp`'s `FEATURES` list, verbatim
and in order, including any duplicate this firmware happens to report (the
fixture reports both `IMG_REDIRECTION` and `CAPTURE_BSOD_RAW` twice) — this
module does not silently deduplicate what the BMC actually said.
`project_features.feature_set` is provided alongside it as a sorted,
deduplicated convenience view.

`fru` is a generic, unopinionated pass-through of whatever `getfruinfo.asp`
returns. **This board's own capture has no populated FRU record** —
`tests/unit/fixtures/asp/getfruinfo.txt` is one of the five fixtures in the
corpus whose array is only the empty-object sentinel, with zero real records —
so this module has no evidence for what a populated record's field names
look like, and does not invent a normalized shape for one. `fru.populated`
tells a caller whether `fru.entries` is real data or, as observed on the
target board, empty.

## Options

| Option | Type | Default | Required | Choices |
|---|---|---|---|---|
| `host` | `str` | — | yes | — |
| `port` | `int` | `443` | no | — |
| `username` | `str` | `admin` | no | — |
| `password` | `str` (`no_log`) | — | yes | — |
| `use_tls` | `bool` | `true` | no | — |
| `allow_insecure_transport` | `bool` | `false` | no | — |
| `validate_certs` | `bool` | `true` | no | — |
| `ca_path` | `path` | — | no | — (mutually exclusive with `tls_fingerprint`) |
| `tls_fingerprint` | `str` | — | no | — (mutually exclusive with `ca_path`; recommended trust mode for this board) |
| `timeout` | `int` | `30` | no | — |
| `connect_timeout` | `int` | `10` | no | — |
| `sections` | `list` of `str` | `[fru, firmware, project_features]` | no | `fru`, `firmware`, `project_features` |

Verified against `argument_spec()` in `plugins/modules/asmb8_inventory.py`.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `firmware.device_id` / `.device_revision` / `.ipmi_version` / `.device_support` | `int` | when `sections` includes `firmware` | `DeviceID`/`DevRevision`/`IPMIVersion`/`DevSupport`, verbatim. |
| `firmware.firmware_revision_1` | `int` | " | `FirmwareRevision1`, verbatim — the major version, plain decimal. |
| `firmware.firmware_revision_2_raw` / `.firmware_revision_2_bcd` | `int` / `str` | " | Raw `FirmwareRevision2` byte, and its two-BCD-digit decode (`20` → `"14"`), or `null` if either nibble is not a valid decimal digit. |
| `firmware.firmware_version` | `str` | " | `firmware_revision_1` joined with `firmware_revision_2_bcd`, e.g. `"1.14"`. `null` if the BCD decode failed. |
| `firmware.aux_firmware_revision` | `int` | " | `AuxFirmwareRevision`, verbatim — plain decimal, not BCD. |
| `firmware.firmware_version_full` | `str` | " | `firmware_version` with `aux_firmware_revision` appended, e.g. `"1.14.2"`. |
| `firmware.firmware_build_date` / `.firmware_build_time` | `str` | " | `FirmwareBuildDate`/`FirmwareBuildTime`, verbatim. |
| `firmware.manufacturer_id.byte_0` / `.byte_1` / `.byte_2` / `.combined` | `int` | " | Raw `MfgID_0`/`MfgID_1`/`MfgID_2` and their little-endian-combined 24-bit value. Not resolved to a vendor name. |
| `firmware.product_id` | `int` | " | `ProdID`, verbatim. Not decoded further. |
| `firmware.completion_code` | `int` | " | `CompletionCode`, verbatim (`0` means success). |
| `project_features.features` | `list` of `str` | when `sections` includes `project_features` | `FEATURES`, verbatim and in order, including duplicates. |
| `project_features.feature_set` | `list` of `str` | " | Sorted, deduplicated view of `features`. |
| `project_features.feature_count` | `int` | " | `len(features)` before deduplication. |
| `fru.populated` | `bool` | when `sections` includes `fru` | Whether `fru.entries` contains any real record. `false` on the target board. |
| `fru.entries` | `list` of `dict` | " | Raw `getfruinfo.asp` records, verbatim. No normalized shape offered — see the module description. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | Always `"get_inventory"`. |
| `operation.endpoint` | `str` | always | The `host:port` this read was performed against. |
| `operation.changed` | `bool` | always | Always `false`. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_inventory.py`,
and against `tests/unit/fixtures/asp/getfwinfo.txt` (`FirmwareRevision1: 1,
FirmwareRevision2: 20`), `getprojectcfg.txt`, and `getfruinfo.txt`.

## `error_class` values this module can raise

- `connection` / `tls_validation` / `authentication` / `timeout` / `bmc_busy`
  — establishing or using the `.asp` session, via the same `AspClient`
  machinery `asmb8_media` uses.
- `protocol` — `getfwinfo.asp` returned no records when `sections` includes
  `firmware`.

## Check-mode behaviour

Full support. A full read runs identically in check mode, since this module
never mutates anything. `diff_mode` is not supported.

## Example

```yaml
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
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    sections: [project_features]
  delegate_to: localhost
  no_log: true
  register: features_only
```

## See also

- [`asmb8_info`](asmb8_info.md), [`asmb8_sensors`](asmb8_sensors.md).
