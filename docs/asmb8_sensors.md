<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_sensors`

Read ASMB8-iKVM sensor readings over the `.asp` web-management surface.
Read-only.

## Synopsis

Logs in to the BMC's `.asp` web-management session and reads
`getallsensors.asp`, the richest single payload in this collection's 54-file
capture corpus (`tests/unit/fixtures/asp/getallsensors.txt`, ~22 KB, 38 real
sensor records on the target board). Every field name and every scaling claim
below was checked directly against that fixture, not assumed from an IPMI SDR
shape a caller might expect.

**This is not the same thing as reading sensors over IPMI.** This collection
already depends on `pyghmi` for IPMI power/boot/reset, and
`pyghmi.ipmi.command.Command` exposes its own generic sensor path
(`get_sensor_data()`/`get_sensor_reprs()`) that walks the BMC's SDR repository
and decodes each reading with the SDR's own linearization — that works
against any IPMI-compliant BMC and is the right choice when IPMI-over-LAN is
the only path available. This module reads the same underlying sensor table
through this board's proprietary `.asp` RPC surface instead: prefer it when
the web-management port is what you actually have open, when you want the
exact same numbers this board's own web UI shows, or when you want this
board's OEM/vendor-specific sensors alongside the standard ones. Neither path
wraps the other, and this module never falls back to IPMI or vice versa.

**Raw versus scaled readings, and the arithmetic behind `reading.value`.**
Every record in the fixture carries both `RawReading` (the sensor's own raw
byte) and `SensorReading` (a larger integer). Cross-checked directly against
the fixture: `CPU1 Temperature` reports `SensorReading=69000, RawReading=69`
— dividing `SensorReading` by 1000 gives `69.0`, a plausible CPU temperature
in Celsius, while `RawReading` alone (`69`) only happens to look plausible
here by coincidence. The voltage rails prove `RawReading` can never be used
directly: `+12V` reports `SensorReading=11904, RawReading=62` — `62` is not a
voltage, but `11904 / 1000 = 11.904` is a believable nominal-12V reading, and
`+VCORE1` (`SensorReading=1776, RawReading=111`) similarly divides to a
believable `1.776V` core voltage. `CPU_FAN1`
(`SensorReading=1000000, RawReading=10`) divides to a believable `1000` RPM.
**This module therefore reports `reading.value` as `SensorReading / 1000` —
never `RawReading` presented as if it already were the physical value** —
but this /1000 factor is an **empirical** match against nominal rail
voltages, a plausible CPU temperature, and a plausible fan speed, **not** a
documented firmware conversion table (there is no sourced IPMI SDR
M/B/exponent conversion behind it). `RawReading` is always kept alongside it,
unmodified.

**Not every record has a meaningful reading, and this module does not
pretend otherwise.** Seven records in the fixture (`NM Capabilities`,
`ChassisIntrusion`, `CPU1_ECC1`, `CPU2_ECC1`, `CPU_CATERR`,
`Memory_Train_ERR`, `Watchdog2`) report `SettableReadableFlags=0`, while every
sensor with a real analog reading reports the identical non-zero value
`16191`. For six of those seven, `SensorReading` is a suspicious round value
(`32768000` or `32832000` — i.e. `0x8000`/`0x8040` × 1000) that looks like a
**firmware placeholder** rather than a real physical value, not a genuine
`0x8000 × 1000` reading. This module reports `kind: discrete` for these, sets
`reading.value` to `null` rather than dividing a placeholder by 1000 and
calling it a reading, and points a caller instead at `state`
(`SensorState`/`DiscreteState`).

`sensor_type_name` is decoded from the standard IPMI Sensor Type Codes table
(the same public spec table `ipmitool`/`pyghmi` decode against). Codes in the
vendor-defined OEM range (`0xC0`-`0xFF`, decimal 192-255 — `NM Capabilities`
at `220` and `Memory_Train_ERR` at `197` both fall here) are deliberately left
unmapped (`unknown(220)`): AMI has not published what those OEM codes mean.
`reading.unit` is decoded the same way, from the Sensor Unit Type Codes table,
but only for the small set of codes this fixture actually exercises — an
unrecognised code is reported as `null` with the raw integer kept at
`reading.unit_code`.

`sensor_names`/`sensor_types` narrow `sensors` down (AND semantics if both are
given); `by_type` is always additionally provided, grouping the (possibly
filtered) result by `sensor_type_name`.

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
| `sensor_names` | `list` of `str` | — | no | — (case-sensitive, board's own names) |
| `sensor_types` | `list` of `int` | — | no | — (raw `SensorType` codes) |

Verified against `argument_spec()` in `plugins/modules/asmb8_sensors.py`.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `sensors[].number` | `int` | always | `SensorNumber`, verbatim. |
| `sensors[].name` | `str` | always | `SensorName`, verbatim. |
| `sensors[].owner_id` / `.owner_lun` | `int` | always | `OwnerID`/`OwnerLUN`, verbatim. |
| `sensors[].sensor_type` | `int` | always | Raw `SensorType` code. |
| `sensors[].sensor_type_name` | `str` | always | Decoded from the standard IPMI Sensor Type Codes table, or `unknown(<code>)` for the vendor-OEM range. |
| `sensors[].kind` | `str` | always | `threshold` if `SettableReadableFlags` is non-zero, `discrete` otherwise. |
| `sensors[].reading.raw` | `int` | always | `RawReading`, verbatim — not usable directly as a physical value. |
| `sensors[].reading.scaled_raw` | `int` | always | `SensorReading`, verbatim, before the /1000 division. |
| `sensors[].reading.value` | `float` | always | `scaled_raw / 1000` for `kind=threshold` only; `null` for `discrete` sensors. |
| `sensors[].reading.unit` / `.unit_code` | `str` / `int` | always | Decoded base unit (or `null` if unrecognised) and the raw `SensorUnit2` code. |
| `sensors[].thresholds.*` | `dict` | always | Six IPMI threshold fields (`low_non_recoverable` … `high_non_recoverable`), each `{raw, scaled}`; `scaled` is `null` for `discrete` sensors. |
| `sensors[].state.sensor_state` / `.discrete_state` | `int` | always | `SensorState`/`DiscreteState`, verbatim — the meaningful signal for a `discrete` sensor. |
| `sensors[].flags.accessible` / `.settable_readable` | `int` | always | `SensorAccessibleFlags`/`SettableReadableFlags`, verbatim — the evidence `kind` was derived from. |
| `sensor_count` | `int` | always | `len(sensors)` after filtering. |
| `by_type` | `dict` | always | `sensors`, grouped by `sensor_type_name` into `{type_name: [sensor_number, ...]}`, always from the (possibly filtered) set. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | Always `"get_sensors"`. |
| `operation.endpoint` | `str` | always | The `host:port` this read was performed against. |
| `operation.changed` | `bool` | always | Always `false`. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_sensors.py`, and
against `tests/unit/fixtures/asp/getallsensors.txt`.

## `error_class` values this module can raise

- `connection` / `tls_validation` / `authentication` / `timeout` / `bmc_busy`
  — establishing or using the `.asp` session, via the same `AspClient`
  machinery `asmb8_media` uses. This module has no other failure mode: it
  never raises on the shape of an individual sensor record.

## Check-mode behaviour

Full support. A full read runs identically in check mode, since this module
never mutates anything. `diff_mode` is not supported.

## Example

```yaml
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
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    sensor_names: ["CPU1 Temperature", "CPU2 Temperature"]
  delegate_to: localhost
  no_log: true
  register: cpu_temps

- name: A discrete sensor reports state, not a bogus scaled reading
  ansible.builtin.assert:
    that:
      - item.reading.value is none
    loop: "{{ sensors.sensors | selectattr('kind', 'equalto', 'discrete') | list }}"
```

## See also

- [`asmb8_info`](asmb8_info.md), [`asmb8_inventory`](asmb8_inventory.md).
