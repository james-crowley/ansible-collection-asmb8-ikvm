#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_boot
short_description: Select a one-time IPMI boot device on an ASMB8-iKVM endpoint
description:
  - >-
    Reads and sets the IPMI boot-device override via C(pyghmi)'s
    C(Command.get_bootdev())/C(set_bootdev()) -- the same already-working IPMI
    path M(james_crowley.asmb8_ikvm.asmb8_power) wraps, and not the C(.asp)/JNLP surface this collection is
    still reverse engineering.
  - >-
    Idempotent: the current override is read first, and C(set_bootdev()) is only
    called when O(device) or O(uefi) actually differ from what the BMC currently
    reports.
  - >-
    B(This module refuses persistent boot-order changes outright.) O(persistent)
    exists only so that refusal is an explicit, documented choice a caller can see
    in C(ansible-doc) rather than a capability that is merely absent -- setting it
    to V(true) always fails with C(error_class=unsupported_capability), before any
    connection is even attempted, and B(does not) get silently downgraded to a
    one-time override. See O(persistent) below for why.
  - >-
    This module talks to the BMC over IPMI (UDP O(ipmi_port), default 623) using
    only O(host), O(username) and O(password) from the shared connection
    documentation fragment. O(port), O(use_tls), O(allow_insecure_transport),
    O(validate_certs), O(ca_path), O(tls_fingerprint), O(timeout) and
    O(connect_timeout) are accepted, for C(module_defaults) compatibility with
    every other module in this collection, but are entirely B(ignored) here. The
    C(requests) entry under C(requirements) below is likewise inherited from that
    shared fragment and does B(not) apply to this module -- only C(pyghmi) is
    actually required for M(james_crowley.asmb8_ikvm.asmb8_boot) to function.
version_added: 0.1.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
options:
  ipmi_port:
    description:
      - >-
        UDP port of the BMC's IPMI-over-LAN (RMCP+) listener. B(Not) the same
        thing as O(port), which is this collection's shared web-management HTTPS
        port (443) and is unused by this module.
    type: int
    default: 623
  device:
    description:
      - >-
        Boot device to select for exactly one upcoming reset, taken from pyghmi's
        own C(set_bootdev())/C(get_bootdev()) vocabulary, which is exactly
        C(community.general.ipmi_boot)'s documented C(bootdev) choices (see
        C(module_utils/models.py)'s C(BOOT_DEVICES) -- sourced from that module's
        own documentation, not invented here).
      - V(network) -- request network (PXE) boot.
      - V(floppy) -- boot from floppy.
      - V(hd) -- boot from hard drive.
      - V(safe) -- boot from hard drive, requesting BIOS "safe mode".
      - V(optical) -- boot from CD/DVD/BD drive.
      - V(setup) -- boot into the firmware setup utility.
      - V(default) -- remove any standing IPMI-directed boot device request.
    type: str
    required: true
    choices: [network, floppy, hd, safe, optical, setup, default]
  uefi:
    description:
      - >-
        Whether to request UEFI boot explicitly for this one override. Many
        systems boot UEFI regardless of this flag if that is how they are
        otherwise configured; pyghmi (and the IPMI spec it implements) offers no
        "don't care" value, only "request UEFI" or not.
    type: bool
    default: false
  persistent:
    description:
      - >-
        B(Always rejected.) This option exists to document, rather than merely
        omit, that persistent (beyond-next-boot) IPMI boot-order changes are out
        of scope for this module: the lab contract for the hardware this
        collection targets permits only one-time boot overrides, never a standing
        change to boot order. Setting this to V(true) fails immediately with
        C(error_class=unsupported_capability), before any IPMI session is opened.
      - >-
        Do not "fix" a V(true) value here by silently treating it as V(false) --
        see the comment beside C(_ASSERT_ONE_TIME_ONLY) in this module's source,
        which exists specifically so a future change cannot re-enable this
        without deleting an assertion that says why not to.
    type: bool
    default: false
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_power
  - module: james_crowley.asmb8_ikvm.asmb8_info
attributes:
  check_mode:
    description: >-
      The current boot-device override is read and compared against O(device)/
      O(uefi) exactly as in normal mode, but C(set_bootdev()) is never sent.
    support: full
  diff_mode:
    description: Returns the previous and desired boot-device override in the operation receipt.
    support: full
requirements:
  - pyghmi (on the Ansible controller)
"""

EXAMPLES = r"""
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

# This fails with error_class=unsupported_capability before touching the BMC --
# persistent boot-order changes are out of scope for this hardware's lab
# contract, and this module refuses to attempt one even if asked directly.
- name: Persistent boot order changes are rejected outright
  james_crowley.asmb8_ikvm.asmb8_boot:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    device: hd
    persistent: true
  delegate_to: localhost
  no_log: true
  ignore_errors: true
"""

RETURN = r"""
device:
  description: The O(device) value that was (or, in check mode, would be) armed.
  type: str
  returned: always
uefi:
  description: The O(uefi) value that was (or, in check mode, would be) armed.
  type: bool
  returned: always
previous:
  description: >-
    The boot-device override observed before any action was taken, exactly as
    pyghmi's C(get_bootdev()) returned it. C(uefimode) is absent on the branch
    where the BMC reports no standing override at all -- see
    module_utils/ipmi.py.
  type: dict
  returned: always
