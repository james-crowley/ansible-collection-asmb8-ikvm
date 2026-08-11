<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_http_origin`

Run (or stop) an ephemeral, path-confined, lifetime-capped local HTTP file
server.

## Synopsis

Serves one local directory over plain HTTP for exactly as long as a play
needs it, so an installer or bootloader that only speaks HTTP — not this
collection's native iUSB path (see [`asmb8_media`](asmb8_media.md)) — can
fetch bulk files at LAN speed instead of over the much slower iUSB relay
(measured throughput on the one board this collection has been tested
against is **≈790 KB/s**). This module runs a **local process only and never
touches the BMC**: `state=started`/`state=stopped` fork, poll, and signal a
background process on the Ansible controller; nothing here opens a
connection to the ASMB8-iKVM BMC at all.

This is this collection's one deliberate exception to its own "no standing
infrastructure" design (no PXE, DHCP, TFTP, NFS, or CIFS service anywhere
else in this collection), and every design choice below exists to make the
exception as narrow and self-limiting as possible.

**It must never outlive the play that started it — this is the module's
primary safety property, not an optional extra.** A play that starts this
server and then crashes, is interrupted, or loses its controller mid-run
leaves nothing behind to ask the background process to stop, so the
background process asks itself: it records a `lifetime_seconds` deadline at
start time and self-terminates once it passes, regardless of whether
`state=stopped` is ever called for it, and it also handles `SIGTERM` for an
orderly stop when something does ask. The failure mode this design guards
against is concrete: a forgotten instance of this server left listening on a
management VLAN, exposing whatever was in the served directory — an
installer ISO, a provisioning answer file that may itself carry secrets —
for as long as the process happens to survive.

**Size `lifetime_seconds` above the install window.** The default (4 hours)
is chosen to comfortably outlast a legitimately slow unattended install, but
during development an origin started with a 30-minute cap expired mid-run
and a later boot attempt hit a dead server. Raise `lifetime_seconds`
explicitly for an install known to run long — there is no way to disable the
cap, and that omission is deliberate.

**Path confinement is enforced on every single request**, independently of
what this module was told to serve: a request naming a `..` segment
(spelled literally, as a single percent-encoded `%2e%2e`, or hidden behind a
percent-encoded separator), a symlink anywhere along the resolved path whose
target lands outside `path`, and a double-percent-encoded segment
(`%252e%252e`, which a naive double-decoding server would turn into `..`)
are all refused with a `404`. The confinement check decodes percent-encoding
exactly once — decoding twice, or looping until stable, is exactly the bug
that would let the double-encoding case through.

**Honours `Range` requests**, returning a correct `206 Partial Content` (or
`416 Range Not Satisfiable` for a range past the end of the file) rather
than always returning `200` with the full body. This matters because
bootloaders and installers commonly issue ranged reads while resuming or
verifying a transfer; a server that ignores `Range` while still returning
`200` silently corrupts that kind of fetch instead of failing it loudly.

**Every request is logged**, as one JSON-lines record per request, to
`<runtime_dir>/<session_id>-access.log`: method, the raw requested path, the
HTTP status returned, a machine-readable outcome (`ok`, `not_found`,
`blocked_traversal`, `range_not_satisfiable`, `client_disconnected`, or
`error`), bytes actually sent, and the requesting client's address. This is
real diagnostic value: a failed install shows which files the target
actually asked for and any `404`s, distinguishing "the installer never asked
for it" from "we refused to serve it" from "we served it and the installer
still failed for some other reason." The background process's own
crash/traceback output goes to a separate `<runtime_dir>/<session_id>.log`
instead.

**The default bind address is loopback (`127.0.0.1`), and a real deployment
must override it.** This server has no authentication of any kind, so the
address it listens on is the only thing standing between "reachable by the
machine being provisioned" and "reachable by anything else on the same
management VLAN." Defaulting to loopback makes the unattended failure mode
"nothing outside this machine can reach it, and the install visibly cannot
fetch anything" rather than "answering on the network with nobody having
decided it should" — but the machine being provisioned is essentially never
the controller itself, so this safe default is also the one that silently
does not work out of the box. A real, working play needs `bind_address` set
explicitly to an address the target can actually reach — typically the
controller's own address on the network segment the target boots on. This
option does not validate that the address given is reachable from anywhere
in particular; that is on the caller to get right for their topology.

Shaped like `state=attached`/`state=detached` on `asmb8_media`:
`state=started` forks a detached background process that owns the listening
socket, writes a small JSON state file keyed by `session_id` under
`runtime_dir`, and returns once that process has reported either `serving`
or an early failure (bounded by `start_timeout`). `state=stopped` looks that
process up by the pid recorded in the state file, asks it to stop, and
waits (bounded by `stop_timeout`) for it to actually exit. Calling
`state=started` again with a `session_id` that already names a live session
is idempotent (`changed=false`); calling `state=stopped` when there is
nothing live to stop is likewise `changed=false`, not an error.

Directory listings are never generated — a request that resolves to a
directory rather than a file is refused (`404`) rather than served as an
index.

**A `serving` receipt means a real HTTP request was already proven to
work — not merely that a socket is bound and listening.** A bound, listening
socket only proves the kernel will accept a TCP connection and queue it; it
says nothing about whether anything on the other end will ever `accept()`
it, read the request, and write a response. A real, reported defect (issue
#2) showed exactly that gap: `state=started` returned `changed=true`,
`session_state=serving`, and a URL, while a real client's TCP connect
succeeded repeatedly against it and zero response bytes were ever sent for
the session's entire lifetime — the state file kept showing
`request_count=0` and no error, because nothing had actually gone wrong from
the daemon's own (mistaken) point of view. Before the background process
ever reports `serving`, it now issues one real GET against the exact address
and port it just bound — for a real file under `path` when one exists,
otherwise for a path guaranteed not to exist, accepting a well-formed `404`
as the best available proof when there is nothing to fetch — and demands the
expected bytes back within a few seconds. Failing that self-test tears the
daemon down and reports `error` (`error_class=connection`) instead of a
receipt nothing downstream could trust. This self-test's own one request is
never counted in `request_count`/`bytes_served`, which exist to describe
real client traffic — a caller checking those immediately after a fresh
`serving` receipt correctly sees `0`, not `1`, before anything downstream has
connected.

**This module has never been exercised against a real provisioning
target.** It has real forked-process unit and integration tests (a real
socket, a real filesystem, real `Range`/traversal handling), which is
evidence the server itself behaves correctly — it is not the same as having
served a booting machine. See
[`docs/capability-matrix.md`](capability-matrix.md).

## Options

| Option | Type | Default | Required | Choices |
|---|---|---|---|---|
| `state` | `str` | — | yes | `started`, `stopped` |
| `path` | `path` | — | required for `state=started`; ignored for `state=stopped` | — |
| `session_id` | `str` | — | required for `state=stopped`; optional (generated if omitted) for `state=started` | — |
| `runtime_dir` | `path` | `~/.ansible/asmb8_ikvm/http-origins` | no | — |
| `bind_address` | `str` | `127.0.0.1` | no | — |
| `port` | `int` | `0` (OS picks a free ephemeral port) | no | — |
| `lifetime_seconds` | `int` | `14400` (4 hours) | no | — (ignored for `state=stopped`; no way to disable) |
| `start_timeout` | `int` | `10` | no | — |
| `stop_timeout` | `int` | `15` | no | — |

Verified against `_argument_spec()` in `plugins/modules/asmb8_http_origin.py`.

### `path`

Directory, on the Ansible controller, to serve over HTTP. Served read-only.
Every file under this directory (recursively) is reachable by its own
relative path; nothing outside it is, however the request path is spelled —
see the Synopsis's path-confinement paragraph.

### `runtime_dir`

Directory holding one JSON state file per `session_id`, plus that session's
two log files: `<session_id>.log` (the background process's own
stdout/stderr, for crash diagnosis) and `<session_id>-access.log` (the
structured per-request log described in the Synopsis). Must be the same
path across the `state=started` call and the later `state=stopped` call for
the same session. Created (mode `0700`) if it does not already exist.

### `bind_address`

See the Synopsis's dedicated paragraph — defaults to loopback, and a real
deployment provisioning a separate target machine must override it.

### `port`

`0` (the default) asks the operating system to pick a free ephemeral port;
the port actually bound is always reported back in `port` and as part of
`url`, regardless of whether `port` was `0` or an explicit value.

### `lifetime_seconds`

Hard cap on how long the background server may run before it terminates
itself, regardless of whether `state=stopped` is ever called for it. See the
Synopsis's paragraph on why this is this module's primary safety property.

### `start_timeout`

Bounded number of seconds `state=started` waits for the background process
to report `serving` or an early failure (most commonly the requested `port`
already being in use) before returning. Expiring without a `serving` report
is a failure with `error_class=timeout` **and `indeterminate=true`** — the
background process is not torn down in that case, because it may simply be
slow to start and about to succeed; re-run this module with the same
`session_id` (idempotent) to re-probe it, rather than retrying blindly.

### `stop_timeout`

Bounded number of seconds `state=stopped` waits for the background process
to actually exit after being asked to stop, before returning anyway.
Exceeding this is reported as a warning, not a failure.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `changed` | `bool` | always | For `state=started`: `true` only when a new background process was actually forked (or, in check mode, would be); `false` when an already-live session for `session_id` was found and confirmed instead. For `state=stopped`: `true` only when a live process was actually asked to stop (or, in check mode, would be); `false` when there was nothing live to stop. |
| `session_id` | `str` | always | The session id in effect — generated for `state=started` when not supplied. |
| `session_state` | `str` | always | The last state the background process reported: `starting`, `serving`, `stopped`, or `error`. `unknown` if `state=stopped` found no state file at all. **`serving` means a real HTTP request was already proven to work** — see the Synopsis's dedicated paragraph. |
| `pid` | `int` | when available | Process id of the background session. `null` if none is recorded. |
| `url` | `str` | when available | The base URL files under `path` are reachable at, e.g. `http://192.0.2.5:8080/`, built from `bind_address` and the actually-bound `port`. `null` until the background process reports `serving`. |
| `port` | `int` | when available | The TCP port actually bound — the real value even when `port` was `0`. |
| `root` | `str` | when available | Resolved, absolute form of `path` as the background process is actually serving it. |
| `request_count` | `int` | always | Total HTTP requests served (or refused) so far. Never includes the startup self-test's own request — see the Synopsis. |
| `bytes_served` | `int` | always | Total response body bytes sent so far. |
| `recovered_stale_session` | `bool` | when a stale session was recovered | `true` when a stale state file (recorded pid no longer running) for `session_id` was found and discarded by this call. |
| `access_log` | `str` | when available | Path to the per-request JSON-lines log described in the Synopsis. |
| `error` | `str` | when `session_state` is `error` | The background process's own error message. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | One of `asmb8_http_origin.start` or `asmb8_http_origin.stop`. |
| `operation.endpoint` | `str` | always | The `bind_address:port` this session listens (or listened) on. |
| `operation.changed` | `bool` | always | Mirrors the top-level `changed`. |
| `operation.previous` | `dict` | always | The session state as read before this call, or `null` when none existed. |
| `operation.desired` | `str` | always | `serving` or `stopped`, whichever this call requested. |
| `operation.observed` | `dict` | always | The session state as read after this call, including nested `session_id`/`pid`/`state`/`root`/`url`/`port`/`request_count`/`bytes_served`/`last_request_at`/`started_at`/`stop_reason`/`error`/`error_class`. `null` in check mode. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_http_origin.py`.

## `error_class` values this module can raise

- `protocol` — `state=started` was called without `path`, an invalid/
  unreadable `path`, or the background session's own state carried no more
  specific class.
- `timeout` **with `indeterminate=true`** — `start_timeout` elapsed while
  the background process was still running but had not yet reported
  `serving`. Re-probe (a repeated `state=started` call with the same
  `session_id`), do not retry blindly — this call is idempotent.
- Any `error_class` the background session itself recorded when its
  `session_state` reached `error` (surfaced via `operation.observed.error_class`
  / the top-level `error_class` on failure) — most commonly `protocol` for a
  bind failure such as the requested `port` already being in use, or
  `connection` when the startup self-test described in the Synopsis could not
  get a real response back from the socket the daemon had just bound.

## Check-mode behaviour

Supported. Validates options and, for `state=started`, resolves `path` and
confirms it is a readable directory, but never binds a socket, never forks
the background process, and never contacts anything on the network. For
`state=stopped`, reports whether a live session would be stopped, but never
signals it. `diff_mode` is not supported — use `session_state` and the
`operation` receipt instead of `--diff`.

## Example

```yaml
- name: Serve a netboot image set for the duration of this play
  james_crowley.asmb8_ikvm.asmb8_http_origin:
    path: /srv/netboot/proxmox-auto
    bind_address: 192.0.2.5 # the controller's address on the target's boot network
    lifetime_seconds: 7200  # size above the expected install window
    state: started
  delegate_to: localhost
  register: origin

