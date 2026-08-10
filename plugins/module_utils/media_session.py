# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Detached, long-lived iUSB virtual-media session process for ``asmb8_media``.

Mirrors the sibling ``james_crowley.intel_amt`` collection's
``media_session.py`` in shape and reasoning -- read that file's module
docstring first if you have not; the two points it makes about forking apply
here unchanged. What is genuinely different about THIS BMC, and therefore
about this module, are the two facts the task brief calls out explicitly:

1. **Idle is not failure, without an upper bound.** Verified live: 130
   consecutive seconds of total silence on an attached, healthy session (a
   host sitting at a bootloader menu), then reads resumed normally. The serve
   loop this module drives (``iusb.Session.serve_forever``) never raises on
   an idle socket -- see that method's docstring -- and this module's own
   idle callback refreshes ``updated_at`` as a heartbeat; it never ends the
   session. A caller debugging a slow install needs to be able to tell "idle
   because the installer is waiting for input" from "dead": that is exactly
   what ``last_request_at`` (set only on real traffic) versus ``updated_at``
   (refreshed on every poll, idle or not) is for.

   A real incident sharpened this further: a trace showed stretches of zero
   reads that were read, at the time, as "the installer is unpacking
   packages" -- when at least one such stretch may actually have been a
   network outage between the controller and the BMC (the guest logged SCSI
   timeouts and I/O errors during the same window, and critically **zero**
   ``REQUEST_SENSE`` commands, meaning it never received an error status at
   all -- a timeout signature, not an error-reply one). ``updated_at`` alone,
   read once after the fact, cannot answer "how long was that quiet stretch,
   and when did it start/end" -- it is a single mutable timestamp, not a
   history. ``current_idle_streak``/``last_idle_streak``/``idle_polls``
   (below, all populated by :func:`_run_daemon`'s idle/request callbacks) are
   this module's answer: not real-time network-failure detection (an idle
   timeout at a frame boundary -- see ``iusb.IdleTimeout``'s own docstring --
   carries no information about *why* nothing arrived, whether the peer is
   genuinely quiet or the network briefly died between two requests), but
   enough recorded start/end timestamps and durations for a post-mortem to
   cross-reference a suspicious quiet stretch against independent evidence
   (the guest's own kernel log timestamps, BMC logs, network monitoring) after
   the fact. A connection that has actually broken -- a stalled read
   mid-frame, not at a boundary, or a socket error -- is unaffected by any of
   this and is reported the way it always was: ``state=error`` with a real
   ``error_class`` (most often ``connection``) and a message naming the
   fault; see ``iusb.SocketTransport.recv_exact``'s own idle-at-boundary
   versus stalled-mid-frame split, which this module builds on top of but
   does not modify.
2. **The media slot is single-occupancy, board-wide, with no server-side
   reclaim.** Unlike IDE-R, this is not scoped to one ``session_id`` this
   collection's own runtime_dir happens to track -- it is one slot for the
   *entire BMC*, and the BMC will hold it forever for a client that never
   closes its TCP connection. ``state=attached`` therefore always runs a
   reclamation pass over every OTHER session this runtime_dir has a record
   for against the same endpoint, live or stale, before ever attempting a
   fresh attach -- see :func:`reclaim_conflicting_sessions`. This is an
   always-run step of the attach flow, never a fallback invoked only after a
   failure: by the time an attach has already failed with
   ``ErrorClass.BMC_BUSY``, whatever software-visible reclamation this module
   can do has already been tried, and the remaining escape hatch is a BMC
   cold reset (``ipmitool mc reset cold`` / the pyghmi equivalent), which does
   not affect host power. See ``iusb.interpret_ack`` for where that message
   is produced.

As in the sibling collection: this module owns exactly the process lifecycle,
state-file bookkeeping, and the single-session reclamation policy. It knows
nothing about Ansible; ``plugins/modules/asmb8_media.py`` is the only caller.
"""

from __future__ import annotations

import contextlib
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import iusb
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import AspClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.daemon_runtime import (
    _now_iso,
    _redirect_std_fds,
    _write_state_atomic,
    _write_state_if_absent,
    generate_session_id,
    is_pid_alive,
    list_session_ids,
    log_file_path,
    read_state,
    remove_paths,
    request_stop,
    state_file_path,
    wait_for_exit,
    wait_for_state,
)
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import (
    IkvmError,
    ProtocolError,
    UnsupportedCapabilityError,
    redact,
)

#: generate_session_id/wait_for_state are re-exported from daemon_runtime for
#: this module's own callers (asmb8_media.py, this module's own tests) to reach
#: as media_session.<name> -- nothing in THIS file calls them itself, unlike
#: e.g. read_state/is_pid_alive/request_stop/wait_for_exit/list_session_ids
#: below, which the reclamation scan uses directly and so need no such
#: declaration. Without this, ruff/pyflakes' F401 would flag both imports as
#: unused, since it only sees this file, not asmb8_media.py's own usage.
__all__ = [
    "generate_session_id",
    "wait_for_state",
]

#: Observable session states, written to the state file's ``state`` key.
STATE_STARTING = "starting"
STATE_CONNECTING = "connecting"
STATE_ATTACHED = "attached"
STATE_DETACHED = "detached"
STATE_ERROR = "error"

#: States a live daemon will never revert out of on its own -- once here, the
#: process is expected to be exiting or already gone.
TERMINAL_STATES = frozenset({STATE_DETACHED, STATE_ERROR})

#: Default location for state/log files, one per session_id. Deliberately a
#: per-user, per-collection directory rather than a shared /tmp path -- see
#: the sibling collection's identical rationale.
DEFAULT_RUNTIME_DIR = "~/.ansible/asmb8_ikvm/media-sessions"

#: How often the daemon's serve loop wakes on a quiet connection to check for
#: a stop request and refresh its heartbeat. This is a responsiveness knob
#: for SIGTERM, NOT an idle deadline -- see iusb.Session.serve_forever's
#: docstring and the module docstring point 1 above. Not a module option:
#: raising or lowering it changes only how quickly a detach request is
#: noticed, never how long an attached session may legitimately sit idle.
_RECV_POLL_TIMEOUT = 2.0

#: Bound on waiting for another locally-tracked session (live process) to
#: exit during the always-run reclamation pass (see module docstring point 2
#: and :func:`reclaim_conflicting_sessions`). Deliberately generous but
#: finite: a session that does not exit within this bound is signalled and
#: then abandoned to its own devices rather than blocking this attach
#: indefinitely -- the auth handshake that follows will surface
#: ``ErrorClass.BMC_BUSY`` on its own if the slot is in fact still held.
_RECLAIM_STOP_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Everything the daemon needs that is not a credential or a media token.

    Deliberately excludes ``username``/``password`` (see the module
    docstring's point 1 in the sibling collection) and has no field at all
    for the iUSB/KVM token: the token is minted fresh inside the daemon by
    :func:`_run_daemon` itself (via ``AspClient.allocate_media_session``) and
    is never constructed by, or passed through, the parent process.
    """

    session_id: str
    host: str
    port: int  # BMC web-management port (asp.py), NOT the iUSB media port.
    use_tls: bool
    allow_insecure_transport: bool
    validate_certs: bool
    ca_path: str | None
    tls_fingerprint: str | None
    timeout: int
    connect_timeout: int
    cd_port: int
    instance: int
    image: str
    runtime_dir: str


# --------------------------------------------------------------------------
# State file plumbing -- shared by the daemon (writer) and the module
# (reader). The primitives themselves (atomic write, create-only write, pid
# liveness, bounded polling) now live in ``daemon_runtime.py``, shared with
# ``http_origin.py``'s daemon -- see that module's own docstring for exactly
# why and what deliberately stayed out of it. ``state_file_path``/
# ``log_file_path``/``read_state``/``list_session_ids``/``is_pid_alive``/
# ``generate_session_id``/``wait_for_state``/``wait_for_exit``/
# ``request_stop`` are imported directly, unchanged, at the top of this file.
# --------------------------------------------------------------------------


def _initial_state(*, session_id: str, endpoint: str, pid: int, image: str) -> dict[str, Any]:
    """The starting state record, in the one shape every writer must use.

    There are two independent writers of a session's *first* state record --
    the daemon itself (:func:`_run_daemon`) and the process that forked it
    (:func:`spawn_session`, for the case where the daemon dies before writing
    anything) -- and, per the sibling collection's own hard-won lesson, they
    must never disagree on shape. Factored into one function so a new key
    added here reaches both writers by construction.

    ``idle_polls``/``idle_poll_interval_seconds``/``current_idle_streak``/
    ``last_idle_streak`` are the idle-versus-broken forensic fields described
    in this module's own docstring point 1 -- populated by :func:`_run_daemon`'s
    ``_on_idle``/``_on_request`` callbacks and :func:`_close_idle_streak`,
    never by anything outside this file.

    ``stop_reason`` records WHY a session that reached ``STATE_DETACHED``
    actually stopped -- ``"signal"`` (asked to, via ``state=detached`` or an
    external ``SIGTERM``), ``"peer_closed"`` (the BMC closed the TCP
    connection first), or ``"bmc_terminate"`` (the BMC sent an explicit
    redirection-terminate, opcode ``0xF6``) -- set only in :func:`_run_daemon`'s
    normal-exit branch, mirroring ``http_origin.py``'s identical field
    (``"signal"``/``"lifetime_expired"`` there). Stays ``None`` on
    ``STATE_ERROR``: a crash is already fully identified by ``state`` +
    ``error_class``, and this field exists to distinguish kinds of *clean*
    stop from each other, not to duplicate that.
    """
    now = _now_iso()
    return {
        "session_id": session_id,
        "pid": pid,
        "endpoint": endpoint,
        "state": STATE_STARTING,
        "error": None,
        "error_class": None,
        "image": image,
        "bytes_read": 0,
        "sectors_served": 0,
        "last_request_at": None,
        "started_at": now,
        "updated_at": now,
        "idle_polls": 0,
        "idle_poll_interval_seconds": _RECV_POLL_TIMEOUT,
        "current_idle_streak": None,
        "last_idle_streak": None,
        "stop_reason": None,
    }


def _close_idle_streak(state: dict[str, Any], *, now: str) -> None:
    """Close the in-progress idle streak (if any) into ``last_idle_streak``.

    Called when a real request arrives after a quiet spell, and again after
    the serve loop exits normally (see :func:`_run_daemon`), so the state
    file always shows how long the MOST RECENT stretch of silence lasted --
    not merely whether one happens to be open right now -- even once new
    traffic has resumed or the session has ended. Deliberately NOT called on
    the exception path: if the daemon crashed mid-read, ``current_idle_streak``
    left open (rather than force-closed here) accurately shows "idle right up
    until this failure", which is itself useful forensic signal, not a bug to
    paper over.
    """
    streak = state.get("current_idle_streak")
    if streak is None:
        return
    state["last_idle_streak"] = {**streak, "ended_at": now}
    state["current_idle_streak"] = None


def remove_state(runtime_dir: str | os.PathLike[str], session_id: str) -> None:
    """Delete the state and log files for ``session_id``, if present.

    Frees the session_id for reuse. Built on ``daemon_runtime.remove_paths``,
    which is silent about files that are already gone -- callers call this
    defensively without checking existence first.
    """
    remove_paths(state_file_path(runtime_dir, session_id), log_file_path(runtime_dir, session_id))


# --------------------------------------------------------------------------
# The single-session hazard: reclamation. See module docstring point 2.
# --------------------------------------------------------------------------


def find_conflicting_sessions(
    runtime_dir: str | os.PathLike[str],
    endpoint: str,
    *,
    exclude_session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Every OTHER session this runtime_dir has a state file for that names
    the same BMC media endpoint (``host:cd_port``), live or stale.

    This is deliberately endpoint-scoped, not limited to sessions this
    module itself considers "the same session_id" -- the BMC's media slot is
    one per board, not one per ``session_id``, so a second ``session_id``
    against the same endpoint is exactly the conflict this exists to find.
    """
    conflicts = []
    for session_id in list_session_ids(runtime_dir):
        if session_id == exclude_session_id:
            continue
        state = read_state(runtime_dir, session_id)
        if state is None or state.get("endpoint") != endpoint:
            continue
        conflicts.append(state)
    return conflicts


def reclaim_conflicting_sessions(
    runtime_dir: str | os.PathLike[str],
    endpoint: str,
    *,
    exclude_session_id: str | None,
    stop_timeout: float = _RECLAIM_STOP_TIMEOUT,
) -> list[str]:
    """Stop and clean up every OTHER locally-tracked session for ``endpoint``.

    Always run as part of ``state=attached`` -- see the module docstring's
    point 2 -- immediately after the same-``session_id`` idempotency check
    and before any media is opened or any connection to the BMC is made.
    "Eject/reset before insert" here means: for every conflicting session
    this collection's own ``runtime_dir`` still has a record of, live or
    stale, ask its process to stop (if it is still running), wait a bounded
    time, and remove the record either way.

    This is the full extent of software reclamation this module can perform.
    It has no way to evict a session held by a process outside this
    ``runtime_dir`` (a different controller, a manually-opened JViewer/
    browser session, or a daemon from a previous ``runtime_dir`` that has
    since been deleted) -- the BMC has no documented "kick the current
    holder" RPC, and this collection's policy (README.md) is to never invent
    an unsourced protocol call. If the slot is still held by something this
    function cannot see, the authentication attempt that follows will fail
    with ``ErrorClass.BMC_BUSY`` and a message naming the BMC cold reset
    escape hatch (see ``iusb.interpret_ack``).

    Returns the list of session_ids that were found and cleaned up (whether
    or not they were still alive), purely for diagnostics.
    """
    reclaimed = []
    for state in find_conflicting_sessions(runtime_dir, endpoint, exclude_session_id=exclude_session_id):
        session_id = state.get("session_id")
        pid = state.get("pid")
        if is_pid_alive(pid):
            request_stop(pid)
            wait_for_exit(pid, timeout=stop_timeout)
        if session_id:
            remove_state(runtime_dir, session_id)
            reclaimed.append(session_id)
    return reclaimed


# --------------------------------------------------------------------------
# Validation shared by check_mode and real attach -- runs in the *caller's*
# process, never in the daemon, so a bad path fails synchronously and
# visibly rather than only showing up later in the daemon's log file.
# --------------------------------------------------------------------------


def validate_image(path: str) -> None:
    """Open and immediately close the configured ISO, to fail fast on a bad path.

    :class:`iusb.FileReader` raises ``IsADirectoryError``/``OSError`` for the
    filesystem-level problems it can detect itself; both are re-raised here
    as :class:`errors.ProtocolError` so ``asmb8_media.py`` can catch one
    exception family, per every other module in this collection.
    """
    try:
        reader = iusb.FileReader.open(path)
    except OSError as exc:
        raise ProtocolError(f"invalid media image {path!r}: {exc}", operation="asmb8_media.validate_image") from exc
    reader.close()


def resolve_local_ip(host: str) -> str:
    """Thin re-export of :func:`iusb.resolve_local_ip`, kept here so
    ``asmb8_media.py`` and this module's own tests have one obvious place to
    monkeypatch it without reaching into ``iusb``.
    """
    return iusb.resolve_local_ip(host)


# --------------------------------------------------------------------------
# Spawning -- double-fork daemonize. No subprocess, no exec, no argv/env
# credential exposure.
#
# Forking (never exec'ing) means the daemon inherits this already-running
# interpreter's memory -- including the plaintext username/password already
# held as ordinary Python values -- via copy-on-write. There is no exec()
# anywhere in this path, so there is no argv and no environment handed to a
# new process image at all: nothing credential-shaped ever lands anywhere
# another process on the box could read it (a process listing, /proc/<pid>/
# environ, a shell history). The daemon closes over its arguments as
# ordinary function-call locals and never externalises them; the state file
# written below carries no credential-shaped field, and the freshly-minted
# iUSB/KVM token lives only in the daemon's own memory for as long as the
# session is open.
# --------------------------------------------------------------------------


def spawn_session(config: SessionConfig, *, username: str, password: str) -> int:
    """Fork the detached daemon and return its pid once it has reported in.

    A conventional double fork. The first child calls ``os.setsid()``
    (detaching from the controlling terminal/session, so nothing that
    signals this module's own process group reaches the daemon) and
    immediately forks the real daemon (the grandchild), then reports the
    grandchild's pid back to *this* process over a pipe and exits. This
    process reaps that short-lived first child with ``waitpid`` (so it never
    lingers as a zombie) and returns the daemon's pid, which the caller
    records in the state file.

    The daemon itself never returns to this function -- it runs
    :func:`_run_daemon` and then calls ``os._exit()`` unconditionally,
    whether it finished cleanly or hit an exception. It must never fall back
    into normal Python interpreter shutdown, which could re-run atexit
    handlers registered before the fork or double-flush inherited buffers.
    """
    if not hasattr(os, "fork"):
        raise UnsupportedCapabilityError(
            "asmb8_media's background-session mechanism requires a POSIX controller with os.fork() "
            "(Linux/macOS). It is not available when the Ansible controller itself runs on Windows.",
            operation="asmb8_media.spawn_session",
        )

    runtime_dir = Path(config.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    read_fd, write_fd = os.pipe()
    first_pid = os.fork()

    if first_pid > 0:
        # Original process: this is the only branch that returns normally.
        os.close(write_fd)
        with os.fdopen(read_fd, encoding="utf-8") as reader:
            reported = reader.read().strip()
        os.waitpid(first_pid, 0)  # reap the short-lived first child; never a zombie.
        if not reported:
            raise ProtocolError(
                "asmb8_media background daemon did not report a pid; it likely failed during "
                "os.setsid()/fork() before it could start the iUSB session -- check the session log",
                operation="asmb8_media.spawn_session",
            )
        pid = int(reported)
        # Create-only, never overwrite -- see _write_state_if_absent's docstring.
        _write_state_if_absent(
            config.runtime_dir,
            config.session_id,
            _initial_state(session_id=config.session_id, endpoint=f"{config.host}:{config.cd_port}", pid=pid, image=config.image),
        )
        return pid

    # First child: never returns to the caller. Always os._exit(), never raise past here.
    os.close(read_fd)
    try:
        os.setsid()
        second_pid = os.fork()
        if second_pid > 0:
            with os.fdopen(write_fd, "w", encoding="utf-8") as writer:
                writer.write(f"{second_pid}\n")
            os._exit(0)
        os.close(write_fd)
        _run_daemon(config, username, password)
        # Last-resort: a forked child must never propagate an exception into
        # normal interpreter shutdown; there is nothing left to report to.
    except BaseException:
        os._exit(1)
    os._exit(0)


# --------------------------------------------------------------------------
# The daemon itself.
# --------------------------------------------------------------------------

#: Set only by :func:`_handle_sigterm`, read only by the ``should_stop``
#: lambda :func:`_run_daemon` hands to ``iusb.Session.serve_forever`` -- see
#: that function's own docstring for exactly how this closes the slot instead
#: of merely ending the daemon process.
_stop_flag = False


def _handle_sigterm(_signum: int, _frame: object) -> None:
    """The ONLY thing a signal handler in this daemon is allowed to do.

    Python signal handlers run on the main thread between bytecode
    instructions, at a point the interpreter chooses, not one the handler
    controls -- doing anything beyond an atomic-enough attribute set here
    (network I/O, closing the iUSB session, even acquiring a lock) risks
    re-entering code that was not ready to be re-entered, or deadlocking
    against whatever the interrupted bytecode was in the middle of.

    So this sets a flag and returns, immediately. The real teardown --
    :func:`_run_daemon`'s existing normal-exit path: closing the iUSB
    session (which sends the TCP FIN the BMC needs to free its one media
    slot -- see this module's own docstring point 2) and writing a final
    state record -- runs on the daemon's own main-thread control flow, at a
    point it chooses: the top of ``iusb.Session.serve_forever``'s loop, via
    the ``should_stop=lambda: _stop_flag`` callback already wired up below.
    That loop wakes at least every ``_RECV_POLL_TIMEOUT`` seconds even on an
    otherwise-silent connection (see that constant's own docstring), so this
    flag is never stuck waiting on real traffic to be noticed.

    A self-pipe (write a byte to a pipe from the handler; have the main loop
    select()/recv() on it) was the other candidate here, and is the more
    common pattern when a loop is blocked in ``select()``/``poll()`` with no
    other periodic wake-up of its own. It buys nothing extra in THIS daemon,
    which already re-checks a plain condition on a bounded cadence for an
    unrelated reason (the idle-heartbeat/idle-streak bookkeeping in this
    module's docstring point 1) -- adding a pipe fd into ``iusb``'s transport
    layer just to multiplex a second wake source would be strictly more
    moving parts for the same outcome. A boolean flag, set from the handler
    and polled from the loop that was already polling something else, is the
    simplest thing that is still signal-handler-safe: assigning a name to a
    ``bool`` is atomic at the bytecode level in CPython (the GIL serialises
    it), so there is no partial-write hazard between the handler and the
    loop reading it, unlike a multi-step data structure would have.

    This is also naturally idempotent: a second (or third, ...) ``SIGTERM``
    arriving before the daemon has finished exiting just sets an
    already-``True`` flag to ``True`` again -- a no-op, never a re-entrant
    teardown, never an exception. There is nothing here that could "wedge"
    on a repeat signal, because there is nothing here beyond one assignment.

    Deliberately NOT ``SIGINT``: this daemon is always reached via
    ``request_stop()``'s ``os.kill(pid, signal.SIGTERM)`` (see
    ``daemon_runtime.request_stop``'s own docstring for why -- a backgrounded
    process inherits ``SIG_IGN`` for ``SIGINT`` from its launching shell's
    job control, so ``kill -INT`` on one is silently swallowed and never
    invokes this handler at all; that is not a theoretical concern, it is
    exactly what made a stray ``pkill`` during this collection's own
    development look, for a while, like the daemon had no signal handling at
    all).
    """
    global _stop_flag
    _stop_flag = True


def _run_daemon(config: SessionConfig, username: str, password: str) -> None:
    """The daemon's entire lifetime: log in, mint a media token, authenticate
    the iUSB session, and serve SCSI requests until told to stop.

    Runs in the grandchild produced by :func:`spawn_session`. Every exit from
    this function is through a state-file write recording the outcome --
    there is no other channel back to whatever, if anything, is still
    watching for this session. The username/password and the freshly-minted
    iUSB token exist only as local variables in this function's frame (and
    inside the ``AspClient``/``iusb.Session`` objects it builds); none of
    them are ever assigned into ``state``.
    """
    log_path = log_file_path(config.runtime_dir, config.session_id)
    _redirect_std_fds(log_path)
    signal.signal(signal.SIGTERM, _handle_sigterm)

    endpoint = f"{config.host}:{config.cd_port}"
    state: dict[str, Any] = _initial_state(session_id=config.session_id, endpoint=endpoint, pid=os.getpid(), image=config.image)

    def _persist(now: str | None = None) -> None:
        # Accepts an already-computed timestamp so a caller that needs "now"
        # for more than just this heartbeat (the idle-streak bookkeeping
        # below) spends exactly one _now_iso() call, not two slightly
        # different ones.
        state["updated_at"] = now if now is not None else _now_iso()
        _write_state_atomic(config.runtime_dir, config.session_id, state)

    # Claim the state file immediately, before opening the image or touching
    # the network -- see the sibling collection's identical rationale: this
    # record carries the daemon's real pid, and getting there first means
    # spawn_session's fallback write leaves this daemon's reports alone.
    _persist()

    # Every literal secret this daemon currently holds, for _fail()'s extra-secrets
    # pass below -- mirrors asp.AspClient._known_secrets()'s reasoning exactly: an
    # IkvmError's own message is already redacted at the point it was raised (see
    # errors.IkvmError.__init__), but the daemon's *own* free-text messages (an
    # unexpected, non-IkvmError exception's str(), or a future message this function
    # composes itself) are not, and redact()'s generic patterns only catch a
    # *labelled* secret (`password: ...`) -- a bare secret string with no separator
    # in front of it, e.g. a third-party library that happened to echo the raw
    # password or token into an exception message, would sail straight through.
    # Exact literal-substring matching is what catches that shape regardless of
    # punctuation. The token is appended once it exists (see below); the list is
    # mutated in place, not reassigned, so this closure always sees the current
    # contents.
    known_secrets: list[str] = [password]

    def _fail(message: str, *, error_class: str = ProtocolError.error_class) -> None:
        # error_class defaults to "protocol" only when nothing more specific
        # is available. Anything raised as an IkvmError keeps its own real
        # error_class -- see the except clause below.
        state["state"] = STATE_ERROR
        state["error"] = redact(message, extra_secrets=known_secrets)
        state["error_class"] = error_class
        _persist()

    reader: iusb.FileReader | None = None
    session: iusb.Session | None = None
    try:
        reader = iusb.FileReader.open(config.image)
        cache = iusb.Cache(reader, writer=None)
        device = iusb.CDROMDevice(cache)

        state["state"] = STATE_CONNECTING
        _persist()

        asp_client = AspClient(
            host=config.host,
            port=config.port,
            username=username,
            password=password,
            use_tls=config.use_tls,
            validate_certs=config.validate_certs,
            ca_path=config.ca_path,
            tls_fingerprint=config.tls_fingerprint,
            allow_insecure_transport=config.allow_insecure_transport,
            timeout=config.timeout,
            connect_timeout=config.connect_timeout,
        )
        asp_client.login()
        local_ip = resolve_local_ip(config.host)
        jnlp = asp_client.allocate_media_session(client_ip=local_ip)
        token = jnlp.kvm_token
        if token is None:
            # allocate_media_session() itself already raises ProtocolError when no
            # token was found in the JNLP; this is an extra, structural guard so a
            # future change there cannot silently let a None token reach build_auth.
            raise ProtocolError("jviewer.jnlp allocation did not yield a usable media token", endpoint=endpoint, operation="asmb8_media.attach")
        known_secrets.append(token)  # see known_secrets' docstring above -- _fail()'s extra-secrets backstop.

        session = iusb.Session.connect(
            config.host,
            config.cd_port,
            token,
            device_type=iusb.DEVICE_CDROM,
            instance=config.instance,
            timeout=config.connect_timeout,
        )
        del token, jnlp  # nothing below this line needs the token; drop the reference promptly.
        session.set_poll_timeout(_RECV_POLL_TIMEOUT)

        state["state"] = STATE_ATTACHED
        _persist()

        def _on_idle() -> None:
            # Heartbeat (updated_at moves; last_request_at does not, so a caller
            # can tell "idle" from "dead") PLUS the idle-streak bookkeeping this
            # module's docstring point 1 describes: how long has the CURRENT
            # quiet stretch run so far, and how many lifetime idle polls has
            # this session seen. One _now_iso() call, reused for both the
            # streak timestamp and the heartbeat itself.
            now = _now_iso()
            streak = state.get("current_idle_streak")
            if streak is None:
                state["current_idle_streak"] = {"started_at": now, "polls": 1, "seconds": _RECV_POLL_TIMEOUT}
            else:
                streak["polls"] += 1
                streak["seconds"] = streak["polls"] * _RECV_POLL_TIMEOUT
            state["idle_polls"] += 1
            _persist(now)

        def _on_request(_req: iusb.Packet) -> None:
            # Real traffic ends whatever idle streak was in progress -- see
            # _close_idle_streak's docstring: the closed streak stays visible
            # in last_idle_streak even after this line moves on. One
            # _now_iso() call, shared by the streak close, last_request_at,
            # and the heartbeat.
            now = _now_iso()
            _close_idle_streak(state, now=now)
            state["bytes_read"] = device.bytes_served()
            state["sectors_served"] = device.blocks_served()
            state["last_request_at"] = now
            _persist(now)

        # should_stop=lambda: _stop_flag is the ENTIRE effect SIGTERM has on this
        # daemon: _handle_sigterm (above) only ever flips that flag, and this is
        # the one place it is read. There is deliberately no second, signal-
        # specific teardown path -- a SIGTERM makes serve_forever() return
        # SERVE_STOPPED through the exact same route a local state=detached
        # request already used, so it inherits everything below unchanged: the
        # idle-streak close, the state-file write, and -- in the finally block
        # at the bottom of this function -- session.close(), which is what
        # actually sends the TCP FIN the BMC needs to see to free its one
        # media slot (see this module's own docstring point 2). Writing a
        # second, SIGTERM-only teardown here would risk it drifting out of
        # sync with this one and would be exactly the kind of thing a signal
        # handler must not attempt directly (see _handle_sigterm's docstring).
        outcome = session.serve_forever(device, should_stop=lambda: _stop_flag, on_idle=_on_idle, on_request=_on_request)

        # A normal exit (stopped/peer-closed/killed) may happen while a streak
        # is still open -- e.g. the session ended during what looked, up to
        # that point, like ordinary idle. Close it here so the final state
        # still shows how long that last stretch ran. Deliberately NOT done on
        # the exception path below -- see _close_idle_streak's own docstring.
        now = _now_iso()
        _close_idle_streak(state, now=now)
        state["bytes_read"] = device.bytes_served()
        state["sectors_served"] = device.blocks_served()
        if outcome == iusb.SERVE_STOPPED:
            # Reached via SIGTERM (request_stop()/asmb8_media state=detached) just as
            # often as via a direct in-process stop in a test -- see _handle_sigterm's
            # docstring. "signal" mirrors http_origin.py's own stop_reason vocabulary.
            state["state"] = STATE_DETACHED
            state["error"] = None
            state["stop_reason"] = "signal"
        elif outcome == iusb.SERVE_PEER_CLOSED:
            state["state"] = STATE_DETACHED
            state["error"] = "connection closed by peer"
            state["stop_reason"] = "peer_closed"
        else:  # iusb.SERVE_KILLED
            state["state"] = STATE_DETACHED
            state["error"] = "BMC sent a redirection-terminate (opcode 0xF6)"
            state["stop_reason"] = "bmc_terminate"
        _persist(now)
    except IkvmError as exc:
        # stop_reason is deliberately left at its initial None here: a crash is
        # already fully identified by state=error + error_class, and this field
        # exists to tell apart kinds of CLEAN stop from each other (see
        # _initial_state's docstring), not to duplicate that classification.
        _fail(str(exc), error_class=exc.error_class)
    except Exception as exc:  # last-resort: the daemon has no other way to report a crash.
        _fail(f"unexpected error: {exc}")
    finally:
        # This close() is the daemon's one and only teardown of the iUSB
        # session, reached on every exit path -- clean stop, peer close, BMC
        # kill, or an exception above -- not just the SIGTERM one. See the
        # comment above serve_forever() for why SIGTERM is routed through the
        # normal exit path specifically so it ends up here too, rather than
        # bypassing it.
        if session is not None:
            with contextlib.suppress(Exception):
                session.close()
        if reader is not None:
            with contextlib.suppress(Exception):
                reader.close()
