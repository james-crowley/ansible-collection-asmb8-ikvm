<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_redirection`

Report and optionally toggle ASMB8-iKVM service enablement.

## Why this module exists (and what used to be here)

Before the first Galaxy release, this name belonged to a different module: it
opened an IVTP console/KVM session — closer to what `asmb8_media` does than
to what the sibling
[`james_crowley.intel_amt`](https://github.com/james-crowley/ansible-collection-intel-amt)
collection's `amt_redirection` module does. That was a naming and semantics
mistake: anyone arriving from the sibling collection, where `amt_redirection`
only *reports and toggles a service-enablement flag* and never itself opens a
session, would be actively misled. That implementation moved, essentially
unchanged, to the new [`asmb8_console`](asmb8_console.md) module. This module
was rewritten from scratch to actually match `amt_redirection`'s shape and
name.

## Synopsis

Reports, and optionally would toggle, whether this BMC's own listed services —
`web`, `kvm`, `cd-media`, `fd-media`, `hd-media`, `ssh`, `telnet`, exactly as
the BMC's own Services page names them — are enabled, and whether each
service's TCP port is actually reachable right now. **It never opens a
console or media session itself** — see [`asmb8_console`](asmb8_console.md)
and [`asmb8_media`](asmb8_media.md) for that.

### The three-signal discipline

Mirrors `amt_redirection` precisely: three separate signals are always
reported per service, and never collapsed into one boolean:

- **`known`** — is this a service name this BMC's own Services page lists at
  all.
- **`enabled`** — does the BMC report it Active.
- **`reachable`** — does a bare TCP connect to its port(s) actually succeed
  right now.

### Why this matters *more* here than on Intel AMT

`kvm`/`cd-media`/`fd-media`/`hd-media` (`on_demand: true`) are **on-demand**
listeners on this board: they return TCP RST until a `.asp` login plus
`GET /Java/jviewer.jnlp?EXTRNIP=<ip>&JNLPSTR=JViewer` allocates a session (see
[`asmb8_console`](asmb8_console.md) and
`plugins/module_utils/asp.py`'s `allocate_media_session`). **"Active but
unreachable" is this module's normal resting state for those four services,
not a fault.** Collapsing `enabled` and `reachable` into one boolean would
report a perfectly healthy, idle board as broken. `web`/`ssh`/`telnet`
(`on_demand: false`) are *not* on-demand — their ports listen continuously
whenever the service is enabled, so an unreachable port there is a more
meaningful signal.

### Where `known`/`enabled` actually come from — read this before trusting them

`known` and `enabled` are read from a **static catalog built into this
module**, sourced from the BMC's own Services page as read from its web UI by
this collection's maintainer — **not observed on the wire**, and **not
re-queried live by this module on any run**, because no sourced `.asp` RPC
exists for fetching that page's live state (see the next section).
`docs/hardware-evidence-2026-08-08.md`'s "Service capacities, and a provenance
caveat" section is the source and carries the caveat this module preserves
exactly: the port numbers and the plaintext/secure split were confirmed on the
wire; the session-timeout and max-session figures, and the live Active/
Inactive state itself, are the BMC's own self-report only. `reachable` is the
one signal this module actually probes fresh, live, on every run — a bare TCP
connect-and-close, never a byte of any BMC protocol.

### Mutation: investigated, and honestly refused

`plugins/module_utils/asp.py` — this BMC's only documented RPC surface — was
checked for a way to toggle a service's enablement. **None exists.** Nothing
in this collection's sourced material (the decompiled vendor client, the
third-party reference clients, the live capture) documents an endpoint for
this. Rather than guess at one, `state` always fails with
`error_class=unsupported_capability`, before any network is touched, with a
message pointing at the BMC's own web UI. A future release can add real
mutation once an RPC for it is confirmed.

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
| `services` | `list` of `str` | all seven | no | `web`, `kvm`, `cd-media`, `fd-media`, `hd-media`, `ssh`, `telnet` |
| `service` | `str` | — | required with `state` | same seven |
| `state` | `str` | — | no | `enabled`, `disabled` |
| `probe_timeout` | `float` | `2.0` | no | — |

Verified against `argument_spec()` and `required_by={"state": ["service"]}`
in `plugins/modules/asmb8_redirection.py`.

**`port`, `username`, `password`, `use_tls`, `allow_insecure_transport`,
`validate_certs`, `ca_path`, `tls_fingerprint`, `timeout`, and
`connect_timeout` are accepted (so a play can share `module_defaults` across
every module in this collection's `asmb8_ikvm` action group without one task
failing on an "unsupported parameter") but are entirely ignored here** — this
module never authenticates against the BMC at all; it only uses `host` (for
the reachability probes) and `probe_timeout`. Unlike every other module in
this collection except `asmb8_power`/`asmb8_boot`, `asmb8_redirection`
requires neither `requests` nor `pyghmi` — its only network activity is a
bare TCP connect via the Python standard library's `socket` module.

### `services`

Which services to report on. Defaults to all seven. Every name is exactly as
the BMC's own Services page spells it (see
`plugins/modules/asmb8_redirection.py`'s `SERVICE_CATALOG`).

### `service` / `state`

`state`, if given, requires `service` alongside it (`required_by`) and always
fails with `error_class=unsupported_capability` — see "Mutation: investigated,
and honestly refused" above. This fails identically whether or not check mode
is set.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `changed` | `bool` | always | Always `false`. |
| `services.<name>.known` | `bool` | always | Whether `<name>` is in this module's catalog. Always `true` for every name `services` can contain. |
| `services.<name>.on_demand` | `bool` | always | `true` for `kvm`/`cd-media`/`fd-media`/`hd-media`; `false` for `web`/`ssh`/`telnet`. |
| `services.<name>.enabled` | `bool` | always | From the static catalog (see above) — `true` for every service except `telnet`. |
| `services.<name>.capacity.nonsecure_port` | `int` | always | Plaintext TCP port, or `null` (`ssh` has none). |
| `services.<name>.capacity.secure_port` | `int` | always | TLS-wrapped TCP port, or `null` (`telnet` has none). |
| `services.<name>.capacity.timeout_seconds` | `int` | always | Server-side inactivity timeout, or `null` (the media services have none). |
| `services.<name>.capacity.max_sessions` | `int` | always | Maximum concurrent sessions, or `null` if not reported. |
| `services.<name>.reachable.nonsecure` | `dict` | always | `{port, reachable}`, or `null` if this service has no nonsecure port. **Live, this-run-only.** |
| `services.<name>.reachable.secure` | `dict` | always | `{port, reachable}`, or `null` if this service has no secure port. **Live, this-run-only.** |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | Always `"asmb8_redirection.report"`. |
| `operation.endpoint` | `str` | always | The bare `host` — there is no single port to report; see `services`. |
| `operation.changed` | `bool` | always | Always `false`. |
| `operation.observed` | `dict` | always | Mirrors `services`. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_redirection.py`.

