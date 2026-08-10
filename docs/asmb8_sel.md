<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_sel`

Read the ASMB8-iKVM BMC's IPMI System Event Log over the `.asp` web
interface. Read-only.

## Synopsis

Reads `getallselentries.asp`, `getmaxselentries.asp`, and `getselcfg.asp`
over the `.asp` web-management surface and reports the BMC's System Event Log
(SEL) entries, its reported capacity, and its raw policy setting. Every field
is sourced from a real capture checked in under `tests/unit/fixtures/asp/`.

**This is not this collection's only path to the SEL, and for most callers
it is not the preferred one.** The same event log is also readable over plain
IPMI (netfn `0x0a`, Get SEL Info/Get SEL Entry), which `pyghmi` wraps as
`get_event_log()`; that IPMI path has been confirmed against the target
hardware, returning the same 24 real entries this module's own
`getallselentries.asp` fixture shows. IPMI is, in general, the better choice
here: it needs no `.asp` login (so no BMC-side web session is created just to
read a log), it is a standard, portable IPMI command rather than an
AMI-specific web endpoint, and this collection has not built a dedicated
module around it — see [`docs/roadmap.md`](roadmap.md)'s Tier 1 "SEL read and
clear" entry. **This module exists for the cases IPMI does not cover, not to
duplicate it**: a caller with only web-management reachability to this BMC
(no IPMI/RMCP+ path, or a firewall that only opens the web port), or a
cross-check between the two independent transports reading the same
underlying log. If IPMI is reachable, prefer it.

**There is no `clear` option, and there will not be one on this module.**
Clearing the SEL is a real, meaningful operation — it would need either a
POST-based `.asp` endpoint this module's fixture corpus contains no capture
of (every fixture under `tests/unit/fixtures/asp/` is a `get*`/status/login
read, by policy), or the IPMI path's own `get_event_log(clear=True))`/Clear
SEL command. This module is scoped strictly read-only, on purpose.

**Pagination via `after_event_id`.** `getselentries.asp` is this board's paged
sibling of `getallselentries.asp`, and it is a `POST` endpoint, not a `GET`: a
real save-action capture (2026-08-10) sourced its request shape as
`POST /rpc/getselentries.asp` with a `WEBVAR_LASTEVENTID` form field,
returning entries **after** that SEL record ID. `AspClient.get_webvar()`
remains `GET`-only by deliberate design, so this module reads this endpoint
through the separate, explicitly-named `AspClient.post_webvar()` instead —
see that method's own docstring for why it is not a widening of
`get_webvar()`. `after_event_id` is optional; the default, unset behaviour is
unchanged — read `getallselentries.asp` in full, which returned the BMC's
entire 24-entry log in one `GET` in this corpus with no evidence of internal
truncation. `limit` applies identically to either path, after whichever read
ran.

**An empty `after_event_id` result can legitimately mean "nothing newer", not
a failure.** This module's own `getselentries_post_lasteventid24.txt`
fixture — the real capture backing `after_event_id` — issued
`WEBVAR_LASTEVENTID=24` against a log holding exactly 24 entries, and got
back zero records. That is the **correct** answer, not evidence the request
failed — contrast this with `getselentries.txt`, a different, older fixture
in the same corpus captured as a bare `GET` with none of this endpoint's
actual parameters, which is evidence only that a parameterless request
returns nothing useful, not that the log itself was empty. Do not treat
`entries` being empty after `after_event_id` as a signal to retry or
escalate; check `entries_available` against the value you passed instead.

**The paged endpoint's own field-level record shape is unverified beyond its
outer envelope.** The one real capture of `getselentries.asp` returned zero
records, so there is no captured evidence for what fields a non-empty page of
this specific endpoint actually carries — only that its outer envelope
matches every other `.asp` read this collection parses. This module reuses
`getallselentries.asp`'s own field mapping for any record `after_event_id`
does return, on the reasoning that both endpoints read the same underlying
SEL table — an inference, not something this corpus has directly confirmed.

`sel_policy` is reported exactly as `getselcfg.asp`'s `SEL_POLICY` field
returns it, with no interpretation attached — this corpus's one sample is
always `0`, and nothing sourced here documents what any value of this
AMI-specific field means. `entries[].timestamp`/`.timestamp_epoch` are read
from the BMC's own clock, which is documented elsewhere in this collection as
unreliable — treat them as diagnostic, not authoritative.

Logging in to the `.asp` web session to read these endpoints creates real,
if short-lived, BMC-side session state. This module has no way to read these
endpoints without it. Everything it subsequently does over that session is a
plain `GET`, and it never mutates board configuration.

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
| `limit` | `int` | — | no | — (must not be negative) |
| `after_event_id` | `int` | — | no | — (must not be negative) |

Verified against `argument_spec()` in `plugins/modules/asmb8_sel.py`.

### `limit`

Return at most this many entries. Applied client-side, after whichever read
ran (`getallselentries.asp`, or `getselentries.asp` if `after_event_id` was
given) has already returned its full response. Entries are kept in exactly
the order the BMC returned them (highest-`RecordID`-first, newest first, in
this corpus's own fixture) — `limit` simply takes the first `limit` of that
order without resorting it. Omit to return every entry.

### `after_event_id`

If given, read `getselentries.asp` (`POST`, `WEBVAR_LASTEVENTID`) for entries
**after** this SEL record ID instead of `getallselentries.asp`'s full read.
See the synopsis above for exactly what was and was not sourced about this
endpoint, including why an empty `entries` result here is often the
**correct** answer, not a failure. Must not be negative. Omit (the default)
to keep reading `getallselentries.asp` in full, unchanged from before this
option existed.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `entries[].record_id` | `int` | always | `RecordID` — this entry's unique, monotonically-assigned SEL record identifier. |
| `entries[].record_type` | `int` | always | `RecordType`, the standard IPMI SEL record type byte, raw. |
| `entries[].timestamp_epoch` / `.timestamp` | `int` / `str` | always | `TimeStamp` (Unix epoch seconds, per the BMC's own unreliable clock) and its ISO-8601 conversion. |
| `entries[].generator_id_1` / `.generator_id_2` | `int` | always | `GenID1`/`GenID2`, the standard IPMI SEL "Generator ID" bytes, raw. |
| `entries[].event_message_format_version` | `int` | always | `EvmRev`. |
| `entries[].sensor_type` | `int` | always | `SensorType`, raw — no sensor-type-to-name lookup is maintained here. |
| `entries[].sensor_name` | `str` | always | `SensorName` — AMI's own convenience field, not part of the raw IPMI SEL record itself. |
| `entries[].event_dir_type` | `int` | always | `EventDirType`, raw — assertion/deassertion bit and event-reading-type field not decoded. |
| `entries[].event_data_1` / `_2` / `_3` | `int` | always | `EventData1`/`2`/`3`, raw and undecoded. |
| `entries[].extra` | `dict` | always | Any field on this entry not named above, keyed as the BMC sent it — empty for every entry in this corpus. |
| `entries_available` | `int` | always | Number of entries this call's own read returned, before `limit`. Without `after_event_id`, this is `getallselentries.asp`'s count (no evidence this endpoint paginates); with `after_event_id`, it is `getselentries.asp`'s count after that record ID — `0` is a legitimate value. |
| `entries_returned` | `int` | always | `len(entries)` after `limit`. |
| `max_entries` | `int` | always | `getmaxselentries.asp`'s `COUNT` field — the SEL's reported total capacity, not its current entry count. |
| `sel_policy` | `int` | always | `getselcfg.asp`'s `SEL_POLICY` field, raw. Not decoded — see the module description. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | Always `"asmb8_sel.read"`. |
| `operation.endpoint` | `str` | always | The `host:port` this read was performed against. |
| `operation.changed` | `bool` | always | Always `false`. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_sel.py`, and
against `tests/unit/fixtures/asp/getallselentries.txt`,
`getmaxselentries.txt`, and `getselcfg.txt`.

