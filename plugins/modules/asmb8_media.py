#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_media
short_description: Attach or detach a local ISO to an ASMB8-iKVM virtual CD-ROM over iUSB
description:
  - >-
    Streams a local ISO file from the Ansible controller to an ASMB8-iKVM BMC's virtual CD-ROM
    over AMI's proprietary iUSB protocol, so a bare-metal host can boot from it with no
    PXE/DHCP/TFTP/NFS/CIFS infrastructure. This is this collection's headline capability.
  - >-
    An iUSB media session is long-lived: the target host stays booted from the attached ISO for
    as long as an install takes, which can be an hour or more, while a single module invocation
    must return in seconds. O(state=attached) never pretends a synchronous call can hold that
    session open -- it forks a detached background process that owns the connection, writes a
    small JSON state file keyed by O(session_id) under O(runtime_dir), and returns once that
    process has reported either V(attached) or an early failure (bounded by O(attach_timeout)).
    O(state=detached) looks that process up by the pid recorded in the state file, asks it to
    stop, and waits (bounded by O(detach_timeout)) for it to actually exit.
  - >-
    B(Idle is normal and has no meaningful upper bound.) Verified directly against the target
    hardware: an attached session went completely silent for 130 consecutive seconds -- the host
    was sitting at a bootloader menu -- and then resumed serving reads normally with no
    intervention. A long-idle O(state=attached) session that still reports V(attached) is B(not)
    hung; it is waiting for the remote host to do something (an interactive installer prompt, a
    firmware setup screen, a slow package mirror). RV(operation.observed.last_request_at) is set
    only when a real SCSI request last arrived, and stays V(null)/stale while the host is idle;
    RV(operation.observed.updated_at) is refreshed on every internal heartbeat regardless, so a
    caller can tell "quiet because idle" from "quiet because the background process died" by
    comparing it against O(attach_timeout)-scale polling, not by assuming any fixed idle bound.
  - >-
    B(An idle-looking stretch of silence is not automatically evidence of a healthy connection --
    only of the absence, so far, of a reported failure.) A real incident produced exactly this
    ambiguity: a stretch of zero reads was read, at the time, as "the installer is unpacking
    packages", when it may in fact have coincided with a brief network outage between the
    controller and the BMC (the guest logged SCSI timeouts with zero C(REQUEST_SENSE) commands,
    meaning it was never answered at all, not answered with an error). Two fields exist to make a
    stretch of silence reviewable after the fact against independent evidence: the still-open
    stretch of silence, if any (RV(operation.observed.current_idle_streak)), and the most
    recently closed one, which survives new traffic resuming (RV(operation.observed.last_idle_streak)) --
    see the C(asmb8_baremetal_install) role's README.md, "Distinguishing idle from a broken
    connection", for the full reasoning. A connection that has genuinely broken (as opposed to
    merely gone quiet) is unaffected by any of this: it still reports
    RV(ignore:session_state) V(error) with a real RV(ignore:error_class), most often V(connection),
    naming the fault.
  - >-
    B(The BMC's iUSB/KVM media service allows exactly one active session, board-wide, and never
    reclaims an abandoned one on its own.) There is no server-side timeout for this: a session
    left attached by a previous, uncleanly-terminated run holds the slot forever until something
    closes that TCP connection. Because of this, O(state=attached) B(always) attempts to reclaim
    every OTHER session this collection's own O(runtime_dir) still has a record of against the
    same endpoint -- signalling its process to stop and removing its state file -- as a normal,
    always-run step of every attach, not a fallback that only runs after a failure. This can only
    reclaim sessions this same O(runtime_dir) is tracking; it cannot forcibly evict a session held
    by a different controller, a manually-opened JViewer/browser session, or a daemon whose
    O(runtime_dir) was deleted out from under it. When a rejected attach reports
    RV(ignore:error_class) V(bmc_busy), and no O(session_id) known to this collection is still
    holding the slot, the operator's escape hatch is a BMC cold reset (C(ipmitool mc reset cold),
    or C(community.general.ipmi_power)/pyghmi's equivalent) -- this does B(not) power-cycle the
    host itself, only the BMC's own management controller.
  - >-
    A stale state file -- the recorded pid is no longer running -- is always recoverable.
    O(state=attached) for that O(session_id) discards the stale file and starts fresh rather than
    refusing to proceed; O(state=detached) simply cleans the file up and reports C(changed=false).
  - >-
    B(Do not confuse these two ports:) O(port) (inherited from the shared connection
    fragment, default 443) is the BMC's HTTPS/HTTP web-management port used to log in and fetch
    the C(jviewer.jnlp) document that mints a media session token. O(cd_port) is the separate,
    on-demand iUSB listener (default 5120) the actual ISO bytes are streamed over. Both are
    contacted by every O(state=attached) call; only O(port) is used by O(state=detached), which
    opens no iUSB connection at all.
  - >-
    On the target hardware, O(cd_port) (and the paired KVM/floppy/HD ports) are bound only after
    a C(jviewer.jnlp) fetch allocates a session -- before that, the port refuses connections
    outright. This module's own attach flow always fetches the JNLP first, so this is
    transparent to a normal O(state=attached) call; it is noted here only so an operator manually
    probing O(cd_port) with, say, C(nc)/C(telnet) is not alarmed to find it closed with no session
    active.
  - >-
    B(RV(operation.observed.read_trace_head)/RV(operation.observed.read_trace_tail) record a
    bounded, two-ended trace of every SCSI READ(10)/READ(12) request served -- opcode, LBA, and
    block count only, B(never media contents or credentials)): the EARLIEST requests (the
    firmware catalogue/El Torito boot-image reads a stalled early boot would show) and the MOST
    RECENT ones (wherever the session actually stopped). Both matter: the only real install
    failure this project has recorded stopped roughly 22,000 reads into a 32,741-read session,
    deep in package extraction -- a trace of only the earliest requests would show nothing but a
    healthy early boot. RV(operation.observed.read_trace_dropped) is the exact count of requests
    discarded between the two, so a reader can never mistake that gap for contiguous history. The
    underlying trace itself lives in a separate, append-only log next to the state file rather
    than inside it, so it costs this module's background daemon a single small write per request
    instead of rewriting a growing trace on every one of (in that real session) 32,741 requests.
