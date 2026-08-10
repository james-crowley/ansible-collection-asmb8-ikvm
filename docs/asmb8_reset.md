<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_reset`

Cold/warm-reset the ASMB8-iKVM BMC's own management controller over IPMI.

## Synopsis

Resets the BMC's management controller itself over IPMI (RMCP+, UDP), netfn
`0x06`, command `0x02` (Cold Reset) or `0x03` (Warm Reset), via `pyghmi`.
This is **not** a chassis power operation: the host stays powered and
running throughout. Only the management controller restarts — use
[`asmb8_power`](asmb8_power.md) `state=reset` to power-cycle the host
itself, a completely different IPMI operation (netfn `0x00`, chassis
control) against a different target.

**The host is unaffected. Verified live**: `get_power()` reported `on`
immediately before a Cold Reset, and the host never rebooted, stayed on, and
was never interrupted. This is what makes the operation safe enough to
automate as a recovery step rather than a last resort.

**It drops every active BMC session as a side effect, including any
in-flight virtual media** — every IPMI session, every `.asp` web-management
login, and any iUSB/KVM session currently streaming. That is the whole point
of this module's existence: this BMC's virtual-media slot (`cd-media`) is
single-occupancy, board-wide, and `getallservicescfg.asp` reports its
`SERVICE_TIMEOUT` as `4294967295` (`0xFFFFFFFF`, the "no timeout" sentinel) —
against `1800` for `web` and `600` for `ssh`. An abandoned media session is
never reclaimed server-side, so if
[`asmb8_media`](asmb8_media.md)'s own software reclamation cannot clear a
stale session (`error_class=bmc_busy` with no known `session_id` still
holding the slot), this module is the escape hatch that follows — the
automated form of the manual `ipmitool mc reset cold` recovery step this
collection's own hardware-evidence notes already point operators at.

That symptom is easy to misdiagnose: a wedged slot can present as a TCP
connection to port 5120 that is fully `ESTABLISHED`, with bytes sitting
unread in the socket's own receive queue, while zero SCSI commands are
actually being serviced. **An established socket is not evidence media is
being served** — the discriminator is whether the client's own
`vmedia: redirection accepted (instance 0, port 5120)` log line is present.
Its absence, alongside an `ESTABLISHED` socket doing nothing, is the actual
signal that a reset is warranted.

**Recovery after either mode is staged, not instantaneous or uniform across
services.** Verified live for `mode=cold`: ICMP and IPMI (UDP 623) answered
several minutes before the `.asp` web/HTTPS stack (port 443) did. A readiness
check keyed on ping therefore reports success while the web-management stack
is still down — a caller that must know the BMC is usable again should poll
the specific service it actually depends on (a fresh IPMI session via this
collection's own modules, or the `.asp` surface via
[`asmb8_info`](asmb8_info.md)), not ICMP. The on-demand iUSB media listener
(port 5120) being closed immediately after a reset is likewise normal, not a
fault: that port only binds once a fresh session allocates it, and this
operation just dropped every session that could have done so.

`mode=cold` and `mode=warm` are deliberately two separate, explicit choices
rather than one default. Per the IPMI 2.0 specification, Cold Reset is the
more thorough of the two — broadly equivalent to the management
controller's own power-up reset sequence — and is the mode **verified live**
against the target board. Warm Reset reinitializes less of the management
controller's state and is documented (by the spec, for IPMI management
controllers generally) to typically recover faster, but has not been
independently verified live against this specific board.

This module never confirms the reset itself: there is nothing `pyghmi` (or
the raw IPMI command it falls back to for `mode=warm`) gives this collection
to poll for after issuing it — the same "fire and forget" reasoning
`asmb8_power`'s own `state=reset`/`state=boot` handling already documents. A
successful `changed` means only that the BMC accepted the reset command
(completion code 0, no `error` key in `pyghmi`'s response); confirming the
management controller actually came back up is a separate, later probe a
caller must make on its own.

This module talks to the BMC over IPMI (UDP `ipmi_port`, default 623) using
only `host`, `username`, and `password` from the shared connection fragment.
`port`, `use_tls`, `allow_insecure_transport`, `validate_certs`, `ca_path`,
`tls_fingerprint`, `timeout`, and `connect_timeout` are **accepted but
entirely ignored** — kept only so a play can share one `module_defaults`
group across every module in this collection without a task failing on an
"unsupported parameter". IPMI-over-LAN has no TLS layer, and none of the
`.asp` web-management surface those options describe is touched here. The
`requests` entry under `requirements` inherited from the shared connection
fragment does **not** apply to this module — only `pyghmi` is actually
required.

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
| `mode` | `str` | — | yes | `cold`, `warm` |

Verified against `argument_spec()` in `plugins/modules/asmb8_reset.py`.

### `mode`

- `cold` — Cold Reset (netfn `0x06` cmd `0x02`), issued via `pyghmi`'s own
  `Command.reset_bmc()`. The mode verified live against the target board.
- `warm` — Warm Reset (netfn `0x06` cmd `0x03`), issued via `pyghmi`'s
  `raw_command()` directly (no dedicated `pyghmi` wrapper exists for it).
  Not independently verified live against this board.

Both drop every active BMC session (see the Synopsis) and leave host power
untouched. There is no default — the choice must be explicit in every
playbook and in `ansible-doc`.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `mode` | `str` | always | The requested `mode`, echoed back. |
| `observed` | `dict` | always | What was actually issued. `null` in check mode. Otherwise a small dict: `mode` plus whatever, if anything, `pyghmi`'s underlying call returned — empty for `mode=cold` (`reset_bmc()` itself returns nothing), potentially non-empty raw completion data for `mode=warm` (`raw_command()`). |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | `asmb8_reset.<mode>`, e.g. `asmb8_reset.cold`. |
| `operation.endpoint` | `str` | always | `host:ipmi_port` this operation was (or, in check mode, would be) performed against. |
| `operation.changed` | `bool` | always | Always `true` for a real run — a self-reset is never idempotent. Mirrors the top-level `changed`. |
| `operation.previous` | `dict` | always | Always `null` — there is no prior state for a self-reset to compare against. |
| `operation.desired` | `str` | always | Same value as `mode`. |
| `operation.observed` | — | always | Same value as `observed` above. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_reset.py`.

