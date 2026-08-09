#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_info
short_description: Gather ASMB8-iKVM capability and state facts
description:
  - >-
    Reads IPMI-observed facts from an ASMB8-iKVM BMC -- power state, boot-device
    override, and management-controller identity -- over the same already-working
    IPMI path M(james_crowley.asmb8_ikvm.asmb8_power)/M(james_crowley.asmb8_ikvm.asmb8_boot) use, plus (only when explicitly
    requested) a small set of read-only facts from the C(.asp) web-management
    surface.
  - This module B(never mutates IPMI state) and always reports C(changed=false).
  - >-
    The one exception to "never mutates" is O(include_web_session), and it is
    exactly that: an exception, opted into explicitly, never on by default.
    Authenticating against this BMC's web session (C(POST /rpc/WEBSES/create.asp))
    creates BMC-side session state even though everything this module subsequently
    reads over that session is itself read-only -- see O(include_web_session)
    below. Nothing else this module does touches the BMC in a way that leaves any
    trace.
  - >-
    RV(asmb8.capabilities) is deliberately honest about what has and has not been
    proven against this hardware. Virtual media and remote console/KVM redirection
    are B(not) reported as available here -- neither has been proven working
    against the target board yet, and this module does not claim otherwise just
    because C(module_utils/asp.py) has code that could, in principle, attempt
    them.
  - >-
    This module never fetches the KVM/media session JNLP (C(/Java/jviewer.jnlp)).
    Doing so is documented in C(module_utils/asp.py) as allocating a BMC-side
    KVM/media session as a side effect of the fetch itself -- not a read, no
    matter how read-only the intent -- and a module documented as never mutating
    anything must not perform it implicitly. RV(asmb8.media.port_mode) is
    therefore always V(unknown) here; determining it for real requires fetching
    that JNLP, which is out of scope for a read-only module and left to a future
    virtual-media module.
version_added: 0.1.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
options:
  ipmi_port:
    description:
      - >-
        UDP port of the BMC's IPMI-over-LAN (RMCP+) listener, used for every IPMI
        fact this module gathers. B(Not) the same thing as O(port), which is this
        collection's shared web-management HTTPS port (443) and is only consulted
        when O(include_web_session=true).
    type: int
    default: 623
  include_web_session:
    description:
      - >-
        Whether to additionally authenticate against the C(.asp) web-management
        session and read a small amount of diagnostic state over it, using
        O(host), O(port), O(username), O(password) and the rest of the TLS-related
        options from the shared connection fragment.
      - >-
        Defaults to V(false) B(on purpose). C(POST /rpc/WEBSES/create.asp) --
        this BMC's login endpoint -- creates real session state on the BMC even
        though this module's own use of that session afterwards is strictly
        read-only; see C(module_utils/asp.py). A caller that only wants IPMI facts
        should not pay that cost, and no caller should have it happen without
        asking.
      - >-
        When V(true) and the login itself fails, this module fails -- it does not
        silently degrade RV(asmb8.web_management) to V(null), because a caller
        that explicitly opted in almost certainly wants to know that the
        credentials or connection it is relying on elsewhere in the same play are
        broken, not have that failure hidden behind a successful-looking IPMI-only
        result.
    type: bool
    default: false
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_power
  - module: james_crowley.asmb8_ikvm.asmb8_boot
attributes:
  check_mode:
    description: A full read runs identically in check mode, since this module never mutates IPMI state.
    support: full
  diff_mode:
    description: Not supported. There is no prior/after state to diff for a read-only module.
    support: none
requirements:
  - pyghmi (on the Ansible controller)
  - requests >= 2.25.0 (on the Ansible controller, only when O(include_web_session=true))
"""

EXAMPLES = r"""
- name: Read IPMI facts only (no BMC-side session created)
  james_crowley.asmb8_ikvm.asmb8_info:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
  delegate_to: localhost
  no_log: true
  register: asmb8

- name: Require IPMI to be reachable before attempting a power/boot change
  ansible.builtin.assert:
    that:
      - asmb8.asmb8.reachable
      - asmb8.asmb8.ipmi.power_state is not none

