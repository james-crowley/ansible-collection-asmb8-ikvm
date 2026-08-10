#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_http_origin
short_description: Run (or stop) an ephemeral, lifetime-capped local HTTP file server
description:
  - >-
    Serves one local directory over plain HTTP for exactly as long as a play needs it, so an
    installer or bootloader that only speaks HTTP -- not this collection's native iUSB path (see
    M(james_crowley.asmb8_ikvm.asmb8_media)) -- can fetch its files with B(no standing
    infrastructure): no PXE, DHCP, TFTP, NFS or CIFS service is started, and none is left behind.
    This is this collection's one deliberate exception to that "nothing left behind" rule, and
    every design choice below exists to make the exception as narrow and self-limiting as
    possible.
  - >-
    Shaped exactly like O(state=attached)/O(state=detached) on
    M(james_crowley.asmb8_ikvm.asmb8_media): O(state=started) forks a detached background process
    that owns the listening socket, writes a small JSON state file keyed by O(session_id) under
    O(runtime_dir), and returns once that process has reported either V(serving) or an early
    failure (bounded by O(start_timeout)). O(state=stopped) looks that process up by the pid
    recorded in the state file, asks it to stop, and waits (bounded by O(stop_timeout)) for it to
    actually exit. Calling O(state=started) again with an O(session_id) that already names a live
    session is idempotent (C(changed=false)); calling O(state=stopped) when there is nothing live
    to stop is likewise C(changed=false), not an error.
  - >-
    B(The hard lifetime cap, O(lifetime_seconds), is this module's primary safety property, not an
    optional extra.) A play that starts this server and then crashes, is interrupted, or loses its
    controller to a power failure leaves nothing behind to ask the background process to stop --
    so the background process asks itself. It records a deadline at start time and self-terminates
    once it passes, deleting nothing it does not need to and depending on nothing outside itself
    (not the controller, not the play, not O(state=stopped) ever being called) still being alive
    to enforce it. Raise O(lifetime_seconds) for an install known to run long; do not disable the
    cap, because there is no option to -- that omission is deliberate.
  - >-
    B(Path confinement is enforced on every single request), independently of what this module was
    told to serve: a request naming a C(..) segment (spelled literally, as a single
    percent-encoded C(%2e%2e), or hidden behind a percent-encoded separator), a request that
    resolves through a symlink to somewhere outside O(path), and a double-percent-encoded segment
    (C(%252e%252e), which a naive double-decoding server would turn into C(..)) are all refused --
    a 404 or 416 is returned and the file is never served. See
    C(module_utils/http_origin.py)'s C(resolve_within_root) for the exact mechanism and why
    decoding percent-encoding exactly once, not in a loop, is what defeats the double-encoding
    case rather than falling for it.
  - >-
    Every request this server receives is appended, as one JSON-lines record per request, to
    C(<runtime_dir>/<session_id>-access.log) -- method, the raw requested path, the HTTP status
    returned, a machine-readable outcome (V(ok), V(not_found), V(blocked_traversal),
    V(range_not_satisfiable), V(client_disconnected), or V(error)), bytes actually sent, and the
    requesting client's address. This is real diagnostic value, not incidental logging: an install
    that failed because the installer requested a file this server never served (a typo'd path, a
    missing companion file, an unexpected extra fetch) is visible in this one file, distinguishing
    "the installer never asked for it" from "we refused to serve it" from "we served it and the
    installer still failed for some other reason." The background process's own crash/traceback
    output, which is a wholly separate concern, goes to C(<runtime_dir>/<session_id>.log> instead
    -- see O(runtime_dir).
  - >-
    Supports C(GET) and C(HEAD), and honours C(Range) requests with a correct C(206 Partial
    Content) response (or C(416 Range Not Satisfiable) for a range past the end of the file).
    Bootloaders and installers commonly issue ranged reads while resuming or verifying a transfer;
    a server that ignores C(Range) and always returns C(200) with the full body silently corrupts
    that kind of fetch instead of failing it loudly.
  - >-
    Directory listings are never generated. A request that resolves to a directory, rather than a
    file, inside O(path) is refused (C(404)) rather than served as an index -- the files an
    installer needs are expected to be fetched by their own exact paths under O(path), not
    discovered by browsing.
version_added: 0.3.0
author:
  - Jim Crowley (@james-crowley)
options:
  state:
    description:
      - >-
        V(started) starts (or, if O(session_id) already names a live session, confirms) a
        background HTTP server rooted at O(path). V(stopped) stops a previously started session.
    type: str
    required: true
    choices: [started, stopped]
  path:
    description:
      - Directory, on the Ansible controller, to serve over HTTP.
      - Required for O(state=started). Ignored for O(state=stopped).
      - >-
        Served read-only. Every file under this directory (recursively) is reachable by its own
        relative path; nothing outside it is, however the request path is spelled -- see the
        module description's path-confinement paragraph.
    type: path
  session_id:
    description:
      - >-
        Identifies the background session across separate module invocations. Required for
        O(state=stopped), so a caller can only ever stop a session it can name. Optional for
        O(state=started) -- when omitted, a fresh id is generated and returned in RV(session_id);
        callers that need to stop it later must capture and reuse that value.
    type: str
  runtime_dir:
    description:
      - >-
        Directory holding one JSON state file per O(session_id), plus that session's two log
        files: C(<session_id>.log) (the background process's own stdout/stderr, for crash
        diagnosis) and C(<session_id>-access.log) (the structured per-request log described in the
        module description). Must be the same path across the O(state=started) call and the later
        O(state=stopped) call for the same session.
      - Created (mode C(0700)) if it does not already exist.
    type: path
    default: ~/.ansible/asmb8_ikvm/http-origins
  bind_address:
    description:
      - >-
        Local address the HTTP server listens on. Defaults to V(127.0.0.1) -- loopback, not every
        interface -- deliberately: this server has no authentication of any kind, so the address
        it listens on is the only thing standing between "reachable by the machine being
        provisioned" and "reachable by anything else on the same management VLAN". Defaulting to
        loopback makes the unattended failure mode "nothing outside this machine can reach it,
        and the install visibly cannot fetch anything" rather than "answering on the network with
        nobody having decided it should".
      - >-
        The machine being provisioned is essentially never the controller itself, so a real,
        working play needs to set this explicitly to an address the target can actually reach --
        typically the controller's own address on the network segment the target boots on. This
        option does not validate that the address you give it is reachable from anywhere in
        particular; that is on the caller to get right for their topology.
    type: str
    default: 127.0.0.1
  port:
    description:
      - >-
        TCP port to listen on. V(0) (the default) asks the operating system to pick a free
        ephemeral port; the port actually bound is always reported back in RV(port) and as part of
        RV(url), regardless of whether O(port) was V(0) or an explicit value.
    type: int
    default: 0
  lifetime_seconds:
    description:
      - >-
        Hard cap, in seconds, on how long the background server may run before it terminates
        itself, regardless of whether O(state=stopped) is ever called for it. See the module
        description's paragraph on why this is this module's primary safety property. The default
        is generous enough to outlast a legitimately slow unattended install several times over
        (see M(james_crowley.asmb8_ikvm.asmb8_media)'s own note that an attached media session
        idly waiting on installer input is normal and can run for an hour or more) while still
        being a real, finite backstop -- raise it explicitly for an install known to need longer;
        there is no way to disable it.
      - Ignored for O(state=stopped).
    type: int
    default: 14400
  start_timeout:
    description:
      - >-
        Bounded number of seconds O(state=started) waits for the background process to report
        V(serving) or an early failure (most commonly the requested O(port) already being in use)
        before returning.
      - >-
        Expiring without a V(serving) report is a failure, with RV(ignore:error_class) V(timeout).
        The background process is not torn down in that case, because it may simply be slow to
        start and about to succeed; re-run this module with the same O(session_id), which is
        idempotent, to re-probe it.
    type: int
    default: 10
  stop_timeout:
    description:
      - >-
        Bounded number of seconds O(state=stopped) waits for the background process to actually
        exit after being asked to stop, before returning anyway. Exceeding this is reported as a
        warning, not a failure.
    type: int
    default: 15
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_media
attributes:
  check_mode:
    description: >-
      Supported. Validates options and, for O(state=started), resolves O(path) and confirms it is
      a readable directory, but never binds a socket, never forks the background process, and
      never contacts anything on the network. For O(state=stopped), reports whether a live session
      would be stopped, but never signals it.
    support: full
  diff_mode:
    description: Not supported. Use RV(session_state) and the C(operation) receipt instead.
    support: none
"""

EXAMPLES = r"""
- name: Serve a netboot image set for the duration of this play
  james_crowley.asmb8_ikvm.asmb8_http_origin:
    path: /srv/netboot/proxmox-auto
    bind_address: 192.0.2.5 # the controller's address on the target's boot network
    state: started
  delegate_to: localhost
  register: origin

- name: Point the installer's kernel command line at the origin this play just started
  ansible.builtin.debug:
    msg: "Fetch installer files from {{ origin.url }}"

- name: Poll the same session id later in the play
  james_crowley.asmb8_ikvm.asmb8_http_origin:
    path: /srv/netboot/proxmox-auto
    session_id: "{{ origin.session_id }}"
    state: started
  delegate_to: localhost
  register: origin_status

- name: Tear the origin down once the install has finished fetching everything it needs
  james_crowley.asmb8_ikvm.asmb8_http_origin:
    session_id: "{{ origin.session_id }}"
    state: stopped
  delegate_to: localhost

- name: A repeated stop is a no-op, not a failure
  james_crowley.asmb8_ikvm.asmb8_http_origin:
    session_id: "{{ origin.session_id }}"
    state: stopped
  delegate_to: localhost
  register: second_stop
  failed_when: second_stop.changed
"""

RETURN = r"""
changed:
  description: >-
    For O(state=started): V(true) only when a new background process was actually forked (or, in
    check mode, would be). V(false) when an already-live session for O(session_id) was found and
    confirmed instead. For O(state=stopped): V(true) only when a live process was actually asked
    to stop (or, in check mode, would be); V(false) when there was nothing live to stop.
  type: bool
  returned: always
session_id:
  description: The session id in effect -- generated for O(state=started) when not supplied.
  type: str
  returned: always
session_state:
  description:
    - >-
      The last state the background process reported: V(starting) (forked, not yet listening),
      V(serving), V(stopped), or V(error). V(unknown) if O(state=stopped) found no state file at
      all.
  type: str
  returned: always
pid:
  description: Process id of the background session, when one is recorded. V(null) if none is.
  type: int
  returned: when available
url:
  description: >-
    The base URL files under O(path) are reachable at, e.g. V(http://192.0.2.5:8080/), built from
    O(bind_address) and the actually-bound RV(port). V(null) until the background process reports
    V(serving).
  type: str
  returned: when available
port:
  description: The TCP port actually bound -- the real value even when O(port) was V(0).
  type: int
  returned: when available
root:
  description: Resolved, absolute form of O(path) as the background process is actually serving it.
  type: str
  returned: when available
request_count:
  description: Total HTTP requests served (or refused) so far, mirroring C(operation.observed.request_count).
  type: int
  returned: always
bytes_served:
  description: Total response body bytes sent so far, mirroring C(operation.observed.bytes_served).
  type: int
  returned: always
recovered_stale_session:
  description: >-
    V(true) when a stale state file (recorded pid no longer running) for O(session_id) was found
    and discarded by this call.
  type: bool
  returned: when a stale session was recovered
access_log:
  description: Path to the per-request JSON-lines log described in the module description.
  type: str
  returned: when available
error:
  description: The background process's own error message, when RV(session_state) is V(error).
  type: str
  returned: when session_state is error
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
      description: One of V(asmb8_http_origin.start) or V(asmb8_http_origin.stop).
      type: str
    endpoint:
      description: The C(bind_address:port) this session listens (or listened) on.
      type: str
    changed:
      description: Mirrors the top-level RV(changed).
      type: bool
    previous:
      description: The session state as read before this call, or V(null) when none existed.
      type: dict
    desired:
      description: V(serving) or V(stopped), whichever this call requested.
      type: str
    observed:
      description: >-
        The session state as read after this call. V(null) in check mode, since nothing was
        actually started (or stopped) to observe.
      type: dict
      contains:
        session_id:
          description: Same value as RV(session_id).
          type: str
        pid:
          description: Same value as RV(pid).
          type: int
        state:
          description: Same value as RV(session_state).
          type: str
        root:
          description: Same value as RV(root).
          type: str
        url:
          description: Same value as RV(url).
          type: str
        port:
          description: Same value as RV(port).
          type: int
        request_count:
          description: Same value as RV(request_count).
          type: int
        bytes_served:
          description: Same value as RV(bytes_served).
          type: int
        last_request_at:
          description: Controller-clock ISO-8601 timestamp of the last request served, or V(null).
          type: str
        started_at:
          description: Controller-clock ISO-8601 timestamp of when the background process started.
          type: str
        stop_reason:
          description: >-
            Why a stopped session actually stopped: V(signal) (asked to, via O(state=stopped) or an
            external SIGTERM) or V(lifetime_expired) (the O(lifetime_seconds) cap elapsed with
            nobody asking). V(null) while still V(serving).
          type: str
        error:
          description: Same value as RV(error).
          type: str
        error_class:
          description: A stable machine-readable failure class for this session, or V(null).
          type: str
    error_class:
      description: A stable machine-readable failure class. V(null) on success.
      type: str
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import http_origin
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import ErrorClass, IkvmError, ProtocolError, TimeoutError_
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt


def _argument_spec() -> dict:
    return {
        "state": {"type": "str", "required": True, "choices": ["started", "stopped"]},
        "path": {"type": "path"},
        "session_id": {"type": "str"},
        "runtime_dir": {"type": "path", "default": http_origin.DEFAULT_RUNTIME_DIR},
        "bind_address": {"type": "str", "default": http_origin.DEFAULT_BIND_ADDRESS},
        "port": {"type": "int", "default": http_origin.DEFAULT_PORT},
        "lifetime_seconds": {"type": "int", "default": http_origin.DEFAULT_LIFETIME_SECONDS},
        "start_timeout": {"type": "int", "default": 10},
        "stop_timeout": {"type": "int", "default": 15},
    }


def build_session_config(params: dict, *, session_id: str, root: str) -> http_origin.SessionConfig:
    return http_origin.SessionConfig(
        session_id=session_id,
        root=root,
        bind_address=params["bind_address"],
        port=params["port"],
        lifetime_seconds=params["lifetime_seconds"],
        runtime_dir=params["runtime_dir"],
    )


def _finalize(receipt: OperationReceipt, *, session_id: str, fields: dict, **extra) -> dict:
    """Assemble the module result: module-specific keys at the top level, the receipt nested.

    Mirrors ``asmb8_media.py``'s helper of the same name and its same reasoning: receipt fields
    are never duplicated at the top level outside of the handful this RETURN documents explicitly.
    """
    return {
        "changed": receipt.changed,
        "session_id": session_id,
        "session_state": fields["session_state"],
        "pid": fields["pid"],
        "url": fields["url"],
        "port": fields["port"],
        "root": fields["root"],
        "request_count": fields["request_count"],
        "bytes_served": fields["bytes_served"],
        "error": fields["error"],
        "operation": receipt.to_dict(),
        **extra,
    }


def _status_fields(state: dict | None) -> dict:
    state = state or {}
    return {
        "session_state": state.get("state", "unknown"),
        "pid": state.get("pid"),
        "url": state.get("url"),
        "port": state.get("port"),
        "root": state.get("root"),
        "request_count": state.get("request_count", 0),
        "bytes_served": state.get("bytes_served", 0),
        "last_request_at": state.get("last_request_at"),
        "started_at": state.get("started_at"),
        "stop_reason": state.get("stop_reason"),
        "error": state.get("error"),
    }


def _error_class_of(state: dict | None) -> str:
    return (state or {}).get("error_class") or ErrorClass.PROTOCOL


def _endpoint_of(params: dict) -> str:
    return f"{params['bind_address']}:{params['port']}"


def _start(module: AnsibleModule, params: dict) -> dict:
    session_id = params.get("session_id") or http_origin.generate_session_id()
    runtime_dir = params["runtime_dir"]

    existing = http_origin.read_state(runtime_dir, session_id)
    if existing is not None and http_origin.is_pid_alive(existing.get("pid")) and existing.get("state") not in http_origin.TERMINAL_STATES:
        # Idempotent: a live session already answers to this id.
        fields = _status_fields(existing)
        endpoint = f"{existing.get('bind_address', params['bind_address'])}:{existing.get('port') or params['port']}"
        receipt = OperationReceipt(action="asmb8_http_origin.start", endpoint=endpoint, changed=False, previous=existing, desired=None, observed=existing)
        return _finalize(receipt, session_id=session_id, fields=fields, access_log=str(http_origin.access_log_file_path(runtime_dir, session_id)))

    recovered_stale = existing is not None
    if recovered_stale:
        http_origin.remove_state(runtime_dir, session_id)

    if not params.get("path"):
        raise ProtocolError("asmb8_http_origin state=started requires path to be set", operation="asmb8_http_origin.start")

    root = http_origin.validate_root(params["path"])
    endpoint = _endpoint_of(params)

    if module.check_mode:
        fields = {
            "session_state": "starting",
            "pid": None,
            "url": None,
            "port": None,
            "root": str(root),
            "request_count": 0,
            "bytes_served": 0,
            "error": None,
        }
        receipt = OperationReceipt(action="asmb8_http_origin.start", endpoint=endpoint, changed=True, previous=existing, desired="serving", observed=None)
        return _finalize(
            receipt,
            session_id=session_id,
            fields=fields,
            recovered_stale_session=recovered_stale,
            access_log=str(http_origin.access_log_file_path(runtime_dir, session_id)),
        )

    config = build_session_config(params, session_id=session_id, root=str(root))
    http_origin.spawn_session(config)

    observed = http_origin.wait_for_state(
        runtime_dir,
        session_id,
        until=lambda s: s.get("state") in (http_origin.STATE_SERVING, http_origin.STATE_ERROR, http_origin.STATE_STOPPED),
        timeout=float(params["start_timeout"]),
    )
    fields = _status_fields(observed)

    if fields["session_state"] == http_origin.STATE_ERROR:
        module.fail_json(
            msg=f"asmb8_http_origin start failed: {fields['error']}",
            error_class=_error_class_of(observed),
            session_id=session_id,
            **fields,
        )

    if fields["session_state"] != http_origin.STATE_SERVING:
        pid = (observed or {}).get("pid")
        if not http_origin.is_pid_alive(pid):
            final = http_origin.read_state(runtime_dir, session_id) or observed
            final_fields = _status_fields(final)
            reported = final_fields.get("error") or fields.get("error")
            module.fail_json(
                msg=(
                    f"asmb8_http_origin start failed: {reported}"
                    if reported
                    else (
                        "asmb8_http_origin start failed: the background process exited without reporting "
                        f"'serving' (last observed state {final_fields['session_state']!r})."
                    )
                ),
                error_class=_error_class_of(final),
                session_id=session_id,
                **final_fields,
            )

        if fields["session_state"] in http_origin.TERMINAL_STATES:
            reported = fields.get("error")
            module.fail_json(
                msg=(
                    f"asmb8_http_origin start failed: the session reached terminal state "
                    f"{fields['session_state']!r} without ever reporting 'serving'" + (f": {reported}" if reported else "")
                ),
                error_class=_error_class_of(observed),
                session_id=session_id,
                **fields,
            )

        # No verdict at all: still 'starting' with the daemon running. Indeterminate timeout, per
        # the module's own documented contract for start_timeout -- re-probe, do not retry.
        err = TimeoutError_(
            f"asmb8_http_origin start did not confirm within start_timeout={params['start_timeout']}s: the "
            f"background process (pid {fields['pid']}) is still running but its last reported state is "
            f"{fields['session_state']!r}, not 'serving'. The process was not stopped and is still running as "
            f"session_id {session_id!r}. Re-probe it with another state=started call for that same session_id "
            "rather than retrying -- this is idempotent -- and raise start_timeout if slow starts are normal "
            "here.",
            endpoint=endpoint,
            indeterminate=True,
        )
        module.fail_json(**err.to_result(), session_id=session_id, **fields)

    endpoint = f"{params['bind_address']}:{fields['port']}"
    receipt = OperationReceipt(action="asmb8_http_origin.start", endpoint=endpoint, changed=True, previous=existing, desired="serving", observed=observed)
    return _finalize(
        receipt,
        session_id=session_id,
        fields=fields,
        recovered_stale_session=recovered_stale,
        access_log=str(http_origin.access_log_file_path(runtime_dir, session_id)),
    )


def _stop(module: AnsibleModule, params: dict) -> dict:
    session_id = params["session_id"]
    runtime_dir = params["runtime_dir"]

    existing = http_origin.read_state(runtime_dir, session_id)
    if existing is None:
        fields = _status_fields(None)
        receipt = OperationReceipt(action="asmb8_http_origin.stop", endpoint="unknown", changed=False, previous=None, desired="stopped", observed=None)
        return _finalize(receipt, session_id=session_id, fields=fields)

    endpoint = f"{existing.get('bind_address', 'unknown')}:{existing.get('port') or 'unknown'}"
    pid = existing.get("pid")
    live = http_origin.is_pid_alive(pid)

    if module.check_mode:
        fields = _status_fields(existing)
        receipt = OperationReceipt(action="asmb8_http_origin.stop", endpoint=endpoint, changed=live, previous=existing, desired="stopped", observed=existing)
        return _finalize(receipt, session_id=session_id, fields=fields)

    if not live:
        http_origin.remove_state(runtime_dir, session_id)
        fields = _status_fields(existing)
        receipt = OperationReceipt(action="asmb8_http_origin.stop", endpoint=endpoint, changed=False, previous=existing, desired="stopped", observed=existing)
        return _finalize(receipt, session_id=session_id, fields=fields, recovered_stale_session=True)

    http_origin.request_stop(pid)
    exited = http_origin.wait_for_exit(pid, timeout=float(params["stop_timeout"]))
    final_state = http_origin.read_state(runtime_dir, session_id) or existing
    http_origin.remove_state(runtime_dir, session_id)

    if not exited:
        module.warn(
            f"asmb8_http_origin session {session_id} (pid {pid}) did not exit within stop_timeout="
            f"{params['stop_timeout']}s after being signalled; it may still be shutting down."
        )

    fields = _status_fields(final_state)
    receipt = OperationReceipt(action="asmb8_http_origin.stop", endpoint=endpoint, changed=True, previous=existing, desired="stopped", observed=final_state)
    return _finalize(receipt, session_id=session_id, fields=fields, exited_cleanly=exited)


def main() -> None:
    module = AnsibleModule(
        argument_spec=_argument_spec(),
        required_if=[("state", "stopped", ["session_id"]), ("state", "started", ["path"])],
        supports_check_mode=True,
    )
    params = module.params

    try:
        if params["state"] == "started":
            result = _start(module, params)
        else:
            result = _stop(module, params)
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    module.exit_json(**result)


if __name__ == "__main__":
    main()
