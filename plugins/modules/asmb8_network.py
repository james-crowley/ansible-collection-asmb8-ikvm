#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_network
short_description: Report ASMB8-iKVM LAN, DNS, and NIC-bonding configuration
description:
  - >-
    Reads this BMC's per-channel LAN configuration (C(getalllancfg.asp)), the LAN-channel-to-interface
    mapping (C(getlanchannelinfo.asp)), DNS configuration (C(getdnscfg.asp)), NIC-bonding
    configuration (C(getnwbondcfg.asp)), and bonding hardware support (C(checknwbond.asp)). All five
    are read-only C(.asp) RPCs; every field name and shape below is sourced from the real, redacted
    capture corpus under C(tests/unit/fixtures/asp/) -- AMI has not published a specification for
    this surface (see C(plugins/module_utils/asp.py)'s module docstring).
  - >-
    B(This module logs in), for the same reason M(james_crowley.asmb8_ikvm.asmb8_users) does: every
    endpoint it reads requires an authenticated C(.asp) session, so logging in is this module's
    ordinary behaviour rather than an opt-in exception the way it is for
    M(james_crowley.asmb8_ikvm.asmb8_info). See O(ignore:check_mode) below for how this module
    avoids spending that session in a mode where nothing is going to be changed anyway.
  - >-
    B(The address/MAC values in this module's own test corpus are redacted, and this module makes
    no assumption about what a real one looks like.) Per C(tests/unit/fixtures/asp/README.md),
    every IPv4/IPv6 address in the corpus was replaced with an RFC 5737/RFC 3849 documentation
    value and every MAC address with C(00:00:5E:00:53:00) before this module's author ever read a
    byte of it. One consequence worth knowing before reading RV(lan_channels): the corpus's own
    C(v4IPAddr)/C(v4Subnet)/C(v4Gateway) values are identical to each other for a given channel --
    that is an artifact of every real address in that channel having been replaced with the B(same)
    documentation value, not evidence that this board's address/subnet/gateway fields are ever
    equal on real hardware.
  - >-
    B(C(getdnscfg.asp)'s C(TSIG_PRIVATE) field is treated as a credential and never returned.) DNS
    TSIG ("transaction signature") is a shared-secret HMAC key used to authorize dynamic DNS
    updates -- despite every sample in this corpus reading the literal string V(Not Available),
    the field's own name says "this holds a secret" clearly enough that this module does not wait
    for corpus evidence of an actual key before treating it as one. Only
    RV(dns_entries[].tsig_key_configured) (a boolean) is returned; the field's raw text is not.
  - >-
    B(No sourced field links a C(getdnscfg.asp) record to a specific LAN channel.) The corpus's
    sample returns four DNS records with no channel-number field of their own, so RV(dns_entries)
    is returned as a flat list in the BMC's own response order -- this module does not guess at
    which record corresponds to which channel in RV(lan_channels).
  - >-
    B(No write capability exists here, deliberately.) Changing this BMC's network configuration --
    static/DHCP source, the address itself, VLAN membership, DNS registration, or NIC bonding -- is
    exactly the class of change that can sever the management path this collection uses to reach
    the board at all, with no independent path left to undo it. A future write capability, if ever
    added, would need at minimum a live reachability re-check performed over a B(second),
    independent path (e.g. IPMI) before and after the change, and this collection's usual
    before/after C(operation) receipt shape. None of that exists yet. This module only reads.
version_added: 0.1.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_info
  - module: james_crowley.asmb8_ikvm.asmb8_users
  - module: james_crowley.asmb8_ikvm.asmb8_sessions
attributes:
  check_mode:
    description: >-
      Supported, but does not log in or read anything -- it returns immediately with every fact
      field V(null). Same reasoning as M(james_crowley.asmb8_ikvm.asmb8_users)'s O(ignore:check_mode):
      this module's login is a real BMC-side side effect, and there is no write path here whose
      effect a check-mode run could be predicting.
    support: full
  diff_mode:
    description: Not supported. There is no prior/after state to diff for a read-only module.
    support: none
requirements:
  - requests >= 2.25.0 (on the Ansible controller)
"""

EXAMPLES = r"""
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
"""

RETURN = r"""
changed:
  description: Always V(false) -- this module never mutates BMC state. See the module description.
  type: bool
  returned: always
lan_channels:
  description: One entry per row of C(getalllancfg.asp), in the BMC's own response order.
  type: list
  elements: dict
  returned: always
  contains:
    channel:
      description: Raw C(channelNum).
      type: int
    enabled:
      description: Whether C(lanEnable) is set for this channel.
      type: bool
    mac_address:
      description: Raw C(macAddress). Redacted to C(00:00:5E:00:53:00) in this module's own test corpus -- see the module description.
      type: str
    ipv4:
      description: This channel's IPv4 configuration.
      type: dict
      contains:
        source_raw:
          description: >-
            Raw C(v4IPSource) (e.g. V(1) or V(2) in this corpus). Not decoded to V(static)/V(dhcp) or
            similar -- no sourced mapping exists; see this module's sibling
            M(james_crowley.asmb8_ikvm.asmb8_users) for the same policy applied to that module's own
            unsourced numeric fields.
          type: int
        address:
          description: Raw C(v4IPAddr).
          type: str
        subnet_mask:
          description: >-
            Raw C(v4Subnet). See the module description's note on why this may read identical to
            RV(lan_channels[].ipv4.address) in this corpus's own redacted samples.
          type: str
        gateway:
          description: Raw C(v4Gateway). Same redaction-artifact caveat as RV(lan_channels[].ipv4.subnet_mask).
          type: str
    ipv6:
      description: This channel's IPv6 configuration.
      type: dict
      contains:
        enabled:
          description: Whether C(v6Enable) is set.
          type: bool
        source_raw:
          description: Raw C(v6IPSource). Same unsourced-encoding caveat as RV(lan_channels[].ipv4.source_raw).
          type: int
        address:
          description: Raw C(v6IPAddr) (V(::) when unconfigured, in this corpus).
          type: str
        prefix_length:
          description: Raw C(v6Prefix).
          type: int
        gateway:
          description: Raw C(v6Gateway).
          type: str
    vlan:
      description: This channel's VLAN tagging configuration.
      type: dict
      contains:
        enabled:
          description: Whether C(vlanEnable) is set.
          type: bool
        id:
          description: Raw C(vlanID).
          type: int
        priority:
          description: Raw C(vlanPriority).
          type: int
interfaces:
  description: One entry per row of C(getlanchannelinfo.asp) -- the physical/logical NIC each LAN channel maps to.
  type: list
  elements: dict
  returned: always
  contains:
    index:
      description: Raw C(ETH_INDEX).
      type: int
    name:
      description: Raw C(INTERFACE_NAME) (e.g. V(eth0)).
      type: str
    channel:
      description: Raw C(CHANNEL_NUM). Matches a RV(lan_channels[].channel) value.
      type: int
    enabled:
      description: Whether C(INTERFACE_ENABLE) is set.
      type: bool
dns_entries:
  description: >-
    One entry per row of C(getdnscfg.asp), in the BMC's own response order -- see the module
    description for why this is a flat list rather than one entry per LAN channel.
  type: list
  elements: dict
  returned: always
  contains:
    enabled:
      description: Whether C(DNS_ENABLE) is set for this entry.
      type: bool
    mdns_enabled:
      description: Whether C(MDNS) (multicast DNS) is set.
      type: bool
    hostname_source_raw:
      description: Raw C(HOST_CFG).
      type: int
    hostname:
      description: Raw C(HOST_NAME).
      type: str
    register_with_bmc:
      description: Whether C(REG_BMC) is set.
      type: bool
    register_with_dhcp:
      description: Whether C(REG_DHCP) is set.
      type: bool
    tsig_enabled:
      description: Whether C(TSIG_ENABLE) is set.
      type: bool
    tsig_key_configured:
      description: >-
        Whether a DNS TSIG key is on file, derived from C(TSIG_EXISTS) and C(TSIG_PRIVATE)'s
        presence. B(The key value itself is never returned) -- see the module description for why
        C(TSIG_PRIVATE) is treated as a credential regardless of what this corpus's own samples
        happen to contain.
      type: bool
    domain_source:
      description: Raw C(DOMAIN_CFG) (e.g. V(Manual)).
      type: str
    domain_name:
      description: Raw C(DOMAIN_NAME).
      type: str
    dns_source_raw:
      description: Raw C(DNS_CFG).
      type: int
    dns_priority:
      description: Raw C(DNS_PRIORITY).
      type: int
    dns_server:
      description: Raw C(DNS_IP) (V(::) when this entry's server is unconfigured, in this corpus).
      type: str
bonding:
  description: NIC-bonding configuration from C(getnwbondcfg.asp)'s one record, or V(null) if that endpoint returned none.
  type: dict
  returned: always
  contains:
    enabled:
      description: Whether C(BOND_ENABLE) is set.
      type: bool
    mode_raw:
      description: Raw C(BOND_MODE). Not decoded -- no sourced mapping to a named bonding mode (e.g. active-backup, 802.3ad) exists.
      type: int
    interface_raw:
      description: Raw C(BOND_IFC).
      type: int
    vlan_enabled:
      description: Whether C(VLAN_ENABLE) is set.
      type: bool
    auto_configured:
      description: Whether C(AUTO_CONF) is set.
      type: bool
bond_support:
  description: NIC-bonding hardware support from C(checknwbond.asp)'s one record, or V(null) if that endpoint returned none.
  type: dict
  returned: always
  contains:
    nic_count:
      description: Raw C(NIC_COUNT) -- how many NICs this board reports as available for bonding.
      type: int
operation:
  description: >-
    The C(asmb8-ikvm-operation/v1) receipt for this read, in the same nested shape every other
    module in this collection returns it under.
  type: dict
  returned: always
  contains:
    schema:
      description: Always V(asmb8-ikvm-operation/v1).
      type: str
    action:
      description: Always V(asmb8_network.report).
      type: str
    endpoint:
      description: The C(host:port) this read was performed against.
      type: str
    changed:
      description: Always V(false).
      type: bool
    observed:
      description: Mirrors RV(lan_channels), RV(interfaces), RV(dns_entries), RV(bonding), and RV(bond_support) together.
      type: dict
    error_class:
      description: A stable machine-readable failure class. V(null) on success.
      type: str
"""

from typing import Any

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import HAS_REQUESTS, REQUESTS_IMPORT_ERROR, AspClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import IkvmError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt

#: Sentinel string this board's firmware uses for "no TSIG key on file", per this corpus's
#: getdnscfg.txt sample. Treated purely as a presence check -- see the module description for why
#: the field's own text is never returned even when it differs from this value.
_TSIG_PRIVATE_NOT_AVAILABLE = "Not Available"


def _connection_argument_spec() -> dict[str, dict]:
    return {
        "host": {"type": "str", "required": True},
        "port": {"type": "int", "default": 443},
        "username": {"type": "str", "default": "admin"},
        "password": {"type": "str", "required": True, "no_log": True},
        "use_tls": {"type": "bool", "default": True},
        "allow_insecure_transport": {"type": "bool", "default": False},
        "validate_certs": {"type": "bool", "default": True},
        "ca_path": {"type": "path"},
        "tls_fingerprint": {"type": "str"},
        "timeout": {"type": "int", "default": 30},
        "connect_timeout": {"type": "int", "default": 10},
    }


def argument_spec() -> dict[str, dict]:
    return _connection_argument_spec()


def build_asp_client(params: dict) -> AspClient:
    return AspClient(
        host=params["host"],
        port=params["port"],
        username=params["username"],
        password=params["password"],
        use_tls=params["use_tls"],
        validate_certs=params["validate_certs"],
        ca_path=params["ca_path"],
        tls_fingerprint=params["tls_fingerprint"],
        allow_insecure_transport=params["allow_insecure_transport"],
        timeout=params["timeout"],
        connect_timeout=params["connect_timeout"],
    )


def decode_lan_channel(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "channel": record.get("channelNum"),
        "enabled": bool(record.get("lanEnable")),
        "mac_address": record.get("macAddress"),
        "ipv4": {
            "source_raw": record.get("v4IPSource"),
            "address": record.get("v4IPAddr"),
            "subnet_mask": record.get("v4Subnet"),
            "gateway": record.get("v4Gateway"),
        },
        "ipv6": {
            "enabled": bool(record.get("v6Enable")),
            "source_raw": record.get("v6IPSource"),
            "address": record.get("v6IPAddr"),
            "prefix_length": record.get("v6Prefix"),
            "gateway": record.get("v6Gateway"),
        },
        "vlan": {
            "enabled": bool(record.get("vlanEnable")),
            "id": record.get("vlanID"),
            "priority": record.get("vlanPriority"),
        },
    }


def decode_interface(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": record.get("ETH_INDEX"),
        "name": record.get("INTERFACE_NAME"),
        "channel": record.get("CHANNEL_NUM"),
        "enabled": bool(record.get("INTERFACE_ENABLE")),
    }


def decode_dns_entry(record: dict[str, Any]) -> dict[str, Any]:
    """Decode one ``getdnscfg.asp`` row. Never carries ``TSIG_PRIVATE``'s raw value -- see the module description."""
    tsig_private = record.get("TSIG_PRIVATE") or ""
    tsig_exists = record.get("TSIG_EXISTS")
    return {
        "enabled": bool(record.get("DNS_ENABLE")),
        "mdns_enabled": bool(record.get("MDNS")),
        "hostname_source_raw": record.get("HOST_CFG"),
        "hostname": record.get("HOST_NAME"),
        "register_with_bmc": bool(record.get("REG_BMC")),
        "register_with_dhcp": bool(record.get("REG_DHCP")),
        "tsig_enabled": bool(record.get("TSIG_ENABLE")),
        "tsig_key_configured": bool(tsig_exists) or (tsig_private != "" and tsig_private != _TSIG_PRIVATE_NOT_AVAILABLE),
        "domain_source": record.get("DOMAIN_CFG"),
        "domain_name": record.get("DOMAIN_NAME"),
        "dns_source_raw": record.get("DNS_CFG"),
        "dns_priority": record.get("DNS_PRIORITY"),
        "dns_server": record.get("DNS_IP"),
    }


def decode_bonding(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(record.get("BOND_ENABLE")),
        "mode_raw": record.get("BOND_MODE"),
        "interface_raw": record.get("BOND_IFC"),
        "vlan_enabled": bool(record.get("VLAN_ENABLE")),
        "auto_configured": bool(record.get("AUTO_CONF")),
    }


def decode_bond_support(record: dict[str, Any]) -> dict[str, Any]:
    return {"nic_count": record.get("NIC_COUNT")}


def build_network_report(
    lan_records: list[dict[str, Any]],
    interface_records: list[dict[str, Any]],
    dns_records: list[dict[str, Any]],
    bond_records: list[dict[str, Any]],
    bond_support_records: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "lan_channels": [decode_lan_channel(r) for r in lan_records],
        "interfaces": [decode_interface(r) for r in interface_records],
        "dns_entries": [decode_dns_entry(r) for r in dns_records],
        "bonding": decode_bonding(bond_records[0]) if bond_records else None,
        "bond_support": decode_bond_support(bond_support_records[0]) if bond_support_records else None,
    }


def gather_report(asp_client: AspClient) -> dict[str, Any]:
    """Log in and read all five source endpoints. The only place this module's login happens."""
    asp_client.login()
    lan_response = asp_client.get_webvar("getalllancfg")
    interfaces_response = asp_client.get_webvar("getlanchannelinfo")
    dns_response = asp_client.get_webvar("getdnscfg")
    bond_response = asp_client.get_webvar("getnwbondcfg")
    bond_support_response = asp_client.get_webvar("checknwbond")

    return build_network_report(
        lan_response.records,
        interfaces_response.records,
        dns_response.records,
        bond_response.records,
        bond_support_response.records,
    )


def main() -> None:
    module = AnsibleModule(argument_spec=argument_spec(), supports_check_mode=True)
    params = module.params

    if not HAS_REQUESTS:
        module.fail_json(msg=missing_required_lib("requests"), exception=REQUESTS_IMPORT_ERROR)
        return

    endpoint = f"{params['host']}:{params['port']}"

    if module.check_mode:
        # See ATTRIBUTES.check_mode's documentation: login is a real BMC-side side effect this
        # module refuses to spend in check mode, when there is no write path whose effect it could
        # even be predicting.
        receipt = OperationReceipt(action="asmb8_network.report", endpoint=endpoint, changed=False, observed=None)
        module.exit_json(
            changed=False,
            lan_channels=None,
            interfaces=None,
            dns_entries=None,
            bonding=None,
            bond_support=None,
            operation=receipt.to_dict(),
        )
        return

    try:
        asp_client = build_asp_client(params)
        report = gather_report(asp_client)
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    receipt = OperationReceipt(action="asmb8_network.report", endpoint=asp_client.endpoint, changed=False, observed=report)
    module.exit_json(changed=False, **report, operation=receipt.to_dict())


if __name__ == "__main__":
    main()
