<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_identify`

Control the ASMB8-iKVM chassis identify LED over standard IPMI.

## Synopsis

Turns the chassis identify LED on (indefinitely, or for a bounded duration) or
off, via the standard IPMI Chassis Identify command (netfn `0x00`, cmd
`0x04`) — `pyghmi`'s `Command.set_identify()`. This is the same
already-working, non-reverse-engineered IPMI path `asmb8_power`/`asmb8_boot`/
`asmb8_reset` use: no `.asp` RPC or JNLP surface is involved, and this module
carries **no lockout risk whatsoever** — it can only ever change whether a
light is lit, never power state, boot device, or any session.

**This is the best value-per-effort capability left on this board**: useful
the moment you are physically in a rack trying to find a machine, and it
required no protocol reverse engineering at all — only reading `pyghmi`'s own
installed source.

## What was verified about `pyghmi`, and where

Verified directly against `pyghmi` 1.6.19's installed source, in a disposable
virtualenv with no BMC reachable from it — never from memory, and never by
making a request against any real BMC:

* `pyghmi.ipmi.command.Command.set_identify(on=True, duration=None,
  blink=False)` (`command.py:555-596`) always calls `self.oem_init()` first,
  which resolves an OEM handler via
  `pyghmi.ipmi.oem.lookup.get_oem_handler()`.
* That lookup's `oemmap` (`pyghmi/ipmi/oem/lookup.py`) contains exactly three
  entries — `20301` (IBM x86 / System X), `19046` (Lenovo x86), `7154` — all
  routed to the same Lenovo handler module. **American Megatrends (this
  board's manufacturer) is not a key in that map**, so `get_oem_handler()`
  falls through to its own `else` branch and returns
  `pyghmi.ipmi.oem.generic.OEMHandler` instead.
* `generic.OEMHandler.set_identify()` (`oem/generic.py:398-404`)
  unconditionally `raise exc.UnsupportedFunctionality()` — it exists only so
  a real vendor handler can override it. `Command.set_identify()` catches
  exactly that exception and falls through to the **standard IPMI command**
  body immediately below it. The exception never reaches this collection's
  caller on this board.
* That standard-command body is where the interval/force-on semantics below
  come from, and where `blink=True` always raises
  `IpmiException('Blink not supported with generic IPMI')`, unconditionally.
* On every successful branch, `set_identify()` returns `None` — a bare
  `return`, never a dict the way `get_power()`/`get_bootdev()` do.

Every one of these facts is cited by file and line in
`plugins/module_utils/ipmi.py`'s own docstring.

## Interface chosen

* `state`: `on` or `off` — not `pyghmi`'s own bare `on: bool`, because a
  module option reads better as a named choice than a boolean flag would.
* `duration`: optional integer seconds, only valid with `state=on`. Omitted
  entirely, it requests the LED stay on **indefinitely** (`pyghmi`'s "Force
  Identify On" behaviour — a two-byte command, `[0, 1]`). Supplied, it
  requests the LED stay on for that many seconds and then turn itself off
  (a one-byte command carrying only the Identify Interval).
* **No `blink` option exists at all.** `pyghmi`'s own generic fallback — the
  only path ever reachable on this board — cannot honour it (see above), so
  this module never offers a capability it cannot deliver.

Two footguns in `pyghmi`'s own standard-command path are refused before any
IPMI session is opened, rather than passed through silently:

* `state=off` combined with `duration` set: `pyghmi` ignores `on` entirely
  whenever `duration` is not `None` (see above), so sending a duration
  alongside `state=off` would not do what a caller might expect. Refused
  with a plain parameter-validation failure — `validate_duration()` in
  `plugins/modules/asmb8_identify.py` — before `build_ipmi_client()` is ever
  called.
* `state=on` combined with `duration=0` (or negative): the IPMI 2.0
  specification defines Identify Interval `0` as "turn off", **regardless**
  of `on`. Sending `state=on, duration=0` would silently turn the LED
  **off** — the opposite of the stated intent. Refused the same way, for the
  same reason.

Values above `255` are accepted but not independently validated — `pyghmi`
itself clamps them to `255` (the Identify Interval field is one byte), and
this module documents that clamp rather than duplicating it.

## How the idempotence problem was handled

**Standard IPMI Chassis Identify has no read-back command.** There is no way
to ask a BMC "is the identify LED currently on", and `pyghmi`'s
`set_identify()` itself returns `None` on success — no state to inspect even
if there were something to compare it against.

This module does not fake idempotence by guessing. `changed` is **always**
`true` on a real run — the same honesty `asmb8_reset` already applies to a
self-reset, and for the identical underlying reason (no prior state exists
to compare against). `check_mode` never opens an IPMI connection at all
(there is nothing it could read even if it tried), and reports `changed:
true` too, matching what a real run always reports. This is documented
explicitly in the module's `DOCUMENTATION` and in `attributes.check_mode`.

## Options

| Option | Type | Default | Required | Choices |
|---|---|---|---|---|
| `host` | `str` | — | yes | — |
| `port` | `int` | `443` | no | — (accepted, **ignored**) |
| `username` | `str` | `admin` | no | — |
| `password` | `str` (`no_log`) | — | yes | — |
| `use_tls` | `bool` | `true` | no | — (accepted, **ignored**) |
| `allow_insecure_transport` | `bool` | `false` | no | — (accepted, **ignored**) |
| `validate_certs` | `bool` | `true` | no | — (accepted, **ignored**) |
| `ca_path` | `path` | — | no | — (accepted, **ignored**) |
| `tls_fingerprint` | `str` | — | no | — (accepted, **ignored**) |
| `timeout` | `int` | `30` | no | — (accepted, **ignored**) |
| `connect_timeout` | `int` | `10` | no | — (accepted, **ignored**) |
| `ipmi_port` | `int` | `623` | no | — |
| `state` | `str` | — | yes | `on`, `off` |
| `duration` | `int` | — | no | — |

Verified against `_connection_argument_spec()`/`argument_spec()` in
`plugins/modules/asmb8_identify.py`.

This module talks to the BMC over IPMI (UDP `ipmi_port`, default 623) using
only `host`, `username`, and `password` from the shared connection fragment,
exactly like `asmb8_power`/`asmb8_boot`/`asmb8_reset`. `port`, `use_tls`,
`allow_insecure_transport`, `validate_certs`, `ca_path`, `tls_fingerprint`,
`timeout`, and `connect_timeout` are **accepted but entirely ignored** —
kept only so a play can share one `module_defaults` group across every
module in this collection without a task failing on an "unsupported
parameter". The `requests` entry under `requirements` inherited from the
shared connection fragment does **not** apply to this module — only `pyghmi`
is actually required.

### `state`

- `on` — turn the LED on, either indefinitely (`duration` omitted) or for a
  bounded number of seconds (`duration` set).
- `off` — turn the LED off immediately. `duration` must not be set alongside
  `off`.

### `duration`

Seconds to leave the LED on before it turns itself off again. Only valid
with `state=on`. Must be at least `1` when supplied with `state=on` — `0` is
refused, not silently sent, for the reason given above. Omit entirely with
`state=on` to request the LED stay on indefinitely instead of for a bounded
time.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `state` | `str` | always | The requested `state`, echoed back. |
| `duration` | `int` | always | The requested `duration`, echoed back. `null` when not supplied. |
| `observed` | `dict` | always | What was actually requested (`{'state': ..., 'duration': ...}`), or `null` in check mode. There is no independent confirmation from the BMC this could carry instead — see the idempotence note above. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | `asmb8_identify.<state>`, e.g. `asmb8_identify.on`. |
| `operation.endpoint` | `str` | always | `host:ipmi_port` this operation was (or, in check mode, would be) performed against. |
| `operation.changed` | `bool` | always | Always `true` on a real run. Mirrors the top-level `changed`. |
| `operation.previous` | `dict` | always | Always `null` — there is no prior state to compare against. |
| `operation.desired` / `.observed` | — | always | Same value as `observed` above. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_identify.py`.

