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
| `include_media_preconditions` | `bool` | `false` | no | — (requires `include_web_session=true`) |

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

### `include_media_preconditions`

Defaults to `false`. **Requires `include_web_session=true`** — this module
fails immediately, before reading anything, if this is `true` while
`include_web_session` is not. Reads `getremotesession.asp` and
`getvmediacfg.asp` over the same authenticated `.asp` session
`include_web_session` creates (no second login), and reports the settings that
actually gate whether a virtual-media attach can succeed, as
`asmb8.media.preconditions` — without attempting an attach.

**Why this exists.** Per
[`docs/hardware-evidence-2026-08-08.md`](hardware-evidence-2026-08-08.md)'s
"Redirection rejection status `3` means bad token" section, a bare protocol
rejection (`vmedia: redirection not accepted (status 3)`, or this collection's
own `error_class=bmc_busy`) does not by itself say *why*. Two wrong theories
were chased for hours in that incident — a media session stranded by a network
outage, then a BMC cold reset having reverted media settings — costing two
wasted boot cycles and an unnecessary reset. Reading `getremotesession.asp`/
`getvmediacfg.asp` first would have ruled out both suspected settings in one
call each. **This option is the recommended first diagnostic step** before
re-attempting an attach or reaching for a BMC reset.

Costs two additional `GET` requests beyond `include_web_session` alone. This
BMC's web server has a small per-listener worker pool and no keep-alive (see
`plugins/module_utils/asp.py`); every request this module's `.asp` client
issues is already serialized, and this option adds no concurrency of its own —
avoid combining it with a concurrent play against the same BMC regardless.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `asmb8.reachable` | `bool` | always | Whether the IPMI session could be established at all. |
| `asmb8.ipmi.power_state` | `dict` | when available | `pyghmi`'s `get_power()` verbatim, or `null` if this particular read failed. |
| `asmb8.ipmi.boot_device` | `dict` | when available | `pyghmi`'s `get_bootdev()` verbatim; `uefimode` may be absent on the branch where the BMC reports no standing override at all. |
| `asmb8.ipmi.mc_info` | `str` | when available | `pyghmi`'s `get_mci()` — a bare string, **not** a dict, unlike the two facts above. |
| `asmb8.web_management.logged_in` | `bool` | when `include_web_session=true` | Always `true` when present — either a rejected login or `hoststatus.asp` returning a session-expired-looking body (see below) fails the whole module instead of appearing here. |
| `asmb8.web_management.host_status_raw` | `str` | when `include_web_session=true` | Raw `hoststatus.asp` text, truncated. **Unparsed and unverified shape** — see `plugins/module_utils/asp.py`'s own TODO on this endpoint. Checked, before this module ever sees it, against `looks_like_session_expired_html()` — see "Diagnosing a failed virtual-media attach" below. |
| `asmb8.capabilities.ipmi_power` / `.ipmi_boot_device` / `.ipmi_mc_info` | `dict` | always | `{supported: true, proven: true, note: ...}` — proven live against the target board. |
| `asmb8.capabilities.web_management` | `dict` | always | `proven` is `true` only when `include_web_session=true` **and** the login succeeded on this run. |
| `asmb8.capabilities.virtual_media` / `.remote_console` | `dict` | always | `{supported: null, proven: false, ...}` — not yet proven against this hardware. `null` (not `false`) means unknown, not unsupported. |
| `asmb8.capabilities.redfish` | `dict` | always | Always `{supported: false, proven: true, ...}` — a confirmed hardware-generation fact (AST2400 predates Redfish), never a live probe. |
| `asmb8.media.port_mode` | `str` | always | Always `"unknown"` — see Synopsis. |
| `asmb8.media.preconditions` | `dict` | always | `null` unless `include_media_preconditions=true`. See "Diagnosing a failed virtual-media attach" below. |
| `asmb8.media.preconditions.encryption.media_encryption_enabled` | `bool` | when `include_media_preconditions=true` | Whether `MEDIAENCRYPTION` is set, via `getremotesession.asp`. `null` if that endpoint could not be read this run. |
| `asmb8.media.preconditions.encryption.secure_channel_enabled` | `bool` | when `include_media_preconditions=true` | Whether `V_STR_SECURE_CHANNEL` is set, via `getvmediacfg.asp`. |
| `asmb8.media.preconditions.licensing.license_status_raw` | `int` | when `include_media_preconditions=true` | Raw `V_MEDIA_LICENSE_STATUS`. |
| `asmb8.media.preconditions.attach.attach_raw` | `int` | when `include_media_preconditions=true` | Raw `VMEDIAATTACH`, via `getremotesession.asp`. `null` if that endpoint could not be read this run. |
| `asmb8.media.preconditions.device_counts.{cd,fd,hd}` | `int` | when `include_media_preconditions=true` | Raw `V_NUM_CD`/`V_NUM_FD`/`V_NUM_HD`. |
| `asmb8.media.preconditions.sessions.cd.{max,current}` | `int` | when `include_media_preconditions=true` | Decoded `V_MAX_CD_SESSIONS`/`V_ACTIVE_CD_SESSIONS` (raw value minus `128`). The raw value (`129`/`128` in this corpus) is never reported. |
| `asmb8.media.preconditions.status_raw` | `int` | when `include_media_preconditions=true` | Raw `V_MEDIA_STATUS`. **Meaning unsourced** — see below. Never used to decide whether an attach can succeed. |
| `asmb8.media.preconditions.remote_session_read.{outcome,error_class}` | `str` | when `include_media_preconditions=true` | Outcome of reading `getremotesession.asp` for this precondition group — `read` or `failed`. See below. |
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
  value at all, when `include_web_session=true`. Also raised when
  `hoststatus.asp` returns a session-expired-looking HTML body (see
  `module_utils/asp.py`'s `looks_like_session_expired_html()`) — this module
  never returns `logged_in: true` alongside that shape; see
  `asmb8.web_management.logged_in` in the return-values table above and
  "Diagnosing a failed virtual-media attach" below.
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
- `protocol` — `getvmediacfg.asp` could not be parsed, when
  `include_media_preconditions=true`. **Not** degraded to `null`: see
  "Diagnosing a failed virtual-media attach" below for why this one endpoint
  is treated differently from `getremotesession.asp`.

## Diagnosing a failed virtual-media attach

`include_media_preconditions=true` reads `getremotesession.asp` and
`getvmediacfg.asp` and reports the settings that actually gate whether a
virtual-media attach can succeed — as `asmb8.media.preconditions` — without
attempting an attach itself.

**Read this before re-attempting an attach or reaching for a BMC reset.** Per
[`docs/hardware-evidence-2026-08-08.md`](hardware-evidence-2026-08-08.md)'s
"Redirection rejection status `3` means bad token" section, a bare protocol
rejection (`vmedia: redirection not accepted (status 3)`, or this collection's
own `error_class=bmc_busy`) does not by itself say *why*. That incident chased
two wrong theories for hours — a media session stranded by a network outage,
then a BMC cold reset having reverted media settings — costing two wasted boot
cycles and an unnecessary reset, before the real cause (a bad token, unrelated
to either theory) was found. Reading `getremotesession.asp`/`getvmediacfg.asp`
first would have ruled out both suspected settings in one call each.

**The single most actionable field: encryption.**
`asmb8.media.preconditions.encryption.media_encryption_enabled`
(`MEDIAENCRYPTION`) and `.secure_channel_enabled` (`V_STR_SECURE_CHANNEL`) —
this collection's iUSB client only speaks the plaintext variant of the
protocol, so either one reading non-`false` means an attach cannot succeed
against this client, independent of every other precondition reported
alongside it.

**`getremotesession.asp` is unverified against a programmatic client, and this
module degrades accordingly.** This project's own testing found that endpoint
answers a fresh, otherwise-successful login with a session-expired-looking
HTML page — the identical request sequence works from a browser.

**Correction (2026-08-11):** this section previously said what a programmatic
client additionally needs "has not been identified". [GitHub issue
#5](https://github.com/james-crowley/ansible-collection-asmb8-ikvm/issues/5)
identified a general mechanism for exactly that symptom, on five *other*
endpoints (`getalllancfg.asp`, `getlanchannelinfo.asp`, `getdnscfg.asp`,
`getnwbondcfg.asp`, `checknwbond.asp`): a missing `CSRFTOKEN` header, which
`AspClient` now attaches to every non-`WEBSES` request (see
`module_utils/asp.py`'s `AspClient._headers()`). Whether `getremotesession.asp`
itself is one of the endpoints that enforces `CSRFTOKEN` is **not** itself
confirmed either way — it was not one of the five issue #5 tested — so this
remains a documented, unverified gap for this specific endpoint, not
something the general fix is known to have closed; see
[`asmb8_sessions`](asmb8_sessions.md) for the same, independently observed
gap. `module_utils/asp.py`'s `looks_like_session_expired_html()` now
recognises this HTML shape structurally (an HTML document with login/session
markers and no `WEBVAR_JSONVAR_`, matched by shape rather than by the byte
length or digest issue #5 measured — see that function's docstring), which
gives the parse failure below a specific, accurate message instead of a
generic complaint that gave no hint the response was ever this identifiable
shape.

`asmb8.media.preconditions.encryption.media_encryption_enabled` and
`.attach.attach_raw` are sourced from that endpoint, so on a parse failure
both degrade to `null` and `asmb8.media.preconditions.remote_session_read`
reports `{outcome: "failed", ...}` — this module does **not** fail outright
over it. `getvmediacfg.asp` has shown no equivalent failure mode in this
project's testing, so a failure reading it **is not** degraded the same way:
it fails this module, consistent with `include_web_session`'s own diagnostic
read.

**`hoststatus.asp`, under plain `include_web_session=true`, is treated
differently again.** Unlike `getremotesession.asp`'s degrade-to-`null`
treatment above, a session-expired body from `hoststatus.asp` is **not**
degraded — `AspClient.get_host_status()` raises `errors.ProtocolError` on it,
and this module lets that propagate and fail the whole run, exactly like a
rejected login already does. This is deliberate, not an inconsistency: this
module's own documented pattern degrades *individual optional field groups*
(the media preconditions above) but hard-fails on its one unconditional
diagnostic read under `include_web_session` — the same asymmetry
`include_web_session`'s own option documentation already describes for a
rejected login. Before this fix, `hoststatus.asp` returning this shape was
returned verbatim and unchecked, so `logged_in: true` could be reported
alongside a `host_status_raw` that was really this HTML page — the exact
false positive GitHub issue #5 reported. See
`asmb8.web_management.logged_in`/`.host_status_raw` in the return-values table
above.

**`asmb8.media.preconditions.status_raw` (`V_MEDIA_STATUS`) is reported raw,
with an explicit meaning-unsourced caveat.** An earlier note in this project's
own history wrongly guessed this field tracked live media-attach state; a
later capture showed the BMC's own web UI writing it, which a live
attach-state field would have no reason for a UI to do. Do not read a change
in this value as evidence that an attach succeeded or failed.

**Session counts are decoded, never raw.**
`asmb8.media.preconditions.sessions.cd.{max,current}` applies the same `+128`
offset [`asmb8_sessions`](asmb8_sessions.md) documents and independently
confirms twice, because `getvmediacfg.asp`'s raw `V_MAX_CD_SESSIONS`/
`V_ACTIVE_CD_SESSIONS` (`129`/`128` in this corpus) are the exact same raw
values `getallservicescfg.asp`'s own `cd-media` `MAXSESS`/`CURSESS` report.
The raw `129`/`128` is never returned by this field — only the decoded `1`/`0`.

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

- name: Diagnose a failed virtual-media attach before re-attempting it or resetting the BMC
  james_crowley.asmb8_ikvm.asmb8_info:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    include_web_session: true
    include_media_preconditions: true
  delegate_to: localhost
  no_log: true
  register: asmb8_media_preconditions

- name: Rule out media encryption before suspecting a stranded session or a reverted BMC setting
  ansible.builtin.assert:
    that:
      # media_encryption_enabled is null (not false) when getremotesession.asp could not be
      # read this run -- see remote_session_read -- so this checks it is false, not merely falsy.
      - asmb8_media_preconditions.asmb8.media.preconditions.encryption.media_encryption_enabled == false
      - asmb8_media_preconditions.asmb8.media.preconditions.encryption.secure_channel_enabled == false
```
