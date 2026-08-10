#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_sessions
short_description: Report ASMB8-iKVM per-service session capacity and remote-session configuration
description:
  - >-
    Reads this BMC's per-service session/port configuration (C(getallservicescfg.asp)) and its
    KVM/media remote-session configuration (C(getremotesession.asp)). Both are read-only C(.asp)
    RPCs; every field name and shape below is sourced from the real, redacted capture corpus under
    C(tests/unit/fixtures/asp/) -- AMI has not published a specification for this surface (see
    C(plugins/module_utils/asp.py)'s module docstring).
  - >-
    B(A directory of currently-active sessions is not implemented here, and this is a known,
    reported gap, not an oversight.) The corpus's C(getsessioninfo.asp) capture was made with a
    B(POST) request, not a C(GET) -- unlike every other endpoint this module (and its siblings
    M(james_crowley.asmb8_ikvm.asmb8_users)/M(james_crowley.asmb8_ikvm.asmb8_network)) reads.
    C(plugins/module_utils/asp.py)'s C(AspClient.get_webvar) is deliberately, permanently
    C(GET)-only -- see its own docstring's warning against exactly the kind of "just one POST
    through the read path" escape hatch this module would need to read C(getsessioninfo.asp), and
    this module does not build one. RV(active_sessions) is therefore always V(null); reading it for
    real needs a properly-named, POST-capable client method (e.g. C(AspClient.get_session_info()))
    added to C(asp.py) first, which this module's author flags as follow-up work rather than
    attempting to invent.
  - >-
    B(C(getremotesession.asp)'s live behaviour is unverified, and RV(remote_session) may
    legitimately be V(null) even immediately after a successful login.) This corpus's fixture for
    it parses cleanly, so this module's parsing of that shape is exercised and correct -- but
    fetching it live, from a programmatic client, has been observed to return a session-expired
    HTML page even with a session that had just been freshly authenticated (the same flow works
    from a browser). Why is not yet understood -- something beyond the plain session cookie appears
    to be required and has not been identified. This module therefore treats a parse failure on
    this one endpoint as an expected, non-fatal outcome (see RV(remote_session_read)) rather than
    failing the whole run over it, and this description is the honest statement that RV(remote_session)
    working at all, on any given run, is unverified rather than guaranteed.
  - >-
    B(This module logs in), for the same reason M(james_crowley.asmb8_ikvm.asmb8_users) does: every
    endpoint it can actually read requires an authenticated C(.asp) session. See
    O(ignore:check_mode) below for how this module avoids spending that session in a mode where
    nothing is going to be changed anyway.
  - >-
    B(Session counts are +128-offset-encoded, and this module decodes them -- with the evidence for
    doing so, not a guess.) C(getallservicescfg.asp)'s C(MAXSESS)/C(CURSESS) fields are not raw
    session counts: the C(web) service's C(MAXSESS) reads V(148) in this corpus, and
    C(plugins/module_utils/errors.py)'s C(ErrorClass.BMC_BUSY) docstring independently documents
    this board's measured concurrent-session cap for that same service as B(20) -- V(148) B(-)
    V(128) C(=) V(20). C(cd-media)'s C(MAXSESS) reads V(129) in this corpus, and
    M(james_crowley.asmb8_ikvm.asmb8_redirection)'s own service catalog independently documents
    that service's capacity as exactly B(one) concurrent session -- V(129) B(-) V(128) C(=) V(1).
    Two independent measurements, two independent confirmations of the same B(+128) offset; this
    module applies it to every service's C(MAXSESS)/C(CURSESS) rather than reporting the raw,
    meaningless-looking V(148)/V(129). The one exception is the literal raw value V(255)
    (C(ssh)/C(telnet) in this corpus): decoding it as V(255) B(-) V(128) C(=) V(127) sessions is not
    plausible for those services and contradicts M(james_crowley.asmb8_ikvm.asmb8_redirection)'s own
    catalog, which reports no session cap at all for either -- V(255) is therefore treated as a
    distinct "not applicable" sentinel, decoding to V(null), not to V(127).
  - >-
    No write capability exists here, deliberately. This module reports session capacity and
    remote-session configuration only; it does not attempt to terminate a session or change any of
    C(getremotesession.asp)'s settings (KVM/media encryption, single-port mode, host lock, and so
    on) -- a mistaken change to host-lock or encryption settings here could strand an operator
    mid-session with no independent path back in. This module only reads.
