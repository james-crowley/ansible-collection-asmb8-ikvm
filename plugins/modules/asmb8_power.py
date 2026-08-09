#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_power
short_description: Control and query ASMB8-iKVM power state over IPMI
description:
  - >-
    Reads and changes the power state of an ASMB8-iKVM managed endpoint over IPMI
    (RMCP+, UDP), via the C(pyghmi) library's C(Command.get_power())/C(set_power()).
    This is the already-working, non-reverse-engineered path for this board: no
    C(.asp) RPC or JNLP surface is involved, and none of this module's behaviour
    depends on anything still under investigation for this hardware.
  - >-
    O(state=on) and O(state=off) are convergent: pyghmi's C(get_power()) can only
    ever report V(on) or V(off) (confirmed by reading its source directly), so
    nothing is sent when the endpoint already reports the requested state.
    O(state=shutdown), O(state=reset) and O(state=boot) can never compare equal to
    a reported power state and are therefore always imperative -- every successful
    run issues the underlying IPMI command, matching
    C(community.general.ipmi_power)'s own convergence check exactly (it is the
    same comparison, not a separately maintained table).
  - >-
    A successfully issued command only means the BMC accepted it; whether the
    transition itself completed is a separate question this module answers by
    letting pyghmi's own bounded confirmation loop run (O(wait_timeout)). If that
    loop exhausts its budget without observing the target state, this module fails
    with C(error_class=timeout) and C(indeterminate=true) -- the command was
    accepted, only confirmation of it timed out, so the BMC may already have
    applied the change. Callers must re-probe (for example with a second
    C(state=on) task, or M(james_crowley.asmb8_ikvm.asmb8_info)) rather than
    blindly retrying.
  - >-
    This module talks to the BMC over IPMI (UDP O(ipmi_port), default 623) using
    only O(host), O(username) and O(password) from the shared connection
    documentation fragment. O(port), O(use_tls), O(allow_insecure_transport),
    O(validate_certs), O(ca_path), O(tls_fingerprint), O(timeout) and
    O(connect_timeout) are accepted (so a play can share C(module_defaults) across
    every module in this collection without one task failing on an
    "unsupported parameter") but are entirely B(ignored) here: IPMI-over-LAN has
    no TLS layer and none of the C(.asp) web-management surface those options
    describe is touched by this module. One consequence worth calling out
    explicitly: the C(requests) entry under C(requirements) below is inherited
    from that same shared fragment and does B(not) apply to this module -- only
    C(pyghmi) is actually required for M(james_crowley.asmb8_ikvm.asmb8_power) to function.
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
  state:
    description:
      - >-
        Desired power action, taken from pyghmi's own C(set_power())/C(get_power())
        vocabulary, which is exactly C(community.general.ipmi_power)'s documented
        C(state) choices (see C(module_utils/models.py)'s C(POWER_STATES) -- sourced
        from that module's own documentation, not invented here).
      - V(on) -- request the system turn on. Convergent.
      - V(off) -- request the system turn off without waiting for the OS to shut down. Convergent.
      - V(shutdown) -- ask the OS to shut down cleanly (requires OS/ACPI cooperation). Always issued.
      - V(reset) -- request an immediate reset without waiting for the OS. Always issued.
      - >-
        V(boot) -- pyghmi's own "smart" action: turns the system on if it is off,
        otherwise resets it. Always issued, since this module cannot know in
        advance which of the two it will resolve to.
    type: str
    required: true
    choices: ['on', 'off', shutdown, reset, boot]
  wait_timeout:
    description:
      - >-
        Seconds to let pyghmi's own confirmation loop run after issuing a command,
        for O(state) values it can actually confirm (V(on), V(off), V(shutdown)).
        V(reset) and V(boot) are never confirmed by pyghmi regardless of this value
        -- there is nothing to poll for after a reset.
      - >-
        V(0) issues the command and returns immediately without waiting for
        confirmation at all; RV(observed) is then pyghmi's raw, unconfirmed
        response rather than a freshly re-read power state.
    type: int
    default: 60
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_info
  - module: james_crowley.asmb8_ikvm.asmb8_boot
attributes:
  check_mode:
    description: >-
      The current power state is read and compared against O(state) exactly as in
      normal mode, but the IPMI power command is never sent.
    support: full
  diff_mode:
    description: Not supported. Use RV(previous_state)/RV(desired_state) instead of C(--diff).
    support: none
requirements:
  - pyghmi (on the Ansible controller)
"""

EXAMPLES = r"""
- name: Ensure the endpoint is powered on
  james_crowley.asmb8_ikvm.asmb8_power:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    state: "on"
  delegate_to: localhost
  no_log: true
  register: power

- name: Force an immediate reset, without waiting for the OS
  james_crowley.asmb8_ikvm.asmb8_power:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    state: reset
  delegate_to: localhost
  no_log: true

- name: Preview a power-on without sending anything
  james_crowley.asmb8_ikvm.asmb8_power:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    state: "on"
  delegate_to: localhost
  no_log: true
  check_mode: true
"""

RETURN = r"""
state:
  description: The requested O(state), echoed back.
  type: str
  returned: always
previous_state:
  description: >-
    Power state observed before any action was taken, exactly as pyghmi's
    C(get_power()) returned it (a dict with a single C(powerstate) key, V(on) or
    V(off)).
  type: dict
  returned: always
desired_state:
  description: The normalized power state this action targets. Same value as O(state).
  type: str
  returned: always
observed:
  description: >-
    What pyghmi's C(set_power())/C(get_power()) returned after acting, or -- when
    nothing was sent because the state already matched -- the same value as
    RV(previous_state). Shape varies with O(state) and O(wait_timeout): a
    confirmed transition carries C(powerstate); an unconfirmed one carries
    C(pendingpowerstate) instead.
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
      description: V(asmb8_power.<state>), for example V(asmb8_power.on).
      type: str
    endpoint:
      description: The C(host:ipmi_port) this operation was performed against.
      type: str
    changed:
      description: Mirrors the top-level C(changed) value Ansible always returns.
      type: bool
    previous:
      description: Same value as RV(previous_state).
      type: dict
    desired:
      description: Same value as RV(desired_state).
      type: str
    observed:
      description: Same value as RV(observed).
      type: dict
    error_class:
      description: A stable machine-readable failure class. V(null) on success.
      type: str
"""

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import IkvmError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.ipmi import DEFAULT_IPMI_PORT, HAS_PYGHMI, PYGHMI_IMPORT_ERROR, IpmiClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import POWER_STATES, OperationReceipt

#: `state` values pyghmi's own confirmation loop will actually poll for --
#: see module_utils/ipmi.py's docstring: `set_power()` only waits when the
#: target is one of these; `reset`/`boot` are fire-and-forget regardless of
#: `wait_timeout`. Kept here (rather than only inline) because check mode's
#: preview needs to know this without calling pyghmi at all.
_CONFIRMABLE_STATES = ("on", "off", "shutdown")


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
    spec["state"] = {"type": "str", "required": True, "choices": list(POWER_STATES)}
    spec["wait_timeout"] = {"type": "int", "default": 60}
    return spec


def build_ipmi_client(params: dict) -> IpmiClient:
    """Construct an :class:`IpmiClient` from the module's connection parameters."""
    return IpmiClient(host=params["host"], port=params["ipmi_port"], username=params["username"], password=params["password"])


def plan(state: str, previous_powerstate: str) -> bool:
    """Whether an IPMI power command needs to be sent at all.

    Deliberately not a lookup table: pyghmi's C(get_power()) can only ever
    report V(on) or V(off) (see module_utils/ipmi.py's docstring), so
    comparing that directly against C(state) is naturally idempotent for
    V(on)/V(off) and naturally imperative for V(shutdown)/V(reset)/V(boot),
    which can never equal a reported power state. This is exactly the
    comparison C(community.general.ipmi_power) itself makes.
    """
    return previous_powerstate != state


def main() -> None:
    module = AnsibleModule(argument_spec=argument_spec(), supports_check_mode=True)

    if not HAS_PYGHMI:
        module.fail_json(msg=missing_required_lib("pyghmi"), exception=PYGHMI_IMPORT_ERROR)
        return

    state = module.params["state"]
    wait_timeout = module.params["wait_timeout"]

    try:
        client = build_ipmi_client(module.params)
        previous = client.get_power_state()
        changed = plan(state, previous.get("powerstate"))

        if not changed or module.check_mode:
            receipt = OperationReceipt(
                action=f"asmb8_power.{state}",
                endpoint=client.endpoint,
                changed=changed,
                previous=previous,
                desired=state,
                observed=previous,
            )
            module.exit_json(
                changed=changed,
                state=state,
                previous_state=previous,
                desired_state=state,
                observed=previous,
                operation=receipt.to_dict(),
            )
            return

        # Only on/off/shutdown are ever confirmed by pyghmi's own wait loop
        # (see module_utils/ipmi.py); passing wait_timeout for reset/boot is
        # harmless -- pyghmi ignores it for those -- but not pretending
        # otherwise here keeps this branch honest about what actually blocks.
        wait = wait_timeout if state in _CONFIRMABLE_STATES else False
        observed = client.set_power_state(state, wait=wait)
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    receipt = OperationReceipt(
        action=f"asmb8_power.{state}",
        endpoint=client.endpoint,
        changed=True,
        previous=previous,
        desired=state,
        observed=observed,
    )
    module.exit_json(
        changed=True,
        state=state,
        previous_state=previous,
        desired_state=state,
        observed=observed,
        operation=receipt.to_dict(),
    )


if __name__ == "__main__":
    main()
