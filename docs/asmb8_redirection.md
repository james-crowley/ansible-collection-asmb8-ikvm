<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_redirection`

Open an ASMB8-iKVM console/KVM (IVTP) session headlessly.

## Synopsis

Opens an ASMB8-iKVM BMC's KVM/console-redirection channel the same way the
vendor's Java `JViewer` client does, but headlessly — no Java, no JRE, and no
on-screen window. It logs in to the `.asp` web-management surface, fetches
`jviewer.jnlp` to mint a fresh `-kvmtoken` and allocate a video session, then
speaks AMI's proprietary IVTP protocol directly over `kvm_port` to complete
the session handshake.

**What this module actually proves, honestly.** The full AMI/ASPEED video
codec (a hybrid vector-quantisation + JPEG/DCT tile stream, optionally
RC4-obfuscated) is **not** implemented by this collection — porting it is a
large, separate undertaking this module does not attempt, and it never
fabricates or approximates a decode:

- `capture=handshake_only` (the default) only proves the channel is live: it
  completes the session handshake and returns the negotiated facts in
  `channel`.
- `capture=raw_frame` additionally waits for and saves one complete video
  frame's raw, still-encoded bytes — **not** a viewable image — to
  `output_path`, clearly labelled `decoded: false` in `frame`.
- `capture=decoded_frame` always fails, before any network is touched, with
  `error_class=unsupported_capability`. Asking for a decoded image is refused
  outright rather than answered with a placeholder.

**This module never mutates persistent BMC state** — `changed` is always
`false`, the same convention `asmb8_info` uses for its own
`include_web_session`. A KVM session is opened and, best-effort, closed again
(`STOP_SESSION_IMMEDIATE` is sent, then the socket is closed) before this
module returns; nothing about the BMC's standing configuration changes as a
result of running it.

**Read this before trusting anything in this module's `RETURN` block as
settled fact — it is genuinely less proven than the other four modules in
this collection.** Unlike `asmb8_media`'s iUSB implementation, no live
capture, and no unit/mock test, has ever exercised this module's IVTP
handshake against anything. Everything below is sourced from decompiled
vendor client analysis alone. Specifically:

- **Whether the wire-level packet-size discrepancy in `VALIDATE_VIDEO_SESSION`
  matters is unverified.** The decompiled vendor client writes its own
  packet's `pktSize` header field as 332 (the frame's total wire length) while
  every other packet-building method in the same decompiled class uses
  `pktSize` to mean the *body* length that follows the header (324 bytes for
  this packet). This module deliberately writes the self-consistent value,
  324, on the theory that the field is likely parsed by a hardcoded length on
  the BMC side rather than trusted from the client — plausible, but **not
  verified against real hardware**. See `plugins/module_utils/ivtp.py`'s
  module docstring, disagreement 2, for the full reasoning.
- **Whether `GET_WEB_TOKEN` (opcode 21) is actually required, or merely
  tolerated, by the BMC is unverified.** `send_get_web_token` defaults to
  `true` because the decompiled client's closest-matching code path sends it,
  not because this collection has confirmed the BMC rejects a handshake
  without it.
- **Whether `client_username`'s value has any effect on BMC behaviour at all
  is unverified**, in either direction.
- **The claimed KVM service capacity — 4 concurrent sessions, an 1800-second
  server-side inactivity timeout — is not sourced from the decompiled client,
  a live capture, or any other authoritative reference cited elsewhere in
  this collection.** It appears only in this module's own `DOCUMENTATION`,
  attributed there to "the task brief this collection was built against."
  Treat it as **unverified** until it is backed by a real source — see
  [`docs/capability-matrix.md`](capability-matrix.md) Tier 4, which flags this
  explicitly as a claim that reads more confident than its evidence supports.
- **No unit or mock test exists for this module or for
  `plugins/module_utils/ivtp.py`** as of this writing (unlike every other
  module and `module_utils` file in this collection). This is the only module
  in this collection with zero Tier 2 coverage, in addition to zero Tier 3
  coverage.

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
| `kvm_port` | `int` | `7578` | no | — |
| `kvm_secure` | `bool` | — (follows the JNLP fetch unless overridden) | required when `token` is set | — |
| `token` | `str` (`no_log`) | — | no | — |
| `client_username` | `str` | (controller's own OS username) | no | — |
| `send_get_web_token` | `bool` | `true` | no | — |
| `capture` | `str` | `handshake_only` | no | `handshake_only`, `raw_frame`, `decoded_frame` |
| `output_path` | `path` | — | required for `capture=raw_frame` | — |
| `handshake_timeout` | `int` | `15` | no | — |
| `frame_timeout` | `int` | `20` | no | — |

Verified against `_connection_argument_spec()`/`argument_spec()`,
`required_if=[("capture", "raw_frame", ["output_path"])]`, and
`required_by={"token": ["kvm_secure"]}` in
`plugins/modules/asmb8_redirection.py`.

### `kvm_secure`

TLS for the KVM socket is governed by this flag, **never inferred from
`kvm_port`** — observed directly from the vendor's own decompiled client:
whether the video socket is TLS-wrapped is carried by a boolean flag,
independent of which TCP port is dialled. When `token` is not supplied,
`kvm_secure` defaults to whatever the `jviewer.jnlp` fetch itself reported for
this session (which follows the scheme `use_tls` selects for that fetch); set
it explicitly to override that. It **must** be set explicitly whenever `token`
is supplied, since there is then no JNLP response to read it from
(`required_by`). On the target hardware this is `false`: the secure KVM port
is refused outright because media/KVM encryption is disabled in that board's
configuration.

### `token`

A pre-existing `-kvmtoken`, if the caller already holds one from a prior
`jviewer.jnlp` fetch (for example, one minted by a concurrently running
`asmb8_media` session) and wants to open a KVM channel without a second web
login. Never written to `channel`, `operation`, or any error message.

### `capture`

- `handshake_only` — confirm the channel is live; report `channel` only.
- `raw_frame` — additionally wait for one complete video frame and save its
  raw, still-encoded bytes to `output_path`.
- `decoded_frame` — always fails immediately with
  `error_class=unsupported_capability`, before any network is touched.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `changed` | `bool` | always | Always `false`. |
| `capture` | `str` | always | The `capture` mode this call ran with. |
| `channel.session_accepted` | `bool` | on success | Always `true` on success — the BMC's initial greeting was `SESSION_ACCEPTED`. |
| `channel.greeting_body_len` | `int` | on success | Byte length of the greeting's own body (an active-client list this module does not parse). |
| `channel.validate_status` | `int` | on success | The raw `VALIDATE_VIDEO_SESSION_RESPONSE` status byte. `1` is `VALID_SESSION`. |
| `channel.validate_status_name` | `str` | on success | Human-readable name for `validate_status`. |
| `channel.validate_sub_status` | `int` | on success | A second status byte the BMC sometimes includes; `null` when absent. The decompiled vendor client never names what this means, and neither does this module. |
| `channel.resumed` | `bool` | on success | Always `true` on success — `RESUME_REDIRECTION` was sent after validation. |
| `frame.decoded` | `bool` | when `capture=raw_frame` | Always `false`. The bytes at `output_path` are the raw, still-encoded fragment data for one frame — not a viewable image. |
| `frame.bytes_written` | `int` | when `capture=raw_frame` | Number of raw bytes written. |
| `frame.output_path` | `str` | when `capture=raw_frame` | Mirrors `output_path`. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | Always `"asmb8_redirection.capture"`. |
| `operation.endpoint` | `str` | always | `host:kvm_port` this call connected (or attempted to connect) to. |
| `operation.changed` | `bool` | always | Always `false`. |
| `operation.observed` | `dict` | always | Mirrors `channel`, or `null` if the handshake never completed (including check mode). |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_redirection.py`.