## `error_class` values this module can raise

- `connection` / `authentication` / `timeout` — establishing the IPMI
  session (`IpmiClient._connect`), classified from `pyghmi`'s own error
  text.
- `remote_operation` — `reset_bmc()`/the raw Warm Reset command itself
  failed at the IPMI level (a raised `pyghmi` exception, or a returned dict
  carrying an `error` key).

There is no `indeterminate` case for this module: unlike `asmb8_power`,
nothing here is ever polled for confirmation, so a failure always means the
reset command itself was not accepted — never "accepted, but confirmation
timed out."

## Check-mode behaviour

Full support, and deliberately conservative: check mode does not open an
IPMI connection at all. There is no "previous state" for a self-reset to
read or compare against — unlike `state`-based modules such as
`asmb8_power`/`asmb8_boot`, this operation is never idempotent, and always
issues the reset when run for real. A dry run therefore touches the network
in no way whatsoever and never resets anything. `diff_mode` is not
supported — use `operation.desired`/`operation.observed` instead of
`--diff`.

## Example

```yaml
- name: Recover a wedged virtual-media slot with a cold reset
  james_crowley.asmb8_ikvm.asmb8_reset:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    mode: cold
  delegate_to: localhost
  no_log: true
  register: reset_result

- name: Issue a faster warm reset when a full cold reinitialization is not needed
  james_crowley.asmb8_ikvm.asmb8_reset:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    mode: warm
  delegate_to: localhost
  no_log: true

- name: Preview the reset without touching the network at all
  james_crowley.asmb8_ikvm.asmb8_reset:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    mode: cold
  delegate_to: localhost
  no_log: true
  check_mode: true

- name: Wait for the management controller to actually be usable again after a reset
  james_crowley.asmb8_ikvm.asmb8_info:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    fields: [power_state]
  delegate_to: localhost
  no_log: true
  register: post_reset_probe
  retries: 20
  delay: 5
  until: post_reset_probe is succeeded
```

## See also

- [`asmb8_power`](asmb8_power.md) — power-cycling the host itself, a
  completely different operation from this module.
- [`asmb8_media`](asmb8_media.md) — the virtual-media module whose own
  software reclamation should be tried first, before reaching for this
  module as the escape hatch.
- [`asmb8_info`](asmb8_info.md) — the recommended probe for confirming the
  management controller is usable again after a reset, rather than polling
  ICMP.
- [`docs/hardware-evidence-2026-08-08.md`](hardware-evidence-2026-08-08.md) —
  the full, dated record this page's live-verified claims are drawn from.
