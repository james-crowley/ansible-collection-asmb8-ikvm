<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_alerts`

Read the ASMB8-iKVM BMC's alerting configuration (SMTP, PEF, policies, LAN
destinations). Read-only.

## Synopsis

Reads seven read-only `.asp` endpoints that together make up this BMC's
alerting configuration — where an alert can be sent (`getsmtpcfg.asp`,
`getlandeststable.asp`), what triggers one (`getallpefcfg.asp`, the IPMI
Platform Event Filter table), which policy routes a triggered filter to a
destination (`getallpolicycfg.asp`), how the email itself is formatted
(`getemailformat.asp`), and when each policy set last fired
(`gettriggercfg.asp`) — and returns them **grouped by what a caller actually
wants to know, not by endpoint name**.

`destinations` answers "where do alerts go": SMTP server(s) and LAN alert
destinations, together. `filtering` answers "what fires, and where it
routes": the PEF table, the alert policy table, and each policy set's
last-fired timestamp, together. `email_format` is the two email-format
options this endpoint reports. `adviser` is `getadvisercfg.asp` — see below
for why it is grouped here despite not describing alert delivery at all.

**`getadvisercfg.asp` is not actually about alert delivery.** Every field
this collection's fixture capture for it contains (KVM licence/status, KVM
port, mouse mode, keyboard layout, web port, single-port status, an OEM
feature status word) describes KVM/remote-console licensing and port
configuration, not SMTP, PEF, policy, or LAN-destination state. It is read by
this module only because it was specified as part of this module's endpoint
set; `adviser` is reported honestly, under its own name, rather than folded
into `destinations` or `filtering` where it would misleadingly imply a
connection to alert delivery the data does not show.

**Every field in every one of these seven endpoints was checked against a
real, captured response before being included here.** A raw field this
module does not name explicitly in its `RETURN` documentation is simply not
read: this module extracts only the specific keys documented below, so a key
this module has not been told about (on a firmware revision this corpus has
not seen) is silently dropped rather than guessed at or passed through
blind.

**No credential ever appears in this module's output, by construction, not
by redaction.** `getsmtpcfg.asp`'s captured response has no password-shaped
field at all when SMTP authentication is disabled — only a channel, sender
address, machine name, and, per SMTP relay, an enabled flag, port, server, an
auth-enabled flag, and a plain username. Because this module only ever
extracts those explicitly-named keys, a firmware revision that *does* report
an SMTP password field under some other key this module has never seen would
still not leak it: an unrecognised key is dropped, not passed through.
`destinations.smtp[].primary.username`/`.secondary.username` are themselves
plain usernames, not passwords, and are returned as read.

**`destinations.lan` addresses are environment detail, returned exactly as
the BMC reports them, with no redaction performed by this module.** The
corpus fixture for `getlandeststable.asp` happens to show every `DestAddr` as
an empty string (no destination configured on the captured board), which is
not evidence that a populated destination address would look like anything
in particular — do not build automation, or a test, that assumes a real
address is IPv4-shaped, always populated, or any other inferred property.

**This module never writes anything, and there is no option anywhere in it
that would.** Changing SMTP settings, PEF filters, alert policies, or LAN
destinations on this BMC is deliberately absent from this release: a mistaken
alert-destination change can silently stop notifications from ever reaching
an operator again, and unlike most misconfigurations that is a failure with
no error message anywhere — the BMC simply stops telling anyone. Adding a
write path for this configuration needs its own explicit review that this
module does not attempt to shortcut by piggybacking a `state` option onto a
read-only module.

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
`argument_spec()` in `plugins/modules/asmb8_alerts.py`.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `changed` | `bool` | always | Always `false`. |
| `destinations.smtp[].channel` / `.sender_address` / `.machine_name` | — | always | `CHANNEL_NUM`, `SENDERADDR`, `MACHINENAME`, one entry per SMTP channel (`1` and `8` in this corpus). |
| `destinations.smtp[].primary.enabled` / `.port` / `.server` / `.auth_enabled` / `.username` | — | always | `SMTPENABLE1`/`SMTPPORT1`/`SMTPSERVER1`/`SMTPAUTHENABLE1`/`USERNAME1` for the primary relay; `.secondary.*` is the same shape from the `*2` fields. |
| `destinations.lan[].channel` / `.destination_id` / `.destination_type` / `.address_format` / `.address` / `.user_id` / `.subject` / `.message` | — | always | `CHANNEL_NUM`, `LANDestID`, `DestType`, `AddrFormat`, `DestAddr` (unredacted, see module description), `UserID`, `Subject`, `Message`. |
| `filtering.event_filters[].*` | `int`/`str` | always | The full IPMI Platform Event Filter table from `getallpefcfg.asp`, field-for-field, exactly as reported and undecoded — see the module's own `RETURN` block for the complete field list. |
| `filtering.policies[].entry_number` / `.policy_number` / `.enabled` / `.policy_set` / `.channel_number` / `.destination_selector` / `.event_specific` / `.alert_string` | — | always | The alert policy table from `getallpolicycfg.asp`. `policy_number` matches `event_filters[].alert_policy_num`; `destination_selector` matches `destinations.lan[].destination_id`. |
| `filtering.triggers[].enabled` / `.timestamp` | `bool` / `int` | always | When each policy set last fired, from `gettriggercfg.asp` (`ENABLE`, `TIMESTAMP` — `0` if never fired). No id field correlates position to a specific policy set. |
| `email_format.available` | `list` of `str` | always | `EMAIL_FORMAT` values `getemailformat.asp` reported, in order (`AMI-Format`, `FixedSubject-Format` in this corpus). This endpoint does not report which is currently selected. |
| `adviser.kvm_license_status` / `.kvm_status` / `.secure_channel` / `.kvm_port` / `.mouse_mode` / `.keyboard_layout` / `.web_port` / `.singleport_status` / `.oem_feature_status` | — | when available | `getadvisercfg.asp`, verbatim — KVM/licensing/port state, **not** alert-delivery configuration. `null` only if this one endpoint's read failed. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | Always `"asmb8_alerts.read"`. |
| `operation.endpoint` | `str` | always | The `host:port` this read was performed against. |
| `operation.changed` | `bool` | always | Always `false`. |
| `operation.error_class` | `str` | always | `null` on success. |
| `operation.endpoint_reads.<field>.outcome` / `.error_class` | `str` | always | Per-endpoint `read`/`failed` outcome for each of the seven source endpoints, keyed by the field it feeds (`smtp`, `lan`, `adviser`, `event_filters`, `policies`, `triggers`, `email_format`). One endpoint failing does not fail the whole module once the `.asp` session is established. |

Verified against the `RETURN` block in `plugins/modules/asmb8_alerts.py`, and
against `tests/unit/fixtures/asp/getsmtpcfg.txt`, `getadvisercfg.txt`,
`getallpefcfg.txt`, `getallpolicycfg.txt`, `getlandeststable.txt`,
`getemailformat.txt`, and `gettriggercfg.txt`.

## `error_class` values this module can raise

- `connection` / `tls_validation` / `authentication` / `timeout` / `bmc_busy`
  — establishing the `.asp` session itself (login), via the same `AspClient`
  machinery `asmb8_media` uses. This fails the whole module.
- Any of the above, per endpoint, once logged in — caught internally and
  reported through `operation.endpoint_reads` instead of failing the module;
  see the module description.

## Check-mode behaviour

Full support. A full read runs identically in check mode, since this module
never mutates board configuration. `diff_mode` is not supported.

## Example

```yaml
- name: Read the BMC's full alerting configuration
  james_crowley.asmb8_ikvm.asmb8_alerts:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
  delegate_to: localhost
  no_log: true
  register: alerts

- name: Confirm at least one SMTP relay is enabled before relying on email alerts
  ansible.builtin.assert:
    that:
      - alerts.destinations.smtp | selectattr('primary.enabled') | list | length > 0

- name: Show every configured (non-empty) LAN alert destination address
  ansible.builtin.debug:
    msg: "{{ alerts.destinations.lan | selectattr('address', 'ne', '') | map(attribute='address') | list }}"
```

## See also

- [`asmb8_auditlog`](asmb8_auditlog.md), [`asmb8_info`](asmb8_info.md).