version_added: 0.1.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
options:
  state:
    description:
      - >-
        V(attached) starts (or, if O(session_id) already names a live session, confirms) a
        background iUSB session serving O(image). V(detached) stops a previously attached
        session.
    type: str
    required: true
    choices: [attached, detached]
  image:
    description:
      - Path, on the Ansible controller, to a local ISO image to serve as the virtual CD-ROM.
      - Required for O(state=attached). Ignored for O(state=detached).
      - >-
        Always served read-only -- the CD-ROM channel this module speaks has no write opcode at
        all in the BMC's own firmware, so there is no writable option to offer here.
    type: path
  session_id:
    description:
      - >-
        Identifies the background session across separate module invocations. Required for
        O(state=detached), so a caller can only ever stop a session it can name. Optional for
        O(state=attached) -- when omitted, a fresh id is generated and returned in RV(session_id);
        callers that need to detach later must capture and reuse that value.
      - >-
        Calling O(state=attached) again with an O(session_id) that already names a live session is
        idempotent: C(changed=false), and no second background process is started.
    type: str
  runtime_dir:
    description:
      - >-
        Directory holding one JSON state file per O(session_id) (plus its background process's log
        file). Must be the same path across the O(state=attached) call and the later
        O(state=detached) call for the same session. It is also the scope of this module's
        single-session reclamation pass (see the module description) -- two O(runtime_dir) values
        pointed at the same BMC are invisible to each other's reclamation logic.
      - Created (mode C(0700)) if it does not already exist.
    type: path
    default: ~/.ansible/asmb8_ikvm/media-sessions
  cd_port:
    description:
      - >-
        TCP port of the BMC's iUSB virtual CD-ROM listener. Confirmed on the target board's current
        configuration; see the module description's note on O(port) vs. this option, and on why
        this port refuses connections until a session is allocated.
    type: int
    default: 5120
  instance:
    description:
      - >-
        iUSB device-slot instance number sent in the authentication packet. V(0) is correct for a
        board with a single virtual CD-ROM slot, which is the only configuration this module has
        been validated against.
    type: int
    default: 0
  attach_timeout:
    description:
      - >-
        Bounded number of seconds O(state=attached) waits for the background process to report
        V(attached) or an early failure before returning.
      - >-
        Expiring without a V(attached) report is a B(failure), with RV(ignore:error_class)
        V(timeout) and RV(ignore:indeterminate) V(true) -- it is not established that the ISO is
        being served, and this module will not report success for an attach it has not confirmed.
        The failure still carries RV(session_id) and RV(pid), and the background session is B(not)
        torn down, because it may simply be slow and about to succeed.
      - >-
        RV(ignore:indeterminate) V(true) means B(re-probe, do not retry): call this module again
        with O(state=attached) and the same O(session_id), which is idempotent. Retrying the attach
        instead risks colliding with the still-running session, since the BMC's media slot is
        single-occupancy -- see the module description.
    type: int
    default: 10
  detach_timeout:
    description:
      - >-
        Bounded number of seconds O(state=detached) waits for the background process to actually
        exit after being asked to stop, before returning anyway. Exceeding this is reported as a
        warning, not a failure.
    type: int
    default: 15
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_boot
  - module: james_crowley.asmb8_ikvm.asmb8_power