## `error_class` values this module can raise

- `unsupported_capability` — `capture=decoded_frame` was requested (raised
  before any network is touched); or the BMC's `VALIDATE_VIDEO_SESSION_RESPONSE`
  reported `KVM_DISABLED`.
- `authentication` — the `.asp` login rejected the credentials; or the BMC
  rejected the KVM/video session token (`INVALID_SESSION`,
  `INVALID_VIDEO_TOKEN`, `INVALID_CDROM_TOKEN`, `INVALID_FLOPPY_TOKEN`); or
  the BMC stopped an already-open session because the underlying web session
  was logged out.
- `protocol` — `output_path`'s parent directory does not exist for
  `capture=raw_frame`; `token` was supplied without `kvm_secure` (a backstop
  behind `required_by`); the BMC's greeting was not `SESSION_ACCEPTED`; or any
  other malformed/unrecognised response.
- `timeout` — a socket read timed out during the handshake or while waiting
  for a video frame (`handshake_timeout`/`frame_timeout`); or the BMC stopped
  the session for its own server-side inactivity timeout
  (`STOP_TIMED_OUT`) — **not necessarily a fault**: the ASPEED video engine
  only sends fragments for changed screen content, so `frame_timeout` can
  legitimately expire against a genuinely idle, unchanged display.
- `invalid_state` — the BMC stopped the session because another client
  requested a KVM disconnect.
- `remote_operation` — the BMC stopped the session for an unclassified
  reason, or `VALIDATE_VIDEO_SESSION_RESPONSE` reported an unrecognised status
  byte.
- `connection` / `tls_validation` — could not reach or complete a TLS
  handshake with either the `.asp` web-management port or `kvm_port`.

## Check-mode behaviour

Full support. Validates options — including that `output_path`'s parent
directory exists for `capture=raw_frame`, and that `capture=decoded_frame` is
rejected — but never logs in, never fetches the JNLP, and never opens a
connection to `kvm_port`. `diff_mode` is not supported — use `channel`/`frame`
and the `operation` receipt instead.

## Example

```yaml
- name: Confirm the KVM channel is live without capturing anything
  james_crowley.asmb8_ikvm.asmb8_redirection:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    capture: handshake_only
  delegate_to: localhost
  no_log: true
  register: kvm_check

- name: Capture one raw (undecoded) console frame for offline inspection
  james_crowley.asmb8_ikvm.asmb8_redirection:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    capture: raw_frame
    output_path: /tmp/asmb8-frame.raw
  delegate_to: localhost
  no_log: true
  register: kvm_frame

- name: Requesting a decoded image fails honestly instead of faking one
  james_crowley.asmb8_ikvm.asmb8_redirection:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    capture: decoded_frame
    output_path: /tmp/asmb8-frame.png
  delegate_to: localhost
  no_log: true
  register: kvm_decode_attempt
  ignore_errors: true

- name: Assert the decode attempt failed the honest way
  ansible.builtin.assert:
    that:
      - kvm_decode_attempt is failed
      - kvm_decode_attempt.error_class == 'unsupported_capability'
```

## What this module does not do

- Decode video into pixels, in any form.
- Send keyboard or mouse input (`OP_HID_PKT` is recognised by
  `plugins/module_utils/ivtp.py` as a real opcode but is never built or sent
  by this module).
- Configure or persist any redirection-service setting on the BMC — despite
  the name suggested by `meta/runtime.yml`'s action group membership
  (`asmb8_ikvm`), this module only opens and closes a session; it has no
  "inspect current redirection configuration" or "enable/disable the
  service" behaviour distinct from that.
