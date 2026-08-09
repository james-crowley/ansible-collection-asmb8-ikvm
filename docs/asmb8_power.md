<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_power`

Control and query ASMB8-iKVM power state over IPMI.

## Synopsis

Reads and changes the power state of an ASMB8-iKVM managed endpoint over IPMI
(RMCP+, UDP), via `pyghmi`'s `Command.get_power()`/`set_power()` directly —
**not** by wrapping `community.general.ipmi_power`. This is this collection's
already-working, non-reverse-engineered path: no `.asp` RPC or JNLP surface is
involved, and nothing about this module's behaviour depends on anything still
under investigation for this hardware.

`state=on`/`state=off` are convergent: `pyghmi`'s `get_power()` can only ever
report `on` or `off` (confirmed by reading its source directly), so nothing is
sent when the endpoint already reports the requested state. `state=shutdown`,
`state=reset`, and `state=boot` can never compare equal to a reported power
state and are therefore always imperative — every successful run issues the
underlying IPMI command, matching `community.general.ipmi_power`'s own
convergence check exactly (the same comparison, not a separately maintained
table).

A successfully issued command only means the BMC **accepted** it; whether the
transition itself completed is a separate question this module answers by
letting `pyghmi`'s own bounded confirmation loop run (`wait_timeout`). If that
loop exhausts its budget without observing the target state, this module fails
with `error_class=timeout` **and** `indeterminate=true` — the command was
accepted, only confirmation of it timed out, so the BMC may already have
applied the change. Callers must re-probe (a second `state=on` task, or
`asmb8_info`) rather than blindly retrying.

This module talks to the BMC over IPMI (UDP `ipmi_port`, default 623) using
only `host`, `username`, and `password` from the shared connection fragment.
`port`, `use_tls`, `allow_insecure_transport`, `validate_certs`, `ca_path`,
`tls_fingerprint`, `timeout`, and `connect_timeout` are **accepted but
entirely ignored** — kept only so a play can share one `module_defaults`
group across every module in this collection without a task failing on an
"unsupported parameter". Consequence worth naming explicitly: the `requests`
entry under `requirements` inherited from the shared connection fragment does
**not** apply to this module — only `pyghmi` is actually required.

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
| `state` | `str` | — | yes | `on`, `off`, `shutdown`, `reset`, `boot` |
| `wait_timeout` | `int` | `60` | no | — |

Verified against `_connection_argument_spec()`/`argument_spec()` in
`plugins/modules/asmb8_power.py`. `state`'s choices are `pyghmi`'s own
`set_power()`/`get_power()` vocabulary, which is exactly
`community.general.ipmi_power`'s documented `state` choices, sourced from that
module's own documentation (`plugins/module_utils/models.py`'s
`POWER_STATES`), not invented here.

### `state`

- `on` — request the system turn on. Convergent.
- `off` — request the system turn off without waiting for the OS to shut
  down. Convergent.
- `shutdown` — ask the OS to shut down cleanly (requires OS/ACPI cooperation).
  Always issued.
- `reset` — request an immediate reset without waiting for the OS. Always
  issued.
- `boot` — `pyghmi`'s own "smart" action: turns the system on if it is off,
  otherwise resets it. Always issued, since this module cannot know in
  advance which of the two it will resolve to.

### `wait_timeout`

Seconds to let `pyghmi`'s own confirmation loop run after issuing a command,
for `state` values it can actually confirm (`on`, `off`, `shutdown`). `reset`
and `boot` are **never** confirmed by `pyghmi` regardless of this value —
there is nothing to poll for after a reset. `wait_timeout=0` issues the
command and returns immediately without waiting for confirmation at all;
`observed` is then `pyghmi`'s raw, unconfirmed response rather than a freshly
re-read power state.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `state` | `str` | always | The requested `state`, echoed back. |
| `previous_state` | `dict` | always | Power state observed before any action, exactly as `pyghmi`'s `get_power()` returned it (a dict with a single `powerstate` key). |
| `desired_state` | `str` | always | Same value as `state`. |
| `observed` | `dict` | always | What `set_power()`/`get_power()` returned after acting, or — when nothing was sent because the state already matched — the same value as `previous_state`. Shape varies: a confirmed transition carries `powerstate`; an unconfirmed one carries `pendingpowerstate` instead. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | `asmb8_power.<state>`, e.g. `asmb8_power.on`. |
| `operation.endpoint` | `str` | always | `host:ipmi_port` this operation was performed against. |
| `operation.changed` | `bool` | always | Mirrors the top-level `changed`. |
| `operation.previous` / `.desired` / `.observed` | — | always | Same values as `previous_state`/`desired_state`/`observed` above. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_power.py`.

## `error_class` values this module can raise

- `connection` / `authentication` / `timeout` — establishing the IPMI session
  (`IpmiClient._connect`), classified from `pyghmi`'s own error text.
- `remote_operation` — `get_power()`/`set_power()` itself failed at the IPMI
  level.
- `timeout` **with `indeterminate=true`** — the power command was accepted
  but `pyghmi`'s own confirmation loop (`wait_timeout`) exhausted its budget
  without observing the target state. This is the one case in this module
  where a failure does not mean the request was rejected — re-probe, do not
  retry.

## Check-mode behaviour

Full support. The current power state is read and compared against `state`
exactly as in normal mode, but the IPMI power command is never sent
(`attributes.check_mode.support: full`). `diff_mode` is not supported — use
`previous_state`/`desired_state` instead of `--diff`.

## Example

```yaml
- name: Ensure the endpoint is powered on
  james_crowley.asmb8_ikvm.asmb8_power:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    state: "on"
  delegate_to: localhost
  no_log: true
  register: power

- name: Force an immediate reset, without waiting for the OS
  james_crowley.asmb8_ikvm.asmb8_power:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    state: reset
  delegate_to: localhost
  no_log: true

- name: Preview a power-on without sending anything
  james_crowley.asmb8_ikvm.asmb8_power:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    state: "on"
  delegate_to: localhost
  no_log: true
  check_mode: true
```
