<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `asmb8_network`

Report ASMB8-iKVM LAN, DNS, and NIC-bonding configuration. Read-only.

## Synopsis

Reads this BMC's per-channel LAN configuration (`getalllancfg.asp`), the
LAN-channel-to-interface mapping (`getlanchannelinfo.asp`), DNS configuration
(`getdnscfg.asp`), NIC-bonding configuration (`getnwbondcfg.asp`), and
bonding hardware support (`checknwbond.asp`). All five are read-only `.asp`
RPCs; every field name and shape is sourced from the real, redacted capture
corpus under `tests/unit/fixtures/asp/` — AMI has not published a
specification for this surface.

**This module logs in unconditionally**, for the same reason `asmb8_users`
does: every endpoint it reads requires an authenticated `.asp` session. See
[Check-mode behaviour](#check-mode-behaviour) for how this module avoids
spending that session in a mode where nothing is going to be changed anyway.

**The address/MAC values in this module's own test corpus are redacted, and
this module makes no assumption about what a real one looks like.** Per
`tests/unit/fixtures/asp/README.md`, every IPv4/IPv6 address in the corpus
was replaced with an RFC 5737/RFC 3849 documentation value and every MAC
address with `00:00:5E:00:53:00` before this module's author ever read a byte
of it. One consequence worth knowing before reading `lan_channels`: the
corpus's own `v4IPAddr`/`v4Subnet`/`v4Gateway` values are identical to each
other for a given channel — **that is an artifact of every real address in
that channel having been replaced with the same documentation value, not
evidence that this board's address/subnet/gateway fields are ever equal on
real hardware.**

**`getdnscfg.asp`'s `TSIG_PRIVATE` field is treated as a credential and
never returned.** DNS TSIG ("transaction signature") is a shared-secret HMAC
key used to authorize dynamic DNS updates — despite every sample in this
corpus reading the literal string `Not Available`, the field's own name says
"this holds a secret" clearly enough that this module does not wait for
corpus evidence of an actual key before treating it as one. Only
`dns_entries[].tsig_key_configured` (a boolean) is returned; the field's raw
text is not.

**No sourced field links a `getdnscfg.asp` record to a specific LAN
channel.** The corpus's sample returns four DNS records with no
channel-number field of their own, so `dns_entries` is returned as a flat
list in the BMC's own response order — this module does not guess at which
record corresponds to which channel in `lan_channels`.

**No write capability exists here, deliberately.** Changing this BMC's
network configuration — static/DHCP source, the address itself, VLAN
membership, DNS registration, or NIC bonding — is exactly the class of change
that can sever the management path this collection uses to reach the board
at all, with no independent path left to undo it. A future write capability,
if ever added, would need at minimum a live reachability re-check over a
**second**, independent path (e.g. IPMI) before and after the change, and
this collection's usual before/after `operation` receipt shape. None of that
exists yet.

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
`argument_spec()` in `plugins/modules/asmb8_network.py`.

## Return values

| Field | Type | Returned | Description |
|---|---|---|---|
| `changed` | `bool` | always | Always `false`. |
| `lan_channels[].channel` / `.enabled` / `.mac_address` | — | always | `channelNum`, `lanEnable`, and `macAddress` (redacted to `00:00:5E:00:53:00` in this module's own test corpus). |
| `lan_channels[].ipv4.source_raw` / `.address` / `.subnet_mask` / `.gateway` | — | always | `v4IPSource` (raw, not decoded to `static`/`dhcp` — no sourced mapping exists), `v4IPAddr`, `v4Subnet`, `v4Gateway`. See the module description's redaction-artifact caveat. |
| `lan_channels[].ipv6.enabled` / `.source_raw` / `.address` / `.prefix_length` / `.gateway` | — | always | `v6Enable`, `v6IPSource` (raw), `v6IPAddr` (`::` when unconfigured in this corpus), `v6Prefix`, `v6Gateway`. |
| `lan_channels[].vlan.enabled` / `.id` / `.priority` | — | always | `vlanEnable`, `vlanID`, `vlanPriority`. |
| `interfaces[].index` / `.name` / `.channel` / `.enabled` | — | always | `ETH_INDEX`, `INTERFACE_NAME` (e.g. `eth0`), `CHANNEL_NUM` (matches `lan_channels[].channel`), `INTERFACE_ENABLE`. |
| `dns_entries[].enabled` / `.mdns_enabled` | `bool` | always | `DNS_ENABLE`, `MDNS`. |
| `dns_entries[].hostname_source_raw` / `.hostname` | — | always | `HOST_CFG` (raw), `HOST_NAME`. |
| `dns_entries[].register_with_bmc` / `.register_with_dhcp` | `bool` | always | `REG_BMC`, `REG_DHCP`. |
| `dns_entries[].tsig_enabled` / `.tsig_key_configured` | `bool` | always | `TSIG_ENABLE`, and whether a TSIG key is on file (`TSIG_EXISTS`/`TSIG_PRIVATE` presence). The key value is never returned. |
| `dns_entries[].domain_source` / `.domain_name` | `str` | always | `DOMAIN_CFG` (e.g. `Manual`), `DOMAIN_NAME`. |
| `dns_entries[].dns_source_raw` / `.dns_priority` / `.dns_server` | — | always | `DNS_CFG` (raw), `DNS_PRIORITY`, `DNS_IP` (`::` when unconfigured in this corpus). |
| `bonding.enabled` / `.mode_raw` / `.interface_raw` / `.vlan_enabled` / `.auto_configured` | — | always | `BOND_ENABLE`, `BOND_MODE` (raw — no sourced mapping to a named bonding mode), `BOND_IFC`, `VLAN_ENABLE`, `AUTO_CONF`. `null` if `getnwbondcfg.asp` returned no record. |
| `bond_support.nic_count` | `int` | always | `checknwbond.asp`'s `NIC_COUNT`. `null` if that endpoint returned no record. |
| `operation.schema` | `str` | always | Always `"asmb8-ikvm-operation/v1"`. |
| `operation.action` | `str` | always | Always `"asmb8_network.report"`. |
| `operation.endpoint` | `str` | always | The `host:port` this read was performed against. |
| `operation.changed` | `bool` | always | Always `false`. |
| `operation.observed` | `dict` | always | Mirrors `lan_channels`, `interfaces`, `dns_entries`, `bonding`, and `bond_support` together. |
| `operation.error_class` | `str` | always | `null` on success. |

Verified against the `RETURN` block in `plugins/modules/asmb8_network.py`,
and against `tests/unit/fixtures/asp/getalllancfg.txt`,
`getlanchannelinfo.txt`, `getdnscfg.txt`, `getnwbondcfg.txt`, and
`checknwbond.txt`.

## `error_class` values this module can raise

- `connection` / `tls_validation` / `authentication` / `timeout` / `bmc_busy`
  — establishing or using the `.asp` session, via the same `AspClient`
  machinery `asmb8_media` uses.

## Check-mode behaviour

Supported, but does **not** log in or read anything — it returns immediately
with every fact field `null`. Same reasoning as `asmb8_users`'s check-mode
behaviour: this module's login is a real BMC-side side effect, and there is
no write path here whose effect a check-mode run could be predicting.
`diff_mode` is not supported.

## Example

```yaml
- name: Report LAN, DNS, and bonding configuration
  james_crowley.asmb8_ikvm.asmb8_network:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
  delegate_to: localhost
  no_log: true
  register: network_report

- name: Assert channel 1 is enabled, without ever seeing a DNS TSIG key value
  ansible.builtin.assert:
    that:
      - network_report.lan_channels[0].enabled

- name: Report whether this board supports NIC bonding at all
  ansible.builtin.debug:
    msg: "{{ network_report.bond_support.nic_count }} NIC(s) available for bonding"
```

## See also

- [`asmb8_info`](asmb8_info.md), [`asmb8_users`](asmb8_users.md),
  [`asmb8_sessions`](asmb8_sessions.md).
