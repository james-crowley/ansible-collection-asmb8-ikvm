<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_info`

Gather ASMB8-iKVM capability and state facts. Read-only.

## Synopsis

Reads IPMI-observed facts from an ASMB8-iKVM BMC — power state, boot-device
override, and management-controller identity — over the same IPMI path
`asmb8_power`/`asmb8_boot` use, plus (only when explicitly requested) a small
set of read-only facts from the `.asp` web-management surface.

**This module never mutates IPMI state** and always reports `changed=false`.
The one deliberate exception is `include_web_session`: authenticating against
the BMC's web session (`POST /rpc/WEBSES/create.asp`) creates real BMC-side
session state even though everything this module subsequently reads over that
session is itself read-only. That is opted into explicitly, never on by
default.

**`asmb8.capabilities` is deliberately honest about what has and has not been
proven.** Virtual media and remote console/KVM redirection are reported as
`supported: null` (unknown, not `false`) and `proven: false` — see
[`docs/capability-matrix.md`](capability-matrix.md). This module never fetches
the KVM/media session JNLP (`/Java/jviewer.jnlp`), because doing so allocates a
BMC-side session as a side effect of the fetch itself (see
`plugins/module_utils/asp.py`) — not a read, no matter how read-only the
intent — and a module documented as never mutating anything must not perform
it implicitly. `asmb8.media.port_mode` is therefore always `"unknown"` here;
determining it for real requires `asmb8_media`.

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
| `ipmi_port` | `int` | `623` | no | — |
| `include_web_session` | `bool` | `false` | no | — |

`port`, `use_tls`, `allow_insecure_transport`, `validate_certs`, `ca_path`,
`tls_fingerprint`, `timeout`, and `connect_timeout` are only consulted when
`include_web_session=true`; they still validate as ordinary options otherwise
(for `module_defaults` group compatibility). Verified against
`_connection_argument_spec()`/`argument_spec()` in `plugins/modules/asmb8_info.py`.

### `include_web_session`

Defaults to `false` on purpose. When `true` and the login itself fails, this
module **fails** — it does not silently degrade `asmb8.web_management` to
`null` — because a caller that explicitly opted in almost certainly wants to
know that credentials or connectivity are broken, not have that hidden behind
a successful-looking IPMI-only result.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `asmb8.reachable` | `bool` | always | Whether the IPMI session could be established at all. |
| `asmb8.ipmi.power_state` | `dict` | when available | `pyghmi`'s `get_power()` verbatim, or `null` if this particular read failed. |
| `asmb8.ipmi.boot_device` | `dict` | when available | `pyghmi`'s `get_bootdev()` verbatim; `uefimode` may be absent on the branch where the BMC reports no standing override at all. |
| `asmb8.ipmi.mc_info` | `str` | when available | `pyghmi`'s `get_mci()` — a bare string, **not** a dict, unlike the two facts above. |
| `asmb8.web_management.logged_in` | `bool` | when `include_web_session=true` | Always `true` when present — a rejected login fails the whole module instead. |
| `asmb8.web_management.host_status_raw` | `str` | when `include_web_session=true` | Raw `hoststatus.asp` text, truncated. **Unparsed and unverified shape** — see `plugins/module_utils/asp.py`'s own TODO on this endpoint. |
| `asmb8.capabilities.ipmi_power` / `.ipmi_boot_device` / `.ipmi_mc_info` | `dict` | always | `{supported: true, proven: true, note: ...}` — proven live against the target board. |
| `asmb8.capabilities.web_management` | `dict` | always | `proven` is `true` only when `include_web_session=true` **and** the login succeeded on this run. |
| `asmb8.capabilities.virtual_media` / `.remote_console` | `dict` | always | `{supported: null, proven: false, ...}` — not yet proven against this hardware. `null` (not `false`) means unknown, not unsupported. |
| `asmb8.capabilities.redfish` | `dict` | always | Always `{supported: false, proven: true, ...}` — a confirmed hardware-generation fact (AST2400 predates Redfish), never a live probe. |
| `asmb8.media.port_mode` | `str` | always | Always `"unknown"` — see Synopsis. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | Always `"get_facts"`. |
| `operation.endpoint` | `str` | always | `host:ipmi_port` this read was performed against. |
| `operation.changed` | `bool` | always | Always `false`. |
| `operation.error_class` | `str` | always | `null` on success. |
| `operation.ipmi_reads.<field>.outcome` | `str` | always | `read` or `failed`, per IPMI fact attempted. |
| `operation.ipmi_reads.<field>.error_class` | `str` | always | The failure class, on `failed` only; `null` otherwise. |

Verified against the `RETURN` block and `gather_ipmi_facts`/`build_capabilities`
in `plugins/modules/asmb8_info.py`.

## `error_class` values this module can raise

From `plugins/module_utils/errors.py`, via `IkvmError` subclasses:

- `connection` / `authentication` / `timeout` — establishing the IPMI session
  itself (`IpmiClient._connect`).
- `remote_operation` — an individual IPMI read that is not degraded per-field
  (only the initial `IpmiClient` construction can raise all the way out of
  this module on the IPMI side; individual IPMI *reads* are caught and
  recorded in `operation.ipmi_reads` instead, per field).
- `protocol` — the `.asp` login response did not contain a `SESSION_COOKIE`
  value at all, when `include_web_session=true`.
- `authentication` — the `.asp` login itself was rejected (this board answers
  bad credentials with HTTP 200 and a `Failure_Login_*` marker, not a 4xx —
  see `plugins/module_utils/asp.py`), when `include_web_session=true`.
- `tls_validation` / `connection` / `timeout` / `bmc_busy` — any of these can
  also surface from the `.asp` login or the subsequent `hoststatus.asp` read
  when `include_web_session=true`, via the same `AspClient` machinery
  `asmb8_media` uses (TLS handshake/trust failures, an unreachable BMC, a
  pre-connect timeout, or this board's saturated-worker-pool hang — see
  [`docs/asmb8_media.md`](asmb8_media.md)'s `error_class` section for what
  each of these means).

Note the asymmetry on the IPMI side specifically: IPMI session establishment
failure fails the whole module; individual post-connection IPMI *read*
failures (`get_power`, `get_bootdev`, `get_mci`) degrade to `null` with the
failure recorded in `operation.ipmi_reads`, never failing the module outright.
On the `.asp` side (`include_web_session=true` only), any failure — login or
the one diagnostic read that follows it — fails the whole module; see that
option's own documentation for why.

Note the asymmetry: session establishment failures (IPMI connect, `.asp`
login) fail the whole module; individual post-connection IPMI *read* failures
(`get_power`, `get_bootdev`, `get_mci`) degrade to `null` with the failure
recorded in `operation.ipmi_reads`, never failing the module outright.

## Check-mode behaviour

Full support. A read-only module runs identically in check mode, since there
is nothing to preview a plan for — `attributes.check_mode.support: full` in
the module's own `DOCUMENTATION`. `diff_mode` is not supported: there is no
prior/after state to diff for a read.

## Example

```yaml
- name: Read IPMI facts only (no BMC-side session created)
  james_crowley.asmb8_ikvm.asmb8_info:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
  delegate_to: localhost
  no_log: true
  register: asmb8

- name: Require IPMI to be reachable before attempting a power/boot change
  ansible.builtin.assert:
    that:
      - asmb8.asmb8.reachable
      - asmb8.asmb8.ipmi.power_state is not none

- name: Do not attempt virtual media -- this module reports it as unproven, not available
  ansible.builtin.assert:
    that:
      - not asmb8.asmb8.capabilities.virtual_media.supported

- name: Also read a small amount of web-management diagnostic state
  james_crowley.asmb8_ikvm.asmb8_info:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    include_web_session: true
  delegate_to: localhost
  no_log: true
  register: asmb8_with_web
```