version_added: 0.1.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_info
  - module: james_crowley.asmb8_ikvm.asmb8_users
  - module: james_crowley.asmb8_ikvm.asmb8_network
  - module: james_crowley.asmb8_ikvm.asmb8_redirection
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
- name: Report per-service session capacity and remote-session configuration
  james_crowley.asmb8_ikvm.asmb8_sessions:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
  delegate_to: localhost
  no_log: true
  register: sessions_report

- name: Assert the web service's decoded session cap matches this collection's measured 20-session limit
  ansible.builtin.assert:
    that:
      - sessions_report.services.web.sessions.max == 20

- name: A null remote_session is an expected, documented gap -- not a failure
  ansible.builtin.debug:
    msg: "remote session config was not readable this run (read outcome: {{ sessions_report.remote_session_read.outcome }})"
  when: sessions_report.remote_session is none

- name: active_sessions is always null -- see the module description for why
  ansible.builtin.assert:
    that:
      - sessions_report.active_sessions is none
"""

RETURN = r"""
changed:
  description: Always V(false) -- this module never mutates BMC state. See the module description.
  type: bool
  returned: always
services:
  description: >-
    Per-service session/port configuration from C(getallservicescfg.asp), keyed by C(SERVICENAME)
    exactly as the BMC reports it (e.g. V(web), V(kvm), V(cd-media)) -- the same naming
    M(james_crowley.asmb8_ikvm.asmb8_redirection) uses for its own, differently-sourced (static
    catalog, not a live read) service report.
  type: dict
  returned: always
  contains:
    id:
      description: Raw C(SERVICEID).
      type: int
    name:
      description: Raw C(SERVICENAME) -- the same string used as this dict's own key.
      type: str
    enabled:
      description: Whether C(STATE) is set.
      type: bool
    interface_scope_raw:
      description: >-
        Raw C(IFCNAME) (e.g. V(both), or the literal string V(FFFFFFFFFFFFFFFF) observed for
        C(ssh)/C(telnet) in this corpus). Not decoded -- no sourced meaning for that sentinel value
        was found.
      type: str
    port:
      description: This service's plaintext and secure TCP ports.
      type: dict
      contains:
        plain:
          description: >-
            Raw C(NSPORT), or V(null) if this service reports the C(4294967295) (C(0xFFFFFFFF))
            "not applicable" sentinel (e.g. C(ssh)'s plaintext port).
          type: int
        secure:
          description: Raw C(SECPORT), or V(null) for the same sentinel (e.g. C(telnet)'s secure port).
          type: int
    timeout_seconds:
      description: >-
        Raw C(SERVICE_TIMEOUT), or V(null) if this service reports the same V(4294967295) sentinel
        (observed for the media services, which have no server-side inactivity timeout).
      type: int
    sessions:
      description: This service's session capacity, decoded per the module description's cited evidence.
      type: dict
      contains:
        max:
          description: >-
            Decoded C(MAXSESS) (raw value minus V(128)), or V(null) if C(MAXSESS) is the raw
            sentinel V(255) (observed for C(ssh)/C(telnet), which this module treats as "no
            reported cap" rather than decoding to V(127) -- see the module description).
          type: int
        current:
          description: Decoded C(CURSESS), with the same V(255)-sentinel handling as RV(services[].sessions.max).
          type: int
    single_port_status_raw:
      description: Raw C(SINGLEPORT_STATUS).
      type: int
remote_session:
  description: >-
    Decoded C(getremotesession.asp) configuration, or V(null) if that endpoint's response could not
    be parsed this run -- see the module description and RV(remote_session_read) for why that is an
    expected, documented possibility rather than necessarily a fault.
  type: dict
  returned: always
  contains:
    kvm_encryption_enabled:
      description: Whether C(KVMENCRYPTION) is set.
      type: bool
    media_encryption_enabled:
      description: Whether C(MEDIAENCRYPTION) is set.
      type: bool
    single_port_enabled:
      description: Whether C(SINGLEPORT) is set.
      type: bool
    keyboard_language:
      description: Raw C(KEYBOARDLANG) (e.g. V(AD)).
      type: str
    local_media_enabled:
      description: Whether C(LMEDIAENABLE) is set.
      type: bool
    remote_media_enabled:
      description: Whether C(RMEDIAENABLE) is set.
      type: bool
    vmedia_attach_raw:
      description: Raw C(VMEDIAATTACH).
      type: int
    host_lock_enabled:
      description: Whether C(HOSTLOCK) is set.
      type: bool
    host_lock_auto_enabled:
      description: Whether C(HOSTLOCKAUTO) is set.
      type: bool
    sd_card_status_raw:
      description: Raw C(SDCARD_STATUS).
      type: int
remote_session_read:
  description: >-
    The outcome of attempting to read RV(remote_session), for the same "tell a null value apart
    from a failed read" reason as M(james_crowley.asmb8_ikvm.asmb8_info)'s C(operation.ipmi_reads).
  type: dict
  returned: always
  contains:
    outcome:
      description: V(read) if RV(remote_session) was populated, V(failed) if the read did not parse.
      type: str
      choices: [read, failed]
    error_class:
      description: The failure class, on V(failed) only; V(null) otherwise.
      type: str
active_sessions:
  description: >-
    Always V(null). A live directory of active sessions would come from C(getsessioninfo.asp), but
    that endpoint's real invocation requires C(POST) and C(AspClient.get_webvar) is deliberately
    C(GET)-only -- see the module description for exactly what would need to be added before this
    can be implemented.
  type: list
  elements: dict
  returned: always
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
      description: Always V(asmb8_sessions.report).
      type: str
    endpoint:
      description: The C(host:port) this read was performed against.
      type: str
    changed:
      description: Always V(false).
      type: bool
    observed:
      description: Mirrors RV(services) and RV(remote_session) together.
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

#: Confirmed against two independent measurements -- see this module's DOCUMENTATION for both:
#: web's MAXSESS (148) against errors.py's ErrorClass.BMC_BUSY-documented 20-session cap, and
#: cd-media's MAXSESS (129) against asmb8_redirection.SERVICE_CATALOG's 1-session capacity.
_SESSION_COUNT_OFFSET = 128

#: The raw MAXSESS/CURSESS value observed for ssh/telnet in this corpus. Decoding it with the
#: offset above (255 - 128 = 127) would contradict asmb8_redirection.SERVICE_CATALOG, which reports
#: no session cap at all for either service -- so this is treated as its own "not applicable"
#: sentinel, decoding to None, not to a number. See the module description.
_SESSION_COUNT_SENTINEL = 255

#: The 0xFFFFFFFF "field not applicable" sentinel observed on NSPORT/SECPORT/SERVICE_TIMEOUT for
#: services that do not have that particular port/timeout (e.g. ssh's plaintext port, telnet's
#: secure port, the media services' inactivity timeout).
_UINT32_NOT_APPLICABLE = 4294967295


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


def decode_session_count(raw: Any) -> int | None:
    """Decode one MAXSESS/CURSESS value. See :data:`_SESSION_COUNT_OFFSET`/:data:`_SESSION_COUNT_SENTINEL`."""
    if raw is None:
        return None
    if raw == _SESSION_COUNT_SENTINEL:
        return None
    return raw - _SESSION_COUNT_OFFSET


def decode_uint32_or_none(raw: Any) -> int | None:
    """Decode one NSPORT/SECPORT/SERVICE_TIMEOUT value. See :data:`_UINT32_NOT_APPLICABLE`."""
    if raw == _UINT32_NOT_APPLICABLE:
        return None
    return raw


def decode_service(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("SERVICEID"),
        "name": record.get("SERVICENAME"),
        "enabled": bool(record.get("STATE")),
        "interface_scope_raw": record.get("IFCNAME"),
        "port": {
            "plain": decode_uint32_or_none(record.get("NSPORT")),
            "secure": decode_uint32_or_none(record.get("SECPORT")),
        },
        "timeout_seconds": decode_uint32_or_none(record.get("SERVICE_TIMEOUT")),
        "sessions": {
            "max": decode_session_count(record.get("MAXSESS")),
            "current": decode_session_count(record.get("CURSESS")),
        },
        "single_port_status_raw": record.get("SINGLEPORT_STATUS"),
    }


def build_services_report(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = {}
    for record in records:
        name = record.get("SERVICENAME")
        if not name:
            continue
        services[name] = decode_service(record)
    return services


def decode_remote_session_config(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "kvm_encryption_enabled": bool(record.get("KVMENCRYPTION")),
        "media_encryption_enabled": bool(record.get("MEDIAENCRYPTION")),
        "single_port_enabled": bool(record.get("SINGLEPORT")),
        "keyboard_language": record.get("KEYBOARDLANG"),
        "local_media_enabled": bool(record.get("LMEDIAENABLE")),
        "remote_media_enabled": bool(record.get("RMEDIAENABLE")),
        "vmedia_attach_raw": record.get("VMEDIAATTACH"),
        "host_lock_enabled": bool(record.get("HOSTLOCK")),
        "host_lock_auto_enabled": bool(record.get("HOSTLOCKAUTO")),
        "sd_card_status_raw": record.get("SDCARD_STATUS"),
    }


def fetch_remote_session_config(asp_client: AspClient) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Read ``getremotesession.asp``, degrading an unparseable response to ``None`` rather than failing the module.

    See the module description: a session-expired-looking response from this specific endpoint has
    been observed even immediately after a fresh login, for reasons not yet identified. Only
    :class:`errors.ProtocolError` is degraded here -- a connection/authentication/timeout failure
    at this point is a real problem with the run and is allowed to propagate and fail the module,
    the same as a failure reading any other endpoint.
    """
    try:
        response = asp_client.get_webvar("getremotesession")
    except ProtocolError as err:
        return None, {"outcome": "failed", "error_class": err.error_class}

    if not response.records:
        return None, {"outcome": "failed", "error_class": None}

    return decode_remote_session_config(response.records[0]), {"outcome": "read", "error_class": None}


def gather_report(asp_client: AspClient) -> dict[str, Any]:
    """Log in and read what this module can. The only place this module's login happens."""
    asp_client.login()
    services_response = asp_client.get_webvar("getallservicescfg")
    remote_session, remote_session_read = fetch_remote_session_config(asp_client)

    return {
        "services": build_services_report(services_response.records),
        "remote_session": remote_session,
        "remote_session_read": remote_session_read,
        "active_sessions": None,
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
        receipt = OperationReceipt(action="asmb8_sessions.report", endpoint=endpoint, changed=False, observed=None)
        module.exit_json(
            changed=False,
            services=None,
            remote_session=None,
            remote_session_read=None,
            active_sessions=None,
            operation=receipt.to_dict(),
        )
        return

    try:
        asp_client = build_asp_client(params)
        report = gather_report(asp_client)
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    receipt = OperationReceipt(
        action="asmb8_sessions.report",
        endpoint=asp_client.endpoint,
        changed=False,
        observed={"services": report["services"], "remote_session": report["remote_session"]},
    )
    module.exit_json(changed=False, **report, operation=receipt.to_dict())


if __name__ == "__main__":
    main()
