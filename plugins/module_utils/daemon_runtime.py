# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared plumbing for this collection's two detached-daemon module_utils:
``media_session.py`` (``asmb8_media``) and ``http_origin.py``
(``asmb8_http_origin``).

Both modules solve the same underlying problem -- "a long-lived process a
short-lived Ansible task must own the lifecycle of, without a subprocess
module's argv/environ surface, and without depending on the controller
staying alive" -- with the same mechanisms: a detached double-``fork()``, an
atomically-updated JSON state file keyed by ``session_id``, liveness checks
by pid, and bounded polling waits for a state transition or process exit.
That machinery was originally written twice, independently, while both
modules were under active development in parallel -- deliberately, so two
agents working concurrently never had to coordinate over a shared file. This
module is the result of de-duplicating it once both daemons had settled:
every function here is *plumbing*, identical in both callers byte-for-byte
before this file existed, with no daemon-specific policy baked in.

What stays OUT of this file, deliberately, because it is policy rather than
plumbing, and the two daemons differ on it:

* The state record's own shape (``_initial_state``) -- the two daemons
  observe different things (idle streaks and SCSI byte counts for one,
  request counts and a served root for the other) and do not share a schema.
* ``STATE_*``/``TERMINAL_STATES`` constants -- the state machines are
  different (``starting``/``connecting``/``attached``/``detached``/``error``
  versus ``starting``/``serving``/``stopped``/``error``).