## `error_class` values this module can raise

- `unsupported_capability` — `state` was given (see "Mutation: investigated,
  and honestly refused" above). This is the only failure mode this module has;
  the reachability probe itself never raises — a refused/timed-out connect is
  reported as `reachable: false`, not a module failure.

## Check-mode behaviour

Full support, and identical to normal mode: this module never mutates, and
its only network activity (a bare TCP connect-and-close per port) is safe to
run in check mode too. `diff_mode` is not supported.

## Example

```yaml
- name: Report every known service's enablement and reachability
  james_crowley.asmb8_ikvm.asmb8_redirection:
    host: "{{ asmb8_host }}"
  delegate_to: localhost
  no_log: true
  register: services

- name: A KVM service that is enabled but unreachable is healthy, not broken -- no session is open
  ansible.builtin.assert:
    that:
      - services.services.kvm.on_demand
      - services.services.kvm.enabled
      # services.services.kvm.reachable.nonsecure.reachable may legitimately be false here.

- name: Requesting a service-enablement change fails honestly instead of guessing at an endpoint
  james_crowley.asmb8_ikvm.asmb8_redirection:
    host: "{{ asmb8_host }}"
    service: telnet
    state: enabled
  delegate_to: localhost
  no_log: true
  register: toggle_attempt
  ignore_errors: true

- name: Assert the toggle attempt failed the honest way
  ansible.builtin.assert:
    that:
      - toggle_attempt is failed
      - toggle_attempt.error_class == 'unsupported_capability'
```

## See also

- [`asmb8_console`](asmb8_console.md) — opens a live IVTP console/KVM session.
  This is what used to live under this module's own name; read its own "Why
  this module exists" note for the full story.
- [`asmb8_media`](asmb8_media.md), [`asmb8_info`](asmb8_info.md).
- The sibling `james_crowley.intel_amt` collection's `amt_redirection` module,
  which this module's shape is deliberately modelled on.
