#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_ntp
short_description: Manage ASMB8-iKVM NTP server configuration
description:
  - >-
    Reads and, when needed, writes this BMC's NTP configuration (C(getntpcfg.asp) /
    C(setntpcfg.asp)) over the AMI C(.asp) RPC surface. This is this collection's first module
    that actually mutates BMC configuration -- every C(.asp)-backed module before it
    (M(james_crowley.asmb8_ikvm.asmb8_network), M(james_crowley.asmb8_ikvm.asmb8_sensors), and the
    rest) is read-only. Everything below that describes a write is sourced from one real
    save-action capture taken 2026-08-10 against the target board, firmware 1.14 (aux 1.14.2);
    see C(docs/protocol-notes.md)'s write-convention section for the full capture this module was
    built from.
  - >-
    B(This module logs in on every run), including in check mode -- see O(ignore:check_mode)
    below. Unlike M(james_crowley.asmb8_ikvm.asmb8_network) or
    M(james_crowley.asmb8_ikvm.asmb8_sessions), which skip login entirely in check mode because
    they have no write path to predict, this module's whole point is idempotence: it must read
    C(getntpcfg.asp) before it can know whether anything needs to change, in check mode exactly as
    much as in a real run.
  - >-
    B(O(server2)'s comparison is byte-exact, deliberately.) The one real capture read back
    C(SERVER_NAME2) carrying a leading space (C(' 192.0.2.10')) and the matching write echoed
    that leading space back unchanged. This module never strips or otherwise normalizes O(server1)
    or O(server2): comparing the value you supply against the BMC's own raw C(SERVER_NAME1)/
    C(SERVER_NAME2) text, character for character, is what makes every subsequent run genuinely a
    no-op rather than reporting C(changed=true) forever because a friendlier trimmed comparison
    quietly disagreed with what the BMC actually stores. If your intended server address has no
    leading space, and the BMC nonetheless routinely reports one back (unconfirmed either way by
    this one capture), expect this module to report a change on every run until you supply the
    value with the same leading space the BMC uses.
  - >-
    B(O(enabled)'s relationship to what this module reads is an inference, not a sourced fact.)
    C(getntpcfg.asp) reports NTP status as C(NTP_STATUS) (observed as V(1) in the one real read);
    the write this module issues sets C(ISNTPENABLE) (observed as V(0) in the one real write, from
    the very same session). Nothing in the capture proves C(NTP_STATUS) and C(ISNTPENABLE) share
    an encoding, or even that they describe the same underlying flag -- one is a read-only status
    field and the other is a distinct write-only field name, and the capture never shows the same
    value on both sides of a single change to compare. This module maps C(NTP_STATUS) to
    RV(previous_state.enabled)/RV(observed.enabled) as "nonzero means enabled" purely as a
    best-effort interpretation, and writes O(enabled)=V(true) as C(ISNTPENABLE=1) on the same
    unconfirmed assumption (the only observed C(ISNTPENABLE) value is V(0), for "disable"; V(1)
    for "enable" is this module's own inference from the field's name, never independently
    confirmed). RV(previous_state.ntp_status_raw)/RV(observed.ntp_status_raw) carry the
    untranslated C(NTP_STATUS) integer specifically so a caller who does not trust this inference
    has the raw value to fall back on.
  - >-
    B(This module owns C(ISNTPENABLE) through C(setntpcfg.asp) only -- it never calls
    C(setdatetime.asp).) The same save-action capture that sourced C(setntpcfg.asp)'s write shape
    also shows C(setdatetime.asp) carrying its own C(ISNTPENABLE=0) in the same save action
    (alongside C(SECONDS)/C(UTCMINUTES)/C(TIMEZONE)) -- the vendor UI's single "save NTP settings"
    click evidently POSTs to both endpoints together. This module deliberately does not follow
    that pattern: reaching C(setdatetime.asp) at all would mean resubmitting C(SECONDS) with
    whatever value this module last read, nudging the BMC's clock forward by the (small but real)
    gap between that read and the write, on every run that changes anything -- a side effect this
    module has no sourced reason to accept for a capability (O(server1)/O(server2)/O(enabled)) that
    has nothing to do with the clock. The consequence, honestly stated: whether C(setdatetime.asp)'s
    own copy of C(ISNTPENABLE) tracks C(setntpcfg.asp)'s automatically on real firmware, or drifts
    out of sync with it, is B(unverified) -- this module manages exactly one copy of that flag and
    makes no claim about the other.
  - >-
    B(No timezone or UTC-offset options exist here, deliberately.) C(setdatetime.asp) is the only
    sourced write path for C(TIMEZONE)/C(UTCMINUTES), and the one real capture of it is a
    full-record write alongside C(SECONDS) with no evidence of what a partial submission (some
    fields omitted or left as sentinel values) does. Between that and the clock-nudging concern
    above, this module does not implement a write path this collection has not actually observed
    in isolation. A future module or option that manages the clock should source its own capture
    of C(setdatetime.asp) rather than extrapolating from this module's C(setntpcfg.asp)-only
    convention: setter field names are per-endpoint, not a collection-wide rule --
    C(setdatetime.asp) reuses C(getdatetime.asp)'s own field names verbatim, while C(setntpcfg.asp)
    does not reuse C(getntpcfg.asp)'s at all (C(SERVER_NAME1) becomes both C(NEW_NTPSERVER_NAME1)
    and C(OLD_NTPSERVER_NAME1); C(NTP_STATUS) becomes C(ISNTPENABLE)) -- one endpoint's convention
    tells you nothing reliable about the next one's.
  - >-
    B(C(OLD_NTPSERVER_NAME1) is always sent; C(OLD_NTPSERVER_NAME2) is never invented.) The one
    real write capture carries both C(NEW_NTPSERVER_NAME1) and C(OLD_NTPSERVER_NAME1) (identical
    values, C(pool.ntp.org), since server 1 was not the field actually changing in that save
    action) but only C(NEW_NTPSERVER_NAME2) -- no C(OLD_NTPSERVER_NAME2) field appears anywhere in
    that capture, even though server 2 (with its leading space) was also present in the same
    write. This module follows that asymmetry exactly: it always sends C(OLD_NTPSERVER_NAME1)
    (this run's freshly-read C(SERVER_NAME1), before any change), and never sends an
    C(OLD_NTPSERVER_NAME2) field under any circumstance. Reading C(OLD_NTPSERVER_NAME1) as "the
    value before this write" is this module's own inference from the field's name -- the capture
    only shows the unchanged case (C(NEW) equal to C(OLD)), never a case where server 1 actually
    changed, so that inference is unconfirmed for a real change, if a genuinely accurate one for
    "no change".
  - >-
    B(A write always resubmits every field C(setntpcfg.asp) takes, not just the one that
    changed.) This mirrors the one real capture exactly: its save action, which was actually only
    toggling NTP on/off, still resubmitted C(NEW_NTPSERVER_NAME1)/C(OLD_NTPSERVER_NAME1) and
    C(NEW_NTPSERVER_NAME2) unchanged alongside the real C(ISNTPENABLE) change. Whenever this
    module needs to write anything, it does the same: any of O(server1)/O(server2)/O(enabled) left
    unset keeps this run's freshly-read current value, sent back verbatim, rather than being
    omitted from the request body -- there is no sourced evidence a partial C(setntpcfg.asp)
    submission does anything sensible, so this module never attempts one.
version_added: 0.5.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
options:
  server1:
    description:
      - >-
        Desired primary NTP server, written to C(setntpcfg.asp) as C(NEW_NTPSERVER_NAME1)
        (alongside C(OLD_NTPSERVER_NAME1), this run's current value) when it differs from the
        BMC's own C(SERVER_NAME1). Unset (the default) leaves the current value alone -- it is
        still resubmitted verbatim if a write happens for O(server2) or O(enabled), per this
        module's description.
      - Compared against the BMC's raw C(SERVER_NAME1) exactly as given; no whitespace normalization.
    type: str
  server2:
    description:
      - >-
        Desired secondary NTP server, written to C(setntpcfg.asp) as C(NEW_NTPSERVER_NAME2) (with
        no accompanying C(OLD_NTPSERVER_NAME2) field -- see this module's description) when it
        differs from the BMC's own C(SERVER_NAME2). Unset (the default) leaves the current value
        alone.
      - >-
        Compared byte-for-byte against the BMC's raw C(SERVER_NAME2), including any leading or
        trailing whitespace -- see this module's description for why a leading space observed on
        the target hardware makes this the only comparison that can be genuinely idempotent.
    type: str
  enabled:
    description:
      - >-
        Desired NTP enable state, written to C(setntpcfg.asp) as C(ISNTPENABLE) (V(1) for
        V(true), V(0) for V(false)) when it differs from this module's own interpretation of the
        BMC's C(NTP_STATUS). Unset (the default) leaves the current value alone.
      - >-
        See this module's description for why that comparison is a best-effort inference, not a
        sourced fact: C(NTP_STATUS) (what this module reads) and C(ISNTPENABLE) (what this module
        writes) are not confirmed to share an encoding.
    type: bool
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_network
attributes:
  check_mode:
    description: >-
      Logs in and reads C(getntpcfg.asp) exactly as a real run does -- unlike this collection's
      read-only C(.asp) modules, there is a real write path here whose effect check mode must be
      able to predict, so login is not skipped. C(setntpcfg.asp) itself is never called; the
      top-level C(changed) and RV(desired_state) report exactly what a real run would do, and
      RV(observed) is the same value as RV(previous_state) rather than a post-write read.
    support: full
  diff_mode:
    description: Not supported. Use RV(previous_state)/RV(desired_state) instead of C(--diff).
    support: none
requirements:
  - requests >= 2.25.0 (on the Ansible controller)
"""

EXAMPLES = r"""
- name: Ensure both NTP servers and enable NTP
  james_crowley.asmb8_ikvm.asmb8_ntp:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    server1: pool.ntp.org
    server2: 192.0.2.10
    enabled: true
  delegate_to: localhost
  no_log: true
  register: ntp

- name: Preview disabling NTP without changing anything
  james_crowley.asmb8_ikvm.asmb8_ntp:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    enabled: false
  delegate_to: localhost
  no_log: true
  check_mode: true
  register: preview

- name: Restore whatever NTP configuration was in place before an earlier change
  james_crowley.asmb8_ikvm.asmb8_ntp:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    server1: "{{ ntp.previous_state.server1 }}"
    server2: "{{ ntp.previous_state.server2 }}"
    enabled: "{{ ntp.previous_state.enabled }}"
  delegate_to: localhost
  no_log: true
  when: ntp.previous_state.enabled is not none
"""

RETURN = r"""
previous_state:
  description: NTP configuration observed before any action was taken.
  type: dict
  returned: always
  contains:
    server1:
      description: Raw C(SERVER_NAME1), exactly as read.
      type: str
    server2:
      description: Raw C(SERVER_NAME2), exactly as read (may carry a leading space; see the module description).
      type: str
    enabled:
      description: This module's inferred boolean reading of C(NTP_STATUS). See the module description's caveat.
      type: bool
    ntp_status_raw:
      description: The untranslated C(NTP_STATUS) integer, for a caller that does not trust RV(previous_state.enabled)'s inference.
      type: int
desired_state:
  description: >-
    The NTP configuration this action targets: each of O(server1)/O(server2)/O(enabled) that was
    given, merged with RV(previous_state)'s value for anything left unset.
  type: dict
  returned: always
  contains:
    server1:
      description: Desired C(SERVER_NAME1).
      type: str
    server2:
      description: Desired C(SERVER_NAME2).
      type: str
    enabled:
      description: Desired NTP enable state, per this module's inference (see the module description).
      type: bool
observed:
  description: >-
    NTP configuration freshly re-read from C(getntpcfg.asp) after a real write, in the same shape
    as RV(previous_state) -- or, when nothing was written (top-level C(changed)=V(false), or check
    mode), the same value as RV(previous_state).
  type: dict
  returned: always
  contains:
    server1:
      description: Raw C(SERVER_NAME1), exactly as read.
      type: str
    server2:
      description: Raw C(SERVER_NAME2), exactly as read (may carry a leading space; see the module description).
      type: str
    enabled:
      description: This module's inferred boolean reading of C(NTP_STATUS). See the module description's caveat.
      type: bool
    ntp_status_raw:
      description: The untranslated C(NTP_STATUS) integer, for a caller that does not trust RV(observed.enabled)'s inference.
      type: int
operation:
  description: >-
    The C(asmb8-ikvm-operation/v1) receipt for this action, in the same nested shape every
    mutating module in this collection returns it under.
  type: dict
  returned: always
  contains:
    schema:
      description: Always V(asmb8-ikvm-operation/v1).
      type: str
    action:
      description: Always V(asmb8_ntp.apply).
      type: str
    endpoint:
      description: The C(host:port) this operation was performed against.
      type: str
    changed:
      description: Mirrors the top-level C(changed) value Ansible always returns.
      type: bool
    previous:
      description: Same value as RV(previous_state).
      type: dict
    desired:
      description: Same value as RV(desired_state).
      type: dict
    observed:
      description: Same value as RV(observed).
      type: dict
    error_class:
      description: A stable machine-readable failure class. V(null) on success.
      type: str
"""

from typing import Any

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import HAS_REQUESTS, REQUESTS_IMPORT_ERROR, AspClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import IkvmError, ProtocolError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt


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
    spec = _connection_argument_spec()
    spec["server1"] = {"type": "str"}
    spec["server2"] = {"type": "str"}
    spec["enabled"] = {"type": "bool"}
    return spec


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


def decode_ntp_state(record: dict[str, Any]) -> dict[str, Any]:
    """Decode one ``getntpcfg.asp`` record into this module's typed NTP state.

    ``server1``/``server2`` are returned exactly as read, with no stripping -- see this module's
    DOCUMENTATION for why ``server2``'s observed leading space makes that non-negotiable for
    idempotence.

    ``enabled`` is an *inference* from ``NTP_STATUS``, not a sourced fact -- see DOCUMENTATION's
    dedicated caveat. ``ntp_status_raw`` is kept alongside it so a caller can bypass the inference
    entirely.
    """
    status_raw = record.get("NTP_STATUS")
    return {
        "server1": record.get("SERVER_NAME1"),
        "server2": record.get("SERVER_NAME2"),
        "enabled": bool(status_raw) if status_raw is not None else None,
        "ntp_status_raw": status_raw,
    }


def plan(current: dict[str, Any], params: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Merge ``params`` over ``current`` and report whether the result differs.

    Only an option actually given (not ``None``) is a candidate for change; anything left unset
    carries ``current``'s own value forward unchanged -- see DOCUMENTATION's note on why a write,
    when one happens, always resubmits every field rather than only the one that changed.
    """
    desired = {
        "server1": params["server1"] if params["server1"] is not None else current["server1"],
        "server2": params["server2"] if params["server2"] is not None else current["server2"],
        "enabled": params["enabled"] if params["enabled"] is not None else current["enabled"],
    }
    changed = desired["server1"] != current["server1"] or desired["server2"] != current["server2"] or desired["enabled"] != current["enabled"]
    return desired, changed


def build_setntpcfg_data(current: dict[str, Any], desired: dict[str, Any]) -> dict[str, str]:
    """Build the ``setntpcfg.asp`` POST body, per the field-name convention DOCUMENTATION sources.

    ``desired["enabled"]`` must not be ``None`` here -- :func:`main` guards that case before ever
    calling this, precisely so this function is never the place that silently guesses at
    ``ISNTPENABLE`` from an unknown current state.
    """
    return {
        "NEW_NTPSERVER_NAME1": desired["server1"] or "",
        "OLD_NTPSERVER_NAME1": current["server1"] or "",
        "NEW_NTPSERVER_NAME2": desired["server2"] or "",
        "ISNTPENABLE": "1" if desired["enabled"] else "0",
    }


def main() -> None:
    module = AnsibleModule(argument_spec=argument_spec(), supports_check_mode=True)
    params = module.params

    if not HAS_REQUESTS:
        module.fail_json(msg=missing_required_lib("requests"), exception=REQUESTS_IMPORT_ERROR)
        return

    try:
        client = build_asp_client(params)
        # Login happens unconditionally, including in check mode: unlike this collection's
        # read-only .asp modules, this module has a real write path whose effect check mode must
        # predict, and predicting it requires the same read a live run needs -- see
        # ATTRIBUTES.check_mode's documentation.
        client.login()
        current_records = client.get_webvar("getntpcfg").records
        current = decode_ntp_state(current_records[0] if current_records else {})
        desired, changed = plan(current, params)

        if not changed or module.check_mode:
            receipt = OperationReceipt(action="asmb8_ntp.apply", endpoint=client.endpoint, changed=changed, previous=current, desired=desired, observed=current)
            module.exit_json(changed=changed, previous_state=current, desired_state=desired, observed=current, operation=receipt.to_dict())
            return

        if desired["enabled"] is None:
            # current["enabled"] was None (NTP_STATUS absent/unparseable) and `enabled` was never
            # given, so there is nothing safe to send for ISNTPENABLE: guessing here is exactly
            # the "clobber a coupled flag as a side effect of an unrelated change" hazard
            # DOCUMENTATION warns about, not a value this module has any basis to pick.
            raise ProtocolError(
                "getntpcfg.asp did not report NTP_STATUS, so this module cannot determine the current NTP "
                "enable state and will not guess an ISNTPENABLE value for an unrelated server change. "
                "Set `enabled` explicitly to proceed.",
                endpoint=client.endpoint,
                operation="asmb8_ntp.apply",
            )

        data = build_setntpcfg_data(current, desired)
        client.set_webvar("setntpcfg", data)

        observed_records = client.get_webvar("getntpcfg").records
        observed = decode_ntp_state(observed_records[0] if observed_records else {})
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    receipt = OperationReceipt(action="asmb8_ntp.apply", endpoint=client.endpoint, changed=True, previous=current, desired=desired, observed=observed)
    module.exit_json(changed=True, previous_state=current, desired_state=desired, observed=observed, operation=receipt.to_dict())


if __name__ == "__main__":
    main()
