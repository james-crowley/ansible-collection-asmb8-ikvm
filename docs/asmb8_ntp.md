<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_ntp`

Manage ASMB8-iKVM NTP server configuration.

## Synopsis

Reads and, when needed, writes this BMC's NTP configuration (`getntpcfg.asp` /
`setntpcfg.asp`) over the AMI `.asp` RPC surface. This is this collection's
**first module that actually mutates BMC configuration** — every
`.asp`-backed module before it (`asmb8_network`, `asmb8_sensors`, and the
rest) is read-only. Everything below that describes a write is sourced from
one real save-action capture taken 2026-08-10 against the target board,
firmware 1.14 (aux 1.14.2); see [`docs/protocol-notes.md`](protocol-notes.md)'s
write-convention section for the full capture this module was built from.

**This module logs in on every run, including in check mode.** Unlike
`asmb8_network` or `asmb8_sessions`, which skip login entirely in check mode
because they have no write path to predict, this module's whole point is
idempotence: it must read `getntpcfg.asp` before it can know whether anything
needs to change, in check mode exactly as much as in a real run.

**`server2`'s comparison is byte-exact, deliberately.** The one real capture
read back `SERVER_NAME2` carrying a leading space (`' 192.0.2.10'`) and the
matching write echoed that leading space back unchanged. This module never
strips or otherwise normalizes `server1` or `server2`: comparing the value
you supply against the BMC's own raw `SERVER_NAME1`/`SERVER_NAME2` text,
character for character, is what makes every subsequent run genuinely a
no-op rather than reporting `changed=true` forever because a friendlier
trimmed comparison quietly disagreed with what the BMC actually stores. If
your intended server address has no leading space, and the BMC nonetheless
routinely reports one back (unconfirmed either way by this one capture),
expect this module to report a change on every run until you supply the
value with the same leading space the BMC uses.

**`enabled`'s relationship to what this module reads is an inference, not a
sourced fact.** `getntpcfg.asp` reports NTP status as `NTP_STATUS` (observed
as `1` in the one real read); the write this module issues sets
`ISNTPENABLE` (observed as `0` in the one real write, from the very same
session). Nothing in the capture proves `NTP_STATUS` and `ISNTPENABLE` share
an encoding, or even that they describe the same underlying flag — one is a
read-only status field and the other is a distinct write-only field name,
and the capture never shows the same value on both sides of a single change
to compare. This module maps `NTP_STATUS` to
`previous_state.enabled`/`observed.enabled` as "nonzero means enabled"
purely as a best-effort interpretation, and writes `enabled=true` as
`ISNTPENABLE=1` on the same unconfirmed assumption (the only observed
`ISNTPENABLE` value is `0`, for "disable"; `1` for "enable" is this module's
own inference from the field's name, never independently confirmed).
`previous_state.ntp_status_raw`/`observed.ntp_status_raw` carry the
untranslated `NTP_STATUS` integer specifically so a caller who does not
trust this inference has the raw value to fall back on.

**This module owns `ISNTPENABLE` through `setntpcfg.asp` only — it never
calls `setdatetime.asp`.** The same save-action capture that sourced
`setntpcfg.asp`'s write shape also shows `setdatetime.asp` carrying its own
`ISNTPENABLE=0` in the same save action (alongside
`SECONDS`/`UTCMINUTES`/`TIMEZONE`) — the vendor UI's single "save NTP
settings" click evidently POSTs to both endpoints together. This module
deliberately does not follow that pattern: reaching `setdatetime.asp` at all
would mean resubmitting `SECONDS` with whatever value this module last read,
nudging the BMC's clock forward by the (small but real) gap between that
read and the write, on every run that changes anything — a side effect this
module has no sourced reason to accept for a capability (`server1`/
`server2`/`enabled`) that has nothing to do with the clock. The consequence,
honestly stated: whether `setdatetime.asp`'s own copy of `ISNTPENABLE`
tracks `setntpcfg.asp`'s automatically on real firmware, or drifts out of
sync with it, is **unverified** — this module manages exactly one copy of
that flag and makes no claim about the other.

**No timezone or UTC-offset options exist here, deliberately.**
`setdatetime.asp` is the only sourced write path for `TIMEZONE`/
`UTCMINUTES`, and the one real capture of it is a full-record write
alongside `SECONDS` with no evidence of what a partial submission (some
fields omitted or left as sentinel values) does. Between that and the
clock-nudging concern above, this module does not implement a write path
this collection has not actually observed in isolation. A future module or
option that manages the clock should source its own capture of
`setdatetime.asp` rather than extrapolating from this module's
`setntpcfg.asp`-only convention: setter field names are per-endpoint, not a
collection-wide rule — `setdatetime.asp` reuses `getdatetime.asp`'s own
field names verbatim, while `setntpcfg.asp` does not reuse `getntpcfg.asp`'s
at all (`SERVER_NAME1` becomes both `NEW_NTPSERVER_NAME1` and
`OLD_NTPSERVER_NAME1`; `NTP_STATUS` becomes `ISNTPENABLE`) — one endpoint's
convention tells you nothing reliable about the next one's.

**`OLD_NTPSERVER_NAME1` is always sent; `OLD_NTPSERVER_NAME2` is never
invented.** The one real write capture carries both `NEW_NTPSERVER_NAME1`
and `OLD_NTPSERVER_NAME1` (identical values, `pool.ntp.org`, since server 1
was not the field actually changing in that save action) but only
`NEW_NTPSERVER_NAME2` — no `OLD_NTPSERVER_NAME2` field appears anywhere in
that capture, even though server 2 (with its leading space) was also
present in the same write. This module follows that asymmetry exactly: it
always sends `OLD_NTPSERVER_NAME1` (this run's freshly-read
`SERVER_NAME1`, before any change), and never sends an `OLD_NTPSERVER_NAME2`
field under any circumstance. Reading `OLD_NTPSERVER_NAME1` as "the value
before this write" is this module's own inference from the field's name —
the capture only shows the unchanged case (`NEW` equal to `OLD`), never a
case where server 1 actually changed, so that inference is unconfirmed for
a real change, if a genuinely accurate one for "no change".

**A write always resubmits every field `setntpcfg.asp` takes, not just the
one that changed.** This mirrors the one real capture exactly: its save
action, which was actually only toggling NTP on/off, still resubmitted
`NEW_NTPSERVER_NAME1`/`OLD_NTPSERVER_NAME1` and `NEW_NTPSERVER_NAME2`
unchanged alongside the real `ISNTPENABLE` change. Whenever this module
needs to write anything, it does the same: any of `server1`/`server2`/
`enabled` left unset keeps this run's freshly-read current value, sent back
verbatim, rather than being omitted from the request body — there is no
sourced evidence a partial `setntpcfg.asp` submission does anything
sensible, so this module never attempts one.

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
| `ca_path` | `path` | — | no | — |
| `tls_fingerprint` | `str` | — | no | — |
| `timeout` | `int` | `30` | no | — |
| `connect_timeout` | `int` | `10` | no | — |
| `server1` | `str` | — | no | — |
| `server2` | `str` | — | no | — |
| `enabled` | `bool` | — | no | — |

Verified against `_connection_argument_spec()`/`argument_spec()` in
`plugins/modules/asmb8_ntp.py`.

### `server1` / `server2`

Desired primary/secondary NTP server. Unset (the default) leaves the
current value alone — but is still resubmitted verbatim if a write happens
for a different option, per the synopsis above. `server1` is compared
against the BMC's raw `SERVER_NAME1` exactly as given; `server2` is compared
byte-for-byte against the BMC's raw `SERVER_NAME2`, including any leading or
trailing whitespace — see the synopsis for why that byte-exact comparison
is what makes this module genuinely idempotent against the target hardware.

### `enabled`

Desired NTP enable state, written as `ISNTPENABLE` (`1` for `true`, `0` for
`false`) when it differs from this module's own interpretation of the BMC's
`NTP_STATUS`. Unset (the default) leaves the current value alone. See the
synopsis's dedicated caveat: this comparison is a best-effort inference, not
a sourced fact.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `previous_state` | `dict` | always | NTP configuration observed before any action was taken. |
| `previous_state.server1` | `str` | always | Raw `SERVER_NAME1`, exactly as read. |
| `previous_state.server2` | `str` | always | Raw `SERVER_NAME2`, exactly as read (may carry a leading space). |
| `previous_state.enabled` | `bool` | always | This module's inferred boolean reading of `NTP_STATUS`. See the synopsis's caveat. |
| `previous_state.ntp_status_raw` | `int` | always | The untranslated `NTP_STATUS` integer. |
| `desired_state` | `dict` | always | Each of `server1`/`server2`/`enabled` that was given, merged with `previous_state`'s value for anything left unset. |
| `observed` | `dict` | always | NTP configuration freshly re-read from `getntpcfg.asp` after a real write, in the same shape as `previous_state` — or, when nothing was written (`changed=false`, or check mode), the same value as `previous_state`. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | Always `asmb8_ntp.apply`. |
| `operation.endpoint` | `str` | always | `host:port` this operation was performed against. |
| `operation.changed` | `bool` | always | Mirrors the top-level `changed`. |
| `operation.previous` / `.desired` / `.observed` | — | always | Same values as `previous_state`/`desired_state`/`observed` above. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_ntp.py`.

