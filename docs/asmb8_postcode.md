<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_postcode`

Read the ASMB8-iKVM BMC's current BIOS POST code, optionally sampled over
time. Read-only.

## Synopsis

Reads `getpostcode.asp` over the `.asp` web-management surface and reports
the current BIOS POST code exactly as the BMC returns it (`CurrPostCode`),
plus its integer value when that text parses as hex.

**Why this module exists, and why it is the highest-value module in this
batch.** IPMI Serial-over-LAN does not work on the target board — the
channel-level SOL payload was enabled, per-user SOL access was already
granted, and both plausible bitrates were tried, with zero bytes ever arriving
across repeated resets (see
[`docs/hardware-evidence-2026-08-08.md`](hardware-evidence-2026-08-08.md),
"Serial-over-LAN: configured correctly and still silent"). `asmb8_console`
opens a live KVM/video channel but does not decode the AMI/ASPEED video codec
into pixels. Between those two facts, this collection currently has **no
other** out-of-band signal of boot progress at all — a POST code, read here,
is the only remote signal this collection can give instead of a human
physically present, photographing a monitor.

**This module does not know what any POST code means, and refuses to
guess.** BIOS POST codes are vendor- and firmware-specific, AMI has not
published a table for this board's BIOS, and nothing in this collection's
`.asp` fixture corpus documents one. `post_code`/`post_code_int` are reported
exactly as read, with no interpretation attached. Do not build a
code-to-meaning table on top of this module's output without an
independently sourced reference for this exact BIOS.

**Sampling is deliberately slow, bounded, and impossible to run concurrently
with itself.** `sample=true` turns a single point-in-time read into a bounded
time series: this module polls `poll_interval_seconds` apart for up to
`max_duration_seconds`, one poll at a time, sequentially, from the single
`.asp` session this module opens for the whole run. This BMC's web server is
HTTP/1.0, keeps no connection alive between requests, caps out at 20
concurrent web sessions, and has been observed, against the target hardware,
to accept a TCP connection under concurrent load and then simply never serve
it — wedging even its own web UI for several minutes
(`plugins/module_utils/errors.py`'s `ErrorClass.BMC_BUSY`). `poll_interval_seconds`
and `max_duration_seconds` are both range-checked specifically so this module
cannot be pointed at this BMC in a way that hammers it.

Logging in to the `.asp` web session (`POST /rpc/WEBSES/create.asp`) to read
`getpostcode.asp` creates real, if short-lived, BMC-side session state,
exactly as `asmb8_info`'s `include_web_session` option documents for the same
login call. Unlike that option, this module has no way to read a POST code
without it — there is no IPMI equivalent of this endpoint. Everything this
module subsequently does over that session is a plain `GET`, and it never
mutates board configuration or power state.

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
| `sample` | `bool` | `false` | no | — |
| `poll_interval_seconds` | `int` | `5` | no | — (must be between 2 and 300 inclusive) |
| `max_duration_seconds` | `int` | `60` | no | — (must be between 1 and 900 inclusive) |

Verified against `argument_spec()` in `plugins/modules/asmb8_postcode.py`.

### `sample`

`false` (the default) reads `getpostcode.asp` exactly once. `true` instead
polls it repeatedly and returns the full observed sequence under `sample` in
addition to `post_code`/`post_code_int`, which continue to reflect the most
recently observed value either way.

### `poll_interval_seconds` / `max_duration_seconds`

Ignored when `sample=false`. Both are bounded and rejected outside their
range — see the module's own description for why this BMC must never be
polled tightly or left blocked on this module for hours. There is no faster
way to sample this endpoint through this module, on purpose.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `post_code` | `str` | always | The current (or, when `sample=true`, most recently observed) POST code exactly as `getpostcode.asp` returned it (`CurrPostCode`). No meaning attached. |
| `post_code_int` | `int` | always | `post_code` parsed as a base-16 integer, or `null` if it does not parse as hex. Carries no more meaning than `post_code`. |
| `sample.observations[].post_code` / `.post_code_int` / `.elapsed_seconds` / `.timestamp` | — | when `sample=true` | Every poll issued during this run, in order, each with the controller-clock elapsed time and ISO-8601 timestamp. |
| `sample.distinct_post_codes` | `list` of `str` | when `sample=true` | Distinct values seen, in first-seen order — a boot sitting on one code for several polls appears once here, not once per poll. |
| `sample.poll_interval_seconds` / `.max_duration_seconds` / `.sample_count` | — | when `sample=true` | The effective bounds used for this run, and how many polls were actually issued. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | Always `"asmb8_postcode.read"`. |
| `operation.endpoint` | `str` | always | The `host:port` this read was performed against. |
| `operation.changed` | `bool` | always | Always `false`. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_postcode.py`, and
against `tests/unit/fixtures/asp/getpostcode.txt`, whose one real record is
`{'CurrPostCode' : '00'}`.

## `error_class` values this module can raise

- `connection` / `tls_validation` / `authentication` / `timeout` / `bmc_busy`
  — establishing or using the `.asp` session (login, or the `getpostcode.asp`
  read itself), via the same `AspClient` machinery `asmb8_media` uses.
- `protocol` — `getpostcode.asp` returned no records, or a record with no
  `CurrPostCode` field — both would mean this endpoint has drifted away from
  the one shape the fixture corpus documents.

## Check-mode behaviour

Full support. A full read runs identically in check mode, since this module
never mutates board configuration or power state — the one, unavoidable
exception is creating the `.asp` session itself, which is not gated behind
check mode any more than reading the POST code itself is. `diff_mode` is not
supported.

## Example

```yaml
- name: Read the current POST code once
  james_crowley.asmb8_ikvm.asmb8_postcode:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
  delegate_to: localhost
  no_log: true
  register: postcode

- name: Watch the first two minutes of a boot, one poll every 5 seconds
  james_crowley.asmb8_ikvm.asmb8_postcode:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    sample: true
    poll_interval_seconds: 5
    max_duration_seconds: 120
  delegate_to: localhost
  no_log: true
  register: boot_sample

- name: Show the sequence of distinct codes observed
  ansible.builtin.debug:
    var: boot_sample.sample.distinct_post_codes
```

## See also

- [`asmb8_console`](asmb8_console.md) — the other, still-unproven path to
  observing boot progress (raw video frames, undecoded).
- [`asmb8_info`](asmb8_info.md).