## `error_class` values this module can raise

- `connection` / `tls_validation` / `authentication` / `timeout` / `bmc_busy`
  — establishing or using the `.asp` session, via the same `AspClient`
  machinery `asmb8_media` uses.
- `protocol` — `getmaxselentries.asp` did not return a `COUNT` field, or
  `getselcfg.asp` did not return a `SEL_POLICY` field.

## Check-mode behaviour

Full support. A full read runs identically in check mode, since this module
never mutates board configuration — the one, unavoidable exception is
creating the `.asp` session itself. `diff_mode` is not supported.

## Example

```yaml
- name: Read the full SEL
  james_crowley.asmb8_ikvm.asmb8_sel:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
  delegate_to: localhost
  no_log: true
  register: sel

- name: Only the 5 most recent entries
  james_crowley.asmb8_ikvm.asmb8_sel:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    limit: 5
  delegate_to: localhost
  no_log: true
  register: recent_sel

- name: Warn if the log is approaching its reported capacity
  ansible.builtin.debug:
    msg: "SEL has {{ sel.entries_available }} of {{ sel.max_entries }} entries"
  when: sel.entries_available > (sel.max_entries * 0.9)

- name: Only entries newer than the last one this run already saw
  james_crowley.asmb8_ikvm.asmb8_sel:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    after_event_id: "{{ last_seen_record_id }}"
  delegate_to: localhost
  no_log: true
  register: new_sel

- name: An empty result after after_event_id is not necessarily a problem
  ansible.builtin.debug:
    msg: "no SEL entries newer than {{ last_seen_record_id }}"
  when: new_sel.entries_returned == 0
```

## See also

- [`asmb8_info`](asmb8_info.md), [`asmb8_postcode`](asmb8_postcode.md).
- [`docs/roadmap.md`](roadmap.md) — the standing gap around a dedicated IPMI
  SEL read/clear module.