## `error_class` values this module can raise

- `connection` / `authentication` / `timeout` / `tls_validation` — logging
  in or reaching the BMC at all (`AspClient.login()`).
- `protocol` — `getntpcfg.asp` returned a malformed body, **or** `NTP_STATUS`
  was absent from the read and a write is needed for an unrelated field
  (`server1`/`server2`) with `enabled` never given: this module refuses to
  guess an `ISNTPENABLE` value it has no basis for, rather than risk
  clobbering the BMC's actual NTP-enable state as a side effect of an
  unrelated change.
- `remote_operation` — `setntpcfg.asp` reported a non-zero `HAPI_STATUS` for
  the write. No BMC state should be assumed changed when this is raised.
- `bmc_busy` — the BMC accepted the connection but never served the
  request (this board's saturated-worker-pool condition).

## Check-mode behaviour

Full support. Logs in and reads `getntpcfg.asp` exactly as a real run does
— unlike this collection's read-only `.asp` modules, there is a real write
path here whose effect check mode must be able to predict, so login is not
skipped. `setntpcfg.asp` itself is never called: the top-level `changed` and
`desired_state` report exactly what a real run would do, and `observed` is
the same value as `previous_state` rather than a post-write read.
`diff_mode` is not supported — use `previous_state`/`desired_state` instead
of `--diff`.

## Example

```yaml
- name: Ensure both NTP servers and enable NTP
  james_crowley.asmb8_ikvm.asmb8_ntp:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    server1: pool.ntp.org
    server2: 192.0.2.10
    enabled: true
  delegate_to: localhost
  no_log: true
  register: ntp

- name: Preview disabling NTP without changing anything
  james_crowley.asmb8_ikvm.asmb8_ntp:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    enabled: false
  delegate_to: localhost
  no_log: true
  check_mode: true
  register: preview

- name: Restore whatever NTP configuration was in place before an earlier change
  james_crowley.asmb8_ikvm.asmb8_ntp:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    server1: "{{ ntp.previous_state.server1 }}"
    server2: "{{ ntp.previous_state.server2 }}"
    enabled: "{{ ntp.previous_state.enabled }}"
  delegate_to: localhost
  no_log: true
  when: ntp.previous_state.enabled is not none
```