- name: Do not attempt virtual media -- this module reports it as unproven, not available
  ansible.builtin.assert:
    that:
      - not asmb8.asmb8.capabilities.virtual_media.supported

- name: Also read a small amount of web-management diagnostic state
  james_crowley.asmb8_ikvm.asmb8_info:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    include_web_session: true
  delegate_to: localhost
  no_log: true
  register: asmb8_with_web
"""

RETURN = r"""
asmb8:
  description: IPMI-observed (and, optionally, C(.asp)-observed) facts.
  returned: success
  type: dict
  contains:
    reachable:
      description: Whether the IPMI session could be established at all.
      type: bool
      returned: always
      sample: true
    ipmi:
      description: Facts read directly over IPMI.
      type: dict
      returned: always
      contains:
        power_state:
          description: >-
            Pyghmi's C(get_power()) result verbatim, or V(null) if this
            particular read failed after the IPMI session was otherwise
            established (see RV(operation.ipmi_reads)).
          type: dict
          returned: when available
          sample: {powerstate: "on"}
        boot_device:
          description: >-
            Pyghmi's C(get_bootdev()) result verbatim, or V(null) if this
            particular read failed. C(uefimode) may be absent from this dict on
            the branch where the BMC reports no standing override at all -- see
            C(module_utils/ipmi.py).
          type: dict
          returned: when available
          sample: {bootdev: default, persistent: false, uefimode: false}
        mc_info:
          description: >-
            Pyghmi's C(get_mci()) result -- a bare string, B(not) a dict, unlike
            RV(asmb8.ipmi.power_state)/RV(asmb8.ipmi.boot_device) above. V(null)
            if this read failed or the BMC reported nothing.
          type: str
          returned: when available
    web_management:
      description: >-
        Read-only C(.asp) web-management facts. V(null) unless O(include_web_session=true).
      type: dict
      returned: when O(include_web_session=true)
      contains:
        logged_in:
          description: >-
            Whether C(create.asp) accepted the credentials. Always V(true) when
            present -- a rejected login fails the whole module rather than being
            reported here (see O(include_web_session)).
          type: bool
        host_status_raw:
          description: >-
            Raw C(hoststatus.asp) response text, truncated, for diagnosis only.
            C(module_utils/asp.py) documents this endpoint's response shape as
            B(unverified) -- it is not parsed, and no field within it is claimed
            to mean anything specific.
          type: str
    capabilities:
      description: >-
        What this board supports, and whether that support has actually been
        proven against the target hardware or is only expected. Every entry
        follows the same C(supported)/C(proven) shape so "not proven" and "not
        supported" are never conflated.
      type: dict
      returned: always
      contains:
        ipmi_power:
          description: Power control over IPMI. Proven live against the target board.
          type: dict
        ipmi_boot_device:
          description: Boot-device override over IPMI. Proven live against the target board.
          type: dict
        ipmi_mc_info:
          description: Management-controller identification over IPMI. Proven live against the target board.
          type: dict
        web_management:
          description: >-
            The read-only C(.asp) facts under RV(asmb8.web_management). C(proven)
            is V(true) only when O(include_web_session=true) B(and) the login
            actually succeeded on this run -- it does not mean "proven at some
            point in the past".
          type: dict
        virtual_media:
          description: >-
            Attaching a local image to the BMC's virtual CD-ROM/floppy over the
            proprietary iUSB protocol. B(Not) supported here: C(supported) is
            V(null) (unknown, not V(false)) and C(proven) is always V(false) --
            this has not yet been proven against the target hardware. Do not read
            this as a firmware limitation finding.
          type: dict
        remote_console:
          description: >-
            KVM/remote-console redirection. Same caveat as
            RV(asmb8.capabilities.virtual_media) -- not yet proven.
          type: dict
        redfish:
          description: >-
            Always C(supported=false), C(proven=true): the target board is an
            ASPEED AST2400, and Redfish support on this firmware family arrived
            with AST2500/ASMB9. This is a confirmed hardware-generation fact, not
            a live probe -- no Redfish endpoint is ever contacted by this module.
          type: dict
    media:
      description: Virtual-media/KVM session facts this module can report without allocating a session.
      type: dict
      returned: always
      contains:
        port_mode:
          description: >-
            Always V(unknown). Determining V(single_port) vs V(dedicated_ports)
            (see C(module_utils/models.py)'s C(JnlpSession)) requires fetching the
            KVM/media JNLP, which allocates a BMC-side session as a side effect --
            a mutation this read-only module refuses to perform. See this module's
            description.
          type: str
operation:
  description: >-
    The non-secret C(asmb8-ikvm-operation/v1) receipt for this read, in the same
    nested shape every other module in this collection returns it under.
    The receipt's C(previous) and C(desired) members are always C(null) here: a
    read has no prior state and no intended change to report. See RV(asmb8) for
    what was actually observed. Those two members are referenced above as literal
    code rather than as return-value directives on purpose - they are not declared
    as documented return values on this read-only module, and validate-modules
    rejects a return-value directive naming something undeclared.
  type: dict
  returned: always
  contains:
    schema:
      description: Always V(asmb8-ikvm-operation/v1).
      type: str
    action:
      description: Always V(get_facts).
      type: str
    endpoint:
      description: The C(host:ipmi_port) this read was performed against.
      type: str
    changed:
      description: Always V(false).
      type: bool
    error_class:
      description: A stable machine-readable failure class. V(null) on success.
      type: str
    ipmi_reads:
      description: >-
        Per-field outcome for each IPMI fact this module attempted, keyed by the
        RV(asmb8.ipmi) field name. Present so a V(null) fact value can be told
        apart from "this firmware does not support the read" versus "the read
        itself failed for a reason worth seeing".
      type: dict
      contains:
        outcome:
          description: V(read) if the field was populated, V(failed) if the read raised.
          type: str
          choices: [read, failed]
        error_class:
          description: The failure class, on V(failed) only; V(null) otherwise.
          type: str
      sample:
        power_state: {outcome: read, error_class: null}
        boot_device: {outcome: read, error_class: null}
        mc_info: {outcome: failed, error_class: remote_operation}
"""

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import HAS_REQUESTS, REQUESTS_IMPORT_ERROR, AspClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import IkvmError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.ipmi import DEFAULT_IPMI_PORT, HAS_PYGHMI, PYGHMI_IMPORT_ERROR, IpmiClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt

#: Bound on the diagnostic text this module keeps from hoststatus.asp. Its
#: response shape is documented in module_utils/asp.py as unverified -- this is
#: purely so a caller can look at *something* when include_web_session=true,
#: never a value this module parses or attaches meaning to.
_HOST_STATUS_DIAGNOSTIC_LIMIT = 2048


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
    spec["include_web_session"] = {"type": "bool", "default": False}
    return spec


def build_ipmi_client(params: dict) -> IpmiClient:
    """Construct an :class:`IpmiClient` from the module's connection parameters."""
    return IpmiClient(host=params["host"], port=params["ipmi_port"], username=params["username"], password=params["password"])


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


def _read_ipmi_field(reads: dict, field: str, getter) -> object:
    """Run one IPMI read, degrading a failure to ``None`` rather than failing the whole module.

    Records the outcome in ``reads`` (this module's C(operation.ipmi_reads)) so
    a V(null) fact can be told apart from "did not attempt" and "attempted and
    failed for a reason worth seeing". Only :class:`errors.IkvmError` is caught
    here -- the IPMI session itself was already established by the time any of
    these run, so a failure at this point is a per-field refusal, not a
    connection problem this module should hide.
    """
    try:
        value = getter()
    except IkvmError as err:
        reads[field] = {"outcome": "failed", "error_class": err.error_class}
        return None
    reads[field] = {"outcome": "read", "error_class": None}
    return value


def gather_ipmi_facts(client: IpmiClient) -> tuple[dict, dict]:
    """Read every IPMI fact this module reports, degrading per-field rather than all-or-nothing."""
    reads: dict[str, dict] = {}
    facts = {
        "power_state": _read_ipmi_field(reads, "power_state", client.get_power_state),
        "boot_device": _read_ipmi_field(reads, "boot_device", client.get_boot_device),
        "mc_info": _read_ipmi_field(reads, "mc_info", client.get_mc_info),
    }
    return facts, reads


def gather_web_management_facts(params: dict) -> dict:
    """Authenticate and read a small amount of read-only diagnostic state over the ``.asp`` surface.

    Only called when O(include_web_session=true) -- see that option's
    documentation for why creating this session is a deliberate, opted-in
    exception to this module never mutating anything. A login failure is
    allowed to propagate: see the same option's documentation for why this
    module does not swallow it.
    """
    asp = build_asp_client(params)
    asp.login()
    host_status_raw = asp.get_host_status()
    if len(host_status_raw) > _HOST_STATUS_DIAGNOSTIC_LIMIT:
        host_status_raw = host_status_raw[:_HOST_STATUS_DIAGNOSTIC_LIMIT]
    return {"logged_in": True, "host_status_raw": host_status_raw}


def build_capabilities(*, web_management: dict | None, include_web_session: bool) -> dict:
    """Report what this board supports and, separately, what this collection has actually proven.

    See this module's DOCUMENTATION for the exact evidence behind each entry.
    Nothing here is inferred from this run's own reads -- IPMI capability
    entries are proven regardless of whether a particular field happened to
    read cleanly this time (that is what RV(operation.ipmi_reads) is for).
    """
    return {
        "ipmi_power": {
            "supported": True,
            "proven": True,
            "note": "Verified live against the target board: get_power() returned {'powerstate': 'on'}.",
        },
        "ipmi_boot_device": {
            "supported": True,
            "proven": True,
            "note": "Verified live against the target board: get_bootdev() returned a real override dict.",
        },
        "ipmi_mc_info": {
            "supported": True,
            "proven": True,
            "note": "Verified live: get_mci() returns a bare string, not a dict -- see module_utils/ipmi.py.",
        },
        "web_management": {
            "supported": True,
            "proven": bool(include_web_session and web_management is not None),
            "note": (
                "Proven for this run only when include_web_session=true and the login succeeded; not attempted otherwise."
                if not include_web_session
                else "Login succeeded this run."
            ),
        },
        "virtual_media": {
            "supported": None,
            "proven": False,
            "note": "Not yet proven against this hardware. A null (not false) 'supported' value means unknown, not unsupported.",
        },
        "remote_console": {
            "supported": None,
            "proven": False,
            "note": "Not yet proven against this hardware. A null (not false) 'supported' value means unknown, not unsupported.",
        },
        "redfish": {
            "supported": False,
            "proven": True,
            "note": "ASPEED AST2400 (this board's generation) predates Redfish support, which arrived with AST2500/ASMB9.",
        },
    }


def main() -> None:
    module = AnsibleModule(argument_spec=argument_spec(), supports_check_mode=True)

    if not HAS_PYGHMI:
        module.fail_json(msg=missing_required_lib("pyghmi"), exception=PYGHMI_IMPORT_ERROR)
        return
    if module.params["include_web_session"] and not HAS_REQUESTS:
        module.fail_json(msg=missing_required_lib("requests"), exception=REQUESTS_IMPORT_ERROR)
        return

    include_web_session = module.params["include_web_session"]

    try:
        client = build_ipmi_client(module.params)
        ipmi_facts, ipmi_reads = gather_ipmi_facts(client)

        web_management = gather_web_management_facts(module.params) if include_web_session else None
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    asmb8 = {
        "reachable": True,
        "ipmi": ipmi_facts,
        "web_management": web_management,
        "capabilities": build_capabilities(web_management=web_management, include_web_session=include_web_session),
        "media": {"port_mode": "unknown"},
    }

    receipt = OperationReceipt(
        action="get_facts",
        endpoint=client.endpoint,
        changed=False,
        extra={"ipmi_reads": ipmi_reads},
    )
    module.exit_json(changed=False, asmb8=asmb8, operation=receipt.to_dict())


if __name__ == "__main__":
    main()