* ``SessionConfig`` -- different fields entirely.
* The ``SIGTERM`` handler and its ``_stop_flag`` -- kept one per daemon
  module, not shared, even though the three-line handler body is identical
  in both. A signal handler mutates a *process-global* flag via Python's
  ``global`` statement bound to whichever module's namespace it is defined
  in; a serve/control loop's ``lambda: _stop_flag`` closes over a name looked
  up in ITS OWN module's globals at call time. Importing one shared
  ``_handle_sigterm``/``_stop_flag`` pair into both daemon modules would
  silently split them into two different variables the moment either module
  rebinds its own copy of the name (which is exactly what every existing
  test's ``monkeypatch.setattr(media_session, "_stop_flag", ...)`` does) --
  the signal handler would set one module's flag while the serve loop reads
  the other's, and SIGTERM would stop being noticed at all. Three duplicated
  lines per daemon is a far smaller cost than that failure mode.
* ``remove_state`` -- each daemon deletes a different set of files (the
  media daemon: state + daemon log; the HTTP origin: state + daemon log +
  a separate request-access log). Both are built from :func:`remove_paths`
  below, so the actual deletion loop is still shared; only the list of which
  paths to pass it differs.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import signal
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: How often callers waiting on a state-file transition (attach/start
#: confirmation, detach/stop confirmation) re-check the file. Identical in
#: both daemons before this file existed; not a per-daemon tuning knob.
_STATE_POLL_INTERVAL = 0.2


# --------------------------------------------------------------------------
# State file plumbing -- shared by each daemon (writer) and its module
# (reader). See each daemon module's own ``_initial_state``/``STATE_*`` for
# the policy this plumbing is deliberately blind to.
# --------------------------------------------------------------------------


def state_file_path(runtime_dir: str | os.PathLike[str], session_id: str) -> Path:
    return Path(runtime_dir) / f"{session_id}.json"


def log_file_path(runtime_dir: str | os.PathLike[str], session_id: str) -> Path:
    """Where a daemon's stdout/stderr are redirected -- see :func:`redirect_std_fds`."""
    return Path(runtime_dir) / f"{session_id}.log"


def read_state(runtime_dir: str | os.PathLike[str], session_id: str) -> dict[str, Any] | None:
    """Read and parse the state file, or ``None`` if it does not exist or is unreadable.

    A parse failure degrades to ``None`` (treated as "no session") rather
    than raising: the atomic write below makes a torn read very unlikely,
    but this file is a best-effort receipt, not a source of truth a caller
    should ever fail hard on.
    """
    try:
        raw = state_file_path(runtime_dir, session_id).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def list_session_ids(runtime_dir: str | os.PathLike[str]) -> list[str]:
    """Every session_id with a state file under ``runtime_dir``. Empty if the
    directory does not exist yet.
    """
    directory = Path(runtime_dir)
    if not directory.is_dir():
        return []
    return [path.stem for path in directory.glob("*.json")]


def _write_state_atomic(runtime_dir: str | os.PathLike[str], session_id: str, data: dict[str, Any]) -> None:
    """Write ``data`` as the current state, atomically.

    Write to a sibling temp path then ``os.replace()`` it over the real
    path, so a reader never observes a partially-written file. The temp file
    is created ``0600`` explicitly rather than left to the umask, since the
    state file may carry operational detail (paths, endpoints) an operator
    would not want world-readable even briefly.
    """
    path = state_file_path(runtime_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")

    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    os.replace(tmp_path, path)


def _write_state_if_absent(runtime_dir: str | os.PathLike[str], session_id: str, data: dict[str, Any]) -> bool:
    """Write ``data`` as the state record only if no record exists yet. Returns whether it wrote.

    Create-only counterpart of :func:`_write_state_atomic`, for the race
    inherent to every ``spawn_session()`` in this collection: by the time the
    forking parent process is ready to write its own fallback first report,
    the daemon it just forked may already have written ``starting`` or even
    its final ``error`` -- an unconditional write here would clobber that
    with an older, less informative record. ``O_CREAT | O_EXCL`` makes the
    kernel the tiebreaker rather than scheduling luck.
    """
    path = state_file_path(runtime_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    return True


def remove_paths(*paths: Path) -> None:
    """Delete every path in ``paths``, silently skipping ones already gone.

    The shared tail of both daemons' ``remove_state()`` -- each calls this
    with its own, different list of files (state, daemon log, and for
    ``asmb8_http_origin`` also its access log) rather than this module
    guessing at what a given daemon writes.
    """
    for path in paths:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def is_pid_alive(pid: int | None) -> bool:
    """Whether ``pid`` refers to a live process this user can at least signal-probe.

    ``os.kill(pid, 0)`` sends no signal; it only asks the kernel whether the
    target exists and is signalable. A ``PermissionError`` means it exists
    but is owned by someone else -- treated as "alive" here, since the only
    thing that matters for staleness detection is whether the pid has been
    recycled for an unrelated process.

    ``pid`` values that cannot be a live daemon's own pid by construction
    (``None``; zero or negative, which ``os.kill`` treats as "every process
    in a group" rather than one specific process) are rejected before ever
    reaching ``os.kill`` -- a state file is untrusted-ish input, and
    signalling an entire process group over a malformed pid field would be a
    far worse failure mode than reporting "not alive".
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def generate_session_id() -> str:
    return uuid.uuid4().hex


def wait_for_state(
    runtime_dir: str | os.PathLike[str],
    session_id: str,
    *,
    until: Callable[[dict[str, Any]], bool],
    timeout: float,
    poll_interval: float = _STATE_POLL_INTERVAL,
) -> dict[str, Any] | None:
    """Poll the state file until ``until(state)`` is true or ``timeout`` elapses.

    Returns whatever was last read -- which may not satisfy ``until`` if the
    timeout won the race; the caller decides what an unsatisfied wait means.
    """
    deadline = time.monotonic() + timeout
    observed: dict[str, Any] | None = None
    while True:
        observed = read_state(runtime_dir, session_id)
        if observed is not None and until(observed):
            return observed
        if time.monotonic() >= deadline:
            return observed
        time.sleep(poll_interval)


def wait_for_exit(pid: int | None, *, timeout: float, poll_interval: float = _STATE_POLL_INTERVAL) -> bool:
    """Poll ``is_pid_alive(pid)`` until it goes false or ``timeout`` elapses. Returns whether it exited."""
    deadline = time.monotonic() + timeout
    while is_pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)
    return True


def request_stop(pid: int | None) -> None:
    """Ask the daemon at ``pid`` to shut down via ``SIGTERM``. Silent if it is
    already gone or not a real pid.

    ``SIGTERM`` specifically, never ``SIGINT``: a backgrounded process
    started from an interactive shell (as happened repeatedly while
    developing the daemons this plumbing serves) inherits ``SIG_IGN`` for
    ``SIGINT`` from that shell's own job control, so ``kill -INT`` on such a
    process is silently swallowed and the process never notices. ``SIGTERM``
    carries no such inherited-disposition trap and is what every daemon in
    this collection actually installs a handler for -- see each daemon
    module's own ``_handle_sigterm``. See :func:`is_pid_alive` for why
    ``None``/non-positive values are rejected before ever reaching
    ``os.kill``.
    """
    if pid is None or pid <= 0:
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _redirect_std_fds(log_path: Path) -> None:
    """Detach stdin/stdout/stderr from whatever a forked daemon inherited across the fork.

    Correctness requirement, not cosmetic: both daemons this serves
    deliberately outlive the Ansible module invocation that forked them, and
    a duplicated stdout pipe would make the controller hang past the
    module's own exit waiting for an EOF a long-lived process never
    provides. Must run before any other daemon logic.
    """
    devnull_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull_fd, 0)
    os.close(devnull_fd)

    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)
