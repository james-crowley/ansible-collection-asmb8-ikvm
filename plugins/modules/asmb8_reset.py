#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_reset
short_description: Reset the ASMB8-iKVM BMC's management controller over IPMI
description:
  - >-
    B(This is a destructive-adjacent operation.) It resets the BMC's own management
    controller -- IPMI (RMCP+, UDP), netfn C(0x06), cmd C(0x02) (Cold Reset) or C(0x03)
    (Warm Reset), via C(pyghmi) -- and B(drops every active BMC session as a side effect):
    every IPMI session, every C(.asp) web-management login, and B(any iUSB/KVM virtual-media
    session currently in flight). If a host is mid-install from an attached
    M(james_crowley.asmb8_ikvm.asmb8_media) session when this runs, that session is torn down
    along with everything else -- there is no way to reset the BMC's management controller
    without doing this, and this module does not pretend otherwise.
  - >-
    B(The host itself is unaffected.) Verified live against the target hardware: C(get_power())
    reported V(on) immediately before a Cold Reset, and the host never rebooted, stayed on, and
    was never interrupted. This module resets only the management controller, never chassis
    power -- do not reach for this to power-cycle a host; use
    M(james_crowley.asmb8_ikvm.asmb8_power) C(state=reset) for that instead, which is a
    completely different IPMI operation (netfn C(0x00), chassis control) against a different
    target.
  - >-
    This is the automated form of the manual C(ipmitool mc reset cold) recovery step this
    collection's own README and hardware-evidence notes already point operators at for one
    specific, observed failure: this BMC's virtual-media slot is single-occupancy, board-wide,
    and has no server-side timeout to reclaim an abandoned session. A wedged slot can present as
    a TCP connection that is fully C(ESTABLISHED) -- verified live: 62 bytes sitting unread in the
    socket's own receive queue -- while zero SCSI commands are actually being serviced. B(An
    established socket is not evidence media is being served); if
    M(james_crowley.asmb8_ikvm.asmb8_media)'s own software reclamation cannot clear a stale
    session (RV(ignore:error_class) V(bmc_busy) with no known C(session_id) still holding the
    slot), this module is the escape hatch that follows.
  - >-
    O(mode=cold) and O(mode=warm) are B(deliberately two separate, explicit choices) rather than
    one default -- see O(mode) below for what each does. Both share the property above (every
    session dropped, host power unaffected); they differ in how much of the BMC's own internal
    state gets reinitialized and, consequently, roughly how long the management controller stays
    unavailable afterwards.
  - >-
    B(Recovery after either mode is staged, not instantaneous or uniform across services.)
    Verified live for O(mode=cold): ICMP answered noticeably before the C(.asp) web/HTTPS stack
    (port 443) did. No exact duration was captured for either stage in that observation, and this
    module does not invent one -- a caller that must know the BMC is usable again should poll the
    specific service it actually depends on (a fresh IPMI session via this collection's own
    modules, or the C(.asp) surface via M(james_crowley.asmb8_ikvm.asmb8_info)) rather than ICMP,
    which recovers first and is not evidence the management stack is back. The on-demand iUSB
    media listener (port 5120) being closed immediately after a reset is normal, not a fault --
    that port is bound only after a fresh session allocates it, and this operation just dropped
    every session that could have done so.
  - >-
    This module never confirms the reset itself: there is nothing pyghmi (or the raw IPMI
    command it falls back to for O(mode=warm)) gives this collection to poll for after issuing
    it, the same "fire and forget" reasoning M(james_crowley.asmb8_ikvm.asmb8_power)'s own
    C(state=reset)/C(state=boot) handling already documents for chassis-level resets. A
    successful C(changed) means only that the BMC accepted the reset command (completion code 0,
    no C(error) key in pyghmi's response) -- confirming the management controller actually came
    back up is a separate, later probe a caller must make on its own.
  - >-
    This module talks to the BMC over IPMI (UDP O(ipmi_port), default 623) using only O(host),
    O(username) and O(password) from the shared connection documentation fragment. O(port),
    O(use_tls), O(allow_insecure_transport), O(validate_certs), O(ca_path), O(tls_fingerprint),
    O(timeout) and O(connect_timeout) are accepted (so a play can share C(module_defaults) across
    every module in this collection without one task failing on an "unsupported parameter") but
    are entirely B(ignored) here: IPMI-over-LAN has no TLS layer and none of the C(.asp)
    web-management surface those options describe is touched by this module. The C(requests)
    entry under C(requirements) below is likewise inherited from that shared fragment and does
    B(not) apply -- only C(pyghmi) is actually required for
    M(james_crowley.asmb8_ikvm.asmb8_reset) to function.
version_added: 0.3.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
options:
  ipmi_port:
    description:
      - >-
        UDP port of the BMC's IPMI-over-LAN (RMCP+) listener. B(Not) the same thing as O(port),
        which is this collection's shared web-management HTTPS port (443) and is unused by this
        module.
    type: int
    default: 623
  mode:
    description:
      - >-
        Which self-reset to issue. B(Required, with no default) -- this option exists precisely
        so the choice is explicit in every playbook and in C(ansible-doc), never silently assumed.
      - >-
        V(cold) -- Cold Reset (netfn C(0x06) cmd C(0x02)), issued via C(pyghmi)'s own
        C(Command.reset_bmc()). Per the IPMI 2.0 specification, this is the more thorough of the
        two: broadly equivalent to the management controller's own power-up reset sequence,
        reinitializing its firmware/runtime state more completely than Warm Reset. This is the
        mode VERIFIED LIVE against the target board -- see the module description -- and the one
        this collection's own documentation already names as the C(ipmitool mc reset cold)
        recovery step.
      - >-
        V(warm) -- Warm Reset (netfn C(0x06) cmd C(0x03)). Per the IPMI 2.0 specification, this
        reinitializes the management controller's software/application state without the fuller
        hardware-level reinitialization Cold Reset performs, and is documented (by the spec, for
        IPMI management controllers generally) to typically recover faster as a result. B(Not)
        independently verified live against this specific board -- see the module description's
        staged-recovery note, which was only captured for O(mode=cold).
      - >-
        Whichever mode is chosen, both drop every active BMC session (see the module description)
        and leave host power untouched.
    type: str
    required: true
    choices: [cold, warm]
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_power
  - module: james_crowley.asmb8_ikvm.asmb8_media
  - module: james_crowley.asmb8_ikvm.asmb8_info
attributes:
  check_mode:
    description: >-
      Fully supported, and deliberately conservative: check mode does not open an IPMI
      connection at all -- there is no "previous state" for a self-reset to read or compare
      against (unlike C(state)-based modules such as
      M(james_crowley.asmb8_ikvm.asmb8_power)/M(james_crowley.asmb8_ikvm.asmb8_boot), this
      operation is never idempotent: it always issues the reset when run for real). A dry run
      therefore touches the network in no way whatsoever and never resets anything.
    support: full
  diff_mode:
    description: Not supported. Use RV(operation.desired)/RV(operation.observed) instead of C(--diff).
    support: none
requirements:
  - pyghmi (on the Ansible controller)
"""

EXAMPLES = r"""
- name: Recover a wedged virtual-media slot with a cold reset
  james_crowley.asmb8_ikvm.asmb8_reset:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    mode: cold
  delegate_to: localhost
  no_log: true
  register: reset_result

- name: Issue a faster warm reset when a full cold reinitialization is not needed
  james_crowley.asmb8_ikvm.asmb8_reset:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    mode: warm
  delegate_to: localhost
  no_log: true

- name: Preview the reset without touching the network at all
  james_crowley.asmb8_ikvm.asmb8_reset:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    mode: cold
  delegate_to: localhost
  no_log: true
  check_mode: true

- name: Wait for the management controller to actually be usable again after a reset
  james_crowley.asmb8_ikvm.asmb8_info:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    fields: [power_state]
  delegate_to: localhost
  no_log: true
  register: post_reset_probe
  retries: 20
  delay: 5
  until: post_reset_probe is succeeded
"""

RETURN = r"""
mode:
  description: The requested O(mode), echoed back.
  type: str
  returned: always
observed:
  description: >-
    What was actually issued. In check mode this is V(null) -- nothing was sent, see O(mode)'s
    check_mode note. Otherwise a small dict: C(mode) (same as RV(mode)) plus whatever, if
    anything, pyghmi's underlying call returned (empty for O(mode=cold), since pyghmi's own
    C(reset_bmc()) returns nothing itself; potentially non-empty raw completion data for
    O(mode=warm), which goes through C(raw_command()) directly).
  type: dict
  returned: always
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
      description: V(asmb8_reset.<mode>), for example V(asmb8_reset.cold).
      type: str
    endpoint:
      description: The C(host:ipmi_port) this operation was (or, in check mode, would be) performed against.
      type: str
    changed:
      description: >-
        Always V(true) when this module runs for real -- a self-reset is never idempotent, see
        O(mode)'s check_mode note. Mirrors the top-level C(changed) value Ansible always returns.
      type: bool
    previous:
      description: Always V(null) -- there is no prior state for a self-reset to compare against.
      type: dict
    desired:
      description: Same value as RV(mode).
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
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt

