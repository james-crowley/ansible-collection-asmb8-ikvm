#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_users
short_description: Report ASMB8-iKVM local user accounts, role groups, and the current session's role
description:
  - >-
    Reads this BMC's local user-account table (C(getalluserinfo.asp)), the privilege role of the
    session this module's own login just created (C(getrole.asp)), and the LDAP/AD role-group
    bindings table (C(getallrolegroupcfg.asp)). All three are read-only C(.asp) RPCs; every field
    name and shape below is sourced from the real, redacted capture corpus under
    C(tests/unit/fixtures/asp/), not from a vendor specification (AMI has not published one for
    this surface -- see C(plugins/module_utils/asp.py)'s module docstring).
  - >-
    B(This module logs in.) Unlike M(james_crowley.asmb8_ikvm.asmb8_info)'s O(ignore:include_web_session),
    which treats creating a C(.asp) web session as an opt-in exception because IPMI facts are that
    module's real purpose, this module's B(entire) purpose lives behind that same login -- there is
    no way to read C(getalluserinfo.asp) without one, so logging in is this module's ordinary,
    unconditional behaviour, not a side path. That login still allocates real BMC-side session
    state (one of this board's limited concurrent web sessions -- see
    C(plugins/module_utils/errors.py)'s C(ErrorClass.BMC_BUSY) docstring, which cites 20 as the
    independently-measured concurrent-session cap for the C(web) service, corroborated by
    M(james_crowley.asmb8_ikvm.asmb8_redirection)'s own service catalog), which is exactly why
    O(ignore:check_mode) skips it entirely -- see that attribute's note below.
  - >-
    B(Every credential-shaped or personal-data field this module's source endpoints expose is
    either omitted entirely or reduced to a boolean.) C(getalluserinfo.asp)'s C(EmailID) is a
    user's real e-mail address when configured (empty in every sample of this corpus, but not
    guaranteed empty on any other board this module runs against) -- this module never returns it,
    only RV(users[].email_configured). Its C(SSHKeyInfo) field is documented by this module as
    B(sensitive by name, not by corpus evidence): every sample here reads the literal string
    V(Not Available), but a board with an uploaded key could plausibly return fingerprint or other
    key material through the same field, and this module refuses to find out the hard way -- only
    RV(users[].ssh_key_configured) is returned, never the field's raw text. See this module's
    C(RETURN) documentation for the field-by-field accounting.
  - >-
    B(Numeric privilege-limit fields are reported raw, not decoded, because no sourced mapping
    exists.) C(PrivLimit_Network)/C(PrivLimit_Serial) take values such as V(84), V(52), V(15) in
    this corpus -- these are B(not) raw IPMI channel-privilege levels (those only run 1-5) and no
    third-party client or specification this module's author found decodes them. Per this
    collection's policy of never asserting a protocol fact it cannot source, RV(users[].network_privilege_limit_raw)
    and RV(users[].serial_privilege_limit_raw) (and C(getrole.asp)'s RV(current_session.privilege_raw)/RV(current_session.extended_privilege_raw),
    and C(getallrolegroupcfg.asp)'s RV(role_groups[].privilege_raw)) are all returned exactly as the
    BMC reported them, with this caveat, rather than as an invented decode.
  - >-
    B(Empty user-table slots are reported as slots, never as accounts.) The corpus's
    C(getalluserinfo.asp) sample carries 9 records with C(UserName='') alongside 3 real accounts
    (C(anonymous), C(admin), C(root)) -- an unconfigured slot, not a fourth kind of user. This
    module filters them out of RV(users) entirely and reports their count separately under
    RV(slots).
  - >-
    B(No write capability exists here, deliberately.) Creating, modifying, or deleting a BMC user
    account, or changing a role-group binding, is exactly the kind of change that can lock an
    operator out of this BMC entirely -- disable the account you are currently authenticated as,
    revoke the only KVM-capable account, or point role-group resolution at an LDAP/AD server that
    is unreachable from where this collection runs. A future write capability, if ever added, would
    need at minimum: an explicit confirmation the caller is not modifying the account named by
    O(username) in the very same task, a check that at least one other enabled account with
    equivalent privilege remains after the change, and the same before/after C(operation) receipt
    shape every mutating module in this collection already returns. None of that exists yet. This
    module only reads.
version_added: 0.1.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_info
  - module: james_crowley.asmb8_ikvm.asmb8_network
  - module: james_crowley.asmb8_ikvm.asmb8_sessions
attributes:
  check_mode:
    description: >-
      Supported, but does not log in or read anything -- it returns immediately with every fact
      field V(null). This module's login is a real BMC-side side effect (see the module
      description), and a check-mode run implies no intended change here (this module has no
      write path to predict the effect of), so there is nothing to justify spending one of this
      BMC's limited concurrent session slots on it.
    support: full
  diff_mode:
    description: Not supported. There is no prior/after state to diff for a read-only module.
    support: none
requirements:
  - requests >= 2.25.0 (on the Ansible controller)
"""

EXAMPLES = r"""
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
"""

RETURN = r"""
changed:
  description: Always V(false) -- this module never mutates BMC state. See the module description.
  type: bool
  returned: always
users:
  description: >-
    One entry per B(configured) user-table slot only -- see RV(slots) for the slots this excludes.
  type: list
  elements: dict
  returned: always
  contains:
    username:
      description: The account name (C(UserName)). Never empty here -- an empty slot is excluded, not listed.
      type: str
    enabled:
      description: >-
        Whether the BMC reports this account's C(UserStatus) as active. Observed in this corpus as
        V(true) for C(admin)/C(root) and V(false) for the built-in, disabled C(anonymous) account --
        not confirmed against any other value C(UserStatus) might take.
      type: bool
    user_status_raw:
      description: The raw C(UserStatus) integer RV(users[].enabled) was derived from.
      type: int
    kvm_privilege:
      description: >-
        Whether C(KVMPriv) is set. Every account in this corpus reports V(true); this module has no
        corpus evidence for what V(false) looks like, but treats the field as an ordinary 0/1 flag.
      type: bool
    vmedia_privilege:
      description: Whether C(VMediaPriv) is set. Same caveat as RV(users[].kvm_privilege).
      type: bool
    network_privilege_limit_raw:
      description: >-
        C(PrivLimit_Network) exactly as reported (values seen in this corpus: V(84), V(52), V(15)).
        B(Not) a raw IPMI privilege level -- see the module description. Do not interpret this
        value without independent sourcing.
      type: int
    serial_privilege_limit_raw:
      description: C(PrivLimit_Serial) exactly as reported. Same caveat as RV(users[].network_privilege_limit_raw).
      type: int
    snmp:
      description: This account's SNMP-related fields, all reported raw for the same reason as the privilege limits.
      type: dict
      contains:
        status_raw:
          description: Raw C(SNMPStatus).
          type: int
        access_raw:
          description: Raw C(SNMPAccess).
          type: int
        auth_protocol_raw:
          description: Raw C(AUTHProtocol) (SNMPv3 authentication protocol selector; encoding not sourced).
          type: int
        priv_protocol_raw:
          description: Raw C(PrivProtocol) (SNMPv3 privacy protocol selector; encoding not sourced).
          type: int
    email_configured:
      description: >-
        Whether C(EmailID) is non-empty. B(The address itself is never returned) -- see the module
        description for why this is treated as personal data regardless of what this corpus's own
        samples happen to contain.
      type: bool
    email_format:
      description: >-
        Raw C(EmailFormat) (e.g. V(AMI-Format)), or V(null) if empty. Not sensitive on its own -- it
        names a message-formatting convention, not an address -- so it is passed through.
      type: str
    serial_over_lan_status_raw:
      description: Raw C(SOL_Status).
      type: int
    ssh_key_configured:
      description: >-
        Whether this account has SSH key material on file, derived from C(SSHKeyStatus) and
        C(SSHKeyInfo). B(The key/fingerprint text itself is never returned) -- see the module
        description for why C(SSHKeyInfo) is treated as sensitive by name even though every sample
        in this corpus reads V(Not Available).
      type: bool
    ssh_key_status_raw:
      description: >-
        Raw C(SSHKeyStatus) integer. Kept because it is a bare status code, not key material, unlike
        C(SSHKeyInfo) itself.
      type: int
    fixed_user_count:
      description: >-
        Raw C(FixedUserCount) as reported alongside this account (V(2) for every account in this
        corpus). What this counts is not sourced beyond the field name.
      type: int
slots:
  description: A count-only summary of the user table's total capacity versus how much of it is configured.
  type: dict
  returned: always
  contains:
    total:
      description: Total rows in C(getalluserinfo.asp)'s table, configured and empty combined.
      type: int
    configured:
      description: How many of those rows have a non-empty C(UserName) -- the length of RV(users).
      type: int
    empty:
      description: How many of those rows are unconfigured slots (C(UserName) empty), excluded from RV(users).
      type: int
current_session:
  description: >-
    The privilege role C(getrole.asp) reports for the session this module's own O(username)/O(password)
    login just created -- B(not) a directory of every active session (see
    M(james_crowley.asmb8_ikvm.asmb8_sessions) for that, and its own documented gap around
    C(getsessioninfo.asp)). V(null) only if C(getrole.asp) returned no record at all.
  type: dict
  returned: always
  contains:
    username:
      description: Raw C(CURUSERNAME) -- normally identical to O(username).
      type: str
    privilege_raw:
      description: >-
        Raw C(CURPRIV) (V(4) in this corpus, for an C(admin) session). Not a raw IPMI privilege
        level and not decoded -- see the module description.
      type: int
    extended_privilege_raw:
      description: Raw C(EXTENDED_PRIV) (V(259) in this corpus). Same caveat as RV(current_session.privilege_raw).
      type: int
role_groups:
  description: >-
    LDAP/AD role-group bindings from C(getallrolegroupcfg.asp), one entry per configured group-ID
    slot. Every sample in this corpus is unconfigured (empty C(ROLEGROUP_NAME)/C(ROLEGROUP_DOMAIN)),
    so RV(role_groups[].configured) is V(false) throughout the corpus but this module does not
    assume that holds for every board.
  type: list
  elements: dict
  returned: always
  contains:
    id:
      description: Raw C(ROLEGROUP_ID) (this table's slot number, 1-5 in this corpus).
      type: int
    configured:
      description: Whether C(ROLEGROUP_NAME) is non-empty for this slot.
      type: bool
    name:
      description: Raw C(ROLEGROUP_NAME), or V(null) if empty. Not treated as sensitive -- it is a label, not a credential.
      type: str
    domain:
      description: >-
        Raw C(ROLEGROUP_DOMAIN) (an LDAP/AD domain name, when configured), or V(null) if empty. A
        domain name identifies a directory server, not a credential, so it is passed through.
      type: str
    privilege_raw:
      description: Raw C(ROLEGROUP_PRIVILEGE). Not decoded -- see the module description.
      type: int
    kvm_privilege_raw:
      description: Raw C(ROLEGROUP_KVM).
      type: int
    vmedia_privilege_raw:
      description: Raw C(ROLEGROUP_VMEDIA).
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
      description: Always V(asmb8_users.report).
      type: str
    endpoint:
      description: The C(host:port) this read was performed against.
      type: str
    changed:
      description: Always V(false).
      type: bool
    observed:
      description: Mirrors RV(users), RV(slots), RV(current_session), and RV(role_groups) together.
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

#: Sentinel string this board's firmware uses for "no SSH key on file", per this corpus's
#: getalluserinfo.txt sample. Treated purely as a presence check -- see the module description for
#: why the field's own text is never returned even when it differs from this value.
_SSH_KEY_INFO_NOT_AVAILABLE = "Not Available"


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


def _is_configured_slot(record: dict[str, Any]) -> bool:
    return bool(record.get("UserName"))


def decode_user_slot(record: dict[str, Any]) -> dict[str, Any]:
    """Decode one configured C(getalluserinfo.asp) row. Never called for an empty slot -- see :func:`build_users_report`.

    Every credential-shaped or personal-data field (``EmailID``, ``SSHKeyInfo``) is reduced to a
    boolean here and never carried through in any other form -- see this module's DOCUMENTATION.
    """
    email_id = record.get("EmailID") or ""
    ssh_key_info = record.get("SSHKeyInfo") or ""
    ssh_key_status = record.get("SSHKeyStatus")
    email_format = record.get("EmailFormat") or None

    return {
        "username": record.get("UserName"),
        "enabled": bool(record.get("UserStatus")),
        "user_status_raw": record.get("UserStatus"),
        "kvm_privilege": bool(record.get("KVMPriv")),
        "vmedia_privilege": bool(record.get("VMediaPriv")),
        "network_privilege_limit_raw": record.get("PrivLimit_Network"),
        "serial_privilege_limit_raw": record.get("PrivLimit_Serial"),
        "snmp": {
            "status_raw": record.get("SNMPStatus"),
            "access_raw": record.get("SNMPAccess"),
            "auth_protocol_raw": record.get("AUTHProtocol"),
            "priv_protocol_raw": record.get("PrivProtocol"),
        },
        "email_configured": bool(email_id),
        "email_format": email_format,
        "serial_over_lan_status_raw": record.get("SOL_Status"),
        "ssh_key_configured": bool(ssh_key_status) or (ssh_key_info != "" and ssh_key_info != _SSH_KEY_INFO_NOT_AVAILABLE),
        "ssh_key_status_raw": ssh_key_status,
        "fixed_user_count": record.get("FixedUserCount"),
    }


def build_users_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Split ``getalluserinfo.asp``'s rows into configured accounts and a slot-count summary.

    See the module description: an empty ``UserName`` is an unconfigured slot, never presented as
    a fourth kind of account.
    """
    configured_records = [r for r in records if _is_configured_slot(r)]
    return {
        "users": [decode_user_slot(r) for r in configured_records],
        "slots": {
            "total": len(records),
            "configured": len(configured_records),
            "empty": len(records) - len(configured_records),
        },
    }


def build_current_session_report(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None
    record = records[0]
    return {
        "username": record.get("CURUSERNAME"),
        "privilege_raw": record.get("CURPRIV"),
        "extended_privilege_raw": record.get("EXTENDED_PRIV"),
    }


def decode_role_group(record: dict[str, Any]) -> dict[str, Any]:
    name = record.get("ROLEGROUP_NAME") or None
    domain = record.get("ROLEGROUP_DOMAIN") or None
    return {
        "id": record.get("ROLEGROUP_ID"),
        "configured": bool(name),
        "name": name,
        "domain": domain,
        "privilege_raw": record.get("ROLEGROUP_PRIVILEGE"),
        "kvm_privilege_raw": record.get("ROLEGROUP_KVM"),
        "vmedia_privilege_raw": record.get("ROLEGROUP_VMEDIA"),
    }


def build_role_groups_report(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [decode_role_group(r) for r in records]


def gather_report(asp_client: AspClient) -> dict[str, Any]:
    """Log in and read all three source endpoints. The only place this module's login happens."""
    asp_client.login()
    users_response = asp_client.get_webvar("getalluserinfo")
    role_response = asp_client.get_webvar("getrole")
    role_groups_response = asp_client.get_webvar("getallrolegroupcfg")

    users_report = build_users_report(users_response.records)
    return {
        "users": users_report["users"],
        "slots": users_report["slots"],
        "current_session": build_current_session_report(role_response.records),
        "role_groups": build_role_groups_report(role_groups_response.records),
    }


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
        receipt = OperationReceipt(action="asmb8_users.report", endpoint=endpoint, changed=False, observed=None)
        module.exit_json(
            changed=False,
            users=None,
            slots=None,
            current_session=None,
            role_groups=None,
            operation=receipt.to_dict(),
        )
        return

    try:
        asp_client = build_asp_client(params)
        report = gather_report(asp_client)
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    receipt = OperationReceipt(action="asmb8_users.report", endpoint=asp_client.endpoint, changed=False, observed=report)
    module.exit_json(changed=False, **report, operation=receipt.to_dict())


if __name__ == "__main__":
    main()
