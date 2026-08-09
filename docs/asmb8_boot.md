<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_boot`

Select a one-time IPMI boot device on an ASMB8-iKVM endpoint.

## Synopsis

Reads and sets the IPMI boot-device override via `pyghmi`'s
`Command.get_bootdev()`/`set_bootdev()` — the same already-working IPMI path
`asmb8_power` wraps, and not the `.asp`/JNLP surface this collection's
virtual-media path speaks.

Idempotent: the current override is read first, and `set_bootdev()` is only
called when `device` or `uefi` actually differ from what the BMC currently
reports.

**This module refuses persistent boot-order changes outright.** `persistent`
exists only so that refusal is an explicit, documented choice visible in
`ansible-doc` rather than a capability that is merely absent — setting it to
`true` always fails with `error_class=unsupported_capability`, **before any
connection is even attempted**, and does **not** get silently downgraded to a
one-time override. This is a deliberate policy decision encoded in the
module's source (`_PERSISTENT_REJECTED_MESSAGE` and `reject_persistent()` in
`plugins/modules/asmb8_boot.py`), not an oversight — do not "fix" this by
treating `persistent=true` the same as `false`.

Like `asmb8_power`, this module talks to the BMC over IPMI only. `port`,
`use_tls`, `allow_insecure_transport`, `validate_certs`, `ca_path`,
`tls_fingerprint`, `timeout`, and `connect_timeout` are accepted (for
`module_defaults` group compatibility) but entirely **ignored** — only
`host`, `username`, `password`, and `ipmi_port` are actually used, and the
`requests` requirement inherited from the shared connection fragment does
**not** apply here.

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
| `device` | `str` | — | yes | `network`, `floppy`, `hd`, `safe`, `optical`, `setup`, `default` |
| `uefi` | `bool` | `false` | no | — |
| `persistent` | `bool` | `false` | no | — (**`true` always fails**) |

Verified against `_connection_argument_spec()`/`argument_spec()` in
`plugins/modules/asmb8_boot.py`. `device`'s choices are `pyghmi`'s own
`set_bootdev()`/`get_bootdev()` vocabulary, exactly
`community.general.ipmi_boot`'s documented `bootdev` choices
(`plugins/module_utils/models.py`'s `BOOT_DEVICES`), sourced from that
module's own documentation, not invented here.

### `device`

- `network` — PXE boot.
- `floppy` — boot from floppy.
- `hd` — boot from hard drive.
- `safe` — boot from hard drive, requesting BIOS "safe mode".
- `optical` — boot from CD/DVD/BD drive. **This is the value paired with
  `asmb8_media`'s virtual CD-ROM.**
- `setup` — boot into the firmware setup utility.
- `default` — remove any standing IPMI-directed boot-device request.

### `uefi`

Whether to request UEFI boot explicitly for this one override. Many systems
boot UEFI regardless of this flag if that is how they are otherwise
configured; IPMI (and `pyghmi`, which implements it) offers no "don't care"
value, only "request UEFI" or not.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `device` | `str` | always | The `device` value that was (or, in check mode, would be) armed. |
| `uefi` | `bool` | always | The `uefi` value that was (or, in check mode, would be) armed. |
| `previous` | `dict` | always | The boot-device override observed before any action, exactly as `pyghmi`'s `get_bootdev()` returned it. `uefimode` is absent on the branch where the BMC reports no standing override at all. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | Always `"asmb8_boot"`. |
| `operation.endpoint` | `str` | always | `host:ipmi_port` this operation was performed against. |
| `operation.changed` | `bool` | always | Mirrors the top-level `changed`. |
| `operation.previous` | `dict` | always | Same value as `previous`. |
| `operation.desired` | `dict` | always | The `bootdev`/`uefiboot`/`persist` arguments this module sent (or would send). `persist` is always `false`. |
| `operation.observed` | `dict` | always | What `set_bootdev()` returned, normalized to the same shape as `previous`. Equal to `previous` when nothing was sent because the override already matched. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_boot.py`.

## `error_class` values this module can raise

- `unsupported_capability` — `persistent=true` was requested. Raised before
  any IPMI session is opened at all.
- `connection` / `authentication` / `timeout` — establishing the IPMI session,
  classified from `pyghmi`'s own error text.
- `remote_operation` — either `get_bootdev()` failed at the IPMI level, or
  `set_bootdev()` failed (including `pyghmi`'s own `{'error': ...}` response
  shape for an unrecognised device name, which does not itself raise but is
  re-raised as this class by `IpmiClient.set_boot_device`).

## Check-mode behaviour

Full support. The current boot-device override is read and compared against
`device`/`uefi` exactly as in normal mode, but `set_bootdev()` is never sent.
`diff_mode` is fully supported — the previous and desired boot-device
override are both present in `operation`.

## Example

```yaml
- name: Arm a one-time PXE boot for an unattended install
  james_crowley.asmb8_ikvm.asmb8_boot:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    device: network
  delegate_to: localhost
  no_log: true

- name: Arm a one-time UEFI HDD boot
  james_crowley.asmb8_ikvm.asmb8_boot:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    device: hd
    uefi: true
  delegate_to: localhost
  no_log: true

- name: Clear any standing IPMI boot-device override
  james_crowley.asmb8_ikvm.asmb8_boot:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    device: default
  delegate_to: localhost
  no_log: true

# This fails with error_class=unsupported_capability before touching the BMC.
- name: Persistent boot-order changes are rejected outright
  james_crowley.asmb8_ikvm.asmb8_boot:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    device: hd
    persistent: true
  delegate_to: localhost
  no_log: true
  ignore_errors: true
```
