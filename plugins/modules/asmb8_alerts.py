#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_alerts
short_description: Read the ASMB8-iKVM BMC's alerting configuration (SMTP, PEF, policies, LAN destinations)
description:
  - >-
    Reads seven read-only C(.asp) endpoints that together make up this BMC's alerting
    configuration -- where an alert can be sent (C(getsmtpcfg.asp), C(getlandeststable.asp)), what
    triggers one (C(getallpefcfg.asp), the IPMI Platform Event Filter table), which policy routes a
    triggered filter to a destination (C(getallpolicycfg.asp)), how the email itself is formatted
    (C(getemailformat.asp)), and when each policy set last fired (C(gettriggercfg.asp)) -- and
    returns them grouped by what a caller actually wants to know, not by endpoint name.
  - >-
    RV(destinations) answers "where do alerts go": SMTP server(s) and LAN alert destinations,
    together. RV(filtering) answers "what fires, and where it routes": the PEF table, the alert
    policy table, and each policy set's last-fired timestamp, together. RV(email_format) is the
    two email-format options this endpoint reports. RV(adviser) is C(getadvisercfg.asp) -- see its
    own note below for why it is grouped here despite not describing alert delivery at all.
  - >-
    B(getadvisercfg.asp is not actually about alert delivery.) Every field this collection's
    fixture capture for it contains (KVM licence/status, KVM port, mouse mode, keyboard layout, web
    port, single-port status, an OEM feature status word) describes KVM/remote-console licensing
    and port configuration, not SMTP, PEF, policy, or LAN-destination state. It is read by this
    module only because it was specified as part of this module's endpoint set; RV(adviser) is
    reported honestly, under its own name, rather than folded into RV(destinations) or
    RV(filtering) where it would misleadingly imply a connection to alert delivery that the data
    does not show.
  - >-
    B(Every field in every one of these seven endpoints was checked against a real, captured
    response before being included here) -- see C(tests/unit/fixtures/asp/) for the seven files
    (C(getsmtpcfg.txt), C(getadvisercfg.txt), C(getallpefcfg.txt), C(getallpolicycfg.txt),
    C(getlandeststable.txt), C(getemailformat.txt), C(gettriggercfg.txt)) this module's shape is
    sourced from. A raw field this module does not name explicitly above is simply not read: this
    module extracts only the specific keys documented under RV(destinations)/RV(filtering)/
    RV(email_format)/RV(adviser) below, so a key this module has not been told about (on a firmware
    revision this corpus has not seen) is silently dropped rather than guessed at or passed
    through blind.
  - >-
    B(No credential ever appears in this module's output, by construction, not by redaction.)
    C(getsmtpcfg.asp)'s captured response has no password-shaped field at all when SMTP
    authentication is disabled (C(SMTPAUTHENABLE1)/C(SMTPAUTHENABLE2) both V(0) in the fixture) --
    only a channel, sender address, machine name, and, per SMTP relay, an enabled flag, port,
    server, an auth-enabled flag, and a plain username. Because this module only ever extracts
    those explicitly-named keys (see the point above), a firmware revision that DOES report an SMTP
    password field under some other key this module has never seen would still not leak it: an
    unrecognised key is dropped, not passed through. RV(destinations.smtp[].primary.username) and
    RV(destinations.smtp[].secondary.username) are themselves plain usernames, not passwords, and
    are returned as read; if that account identity is itself sensitive in your environment, treat
    this module's output the same way you would treat any other configuration dump that names an
    account.
  - >-
    B(RV(destinations.lan) addresses are environment detail, returned exactly as the BMC reports
    them, with no redaction performed by this module.) The corpus fixture for C(getlandeststable.asp)
    happens to show every C(DestAddr) as an empty string (no destination configured on the captured
    board), which is not evidence that a populated destination address would look like anything in
    particular -- do not build automation, or a test, that assumes a real address is IPv4-shaped,
    always populated, or any other inferred property. This module reports whatever the BMC returns,
    unmodified.
  - >-
    B(This module never writes anything, and there is no option anywhere in it that would.)
    Changing SMTP settings, PEF filters, alert policies, or LAN destinations on this BMC is
    B(deliberately absent) from this release: a mistaken alert-destination change can silently stop
    notifications from ever reaching an operator again, and unlike most misconfigurations that is a
    failure with no error message anywhere -- the BMC simply stops telling anyone. Adding a write
    path for this configuration needs its own explicit review (which destinations/policies a caller
    is allowed to touch, what confirms a change actually took effect, how a caller would ever notice
    a write silently broke alerting) that this module does not attempt to shortcut by piggybacking
    a C(state) option onto a read-only module.
version_added: 0.4.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_auditlog
  - module: james_crowley.asmb8_ikvm.asmb8_info
attributes:
  check_mode:
    description: A full read runs identically in check mode, since this module never mutates board configuration.
    support: full
  diff_mode:
    description: Not supported. There is no prior/after state to diff for a read-only module.
    support: none
requirements:
  - requests >= 2.25.0 (on the Ansible controller)
"""

EXAMPLES = r"""
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
"""

RETURN = r"""
changed:
  description: Always V(false). This module never mutates anything.
  type: bool
  returned: always
destinations:
  description: Where an alert this BMC raises can be sent, from C(getsmtpcfg.asp) and C(getlandeststable.asp).
  type: dict
  returned: always
  contains:
    smtp:
      description: One entry per SMTP channel C(getsmtpcfg.asp) reports (the fixture corpus shows channels 1 and 8).
      type: list
      elements: dict
      contains:
        channel:
          description: IPMI LAN channel number this SMTP configuration applies to (C(CHANNEL_NUM)).
          type: int
        sender_address:
          description: From-address used for outgoing alert email (C(SENDERADDR)). Empty string if unset.
          type: str
        machine_name:
          description: Machine/host name string included in outgoing alert email (C(MACHINENAME)). Empty string if unset.
          type: str
        primary:
          description: The primary SMTP relay for this channel.
          type: dict
          contains:
            enabled:
              description: Whether this relay is enabled (C(SMTPENABLE1)).
              type: bool
            port:
              description: TCP port of this relay (C(SMTPPORT1)).
              type: int
            server:
              description: Hostname or address of this relay (C(SMTPSERVER1)). Empty string if unset.
              type: str
            auth_enabled:
              description: Whether SMTP authentication is enabled for this relay (C(SMTPAUTHENABLE1)).
              type: bool
            username:
              description: >-
                SMTP authentication username for this relay (C(USERNAME1)), returned as read. This
                is a plain username, not a password -- see this module's description for why no
                password-shaped field is available here to omit in the first place.
              type: str
        secondary:
          description: The secondary SMTP relay for this channel, same shape as C(primary) (C(*2) fields).
          type: dict
          contains:
            enabled:
              description: Same as C(primary.enabled), for the secondary relay (C(SMTPENABLE2)).
              type: bool
            port:
              description: Same as C(primary.port), for the secondary relay (C(SMTPPORT2)).
              type: int
            server:
              description: Same as C(primary.server), for the secondary relay (C(SMTPSERVER2)).
              type: str
            auth_enabled:
              description: Same as C(primary.auth_enabled), for the secondary relay (C(SMTPAUTHENABLE2)).
              type: bool
            username:
              description: Same as C(primary.username), for the secondary relay (C(USERNAME2)).
              type: str
    lan:
      description: >-
        One entry per row of C(getlandeststable.asp)'s LAN alert destination table. See this
        module's description for why C(address) is returned exactly as reported, unredacted, and
        why a test or a caller must not assume any particular shape for it.
      type: list
      elements: dict
      contains:
        channel:
          description: IPMI LAN channel number this destination row applies to (C(CHANNEL_NUM)).
          type: int
        destination_id:
          description: IPMI LAN destination selector for this row (C(LANDestID)).
          type: int
        destination_type:
          description: IPMI LAN destination type code for this row (C(DestType)).
          type: int
        address_format:
          description: IPMI LAN destination address format code for this row (C(AddrFormat)).
          type: int
        address:
          description: Destination address for this row (C(DestAddr)), exactly as reported. Empty string if unset.
          type: str
        user_id:
          description: IPMI user id this destination row is associated with (C(UserID)).
          type: int
        subject:
          description: Alert email subject template for this row (C(Subject)). Empty string if unset.
          type: str
        message:
          description: Alert email message template for this row (C(Message)). Empty string if unset.
          type: str
filtering:
  description: What fires an alert, and how a fired filter routes to a destination.
  type: dict
  returned: always
  contains:
    event_filters:
      description: >-
        The IPMI Platform Event Filter (PEF) table, from C(getallpefcfg.asp), exactly as reported.
        Field meanings beyond their bare names are IPMI-spec territory this collection has not
        sourced against this board's firmware; see this module's description for why an
        unrecognised field is dropped rather than guessed at, and why no further interpretation is
        attempted here.
      type: list
      elements: dict
      contains:
        filter_config:
          description: Raw C(FilterConfig) value for this PEF entry.
          type: int
        filter_action:
          description: Raw C(EvtFilterAction) value for this PEF entry.
          type: int
        alert_policy_num:
          description: Alert policy set number this PEF entry routes to on match (C(AlertPolicyNum)).
          type: int
        event_severity:
          description: Raw C(EventSeverity) value for this PEF entry.
          type: int
        generator_byte_1:
          description: Raw C(GeneratorByte1) value for this PEF entry.
          type: int
        generator_byte_2:
          description: Raw C(GeneratorByte2) value for this PEF entry.
          type: int
        sensor_type:
          description: Raw C(SensorType) value for this PEF entry.
          type: int
        sensor_name:
          description: Raw C(SensorName) value for this PEF entry (e.g. V(Any)). Empty string if unset.
          type: str
        event_trigger:
          description: Raw C(EventTrigger) value for this PEF entry.
          type: int
        event_data1_offset_mask:
          description: Raw C(EventData1OffsetMask) value for this PEF entry.
          type: int
        event_data1_and_mask:
          description: Raw C(EventData1ANDMask) value for this PEF entry.
          type: int
        event_data1_cmp1:
          description: Raw C(EventData1Cmp1) value for this PEF entry.
          type: int
        event_data1_cmp2:
          description: Raw C(EventData1Cmp2) value for this PEF entry.
          type: int
        event_data2_and_mask:
          description: Raw C(EventData2ANDMask) value for this PEF entry.
          type: int
        event_data2_cmp1:
          description: Raw C(EventData2Cmp1) value for this PEF entry.
          type: int
        event_data2_cmp2:
          description: Raw C(EventData2Cmp2) value for this PEF entry.
          type: int
        event_data3_and_mask:
          description: Raw C(EventData3ANDMask) value for this PEF entry.
          type: int
        event_data3_cmp1:
          description: Raw C(EventData3Cmp1) value for this PEF entry.
          type: int
        event_data3_cmp2:
          description: Raw C(EventData3Cmp2) value for this PEF entry.
          type: int
    policies:
      description: The alert policy table, from C(getallpolicycfg.asp), exactly as reported.
      type: list
      elements: dict
      contains:
        entry_number:
          description: Raw C(PolicyEntryNumber) value for this policy entry.
          type: int
        policy_number:
          description: Policy set number this entry belongs to (C(PolicyNumber)) -- matches C(filtering.event_filters[].alert_policy_num).
          type: int
        enabled:
          description: Whether this policy entry is enabled (C(EnablePolicy)).
          type: bool
        policy_set:
          description: Raw C(PolicySet) value for this policy entry.
          type: int
        channel_number:
          description: IPMI LAN channel this policy entry routes over (C(ChannelNumber)).
          type: int
        destination_selector:
          description: >-
            IPMI LAN destination selector this policy entry routes to (C(DestSelector)) -- matches
            C(destinations.lan[].destination_id).
          type: int
        event_specific:
          description: Raw C(EventSpecific) value for this policy entry.
          type: int
        alert_string:
          description: Raw C(AlertString) value for this policy entry.
          type: int
    triggers:
      description: >-
        When each policy set last fired, from C(gettriggercfg.asp). Entries have no id field of
        their own in this endpoint's response; position in this list is the only correlation this
        module has evidence for, and it is not asserted to line up with C(policies[].policy_set) or
        any other table's numbering -- see C(tests/unit/fixtures/asp/gettriggercfg.txt).
      type: list
      elements: dict
      contains:
        enabled:
          description: Whether this trigger slot is enabled (C(ENABLE)).
          type: bool
        timestamp:
          description: >-
            Unix timestamp this trigger slot last fired (C(TIMESTAMP)), or V(0) if it never has.
            This is the BMC's own clock; see this collection's connection doc fragment note on why
            the BMC's clock is not to be trusted as authoritative.
          type: int
email_format:
  description: Email formatting options, from C(getemailformat.asp).
  type: dict
  returned: always
  contains:
    available:
      description: >-
        The C(EMAIL_FORMAT) values this endpoint reported, in order. The fixture corpus shows
        V(AMI-Format) and V(FixedSubject-Format); this endpoint does not report which one (if
        either) is currently selected, so no "current" value is offered here -- only what was seen.
      type: list
      elements: str
adviser:
  description: >-
    C(getadvisercfg.asp), exactly as reported. See this module's description for why this is KVM/
    licensing/port state, not alert-delivery configuration, despite being grouped in this module.
    V(null) only if this one endpoint's read failed -- see RV(operation.endpoint_reads).
  type: dict
  returned: when available
  contains:
    kvm_license_status:
      description: Raw C(V_STR_KVM_LICENSE_STATUS) value.
      type: int
    kvm_status:
      description: Raw C(V_STR_KVM_STATUS) value.
      type: int
    secure_channel:
      description: Raw C(V_STR_SECURE_CHANNEL) value.
      type: int
    kvm_port:
      description: TCP port of the KVM service (C(V_STR_KVM_PORT)).
      type: int
    mouse_mode:
      description: Raw C(V_STR_MOUSE_MODE) value.
      type: int
    keyboard_layout:
      description: Raw C(V_STR_KEYBOARD_LAYOUT) value (e.g. V(AD)).
      type: str
    web_port:
      description: TCP port of the web management interface (C(V_STR_WEB_PORT)).
      type: int
    singleport_status:
      description: Raw C(V_STR_SINGLEPORT_STATUS) value.
      type: int
    oem_feature_status:
      description: Raw C(V_STR_OEM_FEATURE_STATUS) value.
      type: int
operation:
  description: >-
    The non-secret C(asmb8-ikvm-operation/v1) receipt for this read, in the same nested shape every
    other module in this collection returns it under.
  type: dict
  returned: always
  contains:
    schema:
      description: Always V(asmb8-ikvm-operation/v1).
      type: str
    action:
      description: Always V(asmb8_alerts.read).
      type: str
    endpoint:
      description: The C(host:port) this read was performed against.
      type: str
    changed:
      description: Always V(false).
      type: bool
    error_class:
      description: A stable machine-readable failure class. V(null) on success.
      type: str
    endpoint_reads:
      description: >-
        Per-endpoint outcome for each of the seven C(.asp) endpoints this module reads, keyed by
        the RV(destinations)/RV(filtering)/RV(email_format)/RV(adviser) field the endpoint feeds.
        Present so a V(null)/empty result can be told apart from "this endpoint genuinely reported
        nothing" versus "the read itself failed for a reason worth seeing" -- a single endpoint
        failing does not fail the whole module once the C(.asp) session itself is established.
      type: dict
      contains:
        outcome:
          description: V(read) if the endpoint was read successfully, V(failed) if the read raised.
          type: str
          choices: [read, failed]
        error_class:
          description: The failure class, on V(failed) only; V(null) otherwise.
          type: str
      sample:
        smtp: {outcome: read, error_class: null}
        lan: {outcome: read, error_class: null}
        adviser: {outcome: read, error_class: null}
        event_filters: {outcome: read, error_class: null}
        policies: {outcome: read, error_class: null}
        triggers: {outcome: read, error_class: null}
        email_format: {outcome: read, error_class: null}
"""

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import HAS_REQUESTS, REQUESTS_IMPORT_ERROR, AspClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import IkvmError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt, optional_bool_flag, optional_int, optional_str


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
    """Construct an :class:`AspClient` from the module's connection parameters."""
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


def _smtp_relay(record: dict, suffix: str) -> dict:
    return {
        "enabled": optional_bool_flag(record.get(f"SMTPENABLE{suffix}")),
        "port": optional_int(record.get(f"SMTPPORT{suffix}")),
        "server": optional_str(record.get(f"SMTPSERVER{suffix}")) or "",
        "auth_enabled": optional_bool_flag(record.get(f"SMTPAUTHENABLE{suffix}")),
        "username": optional_str(record.get(f"USERNAME{suffix}")) or "",
    }


def _smtp_channel(record: dict) -> dict:
    """Map one ``getsmtpcfg.asp`` record.

    Only the keys named here are ever read -- see this module's DOCUMENTATION on why an
    unrecognised key (a password field on a firmware revision this corpus has not seen, for
    example) is dropped by construction rather than passed through.
    """
    return {
        "channel": optional_int(record.get("CHANNEL_NUM")),
        "sender_address": optional_str(record.get("SENDERADDR")) or "",
        "machine_name": optional_str(record.get("MACHINENAME")) or "",
        "primary": _smtp_relay(record, "1"),
        "secondary": _smtp_relay(record, "2"),
    }


def _lan_destination(record: dict) -> dict:
    return {
        "channel": optional_int(record.get("CHANNEL_NUM")),
        "destination_id": optional_int(record.get("LANDestID")),
        "destination_type": optional_int(record.get("DestType")),
        "address_format": optional_int(record.get("AddrFormat")),
        "address": optional_str(record.get("DestAddr")) or "",
        "user_id": optional_int(record.get("UserID")),
        "subject": optional_str(record.get("Subject")) or "",
        "message": optional_str(record.get("Message")) or "",
    }


def _event_filter(record: dict) -> dict:
    return {
        "filter_config": optional_int(record.get("FilterConfig")),
        "filter_action": optional_int(record.get("EvtFilterAction")),
        "alert_policy_num": optional_int(record.get("AlertPolicyNum")),
        "event_severity": optional_int(record.get("EventSeverity")),
        "generator_byte_1": optional_int(record.get("GeneratorByte1")),
        "generator_byte_2": optional_int(record.get("GeneratorByte2")),
        "sensor_type": optional_int(record.get("SensorType")),
        "sensor_name": optional_str(record.get("SensorName")) or "",
        "event_trigger": optional_int(record.get("EventTrigger")),
        "event_data1_offset_mask": optional_int(record.get("EventData1OffsetMask")),
        "event_data1_and_mask": optional_int(record.get("EventData1ANDMask")),
        "event_data1_cmp1": optional_int(record.get("EventData1Cmp1")),
        "event_data1_cmp2": optional_int(record.get("EventData1Cmp2")),
        "event_data2_and_mask": optional_int(record.get("EventData2ANDMask")),
        "event_data2_cmp1": optional_int(record.get("EventData2Cmp1")),
        "event_data2_cmp2": optional_int(record.get("EventData2Cmp2")),
        "event_data3_and_mask": optional_int(record.get("EventData3ANDMask")),
        "event_data3_cmp1": optional_int(record.get("EventData3Cmp1")),
        "event_data3_cmp2": optional_int(record.get("EventData3Cmp2")),
    }


def _alert_policy(record: dict) -> dict:
    return {
        "entry_number": optional_int(record.get("PolicyEntryNumber")),
        "policy_number": optional_int(record.get("PolicyNumber")),
        "enabled": optional_bool_flag(record.get("EnablePolicy")),
        "policy_set": optional_int(record.get("PolicySet")),
        "channel_number": optional_int(record.get("ChannelNumber")),
        "destination_selector": optional_int(record.get("DestSelector")),
        "event_specific": optional_int(record.get("EventSpecific")),
        "alert_string": optional_int(record.get("AlertString")),
    }


def _trigger(record: dict) -> dict:
    return {
        "enabled": optional_bool_flag(record.get("ENABLE")),
        "timestamp": optional_int(record.get("TIMESTAMP")) or 0,
    }


def _email_formats(records: list[dict]) -> list[str]:
    formats = (optional_str(record.get("EMAIL_FORMAT")) for record in records)
    return [value for value in formats if value is not None]


def _adviser(record: dict) -> dict:
    return {
        "kvm_license_status": optional_int(record.get("V_STR_KVM_LICENSE_STATUS")),
        "kvm_status": optional_int(record.get("V_STR_KVM_STATUS")),
        "secure_channel": optional_int(record.get("V_STR_SECURE_CHANNEL")),
        "kvm_port": optional_int(record.get("V_STR_KVM_PORT")),
        "mouse_mode": optional_int(record.get("V_STR_MOUSE_MODE")),
        "keyboard_layout": optional_str(record.get("V_STR_KEYBOARD_LAYOUT")) or "",
        "web_port": optional_int(record.get("V_STR_WEB_PORT")),
        "singleport_status": optional_int(record.get("V_STR_SINGLEPORT_STATUS")),
        "oem_feature_status": optional_int(record.get("V_STR_OEM_FEATURE_STATUS")),
    }


def _read_endpoint(asp: AspClient, reads: dict, *, field: str, endpoint: str, transform):
    """Read one endpoint, degrading a failure to ``None`` rather than failing the whole module.

    Mirrors ``asmb8_info.py``'s ``_read_ipmi_field``: records the outcome in ``reads`` (this
    module's C(operation.endpoint_reads)) and only catches :class:`errors.IkvmError` -- the
    C(.asp) session itself was already established by the time any of these run, so a failure at
    this point is a per-endpoint refusal, not a connection problem this module should hide.
    """
    try:
        response = asp.get_webvar(endpoint)
        value = transform(response.records)
    except IkvmError as err:
        reads[field] = {"outcome": "failed", "error_class": err.error_class}
        return None
    reads[field] = {"outcome": "read", "error_class": None}
    return value


def gather_alerts(asp: AspClient) -> tuple[dict, dict]:
    """Read all seven endpoints and group them the way this module's RETURN documents.

    Each endpoint degrades independently to ``None`` on failure -- see :func:`_read_endpoint` --
    so one endpoint being absent on a given firmware revision does not fail the whole read.
    """
    reads: dict[str, dict] = {}

    smtp = _read_endpoint(asp, reads, field="smtp", endpoint="getsmtpcfg", transform=lambda records: [_smtp_channel(r) for r in records])
    lan = _read_endpoint(asp, reads, field="lan", endpoint="getlandeststable", transform=lambda records: [_lan_destination(r) for r in records])
    event_filters = _read_endpoint(asp, reads, field="event_filters", endpoint="getallpefcfg", transform=lambda records: [_event_filter(r) for r in records])
    policies = _read_endpoint(asp, reads, field="policies", endpoint="getallpolicycfg", transform=lambda records: [_alert_policy(r) for r in records])
    triggers = _read_endpoint(asp, reads, field="triggers", endpoint="gettriggercfg", transform=lambda records: [_trigger(r) for r in records])
    email_formats = _read_endpoint(asp, reads, field="email_format", endpoint="getemailformat", transform=_email_formats)
    adviser = _read_endpoint(asp, reads, field="adviser", endpoint="getadvisercfg", transform=lambda records: _adviser(records[0]) if records else None)

    alerts = {
        "destinations": {"smtp": smtp, "lan": lan},
        "filtering": {"event_filters": event_filters, "policies": policies, "triggers": triggers},
        "email_format": {"available": email_formats},
        "adviser": adviser,
    }
    return alerts, reads


def main() -> None:
    module = AnsibleModule(argument_spec=argument_spec(), supports_check_mode=True)

    if not HAS_REQUESTS:
        module.fail_json(msg=missing_required_lib("requests"), exception=REQUESTS_IMPORT_ERROR)
        return

    asp = build_asp_client(module.params)

    try:
        asp.login()
        alerts, reads = gather_alerts(asp)
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    receipt = OperationReceipt(
        action="asmb8_alerts.read",
        endpoint=asp.endpoint,
        changed=False,
        extra={"endpoint_reads": reads},
    )
    module.exit_json(
        changed=False,
        destinations=alerts["destinations"],
        filtering=alerts["filtering"],
        email_format=alerts["email_format"],
        adviser=alerts["adviser"],
        operation=receipt.to_dict(),
    )


if __name__ == "__main__":
    main()
