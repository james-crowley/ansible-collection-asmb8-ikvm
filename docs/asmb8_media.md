<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_media`

Attach or detach a local ISO to an ASMB8-iKVM virtual CD-ROM over iUSB. This
is this collection's headline capability.

## Synopsis

Streams a local ISO file from the Ansible controller to an ASMB8-iKVM BMC's
virtual CD-ROM over AMI's proprietary iUSB protocol, so a bare-metal host can
boot from it with no PXE/DHCP/TFTP/NFS/CIFS infrastructure.

**The session outlives the module call.** An iUSB media session is
long-lived — the target host stays booted from the attached ISO for as long
as an install takes, which can be an hour or more — while a single module
invocation must return in seconds. `state=attached` forks a **detached
background process** that owns the connection, writes a small JSON state file
keyed by `session_id` under `runtime_dir`, and returns once that process has
reported either `attached` or an early failure (bounded by `attach_timeout`).
`state=detached` looks that process up by the pid recorded in the state file,
asks it to stop, and waits (bounded by `detach_timeout`) for it to actually
exit.

**Idle is normal and has no meaningful upper bound.** Verified directly
against the target hardware: an attached session went completely silent for
130 consecutive seconds while the host sat at a bootloader menu, then resumed
serving reads normally with no intervention. A long-idle `state=attached`
session that still reports `attached` is **not** hung. `session_state` alone
cannot tell "idle because the installer is waiting for input" from "dead" —
compare `operation.observed.last_request_at` (set only on real traffic)
against `operation.observed.updated_at` (refreshed on every internal
heartbeat regardless).

**The BMC's iUSB/KVM media service allows exactly one active session,
board-wide, and never reclaims an abandoned one on its own.** There is no
server-side timeout for this. Because of this, `state=attached` **always**
attempts to reclaim every OTHER session this collection's own `runtime_dir`
still has a record of against the same endpoint — signalling its process to
stop and removing its state file — as a normal, always-run step of every
attach, not a fallback that only runs after a failure. This can only reclaim
sessions this same `runtime_dir` is tracking; it cannot forcibly evict a
session held by a different controller, a manually-opened JViewer/browser
session, or a daemon whose `runtime_dir` was deleted out from under it. When a
rejected attach reports `error_class=bmc_busy` and no known `session_id` is
still holding the slot, the operator's escape hatch is a BMC cold reset
(`ipmitool mc reset cold`, or the `pyghmi`/`community.general.ipmi_power`
equivalent) — this does **not** power-cycle the host itself, only the BMC's
own management controller.

A stale state file (recorded pid no longer running) is always recoverable:
`state=attached` for that `session_id` discards it and starts fresh;
`state=detached` simply cleans it up and reports `changed=false`.

**Do not confuse `port` with `cd_port`.** `port` (inherited from the shared
connection fragment, default 443) is the BMC's HTTPS/HTTP web-management port
used to log in and fetch `jviewer.jnlp`, which mints a media session token.
`cd_port` is the separate, on-demand iUSB listener (default 5120) the actual
ISO bytes are streamed over. Both are contacted by every `state=attached`
call; only `port` is used by `state=detached`, which opens no iUSB connection
at all.

On the target hardware, `cd_port` (and the paired KVM/floppy/HD ports) are
bound only after a `jviewer.jnlp` fetch allocates a session — before that, the
port refuses connections outright. This module's own attach flow always
fetches the JNLP first, so this is transparent to a normal `state=attached`
call.

**The READ(10)/READ(12) trace: `operation.observed.read_trace_head` /
`read_trace_tail` / `read_trace_dropped`.** Every SCSI READ(10)/READ(12) the
background session actually serves is recorded — opcode (`0x28` or `0xa8`),
LBA, and block count only. **Never media contents, and never credentials.**
This is a diagnostic aid for exactly one question: *where, in terms of media
access, did this session get to?*

Two failure shapes motivate keeping BOTH ends rather than only the earliest
requests:

- A boot that stops **early** — between the firmware catalogue/El Torito
  boot-image reads and OS handoff (the shape this trace was originally added
  to answer). The documented boot chain
  ([hardware-evidence-2026-08-08.md](hardware-evidence-2026-08-08.md), "Boot
  chain, proven") is under a dozen distinct LBAs, so `read_trace_head`'s first
  128 entries comfortably cover it.
- The only real install failure this project has recorded
  ([hardware-evidence-2026-08-08.md](hardware-evidence-2026-08-08.md), "A real
  installer reached 70% and then failed on media read timeouts") stopped
  roughly 22,000 reads into a 32,741-read session, deep in package extraction
  — far past anything a first-N-only trace could ever show.
  `read_trace_tail`'s most recent 128 entries answer that shape instead.

`read_trace_head` and `read_trace_tail` are always two **separate** lists,
never spliced into one — an operator reading a single combined list could
mistake the seam between "early boot" and "most recent" for two
adjacent-in-time requests. `read_trace_dropped` is the exact count of
requests discarded from the middle: `0` means the two lists together are the
**complete** record with no gap at all; a nonzero value means the boundary
between the tail's earliest entry and the head's latest entry is that many
requests apart, not one, so the gap is never mistakable for contiguous
history.

The trace itself lives in a separate, append-only log next to the session's
state file (never inside the state file, and never rewritten — only ever
appended to), specifically so a 32,741-request session costs one small
`write()` per request instead of rewriting an ever-growing trace on every
single one. `read_trace_head`/`read_trace_tail`/`read_trace_dropped` are
reconstructed from that log only when something asks for status — an
`asmb8_media` call, not the background session's own hot loop — which is at
most a handful of times over an hour-long install, not 30+ times a second.
That log is also this trace's crash-durability mechanism: each entry is
flushed to the OS immediately after being written, so it survives the
background process dying unexpectedly (a `SIGKILL`, an OOM kill, a host power
event) with, at worst, the loss of the single entry that was physically
mid-write at the instant of death — every entry written before it is already
durable. It is deleted, along with the state and log files, once the session
is detached or reclaimed.

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
| `tls_fingerprint` | `str` | — | no | — (mutually exclusive with `ca_path`; recommended trust mode) |
| `timeout` | `int` | `30` | no | — |
| `connect_timeout` | `int` | `10` | no | — |
| `state` | `str` | — | yes | `attached`, `detached` |
| `image` | `path` | — | required for `attached` | — |
| `session_id` | `str` | (generated if omitted) | required for `detached` | — |
| `runtime_dir` | `path` | `~/.ansible/asmb8_ikvm/media-sessions` | no | — |
| `cd_port` | `int` | `5120` | no | — |
| `instance` | `int` | `0` | no | — |
| `attach_timeout` | `int` | `10` | no | — |
| `detach_timeout` | `int` | `15` | no | — |

Verified against `_argument_spec()` and `required_if` in
`plugins/modules/asmb8_media.py`. `image`/`session_id` are enforced via
`required_if=[("state", "detached", ["session_id"]), ("state", "attached", ["image"])]`,
not the argument spec's own `required` flag — `ansible-doc` reports both as
not required, but omitting either with the corresponding `state` still fails,
just via a different Ansible mechanism.

### `image`

Path, on the **Ansible controller**, to a local ISO image to serve as the
virtual CD-ROM. Always served **read-only** — the CD-ROM channel this module
speaks has no write opcode at all in the BMC's own firmware (confirmed by
disassembling the vendor's native SCSI dispatcher — see
[NOTICE](../NOTICE)), so there is no writable option to offer, unlike the
sibling `james_crowley.intel_amt` collection's floppy/USB-R slot.

### `session_id`

Identifies the background session across separate module invocations.
Required for `state=detached`, so a caller can only ever stop a session it can
name. Optional for `state=attached` — when omitted, a fresh id is generated
and returned in `session_id`; callers that need to detach later must capture
and reuse that value. Calling `state=attached` again with a `session_id` that
already names a live session is idempotent: `changed=false`, no second
background process is started.

### `runtime_dir`

Must be the same path across the `state=attached` call and the later
`state=detached` call for the same session. It is also the scope of this
module's single-session reclamation pass — two `runtime_dir` values pointed at
the same BMC are invisible to each other's reclamation logic.

### `attach_timeout`

Bounded seconds `state=attached` waits for the background process to report
`attached` or an early failure. Expiring without an `attached` report is a
**failure**, with `error_class=timeout` and `indeterminate=true` — it is not
established that the ISO is being served, and this module will not report
success for an attach it has not confirmed. The failure still carries
`session_id` and `pid`, and the background session is **not** torn down,
because it may simply be slow and about to succeed. `indeterminate=true`
means re-probe, do not retry: call this module again with `state=attached`
and the same `session_id`, which is idempotent. Retrying the attach instead
risks colliding with the still-running session, since the BMC's media slot is
single-occupancy.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `changed` | `bool` | always | Attach: `true` only if a new background process was actually forked (or, in check mode, would be). Detach: `true` only if a live process was actually asked to stop (or, in check mode, would be). |
| `session_id` | `str` | always | The session id in effect — generated for `state=attached` when not supplied. **Capture this** if you will need to detach later. |
| `session_state` | `str` | always | Last state the background process reported: `starting`, `connecting`, `attached`, `detached`, `error`. `unknown` if `state=detached` found no state file. An `attached` session idle a long time is still `attached` — see Synopsis. |
| `pid` | `int` | when available | Process id of the background session. `null` if none is recorded. |
| `bytes_read` | `int` | always | Total bytes read from `image` so far, mirroring `operation.observed.bytes_read`. |
| `recovered_stale_session` | `bool` | when a stale session was recovered | `true` when a stale state file (recorded pid no longer running) for `session_id` was found and discarded by this call. |
| `reclaimed_sessions` | `list[str]` | when `state=attached` | Session ids of OTHER sessions this collection's `runtime_dir` had a record of against the same endpoint, and which this call attempted to reclaim before attaching. Empty when there were none. |
| `error` | `str` | when `session_state` is `error` | The background process's own error message. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | `asmb8_media.attach` or `asmb8_media.detach`. |
| `operation.endpoint` | `str` | always | The iUSB `host:cd_port` this session connects (or connected) to. |
| `operation.changed` | `bool` | always | Mirrors the top-level `changed`. |
| `operation.previous` | `dict` | always | The session state as read before this call, or `null` when none existed. |
| `operation.desired` | `str` | always | `attached` or `detached`, whichever this call requested. |
| `operation.observed` | `dict` | always | The session state as read after this call, including `bytes_read`, `sectors_served`, `last_request_at`, `updated_at`, `read_trace_head`, `read_trace_tail`, `read_trace_dropped` (see the READ(10)/READ(12) trace note above). `null` in check mode. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_media.py`.