## `error_class` values this module can raise

- A plain parameter-validation failure (no `error_class` at all — the same
  tier a missing `pyghmi` dependency fails at) for the two `state`/`duration`
  combinations refused before any connection is opened — see above.
- `connection` / `authentication` / `timeout` — establishing the IPMI session
  (`IpmiClient._connect`), classified from `pyghmi`'s own error text, exactly
  as in `asmb8_power`/`asmb8_boot`/`asmb8_reset`.
- `unsupported_capability` — `pyghmi`'s standard-command fallback rejected
  `blink=True` (defensive; this module never sends it) or raised
  `UnsupportedFunctionality` directly (defensive; as of `pyghmi` 1.6.19 this
  is always caught internally and never escapes — see above).
- `remote_operation` — the BMC rejected the underlying raw IPMI command
  (a non-zero completion code).

## Check-mode behaviour

Full support, and deliberately conservative: check mode **never opens an
IPMI connection at all** — there is no previous state for chassis identify to
read or compare against, so a dry run touches the network in no way
whatsoever (`attributes.check_mode.support: full`). `changed` is reported
`true` in check mode too, matching what a real run always reports.
`diff_mode` is not supported — use `operation.desired`/`operation.observed`
instead of `--diff`.

## Example

```yaml
- name: Turn on the identify LED indefinitely, to find a host physically in a rack
  james_crowley.asmb8_ikvm.asmb8_identify:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    state: "on"
  delegate_to: localhost
  no_log: true

- name: Blink the identify LED on for five minutes, then let it turn itself off
  james_crowley.asmb8_ikvm.asmb8_identify:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    state: "on"
    duration: 300
  delegate_to: localhost
  no_log: true

- name: Turn the identify LED back off once the host has been located
  james_crowley.asmb8_ikvm.asmb8_identify:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    state: "off"
  delegate_to: localhost
  no_log: true

- name: Preview turning the LED on without touching the network at all
  james_crowley.asmb8_ikvm.asmb8_identify:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    state: "on"
  delegate_to: localhost
  no_log: true
  check_mode: true
```
