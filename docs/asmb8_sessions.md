<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_sessions`

Report ASMB8-iKVM per-service session capacity and remote-session
configuration. Read-only.

## Synopsis

Reads this BMC's per-service session/port configuration
(`getallservicescfg.asp`) and its KVM/media remote-session configuration
(`getremotesession.asp`). Both are read-only `.asp` RPCs; every field name
and shape is sourced from the real, redacted capture corpus under
`tests/unit/fixtures/asp/` — AMI has not published a specification for this
surface.

**A directory of currently-active sessions is not implemented here, and this
is a known, reported gap, not an oversight.** The corpus's `getsessioninfo.asp`
capture was made with a `POST` request, not a `GET` — unlike every other
endpoint this module (and its siblings `asmb8_users`/`asmb8_network`) reads.
`AspClient.get_webvar` is deliberately, permanently `GET`-only. `active_sessions`
is therefore always `null`; reading it for real needs a properly-named,
POST-capable client method added to `asp.py` first — flagged as follow-up
work, not attempted here.

**`getremotesession.asp`'s live behaviour is unverified, and `remote_session`
may legitimately be `null` even immediately after a successful login.** This
corpus's fixture for it parses cleanly, so this module's parsing of that
shape is exercised and correct — but fetching it live, from a programmatic
client, has been observed to return a session-expired HTML page even with a
session that had just been freshly authenticated (the same flow works from a
browser). Why is not yet understood — something beyond the plain session
cookie appears to be required and has not been identified. This module
therefore treats a parse failure on this one endpoint as an expected,
non-fatal outcome (see `remote_session_read`) rather than failing the whole
run over it. **`remote_session` working at all, on any given run, is
unverified rather than guaranteed.**

**This module logs in unconditionally**, for the same reason `asmb8_users`
does: every endpoint it can actually read requires an authenticated `.asp`
session. See [Check-mode behaviour](#check-mode-behaviour) for how this
module avoids spending that session in a mode where nothing is going to be
changed anyway.

**Session counts are +128-offset-encoded, and this module decodes them —
with the evidence for doing so, not a guess.** `getallservicescfg.asp`'s
`MAXSESS`/`CURSESS` fields are not raw session counts: the `web` service's
`MAXSESS` reads `148` in this corpus, and `errors.py`'s `ErrorClass.BMC_BUSY`
docstring independently documents this board's measured concurrent-session
cap for that same service as **20** — `148 - 128 = 20`. `cd-media`'s `MAXSESS`
reads `129` in this corpus, and `asmb8_redirection`'s own service catalog
independently documents that service's capacity as exactly **one** concurrent
session — `129 - 128 = 1`. Two independent measurements, two independent
confirmations of the same **+128** offset; this module applies it to every
service's `MAXSESS`/`CURSESS` rather than reporting the raw, meaningless-
looking `148`/`129`. The one exception is the literal raw value `255`
(`ssh`/`telnet` in this corpus): decoding it as `255 - 128 = 127` sessions is
not plausible for those services and contradicts `asmb8_redirection`'s own
catalog, which reports no session cap at all for either — `255` is therefore
treated as a distinct "not applicable" sentinel, decoding to `null`, not to
`127`.

No write capability exists here, deliberately. This module reports session
capacity and remote-session configuration only; it does not attempt to
terminate a session or change any of `getremotesession.asp`'s settings
(KVM/media encryption, single-port mode, host lock, and so on) — a mistaken
change to host-lock or encryption settings here could strand an operator
mid-session with no independent path back in.

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

This module takes only the shared connection options. Verified against
`argument_spec()` in `plugins/modules/asmb8_sessions.py`.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `changed` | `bool` | always | Always `false`. |
| `services.<name>.id` / `.name` | `int` / `str` | always | `SERVICEID`, `SERVICENAME` (dict key), e.g. `web`, `kvm`, `cd-media` — the same naming `asmb8_redirection` uses for its own, differently-sourced (static catalog, not a live read) service report. |
| `services.<name>.enabled` | `bool` | always | `STATE`. |
| `services.<name>.interface_scope_raw` | `str` | always | `IFCNAME` (e.g. `both`, or the literal string `FFFFFFFFFFFFFFFF` for `ssh`/`telnet`). Not decoded — no sourced meaning for that sentinel was found. |
| `services.<name>.port.plain` / `.secure` | `int` | always | `NSPORT`/`SECPORT`, or `null` for the `4294967295` "not applicable" sentinel (e.g. `ssh`'s plaintext port). |
| `services.<name>.timeout_seconds` | `int` | always | `SERVICE_TIMEOUT`, or `null` for the same sentinel (observed for the media services, which have no server-side inactivity timeout). |
| `services.<name>.sessions.max` / `.current` | `int` | always | Decoded `MAXSESS`/`CURSESS` (raw value minus 128), or `null` if the raw value is the `255` sentinel — see the module description. |
| `services.<name>.single_port_status_raw` | `int` | always | `SINGLEPORT_STATUS`. |
| `remote_session.kvm_encryption_enabled` / `.media_encryption_enabled` / `.single_port_enabled` | `bool` | always | `KVMENCRYPTION`, `MEDIAENCRYPTION`, `SINGLEPORT`. `null` (whole object) if this run's read did not parse — see the module description. |
| `remote_session.keyboard_language` | `str` | always | `KEYBOARDLANG` (e.g. `AD`). |
| `remote_session.local_media_enabled` / `.remote_media_enabled` / `.vmedia_attach_raw` | — | always | `LMEDIAENABLE`, `RMEDIAENABLE`, `VMEDIAATTACH`. |
| `remote_session.host_lock_enabled` / `.host_lock_auto_enabled` / `.sd_card_status_raw` | — | always | `HOSTLOCK`, `HOSTLOCKAUTO`, `SDCARD_STATUS`. |
| `remote_session_read.outcome` / `.error_class` | `str` | always | `read` if `remote_session` was populated, `failed` if the read did not parse; the failure class on `failed` only. |
| `active_sessions` | `list` | always | Always `null`. See the module description for exactly what would need to be added before this can be implemented. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | Always `"asmb8_sessions.report"`. |
| `operation.endpoint` | `str` | always | The `host:port` this read was performed against. |
| `operation.changed` | `bool` | always | Always `false`. |
| `operation.observed` | `dict` | always | Mirrors `services` and `remote_session` together. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_sessions.py`,
and against `tests/unit/fixtures/asp/getallservicescfg.txt` (`web` service
`MAXSESS: 148`, `cd-media` `MAXSESS: 129`, `ssh`/`telnet` `MAXSESS: 255`) and
`getremotesession.txt`.