operation:
  description: >-
    The C(asmb8-ikvm-operation/v1) receipt for this action, in the same nested
    shape every mutating module in this collection returns it under.
  type: dict
  returned: always
  contains:
    schema:
      description: Always V(asmb8-ikvm-operation/v1).
      type: str
    action:
      description: Always V(asmb8_boot).
      type: str
    endpoint:
      description: The C(host:ipmi_port) this operation was performed against.
      type: str
    changed:
      description: Mirrors the top-level C(changed) value Ansible always returns.
      type: bool
    previous:
      description: Same value as RV(previous).
      type: dict
    desired:
      description: "The C(bootdev)/C(uefiboot)/C(persist) arguments this module sent (or would send)."
      type: dict
    observed:
      description: >-
        What pyghmi's C(set_bootdev()) returned, normalized to the same shape as
        RV(previous). Equal to RV(previous) when nothing was sent because the
        override already matched.
      type: dict
    error_class:
      description: A stable machine-readable failure class. V(null) on success.
      type: str
"""

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import IkvmError, UnsupportedCapabilityError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.ipmi import DEFAULT_IPMI_PORT, HAS_PYGHMI, PYGHMI_IMPORT_ERROR, IpmiClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import BOOT_DEVICES, OperationReceipt

#: The message a `persistent=true` refusal carries. Kept as a named constant,
#: not inlined, for the same reason asp.py's BMC_CIPHERS comment gives: this is
#: exactly the kind of thing a future "simplification" deletes as dead weight
#: without the context explaining that it is load-bearing. It is not.
#:
#: DO NOT remove the `persistent=True` rejection below, and do not reinterpret
#: it as "treat True the same as False". The lab contract this hardware is
#: operated under permits only one-time IPMI boot overrides; a persistent
#: boot-order change is out of scope for this module by design, not by
#: oversight, and re-enabling it requires a decision this module's author does
#: not have standing to make unilaterally.
_PERSISTENT_REJECTED_MESSAGE = (
    "asmb8_boot does not support persistent boot-order changes. The lab contract for this hardware permits only "
    "one-time IPMI boot overrides; set persistent=false (the default) to arm a single upcoming boot instead."
)


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
        "ipmi_port": {"type": "int", "default": DEFAULT_IPMI_PORT},
    }


def argument_spec() -> dict[str, dict]:
    spec = _connection_argument_spec()
    spec["device"] = {"type": "str", "required": True, "choices": list(BOOT_DEVICES)}
    spec["uefi"] = {"type": "bool", "default": False}
    spec["persistent"] = {"type": "bool", "default": False}
    return spec


def build_ipmi_client(params: dict) -> IpmiClient:
    """Construct an :class:`IpmiClient` from the module's connection parameters."""
    return IpmiClient(host=params["host"], port=params["ipmi_port"], username=params["username"], password=params["password"])


def reject_persistent(persistent: bool) -> None:
    """Refuse a persistent boot-order change before any connection is opened.

    Raising here, ahead of `build_ipmi_client()`, means this refusal costs
    nothing on the wire -- it is a policy decision this module enforces
    regardless of what the BMC would or would not have accepted.
    """
    if persistent:
        raise UnsupportedCapabilityError(_PERSISTENT_REJECTED_MESSAGE, operation="asmb8_boot")


def plan(device: str, uefi: bool, previous: dict) -> bool:
    """Whether ``set_bootdev()`` needs to be called at all.

    ``previous`` is pyghmi's own ``get_bootdev()`` shape, where ``uefimode`` may
    be absent (see module_utils/ipmi.py) -- treated as "the BMC has no opinion
    on UEFI for this override", which is normalized to the *requested* value
    the same way ``community.general.ipmi_boot`` normalizes it
    (``current.setdefault('uefimode', uefiboot)``), so an override that never
    reported a UEFI flag at all does not spuriously compare as changed.
    """
    observed_uefi = previous.get("uefimode", uefi)
    return previous.get("bootdev") != device or observed_uefi != uefi


def main() -> None:
    module = AnsibleModule(argument_spec=argument_spec(), supports_check_mode=True)

    if not HAS_PYGHMI:
        module.fail_json(msg=missing_required_lib("pyghmi"), exception=PYGHMI_IMPORT_ERROR)
        return

    device = module.params["device"]
    uefi = module.params["uefi"]
    desired = {"bootdev": device, "uefiboot": uefi, "persist": False}

    try:
        reject_persistent(module.params["persistent"])

        client = build_ipmi_client(module.params)
        previous = client.get_boot_device()
        changed = plan(device, uefi, previous)

        if not changed or module.check_mode:
            receipt = OperationReceipt(
                action="asmb8_boot",
                endpoint=client.endpoint,
                changed=changed,
                previous=previous,
                desired=desired,
                observed=previous,
            )
            module.exit_json(changed=changed, device=device, uefi=uefi, previous=previous, operation=receipt.to_dict())
            return

        # persist is always False -- see reject_persistent()/_PERSISTENT_REJECTED_MESSAGE above.
        raw_observed = client.set_boot_device(device, persist=False, uefiboot=uefi)
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    # pyghmi's set_bootdev() returns only {'bootdev': device} on success (see
    # module_utils/ipmi.py) -- it does not echo back persist/uefiboot the way
    # get_bootdev() reports them, so this module reconstructs the same shape
    # get_bootdev() would report for what was just armed, the same way
    # community.general.ipmi_boot re-attaches `persistent`/`uefimode` onto its
    # own response rather than leaving the caller to guess whether they took
    # effect.
    observed = {**raw_observed, "persistent": False, "uefimode": uefi}

    receipt = OperationReceipt(
        action="asmb8_boot",
        endpoint=client.endpoint,
        changed=True,
        previous=previous,
        desired=desired,
        observed=observed,
    )
    module.exit_json(changed=True, device=device, uefi=uefi, previous=previous, operation=receipt.to_dict())


if __name__ == "__main__":
    main()
