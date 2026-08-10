#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_identify
short_description: Control the ASMB8-iKVM chassis identify LED over standard IPMI
description:
  - >-
    Turns the chassis identify LED on (indefinitely, or for a bounded duration) or
    off, via the standard IPMI Chassis Identify command (netfn C(0x00), cmd
    C(0x04)) -- C(pyghmi)'s C(Command.set_identify()). This is the same
    already-working, non-reverse-engineered IPMI path
    M(james_crowley.asmb8_ikvm.asmb8_power)/M(james_crowley.asmb8_ikvm.asmb8_boot)/
    M(james_crowley.asmb8_ikvm.asmb8_reset) use: no C(.asp) RPC or JNLP surface is
    involved, and this module carries no lockout risk whatsoever -- it can only ever
    change whether a light is lit, never power state, boot device, or any session.
  - >-
    Verified directly against C(pyghmi) 1.6.19's installed source (in a disposable
    virtualenv with no BMC reachable from it, never from memory): C(set_identify())
    first resolves an OEM handler via C(pyghmi.ipmi.oem.lookup.get_oem_handler()),
    whose C(oemmap) contains only Lenovo/IBM manufacturer IDs (20301, 19046, 7154).
    American Megatrends -- this board's manufacturer -- is not a key in that map, so
    the lookup falls through to C(pyghmi.ipmi.oem.generic.OEMHandler), whose own
    C(set_identify()) unconditionally raises C(UnsupportedFunctionality). C(pyghmi)'s
    C(Command.set_identify()) catches exactly that and falls through to the standard
    IPMI command itself -- the exception never reaches this module's caller on this
    board. See C(module_utils/ipmi.py)'s docstring for the full citation, by file and
    line.
  - >-
    B(This module cannot be idempotent, and does not pretend otherwise.) Standard
    IPMI Chassis Identify has no documented read-back command -- there is no way to
    ask a BMC "is the identify LED currently on". C(changed) is therefore always
    V(true) on a real run, exactly the same honesty
    M(james_crowley.asmb8_ikvm.asmb8_reset) already applies to a self-reset for the
    same underlying reason (no prior state exists to compare against). Do not build
    automation that expects a second, identical C(asmb8_identify) task to report
    C(changed=false) -- it will not, because there is nothing this module could have
    checked to justify claiming so.
  - >-
    C(blink) is deliberately not an option on this module at all. C(pyghmi)'s own
    generic (standard-command) fallback -- the path this board always takes, per the
    citation above -- raises C(IpmiException('Blink not supported with generic
    IPMI')) unconditionally whenever C(blink=True) reaches it, regardless of any
    other argument. Exposing a C(blink) option here would be offering a capability
    C(pyghmi) itself cannot honour on this board, which this collection's own policy
    (never invent an option a wrapped library cannot actually perform) argues
    against.
  - >-
    O(duration=0) combined with O(state=on) is refused before any IPMI session is
    opened, rather than sent through. C(pyghmi)'s standard-command path ignores the
    "on"/"off" intent entirely whenever a duration is supplied: it sends a single-byte
    Identify Interval, and per the IPMI 2.0 specification a value of C(0) always
    means "turn the LED off" regardless of what was asked for. Silently issuing the
    opposite of a caller's stated intent is exactly the kind of surprising behaviour
    this module chooses to fail loudly on instead.
  - >-
    This module talks to the BMC over IPMI (UDP O(ipmi_port), default 623) using
    only O(host), O(username) and O(password) from the shared connection
    documentation fragment. O(port), O(use_tls), O(allow_insecure_transport),
    O(validate_certs), O(ca_path), O(tls_fingerprint), O(timeout) and
    O(connect_timeout) are accepted (so a play can share C(module_defaults) across
    every module in this collection without one task failing on an "unsupported
    parameter") but are entirely B(ignored) here: IPMI-over-LAN has no TLS layer and
    none of the C(.asp) web-management surface those options describe is touched by
    this module. The C(requests) entry under C(requirements) below is likewise
    inherited from that shared fragment and does B(not) apply to this module -- only
    C(pyghmi) is actually required for M(james_crowley.asmb8_ikvm.asmb8_identify) to
    function.
version_added: 0.4.0
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
      - Desired identify LED state.
      - V(on) -- turn the LED on, either indefinitely (O(duration) omitted) or for a
        bounded number of seconds (O(duration) set).
      - V(off) -- turn the LED off immediately. O(duration) must not be set alongside
        V(off); see O(duration) below for why.
    type: str
    required: true
    choices: ['on', 'off']
  duration:
    description:
      - >-
        Seconds to leave the LED on before it turns itself off again. Only valid
        with O(state=on) -- setting this alongside O(state=off) fails before any IPMI
        session is opened, since C(pyghmi)'s underlying command has no way to combine
        the two (see the module description).
      - >-
        Omit entirely with O(state=on) to request the LED stay on indefinitely (the
        IPMI "Force Identify On" behaviour) instead of for a bounded time.
      - >-
        Must be at least V(1) when supplied with O(state=on) -- V(0) is refused,
        rather than silently sent, because C(pyghmi)'s standard IPMI command treats an
        Identify Interval of V(0) as "turn off" regardless of the requested O(state);
        see the module description.
      - >-
        Values above V(255) are accepted by this module but silently clamped to
        V(255) by C(pyghmi) itself (the IPMI 2.0 Identify Interval field is one byte).
        This module does not duplicate that clamp with its own validation; it only
        documents it here.
    type: int
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_power
  - module: james_crowley.asmb8_ikvm.asmb8_reset
  - module: james_crowley.asmb8_ikvm.asmb8_info
attributes:
  check_mode:
    description: >-
      Fully supported, and deliberately conservative: check mode never opens an IPMI
      connection at all -- there is no "previous state" for chassis identify to read
      or compare against (see the module description's idempotence note), so a dry
      run touches the network in no way whatsoever. C(changed) is reported V(true) in
      check mode too, matching what a real run always reports.
    support: full
  diff_mode:
    description: Not supported. Use RV(operation.desired)/RV(operation.observed) instead of C(--diff).
    support: none
requirements:
  - pyghmi (on the Ansible controller)
"""

EXAMPLES = r"""
- name: Turn on the identify LED indefinitely, to find a host physically in a rack
  james_crowley.asmb8_ikvm.asmb8_identify:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    state: "on"
  delegate_to: localhost
  no_log: true

- name: Blink the identify LED on for five minutes, then let it turn itself off
  james_crowley.asmb8_ikvm.asmb8_identify:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    state: "on"
    duration: 300
  delegate_to: localhost
  no_log: true

- name: Turn the identify LED back off once the host has been located
  james_crowley.asmb8_ikvm.asmb8_identify:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    state: "off"
  delegate_to: localhost
  no_log: true

- name: Preview turning the LED on without touching the network at all
  james_crowley.asmb8_ikvm.asmb8_identify:
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
duration:
  description: The requested O(duration), echoed back. V(null) when not supplied.
  type: int
  returned: always
observed:
  description: >-
    What was actually requested. V(null) in check mode -- nothing was sent, see
    O(state)'s check_mode note. Otherwise a small dict with C(state) and C(duration)
    keys, mirroring the request itself: standard IPMI Chassis Identify has no
    read-back command and C(pyghmi)'s C(set_identify()) returns nothing on success
    (see C(module_utils/ipmi.py)), so there is no independent confirmation from the
    BMC this field could carry instead. Absence of an exception is the only signal of
    success this module (or C(pyghmi), or the IPMI command itself) has.
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
      description: V(asmb8_identify.<state>), for example V(asmb8_identify.on).
      type: str
    endpoint:
      description: The C(host:ipmi_port) this operation was (or, in check mode, would be) performed against.
      type: str
    changed:
      description: >-
        Always V(true) when this module runs for real -- chassis identify is never
        idempotent, see the module description. Mirrors the top-level C(changed)
        value Ansible always returns.
      type: bool
    previous:
      description: Always V(null) -- there is no prior state for chassis identify to compare against.
      type: dict
    desired:
      description: Same value as RV(observed) -- what was (or would be) requested.
      type: dict
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
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt

#: This module's own vocabulary, not sourced from any sibling module's
#: documented choices the way POWER_STATES/BOOT_DEVICES in
#: module_utils/models.py are: there is no community.general equivalent for
#: chassis identify (see docs/roadmap.md's "Delegate to an existing module
#: instead of writing our own" section). Deliberately just `on`/`off`, not
#: pyghmi's own `on: bool` -- see DOCUMENTATION's `state` for why this reads
#: better as a module option than a bare boolean would.
IDENTIFY_STATES = ("on", "off")

#: See DOCUMENTATION's `duration` option and `module_utils/ipmi.py`'s
#: docstring: pyghmi's standard-command path ignores `on` entirely whenever
#: `duration` is not `None`, and an Identify Interval of 0 always means "turn
#: off" per the IPMI 2.0 specification -- sending `duration=0` alongside
#: `state=on` would silently do the opposite of what was asked. Refused here,
#: before any IPMI session is opened, rather than passed through.
_MINIMUM_ON_DURATION = 1


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
    spec["state"] = {"type": "str", "required": True, "choices": list(IDENTIFY_STATES)}
    spec["duration"] = {"type": "int"}
    return spec


def build_ipmi_client(params: dict) -> IpmiClient:
    """Construct an :class:`IpmiClient` from the module's connection parameters."""
    return IpmiClient(host=params["host"], port=params["ipmi_port"], username=params["username"], password=params["password"])


def validate_duration(state: str, duration: int | None) -> str | None:
    """Return an error message if ``state``/``duration`` are an invalid combination, else ``None``.

    A pure parameter-combination check, not an :class:`IkvmError` -- it never
    touches the network and carries no ``error_class``, the same tier
    ``missing_required_lib()`` failures sit at in every module in this
    collection. See DOCUMENTATION's O(duration) for why both cases here are
    refused outright rather than silently reinterpreted.
    """
    if state == "off" and duration is not None:
        return "duration is only valid with state=on. asmb8_identify state=off always turns the LED off immediately, unconditionally."
    if state == "on" and duration is not None and duration < _MINIMUM_ON_DURATION:
        return (
            f"duration must be at least {_MINIMUM_ON_DURATION} when state=on. pyghmi's underlying IPMI command treats "
            "duration=0 as 'turn off' regardless of state=on, which would silently do the opposite of what was requested. "
            "Omit duration entirely to turn the LED on indefinitely instead."
        )
    return None


def main() -> None:
    module = AnsibleModule(argument_spec=argument_spec(), supports_check_mode=True)

    if not HAS_PYGHMI:
        module.fail_json(msg=missing_required_lib("pyghmi"), exception=PYGHMI_IMPORT_ERROR)
        return

    state = module.params["state"]
    duration = module.params["duration"]

    validation_error = validate_duration(state, duration)
    if validation_error is not None:
        module.fail_json(msg=validation_error)
        return

    desired = {"state": state, "duration": duration}

    if module.check_mode:
        # Deliberately never opens a connection -- see DOCUMENTATION's
        # check_mode note. Chassis identify has no read-back, so there is
        # nothing this module could compare against even if it did connect: a
        # real run always issues the command.
        endpoint = f"{module.params['host']}:{module.params['ipmi_port']}"
        receipt = OperationReceipt(action=f"asmb8_identify.{state}", endpoint=endpoint, changed=True, previous=None, desired=desired, observed=None)
        module.exit_json(changed=True, state=state, duration=duration, observed=None, operation=receipt.to_dict())
        return

    try:
        client = build_ipmi_client(module.params)
        client.set_identify(on=(state == "on"), duration=duration)
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    # pyghmi's set_identify() returns nothing on success (see
    # module_utils/ipmi.py), and there is no standard IPMI command to read
    # identify state back at all -- `observed` therefore mirrors the request
    # itself, since "no exception was raised" is the only signal of success
    # this module has to report.
    receipt = OperationReceipt(action=f"asmb8_identify.{state}", endpoint=client.endpoint, changed=True, previous=None, desired=desired, observed=desired)
    module.exit_json(changed=True, state=state, duration=duration, observed=desired, operation=receipt.to_dict())


if __name__ == "__main__":
    main()