attributes:
  check_mode:
    description: >-
      Supported. Validates options and, for O(state=attached), opens (and immediately closes)
      O(image) to confirm it is a readable file, but never forks the background process, never
      signals another session's process during reclamation, and never contacts the BMC. For
      O(state=detached), reports whether a live session would be stopped, but never signals it.
    support: full
  diff_mode:
    description: Not supported. Use RV(session_state) and the C(operation) receipt instead.
    support: none
requirements:
  - requests >= 2.25.0 (on the Ansible controller)
"""

EXAMPLES = r"""
- name: Attach a Proxmox installer ISO for an unattended install
  james_crowley.asmb8_ikvm.asmb8_media:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    image: /srv/images/proxmox-auto.iso
    state: attached
  delegate_to: localhost
  no_log: true
  register: media

- name: Arm a one-time optical boot and reset into the attached ISO
  james_crowley.asmb8_ikvm.asmb8_boot:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    device: optical
  delegate_to: localhost
  no_log: true

- name: Poll the same session id later in the play
  james_crowley.asmb8_ikvm.asmb8_media:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    image: /srv/images/proxmox-auto.iso
    session_id: "{{ media.session_id }}"
    state: attached
  delegate_to: localhost
  no_log: true
  register: media_status

- name: Detach once the install has finished
  james_crowley.asmb8_ikvm.asmb8_media:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    session_id: "{{ media.session_id }}"
    state: detached
  delegate_to: localhost
  no_log: true
"""

RETURN = r"""
changed:
  description: >-
    For O(state=attached): V(true) only when a new background process was actually forked (or, in
    check mode, would be). V(false) when an already-live session for O(session_id) was found and
    confirmed instead. For O(state=detached): V(true) only when a live process was actually asked
    to stop (or, in check mode, would be); V(false) when there was nothing live to stop.
  type: bool
  returned: always
session_id:
  description: The session id in effect -- generated for O(state=attached) when not supplied.
  type: str
  returned: always
session_state:
  description:
    - >-
      The last state the background process reported: V(starting) (forked, not yet connected),
      V(connecting), V(attached), V(detached), or V(error). V(unknown) if O(state=detached) found
      no state file at all.
    - >-
      An V(attached) session that has been idle a long time is still V(attached) -- see the
      module description's note on idle having no meaningful upper bound. Re-run this module with
      O(state=attached) and the same O(session_id) to refresh this value rather than assuming a
      stale first result still holds.
  type: str
  returned: always
pid:
  description: Process id of the background session, when one is recorded. V(null) if none is.
  type: int
  returned: when available
bytes_read:
  description: Total bytes read from O(image) so far, mirroring C(operation.observed.bytes_read).
  type: int
  returned: always
recovered_stale_session:
  description: >-
    V(true) when a stale state file (recorded pid no longer running) for O(session_id) was found
    and discarded by this call.
  type: bool
  returned: when a stale session was recovered