- name: Point the installer's kernel command line at the origin this play just started
  ansible.builtin.debug:
    msg: "Fetch installer files from {{ origin.url }}"

- name: Poll the same session id later in the play
  james_crowley.asmb8_ikvm.asmb8_http_origin:
    path: /srv/netboot/proxmox-auto
    session_id: "{{ origin.session_id }}"
    state: started
  delegate_to: localhost
  register: origin_status

- name: Tear the origin down once the install has finished fetching everything it needs
  james_crowley.asmb8_ikvm.asmb8_http_origin:
    session_id: "{{ origin.session_id }}"
    state: stopped
  delegate_to: localhost

- name: A repeated stop is a no-op, not a failure
  james_crowley.asmb8_ikvm.asmb8_http_origin:
    session_id: "{{ origin.session_id }}"
    state: stopped
  delegate_to: localhost
  register: second_stop
  failed_when: second_stop.changed
```

## See also

- [`asmb8_media`](asmb8_media.md) — the native iUSB path this module offers
  a faster alternative to for bulk file transfer, and the module whose own
  `state=attached`/`state=detached` shape this module's `state=started`/
  `state=stopped` mirrors.
- [`docs/netboot-design.md`](netboot-design.md) — the not-yet-implemented
  design for routing the bulk of an installer's own payload over this
  module instead of iUSB, using iUSB only for a small boot bootstrap.
- [`docs/capability-matrix.md`](capability-matrix.md) — exactly what tier of
  evidence this module's own claims rest on.