## `error_class` values this module can raise

- `connection` / `tls_validation` / `authentication` / `timeout` / `bmc_busy`
  — establishing or using the `.asp` session, via the same `AspClient`
  machinery `asmb8_media` uses. A `getremotesession.asp`-specific
  `protocol`-class parse failure is caught internally and reported through
  `remote_session_read` instead of failing the module — see the module
  description.

## Check-mode behaviour

Supported, but does **not** log in or read anything — it returns immediately
with every fact field `null`. Same reasoning as `asmb8_users`'s check-mode
behaviour: this module's login is a real BMC-side side effect, and there is
no write path here whose effect a check-mode run could be predicting.
`diff_mode` is not supported.

## Example

```yaml
- name: Report per-service session capacity and remote-session configuration
  james_crowley.asmb8_ikvm.asmb8_sessions:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
  delegate_to: localhost
  no_log: true
  register: sessions_report

- name: Assert the web service's decoded session cap matches this collection's measured 20-session limit
  ansible.builtin.assert:
    that:
      - sessions_report.services.web.sessions.max == 20

- name: A null remote_session is an expected, documented gap -- not a failure
  ansible.builtin.debug:
    msg: "remote session config was not readable this run (read outcome: {{ sessions_report.remote_session_read.outcome }})"
  when: sessions_report.remote_session is none

- name: active_sessions is always null -- see the module description for why
  ansible.builtin.assert:
    that:
      - sessions_report.active_sessions is none
```

## See also

- [`asmb8_info`](asmb8_info.md), [`asmb8_users`](asmb8_users.md),
  [`asmb8_network`](asmb8_network.md), [`asmb8_redirection`](asmb8_redirection.md).
