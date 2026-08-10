# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Detached, lifetime-capped local HTTP file server for ``asmb8_http_origin``.

This collection's entire reason for existing is headless bare-metal install
with **no standing infrastructure** -- no PXE, DHCP, TFTP, NFS or CIFS
service left running before or after a play. ``asmb8_http_origin`` is a
deliberate, narrow exception to "no standing infrastructure": a play that
needs to hand an installer a kernel/initrd/squashfs/answer-file set over HTTP
(because the target's firmware or installer only speaks HTTP, not iUSB) needs
*something* to serve those bytes from, for exactly as long as the play needs
it and not one second longer.

Shape mirrors ``media_session.py`` (the ``asmb8_media`` module's background
process) deliberately -- see that file's module docstring first if you have
not. Both modules solve "a long-lived daemon owned by a short-lived Ansible
task" with the same three mechanisms: a detached double-``fork()`` that never
``exec``s, an atomically-updated JSON state file keyed by ``session_id``, and
a bounded wait on that state file for the caller to observe an outcome. What
is genuinely different here is what a leaked instance of this daemon can do:
a forgotten iUSB session merely holds one BMC's single media slot hostage,
but a forgotten HTTP file server left listening on a management VLAN is an
open door to whatever was in the served directory (an installer ISO, a
provisioning answer file that may itself carry secrets) for as long as the
process happens to survive -- which, for a daemon nothing is watching, could
be forever. Three properties in this file exist specifically to bound that:

1. **A hard lifetime cap is the default, not an opt-in.** Every session gets
   a ``lifetime_seconds`` deadline (see :data:`DEFAULT_LIFETIME_SECONDS`) set
   at spawn time; the daemon self-terminates when it elapses, with no
   dependency on the controller, the play, or anything else still being
   alive to ask it to. This is the primary safety property this module
   exists to provide: a controller that crashes, loses power, or is merely
   interrupted mid-play cannot leave this server running past that deadline.
2. **Path confinement is enforced on every single request, independently of
   what the daemon was told to serve.** :func:`resolve_within_root` re-derives
   the real, symlink-resolved target of a request path and refuses anything
   that lands outside the served root -- see its own docstring for the
   ``..``/percent-encoding/double-encoding cases this defends against.
3. **Every request is logged to a file the operator can read after the
   play ends**, specifically so a failed install ("the installer 404'd
   fetching X") is diagnosable from evidence instead of guesswork -- see
   :func:`_append_access_log`.

As with ``media_session.py``: this module owns exactly the process
lifecycle, state-file bookkeeping, path confinement, and request serving. It
knows nothing about Ansible; ``plugins/modules/asmb8_http_origin.py`` is its
only caller.
"""

from __future__ import annotations

import contextlib
import datetime
import http.server
import json
import mimetypes
import os
import signal
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import ErrorClass, ProtocolError, UnsupportedCapabilityError, redact

#: Observable session states, written to the state file's ``state`` key.
STATE_STARTING = "starting"
STATE_SERVING = "serving"
STATE_STOPPED = "stopped"
STATE_ERROR = "error"

#: States a live daemon will never revert out of on its own -- once here, the
#: process is expected to be exiting or already gone. Mirrors
#: ``media_session.TERMINAL_STATES``.
TERMINAL_STATES = frozenset({STATE_STOPPED, STATE_ERROR})

#: Default location for state/log files, one per session_id. A per-user,
#: per-collection directory rather than a shared /tmp path, for the same
#: reason ``media_session.DEFAULT_RUNTIME_DIR`` gives.
DEFAULT_RUNTIME_DIR = "~/.ansible/asmb8_ikvm/http-origins"

#: Bind to loopback, not every interface, unless a caller explicitly opts
#: out. This server's entire purpose is to hand bytes to a machine being
#: provisioned, which -- unlike the controller running this module -- is
#: essentially never the loopback interface itself; a caller who wants this
#: origin reachable from anywhere but the controller (the normal case) must
#: say so explicitly by setting ``bind_address`` to a routable address. That
#: asymmetry is deliberate: the failure mode this collection's owner is
#: guarding against is a file server that answers on a management VLAN
#: without anyone having decided it should, and a default that already binds
#: everywhere would make that the *unattended* outcome the very first time a
#: caller forgets the option. Binding only loopback by default instead makes
#: the unattended outcome "nothing outside this machine can reach it" -- safe
#: to fail forward from, since check-mode and a first real run both surface
#: an unreachable target loudly (the installer cannot fetch anything) rather
#: than silently.
DEFAULT_BIND_ADDRESS = "127.0.0.1"

#: 0 asks the OS to pick a free ephemeral port, which is this module's own
#: default -- see spawn_session/_run_daemon for where the actually-bound port
#: is read back with getsockname() and recorded.
DEFAULT_PORT = 0

#: Four hours. ``media_session.py``'s own module docstring records a real
#: iUSB session sitting idle for 130 consecutive seconds while a host waited
#: at a bootloader menu, and notes unattended installs "can be an hour or
#: more". This default is chosen to comfortably outlast a legitimately slow
#: install (doubling that "an hour or more" figure several times over) while
#: still being a real, finite backstop rather than something indistinguishable
#: from "forever" -- an operator provisioning something that genuinely needs
#: longer should raise ``lifetime_seconds`` explicitly, which is a deliberate
#: choice this module can then show in the play, rather than this module
#: guessing at a bound generous enough for every future workload.
DEFAULT_LIFETIME_SECONDS = 4 * 60 * 60

#: How often the daemon's control loop wakes to check for a stop request or
#: an elapsed lifetime deadline. A responsiveness knob for SIGTERM/the
#: lifetime cap, not a serving-side timeout -- the HTTP server itself runs on
#: its own thread and is not gated by this value.
_CONTROL_POLL_INTERVAL = 0.2

#: How often callers waiting on a state-file transition (start confirmation,
#: stop confirmation) re-check the file. Mirrors
#: ``media_session._STATE_POLL_INTERVAL``.
_STATE_POLL_INTERVAL = 0.2

#: Read/write chunk size used when streaming a file's bytes to a client.
_COPY_BUFFER_SIZE = 64 * 1024


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Everything the daemon needs to bind, serve, and self-terminate.

    Deliberately carries no credential of any kind -- this server has no
    concept of authentication; the confinement it relies on is
    :func:`resolve_within_root`, not a secret in the URL.
    """

    session_id: str
    root: str
    bind_address: str
    port: int
    lifetime_seconds: int
    runtime_dir: str


# --------------------------------------------------------------------------
# State file plumbing -- shared by the daemon (writer) and the module (reader).
# Mirrors media_session.py's identical functions; see that file for the fuller
# rationale behind atomic writes, create-only writes, and treating a corrupt
# read as "no session" rather than a hard failure.
# --------------------------------------------------------------------------


def state_file_path(runtime_dir: str | os.PathLike[str], session_id: str) -> Path:
    return Path(runtime_dir) / f"{session_id}.json"


def log_file_path(runtime_dir: str | os.PathLike[str], session_id: str) -> Path:
    """Where the daemon's own stdout/stderr are redirected -- crash diagnosis only.

    Request-level diagnosis (which files were actually fetched, and any
    404s) belongs in :func:`access_log_file_path`, a separate file, not here.
    """
    return Path(runtime_dir) / f"{session_id}.log"


def access_log_file_path(runtime_dir: str | os.PathLike[str], session_id: str) -> Path:
    """One JSON-lines record per HTTP request served (or refused) by this session.

    This is the file an operator reads after a failed install to answer "did
    the installer even ask for the file it needed" -- see this module's own
    docstring point 3. Kept separate from :func:`log_file_path` (the daemon's
    stdout/stderr) so a request log with potentially many lines never mixes
    with the much rarer crash/traceback output on the same stream.
    """
    return Path(runtime_dir) / f"{session_id}-access.log"


def read_state(runtime_dir: str | os.PathLike[str], session_id: str) -> dict[str, Any] | None:
    """Read and parse the state file, or ``None`` if it does not exist or is unreadable."""
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
    """Write ``data`` as the current state, atomically -- write-temp-then-rename."""
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


def _initial_state(*, session_id: str, pid: int, root: str, bind_address: str, port: int) -> dict[str, Any]:
    """The starting state record, in the one shape every writer must use.

    Two independent writers of a session's *first* state record exist -- the
    daemon itself (:func:`_run_daemon`) and the process that forked it
    (:func:`spawn_session`, for the case where the daemon dies before writing
    anything) -- and, per ``media_session.py``'s identical lesson, they must
    never disagree on shape.
    """
    now = _now_iso()
    return {
        "session_id": session_id,
        "pid": pid,
        "state": STATE_STARTING,
        "error": None,
        "error_class": None,
        "root": root,
        "bind_address": bind_address,
        "port": port,
        "url": None,
        "request_count": 0,
        "bytes_served": 0,
        "last_request_at": None,
        "started_at": now,
        "updated_at": now,
        "stop_reason": None,
    }


def _write_state_if_absent(runtime_dir: str | os.PathLike[str], session_id: str, data: dict[str, Any]) -> bool:
    """Write ``data`` as the state record only if no record exists yet. Returns whether it wrote.

    Create-only counterpart of :func:`_write_state_atomic` -- see
    ``media_session._write_state_if_absent``'s docstring for the exact race
    this closes: by the time :func:`spawn_session` returns, the daemon may
    already have written ``serving`` or even its final ``error``, and an
    unconditional write here would clobber that with an older, less
    informative record.
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


def remove_state(runtime_dir: str | os.PathLike[str], session_id: str) -> None:
    """Delete the state, daemon-log and access-log files for ``session_id``, if present.

    Silent about files that are already gone -- callers call this defensively
    without checking existence first.
    """
    for path in (
        state_file_path(runtime_dir, session_id),
        log_file_path(runtime_dir, session_id),
        access_log_file_path(runtime_dir, session_id),
    ):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def is_pid_alive(pid: int | None) -> bool:
    """Whether ``pid`` refers to a live process this user can at least signal-probe.

    Identical reasoning to ``media_session.is_pid_alive`` -- see that
    function's docstring for why ``None``/non-positive values are rejected
    before ever reaching ``os.kill``.
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
    """Poll the state file until ``until(state)`` is true or ``timeout`` elapses."""
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
    """Ask the daemon at ``pid`` to shut down. Silent if it is already gone or not a real pid."""
    if pid is None or pid <= 0:
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)


# --------------------------------------------------------------------------
# Validation shared by check_mode and a real start -- runs in the *caller's*
# process, never in the daemon, so a bad path fails synchronously and
# visibly rather than only showing up later in the daemon's log file.
# --------------------------------------------------------------------------


def validate_root(path: str) -> Path:
    """Resolve ``path`` to a real, existing directory, or raise :class:`ProtocolError`.

    The resolved, symlink-followed path is what :func:`spawn_session` and
    :func:`resolve_within_root` both use as the served root -- resolving once
    here, up front, means the confinement check on every later request
    compares against one settled value rather than re-resolving (and
    potentially re-following a symlink that changed underneath it) per
    request.
    """
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProtocolError(f"invalid path {path!r}: {exc}", operation="asmb8_http_origin.validate_root") from exc
    if not resolved.is_dir():
        raise ProtocolError(f"path {path!r} is not a directory", operation="asmb8_http_origin.validate_root")
    return resolved


# --------------------------------------------------------------------------
# Path confinement. Enforced on every single request -- see the module
# docstring's point 2.
# --------------------------------------------------------------------------


def resolve_within_root(root: Path, raw_target: str) -> Path | None:
    """Resolve an HTTP request-target to a real path guaranteed to be inside ``root``.

    ``root`` must already be fully resolved (see :func:`validate_root`).
    ``raw_target`` is the raw request-target exactly as received on the
    request line (``self.path`` on a ``http.server.BaseHTTPRequestHandler``)
    -- still percent-encoded, still possibly carrying a query string.

    Returns ``None`` -- meaning "refuse; never serve this" -- for any of:

    * A ``..`` segment, however it was spelled: a literal ``..``, a single
      percent-encoded ``%2e%2e``, or a percent-encoded separator
      (``foo%2f..%2fbar``) that only becomes a real ``/``-delimited ``..``
      segment after decoding.
    * A symlink, anywhere along the resolved path, whose target lands
      outside ``root`` -- checked by resolving the *whole* candidate path
      (not just inspecting the final component) and comparing the result
      against ``root``.
    * A NUL byte, or a percent-encoding that does not decode as UTF-8.

    **Deliberately decodes the percent-encoding exactly once.** A *double*
    encoding traversal (``%252e%252e``) depends on a second decode pass to
    turn into ``..`` -- ``%25`` decodes to a literal ``%``, so one pass turns
    ``%252e%252e`` into the literal, inert string ``%2e%2e``, which this
    function then treats as an ordinary (almost certainly nonexistent)
    filename component, never as a parent-directory reference. Decoding
    twice, or decoding in a loop until stable, is exactly the bug that would
    let this style of attack through, so this function must never do either.

    A caller-visible corollary of decoding once and then splitting on ``/``:
    a percent-encoded separator (``%2f``) is decoded into a real ``/``
    *before* the split happens, so it is caught by the very same
    segment-based ``..`` check as a literal separator -- there is no need
    for it to be handled as a special case.
    """
    # Strip the query string (and, defensively, a fragment -- real HTTP clients never
    # send one, but nothing stops a hostile one from trying) by hand, rather than with
    # urllib.parse.urlsplit(). An HTTP request-target is an origin-form path, not a full
    # URI with an authority component, and urlsplit() does not know that: it treats a
    # LEADING DOUBLE SLASH as introducing a netloc ("//file.txt" parses with path=""
    # and netloc="file.txt"), which would make this function silently ignore the very
    # segment it is supposed to be confining.
    path_part = raw_target.split("?", 1)[0].split("#", 1)[0]
    try:
        decoded = urllib.parse.unquote(path_part, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if "\x00" in decoded:
        return None

    segments = [segment for segment in decoded.split("/") if segment not in ("", ".")]
    if any(segment == ".." for segment in segments):
        return None

    candidate = root.joinpath(*segments) if segments else root
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None

    if resolved != root and root not in resolved.parents:
        return None
    return resolved


# --------------------------------------------------------------------------
# Range requests (RFC 7233). Bootloaders and installers commonly issue
# ranged reads; a server that ignores Range while still returning 200 (the
# full body) silently corrupts a fetch that expected only the requested
# slice. Parsed as a pure function so it is directly unit-testable without a
# running server.
# --------------------------------------------------------------------------


class RangeNotSatisfiable(Exception):
    """A syntactically valid single byte-range names no valid position in the
    file -- the caller must respond 416 with ``Content-Range: bytes */<size>``.
    """


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int  # inclusive


def parse_range_header(value: str | None, size: int) -> ByteRange | None:
    """Parse a single ``Range: bytes=...`` header against a resource of ``size`` bytes.

    Returns ``None`` when there is no ``Range`` header, or it is present but
    not a single ``bytes`` range this parser understands (a multi-range
    request, a non-``bytes`` unit, or malformed syntax) -- per RFC 7233
    Â§3.1, a server MAY ignore a Range header it does not support and respond
    as if it were absent, which is exactly what a caller receiving ``None``
    back should do (serve the full body, status 200).

    Raises :class:`RangeNotSatisfiable` when the header names a single valid
    ``bytes`` range whose start position is at or beyond the end of the
    resource (RFC 7233 Â§4.4) -- e.g. ``Range: bytes=10000-`` against a
    10-byte file, or any range at all against a zero-byte one.
    """
    if not value:
        return None
    if "," in value:
        # Multiple ranges: within scope this module can ignore per Â§3.1 rather
        # than implement multipart/byteranges, which no installer this
        # collection targets has been observed to require.
        return None
    if not value.startswith("bytes="):
        return None

    spec = value[len("bytes=") :].strip()
    if "-" not in spec:
        return None
    start_text, _sep, end_text = spec.partition("-")

    if start_text == "" and end_text == "":
        return None

    if start_text == "":
        # Suffix range: "the last N bytes". A suffix length of 0 is treated as
        # unsatisfiable rather than a valid empty range -- matching common
        # server practice, and avoiding a zero-length "successful" 206 that
        # would be indistinguishable from a client-visible non-event.
        try:
            suffix_length = int(end_text)
        except ValueError:
            return None
        if suffix_length <= 0 or size == 0:
            raise RangeNotSatisfiable
        start = max(0, size - suffix_length)
        return ByteRange(start=start, end=size - 1)

    try:
        start = int(start_text)
    except ValueError:
        return None

    if start < 0 or size == 0 or start >= size:
        raise RangeNotSatisfiable

    if end_text == "":
        return ByteRange(start=start, end=size - 1)

    try:
        end = int(end_text)
    except ValueError:
        return None
    if end < start:
        return None  # malformed ("bytes=10-5"): ignore rather than guess at intent.
    return ByteRange(start=start, end=min(end, size - 1))


# --------------------------------------------------------------------------
# Request logging. See the module docstring's point 3.
# --------------------------------------------------------------------------


def _append_access_log(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON-lines record. Best-effort: a logging failure must never
    take down a request that otherwise served (or correctly refused to serve)
    successfully.
    """
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        line = json.dumps(record, sort_keys=True) + "\n"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


# --------------------------------------------------------------------------
# The request handler. Built by a factory (rather than defined at module
# scope) because http.server.HTTPServer instantiates its handler class with a
# fixed 3-argument signature (request, client_address, server) -- a closure
# over the per-session root/log path/shared state is the standard way to hand
# a stdlib request handler extra, per-instance context.
# --------------------------------------------------------------------------


def _build_handler_class(
    *,
    root: Path,
    access_log_path: Path,
    state: dict[str, Any],
    state_lock: threading.Lock,
    persist: Callable[[], None],
) -> type[http.server.BaseHTTPRequestHandler]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        server_version = "asmb8-http-origin/1.0"
        sys_version = ""  # do not advertise the controller's Python version to the target.
        protocol_version = "HTTP/1.1"

        root_dir: ClassVar[Path] = root

        def do_HEAD(self) -> None:
            self._handle(send_body=False)

        def do_GET(self) -> None:
            self._handle(send_body=True)

        def _handle(self, *, send_body: bool) -> None:
            status = 200
            outcome = "ok"
            bytes_sent = 0
            try:
                resolved = resolve_within_root(self.root_dir, self.path)
                if resolved is None:
                    status, outcome = 404, "blocked_traversal"
                    self.send_error(404, "Not Found")
                    return
                if not resolved.is_file():
                    status, outcome = 404, "not_found"
                    self.send_error(404, "Not Found")
                    return

                try:
                    file_size = resolved.stat().st_size
                except OSError:
                    status, outcome = 404, "not_found"
                    self.send_error(404, "Not Found")
                    return

                byte_range: ByteRange | None = None
                try:
                    byte_range = parse_range_header(self.headers.get("Range"), file_size)
                except RangeNotSatisfiable:
                    status, outcome = 416, "range_not_satisfiable"
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                start, end = (0, file_size - 1) if byte_range is None else (byte_range.start, byte_range.end)
                length = end - start + 1
                content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"

                status = 206 if byte_range is not None else 200
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if byte_range is not None:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.end_headers()

                if send_body:
                    try:
                        with resolved.open("rb") as handle:
                            handle.seek(start)
                            remaining = length
                            while remaining > 0:
                                chunk = handle.read(min(_COPY_BUFFER_SIZE, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                bytes_sent += len(chunk)
                                remaining -= len(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        outcome = "client_disconnected"
                else:
                    bytes_sent = 0
            except OSError as exc:
                status, outcome = 500, "error"
                with contextlib.suppress(Exception):
                    self.send_error(500, redact(str(exc)))
            finally:
                with state_lock:
                    state["request_count"] += 1
                    state["bytes_served"] += bytes_sent
                    state["last_request_at"] = _now_iso()
                    persist()
                _append_access_log(
                    access_log_path,
                    {
                        "time": _now_iso(),
                        "method": self.command,
                        "path": self.path,
                        "status": status,
                        "outcome": outcome,
                        "bytes_sent": bytes_sent,
                        "client": self.client_address[0],
                    },
                )

        def log_message(self, log_format: str, *args: Any) -> None:
            # Suppress the default stderr access log entirely -- _append_access_log()
            # above is this module's authoritative, structured request log.
            pass

    return _Handler


# --------------------------------------------------------------------------
# Spawning -- double-fork daemonize. No subprocess, no exec.
#
# This server has no secret to protect via copy-on-write the way
# asmb8_media's iUSB daemon protects a BMC password -- but forking (never
# exec'ing) is kept anyway, matching this collection's one established
# pattern for "a long-lived process a short-lived Ansible task must own the
# lifecycle of, without a subprocess module's argv/environ surface, and
# without depending on the controller staying alive": that pattern is what
# makes the lifetime cap and SIGTERM-based stop in this file work the same
# way asmb8_media's do, for the same reasons.
# --------------------------------------------------------------------------


def spawn_session(config: SessionConfig) -> int:
    """Fork the detached daemon and return its pid once it has reported in.

    A conventional double fork -- see ``media_session.spawn_session``'s
    docstring for the full mechanics (first child ``setsid()``s and forks the
    real daemon, then reports the grandchild's pid back over a pipe and
    exits; this process reaps it via ``waitpid`` so it never lingers as a
    zombie). The daemon itself never returns to this function.
    """
    if not hasattr(os, "fork"):
        raise UnsupportedCapabilityError(
            "asmb8_http_origin's background-server mechanism requires a POSIX controller with os.fork() "
            "(Linux/macOS). It is not available when the Ansible controller itself runs on Windows.",
            operation="asmb8_http_origin.spawn_session",
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
                "asmb8_http_origin background server did not report a pid; it likely failed during "
                "os.setsid()/fork() before it could start listening -- check the session log",
                operation="asmb8_http_origin.spawn_session",
            )
        pid = int(reported)
        # Create-only, never overwrite -- see _write_state_if_absent's docstring.
        _write_state_if_absent(
            config.runtime_dir,
            config.session_id,
            _initial_state(session_id=config.session_id, pid=pid, root=config.root, bind_address=config.bind_address, port=config.port),
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
        _run_daemon(config)
        # Last-resort: a forked child must never propagate an exception into
        # normal interpreter shutdown; there is nothing left to report to.
    except BaseException:
        os._exit(1)
    os._exit(0)


# --------------------------------------------------------------------------
# The daemon itself.
# --------------------------------------------------------------------------

_stop_flag = False


def _handle_sigterm(_signum: int, _frame: object) -> None:
    global _stop_flag
    _stop_flag = True


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _redirect_std_fds(log_path: Path) -> None:
    """Detach stdin/stdout/stderr from whatever this process inherited across the fork.

    Identical rationale to ``media_session._redirect_std_fds``: this daemon
    deliberately outlives the Ansible module invocation that forked it, and a
    duplicated stdout pipe would make the controller hang past the module's
    own exit waiting for an EOF this long-lived process never provides.
    """
    devnull_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull_fd, 0)
    os.close(devnull_fd)

    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)


def _format_bound_url(bind_address: str, port: int) -> str:
    host = f"[{bind_address}]" if ":" in bind_address else bind_address
    return f"http://{host}:{port}/"


def _run_daemon(config: SessionConfig) -> None:
    """The daemon's entire lifetime: bind, serve, and self-terminate.

    Runs in the grandchild produced by :func:`spawn_session`. Every exit from
    this function is through a state-file write recording the outcome -- see
    ``media_session._run_daemon``'s identical framing.

    The HTTP server's own ``serve_forever()`` runs on a dedicated thread; this
    function's own thread (the daemon's main thread) does nothing but wait
    for a stop signal or the lifetime deadline, then calls
    ``server.shutdown()`` from a *different* thread than the one running
    ``serve_forever()`` -- which is the one supported way to call it without
    deadlocking (calling it from the same thread that is blocked inside
    ``serve_forever()``, e.g. from within a signal handler that interrupted
    that same thread, waits on an event only that same thread's own loop can
    ever set).
    """
    log_path = log_file_path(config.runtime_dir, config.session_id)
    _redirect_std_fds(log_path)
    signal.signal(signal.SIGTERM, _handle_sigterm)

    access_log_path = access_log_file_path(config.runtime_dir, config.session_id)
    state: dict[str, Any] = _initial_state(session_id=config.session_id, pid=os.getpid(), root=config.root, bind_address=config.bind_address, port=config.port)
    state_lock = threading.Lock()

    def _persist() -> None:
        state["updated_at"] = _now_iso()
        with contextlib.suppress(OSError):
            _write_state_atomic(config.runtime_dir, config.session_id, dict(state))

    # Claim the state file immediately, before ever resolving the root or binding a socket --
    # see media_session._run_daemon's identical rationale: this record carries the daemon's real
    # pid, and getting there first means spawn_session's fallback write leaves this daemon's own
    # reports alone.
    _persist()

    try:
        # Re-resolve here rather than trusting config.root verbatim: the confinement check in
        # resolve_within_root() is only sound if the root it compares against is itself fully
        # resolved (symlinks followed, made absolute) -- callers are expected to have already
        # done this once (see validate_root()), but resolving again is cheap and idempotent, and
        # doing it defensively here means a caller-side mistake fails as an ordinary,
        # classified STATE_ERROR instead of silently 404-ing every single request.
        root = Path(config.root).resolve(strict=True)
    except OSError as exc:
        state["state"] = STATE_ERROR
        state["error"] = redact(f"invalid root {config.root!r}: {exc}")
        state["error_class"] = ErrorClass.PROTOCOL
        _persist()
        return

    try:
        handler_cls = _build_handler_class(root=root, access_log_path=access_log_path, state=state, state_lock=state_lock, persist=_persist)
        server = http.server.ThreadingHTTPServer((config.bind_address, config.port), handler_cls)
    except OSError as exc:
        state["state"] = STATE_ERROR
        state["error"] = redact(f"failed to bind {config.bind_address}:{config.port}: {exc}")
        state["error_class"] = ErrorClass.CONNECTION
        _persist()
        return

    actual_port = server.server_address[1]
    state["port"] = actual_port
    state["url"] = _format_bound_url(config.bind_address, actual_port)
    state["state"] = STATE_SERVING
    _persist()

    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    server_thread.start()

    deadline = time.monotonic() + config.lifetime_seconds
    reason = "signal"
    try:
        while True:
            if _stop_flag:
                reason = "signal"
                break
            if time.monotonic() >= deadline:
                reason = "lifetime_expired"
                break
            time.sleep(_CONTROL_POLL_INTERVAL)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5.0)

    state["state"] = STATE_STOPPED
    state["stop_reason"] = reason
    _persist()