## `error_class` values this module can raise

- `protocol` — `image` was not set for `state=attached` (belt-and-suspenders
  behind `required_if`), or the background daemon crashed with an
  unclassified error.
- `bmc_busy` — the iUSB auth handshake was rejected because the single media
  slot is already held (`connectionStatus != 1` in the ACK — see
  `plugins/module_utils/iusb.py`'s `interpret_ack`), or the `.asp` login/JNLP
  fetch hit this board's saturated-worker-pool hang.
- `timeout` **with `indeterminate=true`** — `attach_timeout` expired with the
  background process still running but not yet `attached`. Re-probe, do not
  retry.
- `authentication` — the `.asp` login rejected the credentials (this board
  returns HTTP 200 with a `Failure_Login_*` marker on bad credentials, not a
  4xx — see `plugins/module_utils/asp.py`).
- `connection` / `tls_validation` — could not reach or complete a TLS
  handshake with the BMC's web-management port, or dial the iUSB `cd_port`.
- Any classified failure the background daemon itself raised is surfaced with
  its own real `error_class`, recorded in the session state file's
  `error_class` field and read back by `_error_class_of()` in
  `plugins/modules/asmb8_media.py` — it falls back to `protocol` only if the
  daemon somehow recorded an error with no classification at all.

## Check-mode behaviour

Full support (`attributes.check_mode.support: full`). For `state=attached`:
opens (and immediately closes) `image` to confirm it is a readable file, and
reports which other sessions *would* be reclaimed, but never forks the
background process, never signals another session's process, and never
contacts the BMC. For `state=detached`: reports whether a live session would
be stopped, but never signals it. `diff_mode` is not supported — use
`session_state` and the `operation` receipt instead.

## Example

```yaml
- name: Attach a prepared Proxmox installer ISO for an unattended install
  james_crowley.asmb8_ikvm.asmb8_media:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    image: /srv/images/proxmox-ve-auto.iso
    state: attached
  delegate_to: localhost
  no_log: true
  register: media

- name: Arm a one-time optical boot and reset into the attached ISO
  james_crowley.asmb8_ikvm.asmb8_boot:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    device: optical
  delegate_to: localhost
  no_log: true

- name: Poll the same session id later in the play
  james_crowley.asmb8_ikvm.asmb8_media:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    image: /srv/images/proxmox-ve-auto.iso
    session_id: "{{ media.session_id }}"
    state: attached
  delegate_to: localhost
  no_log: true
  register: media_status

- name: Detach once the install has finished
  james_crowley.asmb8_ikvm.asmb8_media:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    session_id: "{{ media.session_id }}"
    state: detached
  delegate_to: localhost
  no_log: true
```

## What this module does not do

- No writable media slot of any kind — see `image`, above.
- No answer-file/floppy image support — the answer file (e.g. a
  `proxmox-auto-install-assistant prepare-iso` output) must already be baked
  into the ISO you hand this module.
- No detection of whether the ISO you attached is actually auto-install
  prepared. Attaching a stock installer ISO will attach and boot it
  faithfully, then wait at a GRUB menu it has no way to know about — see
  [Known limitations](../README.md#known-limitations) in the top-level
  README.
- No guarantee that a guest OS can obtain its own media session once it
  boots and re-enumerates USB storage with its own driver — untested, see
  [`docs/capability-matrix.md`](capability-matrix.md) Tier 4.
