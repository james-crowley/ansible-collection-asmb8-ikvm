#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_redirection
short_description: Report and optionally toggle ASMB8-iKVM service enablement
description:
  - >-
    Reads (and, if O(state) is given, would mutate) whether this BMC's own listed services --
    C(web), C(kvm), C(cd-media), C(fd-media), C(hd-media), C(ssh), C(telnet), exactly as the BMC's
    own Services page names them -- are enabled, plus whether each service's TCP port is actually
    reachable right now. This module is named, and shaped, after the sibling
    M(james_crowley.intel_amt.amt_redirection) module: same three-signal reporting discipline, same
    read-only-unless-O(state)-is-given default. It does B(not) open a console/video channel itself
    -- see M(james_crowley.asmb8_ikvm.asmb8_console) for that, and this module's own C(seealso).
  - >-
    Three separate signals are always reported per service, per the same discipline the sibling
    module uses, and never collapsed into one boolean: RV(services[].known) -- is this a service
    name this BMC's own Services page lists at all; RV(services[].enabled) -- does the BMC report
    it Active; and RV(services[].reachable) -- does a bare TCP connect to its port(s) actually
    succeed right now. A service can be enabled yet unreachable (see the next paragraph for why
    that is the B(normal) case here, not a fault), or its port can be open while the BMC reports
    the service itself disabled -- collapsing these would hide exactly the distinction an operator
    needs.
  - >-
    B(This distinction matters even more here than on Intel AMT, and it must be understood before
    reading RV(services[].reachable).) C(kvm)/C(cd-media)/C(fd-media)/C(hd-media) (RV(services[].on_demand)
    V(true)) are B(on-demand) listeners on this board: they return TCP RST until a C(.asp) login
    plus C(GET /Java/jviewer.jnlp?EXTRNIP=<ip>&JNLPSTR=JViewer) allocates a session (see
    M(james_crowley.asmb8_ikvm.asmb8_console) and C(plugins/module_utils/asp.py)'s
    C(allocate_media_session)). B("Active but unreachable" is this module's normal resting state
    for those four services, not a fault.) Collapsing RV(services[].enabled) and
    RV(services[].reachable) into one boolean would report a perfectly healthy board as broken the
    moment nobody happens to hold a session open. C(web)/C(ssh)/C(telnet) are B(not) on-demand --
    their ports listen continuously whenever the service is enabled, so an unreachable port there
    is a more meaningful signal.
  - >-
    RV(services[].known) and RV(services[].enabled) come from a B(static catalog) built into this
    module from the BMC's own Services page -- read from its web UI by this collection's
    maintainer, B(not) observed on the wire -- documented in C(docs/hardware-evidence-2026-08-08.md)
    and cited there with an explicit caveat this module preserves: the port numbers and the
    plaintext/secure split are confirmed on the wire, but the exact session-timeout and
    max-session figures, and the live Active/Inactive state itself, are the BMC's own self-report,
    not independently measured or re-queried by this module on every run. B(No sourced C(.asp) RPC
    exists to fetch the Services page's live state over the wire) -- this module does not invent
    one; see O(state) below for what that means for mutation. RV(services[].reachable) is the one
    signal this module actually probes fresh, live, on every run: a bare TCP connect-and-close,
    never a byte of any BMC protocol.
  - >-
    Without O(state), this module is read-only and always reports C(changed=false) -- exactly like
    the sibling module.
  - >-
    O(state), if given, always fails with C(error_class=unsupported_capability), before any
    network is touched. C(plugins/module_utils/asp.py) -- this BMC's only documented RPC surface --
    exposes no endpoint for toggling a service's enablement; nothing here was sourced from a
    capture, a decompiled client, or a specification, so this module refuses to guess at one rather
    than silently building a mutating feature on an invented endpoint. Change a service's
    enablement from the BMC's own web UI (its Services configuration page) until a real RPC for
    this is confirmed and a future release can add it honestly.
version_added: 0.1.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
options:
  services:
    description:
      - >-
        Which of the BMC's own listed services to report on. Defaults to all seven this module's
        catalog knows about (see the module description). Every name here is exactly as the BMC's
        own Services page spells it.
    type: list
    elements: str
    choices: [web, kvm, cd-media, fd-media, hd-media, ssh, telnet]
    default: [web, kvm, cd-media, fd-media, hd-media, ssh, telnet]
  service:
    description:
      - Which single service O(state) targets. Required together with O(state) (and meaningless without it).
    type: str
    choices: [web, kvm, cd-media, fd-media, hd-media, ssh, telnet]
  state:
    description:
      - >-
        When set, requests that O(service) be toggled to V(enabled) or V(disabled). B(Always
        fails) with C(error_class=unsupported_capability), before any network is touched -- see
        the module description for why: no sourced RPC exists on this BMC's C(.asp) surface for
        toggling a service's enablement, and this module will not invent one. Change it from the
        BMC's own web UI instead.
      - When absent (the default), this module only reads and reports current state; C(changed) is always V(false).
    type: str
    choices: [enabled, disabled]
  probe_timeout:
    description:
      - Seconds to wait for each per-port TCP reachability probe (RV(services[].reachable)).
    type: float
    default: 2.0
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_console
  - module: james_crowley.asmb8_ikvm.asmb8_info
  - module: james_crowley.intel_amt.amt_redirection
attributes:
  check_mode:
    description: >-
      Supported, and behaves identically to normal mode. This module never mutates (O(state) fails
      before any network access, in or out of check mode), and its only network activity -- a
      bare TCP connect-and-close per port -- is safe to perform in check mode too.
    support: full
  diff_mode:
    description: Not supported. Use RV(services) and the C(operation) receipt instead.
    support: none
requirements: []
"""

EXAMPLES = r"""
- name: Report every known service's enablement and reachability
  james_crowley.asmb8_ikvm.asmb8_redirection:
    host: "{{ asmb8_host }}"
  delegate_to: localhost
  no_log: true
  register: services

- name: Report only the KVM and CD-media services
  james_crowley.asmb8_ikvm.asmb8_redirection:
    host: "{{ asmb8_host }}"
    services: [kvm, cd-media]
  delegate_to: localhost
  no_log: true
  register: media_services

- name: A KVM service that is enabled but unreachable is healthy, not broken -- no session is open
  ansible.builtin.assert:
    that:
      - services.services.kvm.on_demand
      - services.services.kvm.enabled
      # services.services.kvm.reachable.nonsecure.reachable may legitimately be false here.

- name: Requesting a service-enablement change fails honestly instead of guessing at an endpoint
  james_crowley.asmb8_ikvm.asmb8_redirection:
    host: "{{ asmb8_host }}"
    service: telnet
    state: enabled
  delegate_to: localhost
  no_log: true
  register: toggle_attempt
  ignore_errors: true

- name: Assert the toggle attempt failed the honest way
  ansible.builtin.assert:
    that:
      - toggle_attempt is failed
      - toggle_attempt.error_class == 'unsupported_capability'
"""

RETURN = r"""
changed:
  description: Always V(false) -- see the module description.
  type: bool
  returned: always
services:
  description: Per-service report, keyed by service name, for every name in O(services).
  type: dict
  returned: always
  contains:
    known:
      description: >-
        Whether this service name appears in this module's built-in catalog of the BMC's own
        Services page. Always V(true) for every name O(services) can contain -- O(services)'
        C(choices) is exactly that catalog -- kept as its own field for parity with the sibling
        module's C(supported) signal, and because a future firmware revision that drops or renames
        a service is exactly the kind of drift this field exists to eventually catch.
      type: bool
    on_demand:
      description: >-
        Whether this service's listener is on-demand (V(true) for C(kvm)/C(cd-media)/C(fd-media)/C(hd-media))
        rather than continuously listening whenever enabled (V(false) for C(web)/C(ssh)/C(telnet)).
        See the module description for why this makes "enabled but unreachable" the normal, healthy
        state for an on-demand service.
      type: bool
    enabled:
      description: >-
        Whether the BMC's own Services page reports this service Active. Sourced from this
        module's static catalog (see the module description) -- B(not) independently re-queried on
        this or any run, because no sourced RPC exists to do so. V(true) for every service except
        C(telnet), which the target hardware's Services page reported Inactive.
      type: bool
    capacity:
      description: >-
        This service's configured capacity, per the BMC's own Services page. Vendor self-report --
        see C(docs/hardware-evidence-2026-08-08.md) for exactly what was and was not independently
        confirmed on the wire (the port numbers and the plaintext/secure split were; the timeout
        and max-session figures were not).
      type: dict
      contains:
        nonsecure_port:
          description: The plaintext TCP port, or V(null) if this service has none (C(ssh)).
          type: int
        secure_port:
          description: The TLS-wrapped TCP port, or V(null) if this service has none (C(telnet)).
          type: int
        timeout_seconds:
          description: Server-side inactivity timeout, or V(null) if none is configured (the media services).
          type: int
        max_sessions:
          description: Maximum concurrent sessions, or V(null) if not reported for this service.
          type: int
    reachable:
      description: >-
        Live, this-run-only TCP connect-and-close results, never a byte of any BMC protocol. Each
        of RV(services[].reachable.nonsecure)/RV(services[].reachable.secure) is V(null) if this
        service has no port in that role (see RV(services[].capacity)), otherwise a dict.
      type: dict
      contains:
        nonsecure:
          description: Reachability of RV(services[].capacity.nonsecure_port), or V(null).
          type: dict
          contains:
            port:
              description: The port probed.
              type: int
            reachable:
              description: >-
                Whether the TCP connect succeeded. For a service with RV(services[].on_demand)
                V(true) this is normally V(false) with no session open -- see the module description.
              type: bool
        secure:
          description: Reachability of RV(services[].capacity.secure_port), or V(null).
          type: dict
          contains:
            port:
              description: The port probed.
              type: int
            reachable:
              description: Whether the TCP connect succeeded.
              type: bool
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
      description: Always V(asmb8_redirection.report).
      type: str
    endpoint:
      description: >-
        The bare O(host) this read was performed against. There is no single port to report here
        -- this module probes several per-service ports separately; see RV(services).
      type: str
    changed:
      description: Always V(false).
      type: bool
    observed:
      description: Mirrors RV(services).
      type: dict
    error_class:
      description: A stable machine-readable failure class. V(null) on success.
      type: str
"""

import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import IkvmError, UnsupportedCapabilityError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt

#: The reachability probe's socket factory. Injectable so unit tests exercise this module without
#: ever opening a real socket -- mirrors the sibling james_crowley.intel_amt collection's
#: redirection_service.ConnectFn exactly.
ConnectFn = Callable[[tuple[str, int], float], Any]


@dataclass(frozen=True, slots=True)
class ServiceCapacity:
    """One service's configured capacity, per the BMC's own Services page.

    Vendor self-report, per docs/hardware-evidence-2026-08-08.md's "Service capacities, and a
    provenance caveat" section: the port numbers and the plaintext/secure split were confirmed on
    the wire; ``timeout_seconds``, ``max_sessions``, and ``known_active`` were not independently
    measured -- no session count was ever pushed to its limit, and no session was ever left idle
    long enough to watch a timeout reclaim it. This dataclass is this module's one and only source
    for those three fields; nothing here is re-queried live because no sourced RPC exists to do so.
    """

    nonsecure_port: int | None
    secure_port: int | None
    timeout_seconds: int | None
    max_sessions: int | None
    on_demand: bool
    known_active: bool


#: Every service name this module recognises, in the same order the BMC's own Services page lists
#: them. Sourced from docs/hardware-evidence-2026-08-08.md's "Service capacities, and a provenance
#: caveat" table -- see ServiceCapacity's docstring for exactly what is and is not independently
#: confirmed within it.
SERVICE_CATALOG: dict[str, ServiceCapacity] = {
    "web": ServiceCapacity(nonsecure_port=80, secure_port=443, timeout_seconds=1800, max_sessions=20, on_demand=False, known_active=True),
    "kvm": ServiceCapacity(nonsecure_port=7578, secure_port=7582, timeout_seconds=1800, max_sessions=4, on_demand=True, known_active=True),
    "cd-media": ServiceCapacity(nonsecure_port=5120, secure_port=5124, timeout_seconds=None, max_sessions=1, on_demand=True, known_active=True),
    "fd-media": ServiceCapacity(nonsecure_port=5122, secure_port=5126, timeout_seconds=None, max_sessions=1, on_demand=True, known_active=True),
    "hd-media": ServiceCapacity(nonsecure_port=5123, secure_port=5127, timeout_seconds=None, max_sessions=1, on_demand=True, known_active=True),
    "ssh": ServiceCapacity(nonsecure_port=None, secure_port=22, timeout_seconds=600, max_sessions=None, on_demand=False, known_active=True),
    # Per docs/hardware-evidence-2026-08-08.md: telnet was observed Inactive on the target board's
    # Services page. Not on-demand -- an inactive standing listener, not an allocate-on-use one.
    "telnet": ServiceCapacity(nonsecure_port=23, secure_port=None, timeout_seconds=600, max_sessions=None, on_demand=False, known_active=False),
}

SERVICE_NAMES: tuple[str, ...] = tuple(SERVICE_CATALOG)


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
    spec.update(
        {
            "services": {"type": "list", "elements": "str", "choices": list(SERVICE_NAMES), "default": list(SERVICE_NAMES)},
            "service": {"type": "str", "choices": list(SERVICE_NAMES)},
            "state": {"type": "str", "choices": ["enabled", "disabled"]},
            "probe_timeout": {"type": "float", "default": 2.0},
        }
    )
    return spec


def probe_port(host: str, port: int, *, timeout: float, connect: ConnectFn = socket.create_connection) -> bool:
    """Attempt a bare TCP connect-and-close to ``host:port``. Never raises; a failure is simply V(false).

    Mirrors the sibling james_crowley.intel_amt collection's
    ``redirection_service.probe_transport_reachable`` exactly, one port at a time so a caller can
    attribute the result to a specific service/port-role pair. ``connect`` defaults to
    :func:`socket.create_connection` but is always injectable -- unit tests must never open a real
    socket.
    """
    try:
        connection = connect((host, port), timeout)
    except OSError:
        return False
    close = getattr(connection, "close", None)
    if callable(close):
        close()
    return True


def _reachable_entry(host: str, port: int | None, *, timeout: float, connect: ConnectFn) -> dict | None:
    if port is None:
        return None
    return {"port": port, "reachable": probe_port(host, port, timeout=timeout, connect=connect)}


def build_service_report(name: str, host: str, *, timeout: float, connect: ConnectFn = socket.create_connection) -> dict:
    """Assemble one service's three-signal report: known/enabled (catalog) plus reachable (live probe)."""
    capacity = SERVICE_CATALOG[name]
    return {
        "known": True,
        "on_demand": capacity.on_demand,
        "enabled": capacity.known_active,
        "capacity": {
            "nonsecure_port": capacity.nonsecure_port,
            "secure_port": capacity.secure_port,
            "timeout_seconds": capacity.timeout_seconds,
            "max_sessions": capacity.max_sessions,
        },
        "reachable": {
            "nonsecure": _reachable_entry(host, capacity.nonsecure_port, timeout=timeout, connect=connect),
            "secure": _reachable_entry(host, capacity.secure_port, timeout=timeout, connect=connect),
        },
    }


def build_services_report(names: list[str], host: str, *, timeout: float, connect: ConnectFn = socket.create_connection) -> dict:
    return {name: build_service_report(name, host, timeout=timeout, connect=connect) for name in names}


def main() -> None:
    module = AnsibleModule(
        argument_spec=argument_spec(),
        required_by={"state": ["service"]},
        supports_check_mode=True,
    )
    params = module.params
    endpoint = params["host"]

    try:
        if params["state"] is not None:
            # Fail before any network access at all, check mode or not -- see the module
            # description: no sourced RPC exists on this BMC's .asp surface for toggling a
            # service's enablement, and this module refuses to invent one.
            raise UnsupportedCapabilityError(
                f"asmb8_redirection cannot set state={params['state']!r} for service={params['service']!r}: "
                "no documented RPC exists on this BMC's .asp surface for toggling a service's enablement "
                "(see plugins/module_utils/asp.py). Change this from the BMC's own web UI (its Services "
                "configuration page) instead; a future release can add this once a real RPC is confirmed.",
                endpoint=endpoint,
                operation="asmb8_redirection.report",
            )

        services = build_services_report(params["services"], params["host"], timeout=float(params["probe_timeout"]))
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    receipt = OperationReceipt(
        action="asmb8_redirection.report",
        endpoint=endpoint,
        changed=False,
        previous=None,
        desired=None,
        observed=services,
    )
    module.exit_json(changed=False, services=services, operation=receipt.to_dict())


if __name__ == "__main__":
    main()