reclaimed_sessions:
  description: >-
    Session ids of OTHER sessions this collection's O(runtime_dir) had a record of against the
    same endpoint, and which this call attempted to reclaim (signal to stop, then remove) before
    attaching -- see the module description's single-session-hazard note. Empty when there were
    none. Only meaningful for O(state=attached).
  type: list
  elements: str
  returned: when state is attached
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
      description: One of V(asmb8_media.attach) or V(asmb8_media.detach).
      type: str
    endpoint:
      description: The iUSB C(host:cd_port) this session connects (or connected) to.
      type: str
    changed:
      description: Mirrors the top-level RV(changed).
      type: bool
    previous:
      description: The session state as read before this call, or V(null) when none existed.
      type: dict
    desired:
      description: V(attached) or V(detached), whichever this call requested.
      type: str
    observed:
      description: >-
        The session state as read after this call. V(null) in check mode, since nothing was
        actually attached (or reclaimed/detached) to observe.
      type: dict
      contains:
        session_id:
          description: Same value as RV(session_id).
          type: str
        pid:
          description: Same value as RV(pid).
          type: int
        endpoint:
          description: Same value as RV(operation.endpoint).
          type: str
        state:
          description: Same value as RV(session_state).
          type: str
        image:
          description: Resolved path of O(image).
          type: str
        bytes_read:
          description: Same value as RV(bytes_read).
          type: int
        sectors_served:
          description: Total SCSI blocks served by READ(10)/READ(12) commands so far.
          type: int
        read_trace_head:
          description:
            - >-
              The EARLIEST READ(10)/READ(12) requests this session has served -- opcode
              (C(0x28)/C(0xA8)), LBA, and block count only. B(Never media contents or
              credentials.) Answers "did this session ever get past the firmware
              catalogue/El Torito boot-image reads" for a boot that stalls early.
            - >-
              Bounded (128 entries): once full, it stops growing and simply stays as the
              earliest requests this session ever served. Reconstructed on read from a
              separate, append-only sidecar log next to the state file, never itself
              part of the atomically-rewritten state -- see
              C(plugins/module_utils/media_session.py)'s module docstring point 3 for
              why (a 32,741-request real install would otherwise pay a full-trace
              rewrite on every single request).
          type: list
          elements: dict
          contains:
            opcode:
              description: C(0x28) (READ(10)) or C(0xa8) (READ(12)), as a lowercase hex string.
              type: str
            lba:
              description: The logical block address requested.
              type: int
            blocks:
              description: The number of blocks requested.
              type: int
        read_trace_tail:
          description:
            - >-
              The MOST RECENT READ(10)/READ(12) requests this session has served, in the
              same C(opcode)/C(lba)/C(blocks) shape as C(read_trace_head). Answers "where
              did this session actually stop" for a stall deep into an install -- the only
              real failure this project has recorded (a 32,741-read session that stopped
              roughly 22,000 reads in, mid package-extraction) stalled far past where
              C(read_trace_head) alone could ever show.
            - >-
              Never overlaps C(read_trace_head): while the total request count is at or
              below C(read_trace_head)'s own limit, this stays empty rather than
              duplicating it. Bounded at 128 entries as a rolling window once the total
              exceeds both limits combined.
          type: list
          elements: dict
          contains:
            opcode:
              description: Same shape as C(read_trace_head.opcode).
              type: str
            lba:
              description: Same shape as C(read_trace_head.lba).
              type: int
            blocks:
              description: Same shape as C(read_trace_head.blocks).
              type: int
        read_trace_dropped:
          description: >-
            Exactly how many requests fall in the discarded gap between C(read_trace_head)
            and C(read_trace_tail) -- V(0) means those two lists are the COMPLETE record
            with no gap at all. A nonzero value means the boundary between the tail's
            earliest entry and the head's latest entry is NOT one request apart; it is
            this many requests apart, so a reader can never mistake the gap for
            contiguous history.
          type: int
        last_request_at:
          description: >-
            Controller-clock ISO-8601 timestamp of the last real SCSI request the background
            process actually served, or V(null) if none has arrived yet. Set only on real traffic
            -- unlike C(updated_at) below, this does NOT move during an idle period, however long.
            Compare the two to tell "idle because the remote host is waiting on something" from
            "the background process died": see the module description's note that idle has no
            meaningful upper bound on this hardware (measured live at 130 continuous seconds of
            silence on a healthy session).
          type: str
        stop_reason:
          description:
            - >-
              Why a session that reached V(detached) actually stopped: V(signal) (asked to, via
              O(state=detached) or an external C(SIGTERM) -- including the one this module's own
              background daemon installs a handler for specifically so an interrupted play cannot
              strand the BMC's single media slot, see the module description's single-media-session
              note), V(peer_closed) (the BMC closed the TCP connection first), or V(bmc_terminate)
              (the BMC sent an explicit redirection-terminate, opcode C(0xF6)). V(null) while still
              V(attached), and also V(null) on V(error) -- a crash is already fully identified by
              RV(ignore:session_state) plus RV(ignore:error_class); this field distinguishes kinds
              of CLEAN stop from each other, not a clean stop from a crash.
            - >-
              Mirrors C(james_crowley.asmb8_ikvm.asmb8_http_origin)'s identically-named field
              (whose own vocabulary is V(signal)/V(lifetime_expired) -- this module has no
              lifetime cap of its own, so that second value never appears here).
          type: str
        updated_at:
          description: >-
            Controller-clock ISO-8601 timestamp of the last time the background process wrote its
            state file at all, refreshed on every internal heartbeat whether or not any SCSI
            traffic arrived. A caller polling this value should expect it to keep moving for as
            long as the background process is alive, including through an arbitrarily long idle
            stretch -- a value that stops advancing, not merely a large gap since
            C(last_request_at), is what indicates the process is no longer running.
          type: str
        started_at:
          description: Controller-clock ISO-8601 timestamp of when the background process started.
          type: str
        idle_polls:
          description: >-
            Lifetime count of idle heartbeats (poll timeouts with zero bytes at a fresh frame
            boundary -- see C(plugins/module_utils/iusb.py)'s C(IdleTimeout)) this session has
            observed. A coarse "how quiet has this session been overall" figure; see
            C(current_idle_streak)/C(last_idle_streak) below for actual start/end timestamps of
            individual quiet stretches.
          type: int
        idle_poll_interval_seconds:
          description: >-
            Seconds between idle heartbeats, so C(idle_polls) (or a streak's own C(polls)) can be
            converted to an approximate duration without hardcoding this module's internal poll
            cadence externally.
          type: float
        current_idle_streak:
          description: >-
            The still-open stretch of silence, if the session is idle right now, or V(null) if it
            last saw real traffic (or has not gone idle since it last did). Cleared back to
            V(null) the moment a real SCSI request arrives -- see the module description's note
            on distinguishing idle from a broken connection.
          type: dict
          contains:
            started_at:
              description: Controller-clock ISO-8601 timestamp of when this streak of silence began.
              type: str
            polls:
              description: Idle heartbeats observed so far in this streak.
              type: int
            seconds:
              description: Approximately C(polls) times C(idle_poll_interval_seconds).
              type: float
        last_idle_streak:
          description: >-
            The most recently CLOSED stretch of silence -- unlike C(current_idle_streak), this is
            not cleared by new traffic resuming, so it stays visible for a post-mortem even after
            the session kept running. V(null) if the session has never yet closed an idle streak
            (no idle period has ended in either a real request or the session itself ending).
          type: dict
          contains:
            started_at:
              description: Controller-clock ISO-8601 timestamp of when this streak of silence began.
              type: str
            ended_at:
              description: >-
                Controller-clock ISO-8601 timestamp of when this streak ended -- either a real
                SCSI request arrived, or the session itself ended while this streak was still open.
              type: str
            polls:
              description: Idle heartbeats observed during this streak.
              type: int
            seconds:
              description: Approximately C(polls) times C(idle_poll_interval_seconds).
              type: float
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

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import media_session
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import HAS_REQUESTS, REQUESTS_IMPORT_ERROR, enforce_transport_policy
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import (
    ErrorClass,
    IkvmError,
    ProtocolError,
    TimeoutError_,
)
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt


def _argument_spec() -> dict:
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
        "state": {"type": "str", "required": True, "choices": ["attached", "detached"]},
        "image": {"type": "path"},
        "session_id": {"type": "str"},
        "runtime_dir": {"type": "path", "default": media_session.DEFAULT_RUNTIME_DIR},
        "cd_port": {"type": "int", "default": 5120},
        "instance": {"type": "int", "default": 0},
        "attach_timeout": {"type": "int", "default": 10},
        "detach_timeout": {"type": "int", "default": 15},
    }


def build_session_config(params: dict, *, session_id: str) -> media_session.SessionConfig:
    return media_session.SessionConfig(
        session_id=session_id,
        host=params["host"],
        port=params["port"],
        use_tls=params["use_tls"],
        allow_insecure_transport=params["allow_insecure_transport"],
        validate_certs=params["validate_certs"],
        ca_path=params.get("ca_path"),
        tls_fingerprint=params.get("tls_fingerprint"),
        timeout=params["timeout"],
        connect_timeout=params["connect_timeout"],
        cd_port=params["cd_port"],
        instance=params["instance"],
        image=params["image"],
        runtime_dir=params["runtime_dir"],
    )


def _finalize(receipt: OperationReceipt, *, session_id: str, fields: dict, **extra) -> dict:
    """Assemble the module result: module-specific keys at the top level, the receipt nested.

    Mirrors the sibling ``amt_media`` module's helper of the same name and its same reasoning
    (see issue #22 there): receipt fields are never duplicated at the top level outside of the
    handful this RETURN documents explicitly.
    """
    return {
        "changed": receipt.changed,
        "session_id": session_id,
        "session_state": fields["session_state"],
        "pid": fields["pid"],
        "bytes_read": fields["bytes_read"],
        "error": fields["error"],
        "operation": receipt.to_dict(),
        **extra,
    }


#: What every ``read_trace_*`` field defaults to when there is no sidecar log
#: to read yet (no session, or one that never served a single READ(10)/
#: READ(12)) -- the same empty-but-present shape a real, quiet session would
#: also produce, per ``media_session.read_trace_summary()``'s own contract.
_EMPTY_READ_TRACE = {"read_trace_head": [], "read_trace_tail": [], "read_trace_dropped": 0}


def _with_trace(state: dict | None, trace: dict) -> dict | None:
    """Merge a read-trace summary into a raw state dict, for ``operation.observed``.

    The trace never lives inside the state file itself (see
    ``media_session.py``'s module docstring point 3 -- it is reconstructed on
    demand from a separate sidecar log), so it has to be stitched into
    whatever raw dict a caller is about to hand ``OperationReceipt`` as
    ``observed=`` -- that dict is exactly what ends up under
    ``operation.observed`` (see ``models.OperationReceipt.to_dict``, which
    passes a plain dict through unchanged). Returns ``None`` unchanged: a
    check-mode attach with nothing observed yet has no trace to merge in
    either.
    """
    if state is None:
        return None
    return {**state, **trace}


def _status_fields(state: dict | None, *, trace: dict | None = None) -> dict:
    state = state or {}
    trace = trace or _EMPTY_READ_TRACE
    return {
        "session_state": state.get("state", "unknown"),
        "pid": state.get("pid"),
        "bytes_read": state.get("bytes_read", 0),
        "sectors_served": state.get("sectors_served", 0),
        "last_request_at": state.get("last_request_at"),
        "updated_at": state.get("updated_at"),
        "error": state.get("error"),
        # Idle-versus-broken forensic fields -- see media_session.py's module
        # docstring point 1. Not surfaced at the top level by _finalize() (see
        # its own docstring on which fields are), but included here so a
        # failure path's **fields spread carries them too, and so they are
        # available for RETURN's operation.observed documentation below.
        "idle_polls": state.get("idle_polls", 0),
        "current_idle_streak": state.get("current_idle_streak"),
        "last_idle_streak": state.get("last_idle_streak"),
        # Surfaced for the same reason asmb8_http_origin already surfaces its own
        # stop_reason: it is what lets a post-mortem tell a signalled stop from a
        # BMC-initiated one after the fact -- see media_session.py's module
        # docstring point 2 and roles/asmb8_baremetal_install/README.md.
        "stop_reason": state.get("stop_reason"),
        # The READ(10)/READ(12) trace -- see media_session.py's module
        # docstring point 3 and read_trace_summary()'s own docstring for the
        # retention shape (earliest read_trace_head + most recent
        # read_trace_tail, with read_trace_dropped counting exactly what falls
        # in between). No media contents, no credentials -- opcode/LBA/block
        # count only, per entry.
        "read_trace_head": trace["read_trace_head"],
        "read_trace_tail": trace["read_trace_tail"],
        "read_trace_dropped": trace["read_trace_dropped"],
    }


def _error_class_of(state: dict | None) -> str:
    """The error_class to fail with for a state whose ``state`` key is V(error).

    Falls back to the generic class only if the daemon somehow recorded an error with no
    classification at all -- every ``IkvmError`` the daemon can raise carries its own real
    ``error_class`` (see ``media_session._run_daemon``'s ``_fail`` helper).
    """
    return (state or {}).get("error_class") or ErrorClass.PROTOCOL


def _attach(module: AnsibleModule, params: dict, *, endpoint: str) -> dict:
    session_id = params.get("session_id") or media_session.generate_session_id()
    runtime_dir = params["runtime_dir"]

    existing = media_session.read_state(runtime_dir, session_id)
    if existing is not None and media_session.is_pid_alive(existing.get("pid")) and existing.get("state") not in media_session.TERMINAL_STATES:
        # Idempotent: a live session already answers to this id. Never start a second one --
        # this BMC's media slot is single-occupancy, board-wide.
        trace = media_session.read_trace_summary(runtime_dir, session_id)
        fields = _status_fields(existing, trace=trace)
        receipt = OperationReceipt(
            action="asmb8_media.attach", endpoint=endpoint, changed=False, previous=existing, desired=None, observed=_with_trace(existing, trace)
        )
        return _finalize(receipt, session_id=session_id, fields=fields, reclaimed_sessions=[])

    recovered_stale = existing is not None
    if recovered_stale:
        media_session.remove_state(runtime_dir, session_id)

    if not params.get("image"):
        raise ProtocolError("asmb8_media state=attached requires image to be set", operation="asmb8_media.attach")
    media_session.validate_image(params["image"])

    # Always-run reclamation pass -- see media_session.py's module docstring point 2 and
    # reclaim_conflicting_sessions()'s own docstring. This runs for every attach, live or
    # check-mode, NOT only after a failure -- except that check mode must never actually signal
    # another process (that would be a mutation), so it only reports what it would reclaim.
    if module.check_mode:
        would_reclaim = [s.get("session_id") for s in media_session.find_conflicting_sessions(runtime_dir, endpoint, exclude_session_id=session_id)]
        fields = {
            "session_state": "starting",
            "pid": None,
            "bytes_read": 0,
            "sectors_served": 0,
            "last_request_at": None,
            "updated_at": None,
            "error": None,
            **_EMPTY_READ_TRACE,  # nothing has been attached (or read) yet in check mode.
        }
        receipt = OperationReceipt(action="asmb8_media.attach", endpoint=endpoint, changed=True, previous=existing, desired="attached", observed=None)
        return _finalize(receipt, session_id=session_id, fields=fields, recovered_stale_session=recovered_stale, reclaimed_sessions=would_reclaim)

    reclaimed = media_session.reclaim_conflicting_sessions(runtime_dir, endpoint, exclude_session_id=session_id)

    config = build_session_config(params, session_id=session_id)
    media_session.spawn_session(config, username=params["username"], password=params["password"])

    observed = media_session.wait_for_state(
        runtime_dir,
        session_id,
        until=lambda s: s.get("state") in (media_session.STATE_ATTACHED, media_session.STATE_ERROR, media_session.STATE_DETACHED),
        timeout=float(params["attach_timeout"]),
    )
    trace = media_session.read_trace_summary(runtime_dir, session_id)
    fields = _status_fields(observed, trace=trace)

    if fields["session_state"] == media_session.STATE_ERROR:
        module.fail_json(
            msg=f"asmb8_media attach failed: {fields['error']}",
            error_class=_error_class_of(observed),
            session_id=session_id,
            reclaimed_sessions=reclaimed,
            **fields,
        )

    # Nothing short of ATTACHED is a success -- see the sibling collection's identical rule and
    # the reasoning behind it (issue #44/#69 there): liveness of the pid decides *which* kind of
    # failure this is, but the module never falls through to a success receipt on an unconfirmed
    # attach.
    if fields["session_state"] != media_session.STATE_ATTACHED:
        pid = (observed or {}).get("pid")
        if not media_session.is_pid_alive(pid):
            final = media_session.read_state(runtime_dir, session_id) or observed
            final_trace = media_session.read_trace_summary(runtime_dir, session_id)
            final_fields = _status_fields(final, trace=final_trace)
            reported = final_fields.get("error") or fields.get("error")
            module.fail_json(
                msg=(
                    f"asmb8_media attach failed: {reported}"
                    if reported
                    else (
                        "asmb8_media attach failed: the session process exited without reporting "
                        f"'attached' (last observed state {final_fields['session_state']!r}). "
                        "Check the BMC credentials and that the iUSB port is reachable."
                    )
                ),
                error_class=_error_class_of(final),
                session_id=session_id,
                reclaimed_sessions=reclaimed,
                **final_fields,
            )

        if fields["session_state"] in media_session.TERMINAL_STATES:
            # A live pid whose last published state is terminal but not 'attached' -- a settled
            # failure (the session ended without ever attaching), not something to re-probe.
            reported = fields.get("error")
            module.fail_json(
                msg=(
                    f"asmb8_media attach failed: the session reached terminal state "
                    f"{fields['session_state']!r} without ever reporting 'attached'" + (f": {reported}" if reported else "")
                ),
                error_class=_error_class_of(observed),
                session_id=session_id,
                reclaimed_sessions=reclaimed,
                **fields,
            )

        # No verdict at all: still 'starting'/'connecting' with the daemon running. Classified
        # as an indeterminate timeout, per the module's own documented contract -- the caller
        # must re-probe rather than retry, because a blind retry would collide with a daemon
        # that may still hold the one media slot the BMC has to give. The session is
        # deliberately NOT torn down here; see asmb8_media.py's RETURN docs for attach_timeout.
        err = TimeoutError_(
            f"asmb8_media attach did not confirm within attach_timeout={params['attach_timeout']}s: the session "
            f"process (pid {fields['pid']}) is still running but its last reported state is "
            f"{fields['session_state']!r}, not 'attached', so it is not established that the ISO is being "
            f"served. The session was not detached and is still running as session_id {session_id!r}. "
            "Re-probe it with another state=attached call for that same session_id rather than retrying the "
            "attach -- this BMC's media slot is single-occupancy, so a second attach would collide with this "
            "one. Detach that session_id if you do not intend to wait, and raise attach_timeout if slow "
            "attaches are normal for this endpoint.",
            endpoint=endpoint,
            indeterminate=True,
        )
        module.fail_json(**err.to_result(), session_id=session_id, reclaimed_sessions=reclaimed, **fields)

    receipt = OperationReceipt(
        action="asmb8_media.attach", endpoint=endpoint, changed=True, previous=existing, desired="attached", observed=_with_trace(observed, trace)
    )
    return _finalize(receipt, session_id=session_id, fields=fields, recovered_stale_session=recovered_stale, reclaimed_sessions=reclaimed)


def _detach(module: AnsibleModule, params: dict, *, endpoint: str) -> dict:
    session_id = params["session_id"]
    runtime_dir = params["runtime_dir"]

    existing = media_session.read_state(runtime_dir, session_id)
    if existing is None:
        fields = _status_fields(None)
        receipt = OperationReceipt(action="asmb8_media.detach", endpoint=endpoint, changed=False, previous=None, desired="detached", observed=None)
        return _finalize(receipt, session_id=session_id, fields=fields)

    pid = existing.get("pid")
    live = media_session.is_pid_alive(pid)

    if module.check_mode:
        trace = media_session.read_trace_summary(runtime_dir, session_id)
        fields = _status_fields(existing, trace=trace)
        receipt = OperationReceipt(
            action="asmb8_media.detach", endpoint=endpoint, changed=live, previous=existing, desired="detached", observed=_with_trace(existing, trace)
        )
        return _finalize(receipt, session_id=session_id, fields=fields)

    if not live:
        # Read the trace BEFORE remove_state() -- remove_state() deletes the sidecar
        # log too (see media_session.remove_state's own docstring), and this is the
        # last chance to surface it in this call's own receipt.
        trace = media_session.read_trace_summary(runtime_dir, session_id)
        fields = _status_fields(existing, trace=trace)
        media_session.remove_state(runtime_dir, session_id)
        receipt = OperationReceipt(
            action="asmb8_media.detach", endpoint=endpoint, changed=False, previous=existing, desired="detached", observed=_with_trace(existing, trace)
        )
        return _finalize(receipt, session_id=session_id, fields=fields, recovered_stale_session=True)

    media_session.request_stop(pid)
    exited = media_session.wait_for_exit(pid, timeout=float(params["detach_timeout"]))
    final_state = media_session.read_state(runtime_dir, session_id) or existing
    # Same ordering reason as the stale-session branch above: read the trace before
    # remove_state() deletes its sidecar log out from under this call's own receipt.
    trace = media_session.read_trace_summary(runtime_dir, session_id)
    media_session.remove_state(runtime_dir, session_id)

    if not exited:
        module.warn(
            f"asmb8_media session {session_id} (pid {pid}) did not exit within detach_timeout="
            f"{params['detach_timeout']}s after being signalled; it may still be shutting down."
        )

    fields = _status_fields(final_state, trace=trace)
    receipt = OperationReceipt(
        action="asmb8_media.detach", endpoint=endpoint, changed=True, previous=existing, desired="detached", observed=_with_trace(final_state, trace)
    )
    return _finalize(receipt, session_id=session_id, fields=fields, exited_cleanly=exited)


def main() -> None:
    module = AnsibleModule(
        argument_spec=_argument_spec(),
        required_if=[("state", "detached", ["session_id"]), ("state", "attached", ["image"])],
        supports_check_mode=True,
    )
    params = module.params

    if not HAS_REQUESTS:
        module.fail_json(msg=missing_required_lib("requests"), exception=REQUESTS_IMPORT_ERROR)
        return

    endpoint = f"{params['host']}:{params['cd_port']}"

    try:
        if params["state"] == "attached":
            # Trust/transport policy is only meaningful when we are about to open a
            # connection, which only O(state=attached) ever does. Checked here,
            # synchronously, before _attach() ever validates the image or forks the
            # background daemon -- AspClient (built only inside that daemon, after
            # the fork) enforces the same policy on its own, but by then a bad
            # configuration would have cost a real fork and a real background
            # process for no reason. Detach must never be gated on this: it opens no
            # connection at all, and gating it would make a running session
            # unstoppable by the very configuration change meant to let an operator
            # shut it down.
            enforce_transport_policy(use_tls=params["use_tls"], allow_insecure_transport=params["allow_insecure_transport"])
            result = _attach(module, params, endpoint=endpoint)
        else:
            result = _detach(module, params, endpoint=endpoint)
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    module.exit_json(**result)


if __name__ == "__main__":
    main()