#: The two IPMI self-reset modes this module supports -- see DOCUMENTATION's
#: O(mode) for exactly what each does and how each was (or was not) verified
#: live. Not sourced from any sibling module's own vocabulary the way
#: POWER_STATES/BOOT_DEVICES in module_utils/models.py are: there is no
#: community.general equivalent for a BMC self-reset (see docs/roadmap.md's
#: "Delegate to an existing module instead of writing our own" section), so
#: this vocabulary is this module's own, taken directly from the IPMI 2.0
#: specification's own Cold Reset/Warm Reset command names.
RESET_MODES = ("cold", "warm")


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
    spec["mode"] = {"type": "str", "required": True, "choices": list(RESET_MODES)}
    return spec


def build_ipmi_client(params: dict) -> IpmiClient:
    """Construct an :class:`IpmiClient` from the module's connection parameters."""
    return IpmiClient(host=params["host"], port=params["ipmi_port"], username=params["username"], password=params["password"])


def main() -> None:
    module = AnsibleModule(argument_spec=argument_spec(), supports_check_mode=True)

    if not HAS_PYGHMI:
        module.fail_json(msg=missing_required_lib("pyghmi"), exception=PYGHMI_IMPORT_ERROR)
        return

    mode = module.params["mode"]
    endpoint = f"{module.params['host']}:{module.params['ipmi_port']}"

    if module.check_mode:
        # Deliberately never opens a connection -- see DOCUMENTATION's
        # check_mode note. A self-reset has no idempotency concept, so there
        # is nothing worth reading from the BMC before deciding what a real
        # run would do: it would always issue the reset.
        receipt = OperationReceipt(action=f"asmb8_reset.{mode}", endpoint=endpoint, changed=True, previous=None, desired=mode, observed=None)
        module.exit_json(changed=True, mode=mode, observed=None, operation=receipt.to_dict())
        return

    try:
        client = build_ipmi_client(module.params)
        observed = client.reset_bmc(mode)
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    receipt = OperationReceipt(action=f"asmb8_reset.{mode}", endpoint=client.endpoint, changed=True, previous=None, desired=mode, observed=observed)
    module.exit_json(changed=True, mode=mode, observed=observed, operation=receipt.to_dict())


if __name__ == "__main__":
    main()
