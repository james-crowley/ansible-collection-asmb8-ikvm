<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_users`

Report ASMB8-iKVM local user accounts, role groups, and the current
session's role. Read-only.

## Synopsis

Reads this BMC's local user-account table (`getalluserinfo.asp`), the
privilege role of the session this module's own login just created
(`getrole.asp`), and the LDAP/AD role-group bindings table
(`getallrolegroupcfg.asp`). All three are read-only `.asp` RPCs; every field
name and shape is sourced from the real, redacted capture corpus under
`tests/unit/fixtures/asp/`, not from a vendor specification — AMI has not
published one for this surface.

**This module logs in unconditionally.** Unlike `asmb8_info`'s
`include_web_session`, which treats creating a `.asp` web session as an
opt-in exception because IPMI facts are that module's real purpose, this
module's entire purpose lives behind that same login — there is no way to
read `getalluserinfo.asp` without one. That login still allocates real
BMC-side session state (one of this board's limited concurrent web sessions
— the independently-measured cap for the `web` service is 20), which is
exactly why check mode skips it entirely — see below.

**Every credential-shaped or personal-data field is either omitted entirely
or reduced to a boolean.** `getalluserinfo.asp`'s `EmailID` is a user's real
e-mail address when configured (empty in every sample of this corpus, but
not guaranteed empty on any other board) — this module never returns it,
only `users[].email_configured`. Its `SSHKeyInfo` field is treated as
**sensitive by name, not by corpus evidence**: every sample here reads the
literal string `Not Available`, but a board with an uploaded key could
plausibly return fingerprint or other key material through the same field,
and this module refuses to find out the hard way — only
`users[].ssh_key_configured` is returned, never the field's raw text.

**Numeric privilege-limit fields are reported raw, not decoded, because no
sourced mapping exists.** `PrivLimit_Network`/`PrivLimit_Serial` take values
such as `84`, `52`, `15` in this corpus — these are **not** raw IPMI
channel-privilege levels (those only run 1-5) and no third-party client or
specification found by this module's author decodes them. Do not interpret
`users[].network_privilege_limit_raw`, `users[].serial_privilege_limit_raw`,
`current_session.privilege_raw`/`.extended_privilege_raw`, or
`role_groups[].privilege_raw` without independent sourcing.

**Empty user-table slots are reported as slots, never as accounts.** The
corpus's `getalluserinfo.asp` sample carries 9 records with `UserName=''`
alongside 3 real accounts (`anonymous`, `admin`, `root`) — an unconfigured
slot, not a fourth kind of user. This module filters them out of `users`
entirely and reports their count separately under `slots`.

**No write capability exists here, deliberately.** Creating, modifying, or
deleting a BMC user account, or changing a role-group binding, is exactly the
kind of change that can lock an operator out of this BMC entirely — disable
the account you are currently authenticated as, revoke the only KVM-capable
account, or point role-group resolution at an unreachable LDAP/AD server. A
future write capability, if ever added, would need at minimum an explicit
confirmation the caller is not modifying the account named by `username` in
the same task, a check that at least one other enabled account with
equivalent privilege remains after the change, and this collection's usual
before/after `operation` receipt shape. None of that exists yet.

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
`argument_spec()` in `plugins/modules/asmb8_users.py`.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `changed` | `bool` | always | Always `false`. |
| `users[].username` | `str` | always | The account name (`UserName`). Never empty — an empty slot is excluded, not listed. |
| `users[].enabled` / `.user_status_raw` | `bool` / `int` | always | Whether `UserStatus` reports active, and the raw integer it was derived from. |
| `users[].kvm_privilege` / `.vmedia_privilege` | `bool` | always | `KVMPriv`/`VMediaPriv`. Every account in this corpus reports `true` for both. |
| `users[].network_privilege_limit_raw` / `.serial_privilege_limit_raw` | `int` | always | `PrivLimit_Network`/`PrivLimit_Serial`, raw. **Not** a raw IPMI privilege level — see the module description. |
| `users[].snmp.status_raw` / `.access_raw` / `.auth_protocol_raw` / `.priv_protocol_raw` | `int` | always | `SNMPStatus`/`SNMPAccess`/`AUTHProtocol`/`PrivProtocol`, all raw. |
| `users[].email_configured` | `bool` | always | Whether `EmailID` is non-empty. The address itself is never returned. |
| `users[].email_format` | `str` | always | Raw `EmailFormat` (e.g. `AMI-Format`), or `null` if empty. Not treated as sensitive — it names a formatting convention, not an address. |
| `users[].serial_over_lan_status_raw` | `int` | always | Raw `SOL_Status`. |
| `users[].ssh_key_configured` | `bool` | always | Whether SSH key material is on file, derived from `SSHKeyStatus`/`SSHKeyInfo`. The key/fingerprint text itself is never returned. |
| `users[].ssh_key_status_raw` | `int` | always | Raw `SSHKeyStatus` — a bare status code, not key material. |
| `users[].fixed_user_count` | `int` | always | Raw `FixedUserCount` (`2` for every account in this corpus). Meaning beyond the field name is not sourced. |
| `slots.total` / `.configured` / `.empty` | `int` | always | Total rows in `getalluserinfo.asp`'s table, how many are configured (`len(users)`), and how many are unconfigured (excluded). |
| `current_session.username` / `.privilege_raw` / `.extended_privilege_raw` | `str` / `int` | always | `getrole.asp`'s report for the session this module's own login just created — **not** a directory of every active session (see `asmb8_sessions`). `null` only if `getrole.asp` returned no record. |
| `role_groups[].id` / `.configured` / `.name` / `.domain` / `.privilege_raw` / `.kvm_privilege_raw` / `.vmedia_privilege_raw` | — | always | LDAP/AD role-group bindings from `getallrolegroupcfg.asp`, one per configured group-ID slot. Every sample in this corpus is unconfigured. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | Always `"asmb8_users.report"`. |
| `operation.endpoint` | `str` | always | The `host:port` this read was performed against. |
| `operation.changed` | `bool` | always | Always `false`. |
| `operation.observed` | `dict` | always | Mirrors `users`, `slots`, `current_session`, and `role_groups` together. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_users.py`, and
against `tests/unit/fixtures/asp/getalluserinfo.txt`, `getrole.txt`, and
`getallrolegroupcfg.txt`.

## `error_class` values this module can raise

- `connection` / `tls_validation` / `authentication` / `timeout` / `bmc_busy`
  — establishing or using the `.asp` session, via the same `AspClient`
  machinery `asmb8_media` uses.

## Check-mode behaviour

Supported, but does **not** log in or read anything — it returns immediately
with every fact field `null`. This module's login is a real BMC-side side
effect, and a check-mode run implies no intended change here (this module has
no write path to predict the effect of), so there is nothing to justify
spending one of this BMC's limited concurrent session slots on it.
`diff_mode` is not supported.

## Example

```yaml
- name: Report local user accounts, the current session's role, and role-group bindings
  james_crowley.asmb8_ikvm.asmb8_users:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
  delegate_to: localhost
  no_log: true
  register: users_report

- name: Assert the admin account exists and is enabled, without ever seeing its e-mail address
  ansible.builtin.assert:
    that:
      - "'admin' in users_report.users | map(attribute='username')"

- name: Empty user-table slots are counted, not listed as accounts
  ansible.builtin.debug:
    msg: "{{ users_report.slots.configured }} of {{ users_report.slots.total }} user slots are configured"
```

## See also

- [`asmb8_info`](asmb8_info.md), [`asmb8_network`](asmb8_network.md),
  [`asmb8_sessions`](asmb8_sessions.md).
