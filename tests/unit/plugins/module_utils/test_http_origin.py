# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for ``http_origin.py``, the background HTTP file server behind
``asmb8_http_origin``.

Split deliberately into two tiers, both exercised for real -- nothing here
talks to a BMC or any lab host, so unlike ``media_session.py`` there is no
reason to defer the interesting behaviour to an "another agent's
responsibility" integration target:

* Request handling (path confinement, Range, HEAD/GET, the access log) is
  driven against a real ``http.server.ThreadingHTTPServer`` running the
  production handler class on a background thread of *this* process, bound
  to loopback on an OS-picked port. No ``fork()`` involved -- this is the
  fast, high-volume tier.
* Process lifecycle (the double fork, the hard lifetime cap actually
  terminating the daemon, SIGTERM producing a clean stop, the create-only
  state-file race) goes through the real :func:`http_origin.spawn_session`,
  because that is the one thing an in-process server cannot exercise at all.
  Every test in this tier binds and connects on 127.0.0.1 only and always
  cleans up the forked pid, including on failure.

Historical note, because it explains why ``TestSpawnSessionRealFork`` looks
the way it does: an earlier version of this suite drove request handling
only against the in-process ``ThreadingHTTPServer`` (tier one above) and
process lifecycle only through bare ``spawn_session()`` calls that never
issued a real HTTP request at all (tier two above) -- which meant "a request
served by a real forked daemon, through its real accept loop" was never
exercised by *either* tier, only their union, which nothing actually tested.
That gap is exactly where a real, reported defect (issue #2) lived: the
daemon persisted V(serving) before its own serving thread was ever confirmed
to be answering anything, so a caller could see a green receipt while every
real request against it hung forever. ``TestSpawnSessionRealFork`` now
always issues a real request against the real forked daemon
(``test_forked_daemon_actually_serves_a_real_http_request``), and
``test_daemon_whose_serve_loop_never_actually_runs_reports_error_not_serving``
below reproduces the defect's exact outward shape (a bound, listening socket
with nothing on the other end ever calling ``accept()``) through that same
real fork, to prove the startup self-test in ``_run_daemon`` actually catches
it rather than merely being present.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

import pytest

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import http_origin as ho
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import ErrorClass, ProtocolError, UnsupportedCapabilityError

LOOPBACK = "127.0.0.1"

#: Generous margin over ho._SELF_TEST_TIMEOUT_SECONDS for tests that force the
#: startup self-test to actually hit its own bounded timeout (see
#: test_daemon_whose_serve_loop_never_actually_runs_reports_error_not_serving) --
#: covers fork/bind overhead on a loaded box without making a hung self-test
#: (a bug in the self-test itself, not this suite) wait forever either.
_SELF_TEST_WAIT_TIMEOUT = ho._SELF_TEST_TIMEOUT_SECONDS + 10.0


def _dead_pid() -> int:
    """A pid guaranteed not to be alive, for stale-session tests."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])  # fixed argv, no shell
    proc.wait()
    return proc.pid


def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.02) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses.

    Needed because a client that has finished reading a response has no guarantee the
    server-side handler thread has *also* finished its own ``finally`` block (the state
    update and access-log append both happen there, after the response bytes are already
    on the wire) -- without this, asserting on ``state["request_count"]`` or the access
    log's contents immediately after ``conn.close()`` is a real, if narrow, race.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ===========================================================================
# State file plumbing
# ===========================================================================


class TestStateFileRoundTrip:
    def test_missing_file_reads_as_none(self, tmp_path):
        assert ho.read_state(tmp_path, "no-such-session") is None

    def test_write_then_read_round_trips(self, tmp_path):
        data = {"session_id": "abc", "pid": 123, "state": "serving"}
        ho._write_state_atomic(tmp_path, "abc", data)
        assert ho.read_state(tmp_path, "abc") == data

    def test_corrupt_file_reads_as_none_not_an_exception(self, tmp_path):
        ho.state_file_path(tmp_path, "abc").write_text("{not json", encoding="utf-8")
        assert ho.read_state(tmp_path, "abc") is None

    def test_non_dict_json_reads_as_none(self, tmp_path):
        ho.state_file_path(tmp_path, "abc").write_text("[1, 2, 3]", encoding="utf-8")
        assert ho.read_state(tmp_path, "abc") is None

    def test_write_is_atomic_no_temp_file_left_behind(self, tmp_path):
        ho._write_state_atomic(tmp_path, "abc", {"state": "serving"})
        assert list(tmp_path.glob("*.tmp")) == []

    def test_remove_state_deletes_state_and_both_log_files(self, tmp_path):
        ho._write_state_atomic(tmp_path, "abc", {"state": "serving"})
        ho.log_file_path(tmp_path, "abc").write_text("log line\n", encoding="utf-8")
        ho.access_log_file_path(tmp_path, "abc").write_text('{"ok": true}\n', encoding="utf-8")
        ho.remove_state(tmp_path, "abc")
        assert not ho.state_file_path(tmp_path, "abc").exists()
        assert not ho.log_file_path(tmp_path, "abc").exists()
        assert not ho.access_log_file_path(tmp_path, "abc").exists()

    def test_remove_state_is_silent_when_nothing_exists(self, tmp_path):
        ho.remove_state(tmp_path, "never-existed")  # must not raise

    def test_list_session_ids_on_missing_directory_is_empty(self, tmp_path):
        assert ho.list_session_ids(tmp_path / "does-not-exist") == []

    def test_list_session_ids_finds_every_state_file(self, tmp_path):
        ho._write_state_atomic(tmp_path, "a", {"state": "serving"})
        ho._write_state_atomic(tmp_path, "b", {"state": "serving"})
        assert set(ho.list_session_ids(tmp_path)) == {"a", "b"}


class TestStateFilePermissions:
    def test_state_file_is_owner_only(self, tmp_path):
        ho._write_state_atomic(tmp_path, "sess-perm", {"state": "serving"})
        mode = stat.S_IMODE(ho.state_file_path(tmp_path, "sess-perm").stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


class TestInitialStateRecord:
    def test_record_carries_every_documented_field(self):
        record = ho._initial_state(session_id="abc", pid=4242, root="/srv/x", bind_address=LOOPBACK, port=8080)
        for key in (
            "session_id",
            "pid",
            "state",
            "error",
            "error_class",
            "root",
            "bind_address",
            "port",
            "url",
            "request_count",
            "bytes_served",
            "last_request_at",
            "started_at",
            "updated_at",
            "stop_reason",
        ):
            assert key in record, f"missing documented field {key!r}"
        assert record["state"] == ho.STATE_STARTING
        assert record["request_count"] == 0
        assert record["bytes_served"] == 0

    def test_create_only_write_does_not_clobber_a_daemons_report(self, tmp_path):
        daemon_report = {"session_id": "abc", "pid": 4242, "state": ho.STATE_ERROR, "error": "port in use", "error_class": ErrorClass.CONNECTION}
        ho._write_state_atomic(tmp_path, "abc", daemon_report)
        wrote = ho._write_state_if_absent(tmp_path, "abc", ho._initial_state(session_id="abc", pid=4242, root="/srv", bind_address=LOOPBACK, port=0))
        assert wrote is False
        assert ho.read_state(tmp_path, "abc") == daemon_report

    def test_create_only_write_does_create_when_the_daemon_wrote_nothing(self, tmp_path):
        record = ho._initial_state(session_id="abc", pid=4242, root="/srv", bind_address=LOOPBACK, port=0)
        assert ho._write_state_if_absent(tmp_path, "abc", record) is True
        assert ho.read_state(tmp_path, "abc") == record


# ===========================================================================
# pid liveness / stop signalling / bounded waits
# ===========================================================================


class TestIsPidAlive:
    def test_self_pid_is_alive(self):
        assert ho.is_pid_alive(os.getpid()) is True

    def test_none_is_not_alive(self):
        assert ho.is_pid_alive(None) is False

    @pytest.mark.parametrize("bad_pid", [0, -1, -999])
    def test_non_positive_pid_is_not_alive_and_never_reaches_os_kill(self, bad_pid, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise AssertionError("os.kill must not be called for a non-positive pid")

        monkeypatch.setattr(ho.os, "kill", _boom)
        assert ho.is_pid_alive(bad_pid) is False

    def test_dead_pid_is_not_alive(self):
        assert ho.is_pid_alive(_dead_pid()) is False


class TestRequestStopAndWaitForExit:
    @pytest.fixture
    def sleeper(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # fixed argv, no shell
        yield proc
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        proc.wait()

    def test_request_stop_terminates_a_live_process(self, sleeper):
        assert ho.is_pid_alive(sleeper.pid) is True
        ho.request_stop(sleeper.pid)
        sleeper.wait(timeout=5.0)
        assert ho.is_pid_alive(sleeper.pid) is False

    @pytest.mark.parametrize("bad_pid", [None, 0, -1])
    def test_request_stop_rejects_non_positive_pid_before_os_kill(self, bad_pid, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise AssertionError("os.kill must not be called for a non-positive/None pid")

        monkeypatch.setattr(ho.os, "kill", _boom)
        ho.request_stop(bad_pid)

    def test_wait_for_exit_times_out_on_a_still_live_process(self, sleeper):
        assert ho.wait_for_exit(sleeper.pid, timeout=0.2, poll_interval=0.05) is False


class TestWaitForState:
    def test_returns_immediately_when_predicate_already_satisfied(self, tmp_path):
        ho._write_state_atomic(tmp_path, "abc", {"state": "serving"})
        start = time.monotonic()
        result = ho.wait_for_state(tmp_path, "abc", until=lambda s: s["state"] == "serving", timeout=5.0, poll_interval=0.05)
        assert result == {"state": "serving"}
        assert time.monotonic() - start < 1.0

    def test_times_out_returning_the_last_observed_state(self, tmp_path):
        ho._write_state_atomic(tmp_path, "abc", {"state": "starting"})
        result = ho.wait_for_state(tmp_path, "abc", until=lambda s: s["state"] == "serving", timeout=0.2, poll_interval=0.05)
        assert result == {"state": "starting"}

    def test_observes_a_transition_that_happens_mid_wait(self, tmp_path):
        ho._write_state_atomic(tmp_path, "abc", {"state": "starting"})

        def _flip_soon():
            time.sleep(0.1)
            ho._write_state_atomic(tmp_path, "abc", {"state": "serving"})

        thread = threading.Thread(target=_flip_soon)
        thread.start()
        result = ho.wait_for_state(tmp_path, "abc", until=lambda s: s["state"] == "serving", timeout=5.0, poll_interval=0.02)
        thread.join()
        assert result == {"state": "serving"}


class TestGenerateSessionId:
    def test_generates_a_plausible_unique_hex_id(self):
        first = ho.generate_session_id()
        second = ho.generate_session_id()
        assert first != second
        assert len(first) == 32
        int(first, 16)


class TestSignalHandlerSetsStopFlag:
    def test_handle_sigterm_sets_the_module_global(self, monkeypatch):
        monkeypatch.setattr(ho, "_stop_flag", False)
        ho._handle_sigterm(signal.SIGTERM, None)
        assert ho._stop_flag is True


# ===========================================================================
# validate_root
# ===========================================================================


class TestValidateRoot:
    def test_valid_directory_passes_and_is_resolved(self, tmp_path):
        (tmp_path / "sub").mkdir()
        resolved = ho.validate_root(str(tmp_path / "sub"))
        assert resolved.is_absolute()
        assert resolved.is_dir()

    def test_missing_path_raises_protocol_error(self, tmp_path):
        with pytest.raises(ProtocolError):
            ho.validate_root(str(tmp_path / "missing"))

    def test_a_file_not_a_directory_raises_protocol_error(self, tmp_path):
        path = tmp_path / "file.txt"
        path.write_text("x")
        with pytest.raises(ProtocolError):
            ho.validate_root(str(path))


# ===========================================================================
# resolve_within_root -- path confinement. This is the single most important
# correctness property in this file: every case below must resolve to None.
# ===========================================================================


class TestResolveWithinRoot:
    @pytest.fixture
    def root(self, tmp_path):
        served = tmp_path / "served"
        served.mkdir()
        (served / "file.txt").write_bytes(b"hello")
        nested = served / "nested"
        nested.mkdir()
        (nested / "deep.txt").write_bytes(b"deep")
        return served.resolve(strict=True)

    def test_a_top_level_file_resolves(self, root):
        resolved = ho.resolve_within_root(root, "/file.txt")
        assert resolved == root / "file.txt"

    def test_a_nested_file_resolves(self, root):
        resolved = ho.resolve_within_root(root, "/nested/deep.txt")
        assert resolved == root / "nested" / "deep.txt"

    def test_query_string_is_stripped_before_resolution(self, root):
        resolved = ho.resolve_within_root(root, "/file.txt?checksum=abc")
        assert resolved == root / "file.txt"

    def test_a_missing_file_still_resolves_inside_root(self, root):
        # resolve_within_root only answers "is this inside root"; existence is the
        # handler's separate is_file() check -- see TestRealServerRequestHandling.
        resolved = ho.resolve_within_root(root, "/does-not-exist.txt")
        assert resolved == root / "does-not-exist.txt"

    def test_literal_dotdot_is_refused(self, root):
        assert ho.resolve_within_root(root, "/../etc/passwd") is None

    def test_literal_dotdot_nested_is_refused(self, root):
        assert ho.resolve_within_root(root, "/nested/../../etc/passwd") is None

    def test_single_percent_encoded_dotdot_is_refused(self, root):
        assert ho.resolve_within_root(root, "/%2e%2e/%2e%2e/etc/passwd") is None

    def test_percent_encoded_separator_hiding_dotdot_is_refused(self, root):
        assert ho.resolve_within_root(root, "/nested%2f..%2f..%2fetc/passwd") is None

    def test_mixed_case_percent_encoded_dotdot_is_refused(self, root):
        assert ho.resolve_within_root(root, "/%2E%2e/%2e%2E/etc/passwd") is None

    def test_double_percent_encoded_dotdot_does_not_traverse(self, root):
        # %252e%252e decodes ONCE to the literal string "%2e%2e" -- inert, not "..".
        # This must resolve to a (nonexistent) literal filename component still
        # inside root, never outside it.
        resolved = ho.resolve_within_root(root, "/%252e%252e/%252e%252e/etc/passwd")
        assert resolved is not None
        assert resolved == root / "%2e%2e" / "%2e%2e" / "etc" / "passwd"

    def test_symlink_escaping_root_is_refused(self, root):
        outside = root.parent / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"TOP SECRET")
        (root / "escape").symlink_to(outside)
        assert ho.resolve_within_root(root, "/escape/secret.txt") is None

    def test_symlink_staying_inside_root_is_allowed(self, root):
        (root / "alias").symlink_to(root / "file.txt")
        resolved = ho.resolve_within_root(root, "/alias")
        assert resolved == (root / "file.txt").resolve()

    def test_nul_byte_is_refused(self, root):
        assert ho.resolve_within_root(root, "/file.txt\x00.png") is None

    def test_leading_double_slash_does_not_escape(self, root):
        resolved = ho.resolve_within_root(root, "//file.txt")
        assert resolved == root / "file.txt"

    def test_root_itself_resolves_to_root(self, root):
        assert ho.resolve_within_root(root, "/") == root

    def test_dot_segments_are_harmless(self, root):
        resolved = ho.resolve_within_root(root, "/./nested/./deep.txt")
        assert resolved == root / "nested" / "deep.txt"


# ===========================================================================
# parse_range_header
# ===========================================================================


class TestParseRangeHeader:
    def test_no_header_is_none(self):
        assert ho.parse_range_header(None, 100) is None

    def test_empty_header_is_none(self):
        assert ho.parse_range_header("", 100) is None

    def test_simple_range(self):
        assert ho.parse_range_header("bytes=0-99", 1000) == ho.ByteRange(start=0, end=99)

    def test_open_ended_range(self):
        assert ho.parse_range_header("bytes=500-", 1000) == ho.ByteRange(start=500, end=999)

    def test_suffix_range(self):
        assert ho.parse_range_header("bytes=-100", 1000) == ho.ByteRange(start=900, end=999)

    def test_suffix_longer_than_file_clamps_to_start(self):
        assert ho.parse_range_header("bytes=-10000", 1000) == ho.ByteRange(start=0, end=999)

    def test_end_beyond_size_is_clamped(self):
        assert ho.parse_range_header("bytes=0-999999", 1000) == ho.ByteRange(start=0, end=999)

    def test_start_at_or_beyond_size_is_unsatisfiable(self):
        with pytest.raises(ho.RangeNotSatisfiable):
            ho.parse_range_header("bytes=1000-", 1000)

    def test_any_range_on_a_zero_length_resource_is_unsatisfiable(self):
        with pytest.raises(ho.RangeNotSatisfiable):
            ho.parse_range_header("bytes=0-0", 0)

    def test_suffix_length_zero_is_unsatisfiable(self):
        with pytest.raises(ho.RangeNotSatisfiable):
            ho.parse_range_header("bytes=-0", 1000)

    def test_multiple_ranges_are_ignored_not_rejected(self):
        # Per RFC 7233 3.1: a server MAY ignore a Range it does not implement and
        # respond as if absent -- returning None here is what makes that happen.
        assert ho.parse_range_header("bytes=0-99,200-299", 1000) is None

    def test_non_bytes_unit_is_ignored(self):
        assert ho.parse_range_header("items=0-1", 1000) is None

    def test_malformed_no_dash_is_ignored(self):
        assert ho.parse_range_header("bytes=abc", 1000) is None

    def test_end_before_start_is_ignored(self):
        assert ho.parse_range_header("bytes=100-50", 1000) is None

    def test_non_numeric_start_is_ignored(self):
        assert ho.parse_range_header("bytes=x-99", 1000) is None


# ===========================================================================
# The real request handler, driven in-process (no fork) against a real
# ThreadingHTTPServer bound to loopback -- exercises resolve_within_root,
# parse_range_header, HEAD/GET, and the access log through the actual
# production code path a request takes.
# ===========================================================================


class _RunningServer:
    def __init__(self, server: object, base_url: str, port: int, access_log_path, state: dict) -> None:
        self.server = server
        self.base_url = base_url
        self.port = port
        self.access_log_path = access_log_path
        self.state = state

    def connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(LOOPBACK, self.port, timeout=5)

    def access_log_records(self) -> list[dict]:
        if not self.access_log_path.exists():
            return []
        return [json.loads(line) for line in self.access_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def running_server(tmp_path):
    import http.server as http_server_module

    root = tmp_path / "served"
    root.mkdir()
    (root / "file.txt").write_bytes(b"0123456789" * 10)  # 100 bytes
    (root / "empty.txt").write_bytes(b"")
    resolved_root = root.resolve(strict=True)

    access_log_path = ho.access_log_file_path(tmp_path, "inproc")
    state: dict = ho._initial_state(session_id="inproc", pid=os.getpid(), root=str(resolved_root), bind_address=LOOPBACK, port=0)
    lock = threading.Lock()

    handler_cls = ho._build_handler_class(root=resolved_root, access_log_path=access_log_path, state=state, state_lock=lock, persist=lambda: None)
    server = http_server_module.ThreadingHTTPServer((LOOPBACK, 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        yield _RunningServer(server, f"http://{LOOPBACK}:{port}/", port, access_log_path, state), root
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


class TestRealServerRequestHandling:
    def test_get_returns_full_body_with_200(self, running_server):
        server, _root = running_server
        conn = server.connection()
        conn.request("GET", "/file.txt")
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 200
        assert body == b"0123456789" * 10
        assert resp.getheader("Content-Length") == "100"
        conn.close()

    def test_head_returns_no_body_but_correct_content_length(self, running_server):
        server, _root = running_server
        conn = server.connection()
        conn.request("HEAD", "/file.txt")
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 200
        assert body == b""
        assert resp.getheader("Content-Length") == "100"
        conn.close()

    def test_range_request_returns_206_with_correct_slice(self, running_server):
        server, _root = running_server
        conn = server.connection()
        conn.request("GET", "/file.txt", headers={"Range": "bytes=10-19"})
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 206
        assert body == b"0123456789"
        assert resp.getheader("Content-Range") == "bytes 10-19/100"
        assert resp.getheader("Content-Length") == "10"
        conn.close()

    def test_suffix_range_request_returns_last_n_bytes(self, running_server):
        server, _root = running_server
        conn = server.connection()
        conn.request("GET", "/file.txt", headers={"Range": "bytes=-10"})
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 206
        assert body == b"0123456789"
        assert resp.getheader("Content-Range") == "bytes 90-99/100"
        conn.close()

    def test_head_honours_range_too(self, running_server):
        server, _root = running_server
        conn = server.connection()
        conn.request("HEAD", "/file.txt", headers={"Range": "bytes=0-9"})
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 206
        assert body == b""
        assert resp.getheader("Content-Length") == "10"
        conn.close()

    def test_unsatisfiable_range_returns_416_with_content_range_star(self, running_server):
        server, _root = running_server
        conn = server.connection()
        conn.request("GET", "/file.txt", headers={"Range": "bytes=10000-"})
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 416
        assert resp.getheader("Content-Range") == "bytes */100"
        conn.close()

    def test_missing_file_is_404(self, running_server):
        server, _root = running_server
        conn = server.connection()
        conn.request("GET", "/no-such-file.txt")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 404
        conn.close()

    def test_a_directory_request_is_404_not_a_listing(self, running_server):
        server, root = running_server
        (root / "subdir").mkdir()
        conn = server.connection()
        conn.request("GET", "/subdir")
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 404
        assert b"<html" not in body.lower() or b"error" in body.lower()  # never an index listing
        conn.close()

    def test_traversal_attempt_is_404_and_never_serves_outside_content(self, running_server):
        server, root = running_server
        outside = root.parent / "outside-server"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"TOP SECRET DO NOT SERVE")
        conn = server.connection()
        conn.request("GET", "/../outside-server/secret.txt")
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 404
        assert b"TOP SECRET" not in body
        conn.close()

    def test_double_encoded_traversal_attempt_is_404(self, running_server):
        server, _root = running_server
        conn = server.connection()
        conn.request("GET", "/%252e%252e/%252e%252e/etc/passwd")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 404
        conn.close()

    def test_request_count_and_bytes_served_accumulate(self, running_server):
        server, _root = running_server
        conn = server.connection()
        conn.request("GET", "/file.txt")
        conn.getresponse().read()
        conn.request("GET", "/file.txt", headers={"Range": "bytes=0-9"})
        conn.getresponse().read()
        conn.close()
        assert _wait_until(lambda: server.state["request_count"] == 2)
        assert server.state["bytes_served"] == 110  # 100 (full) + 10 (range)

    def test_access_log_records_every_request_with_outcome(self, running_server):
        server, _root = running_server
        conn = server.connection()
        conn.request("GET", "/file.txt")
        conn.getresponse().read()
        conn.request("GET", "/missing.txt")
        conn.getresponse().read()
        conn.request("GET", "/../etc/passwd")
        conn.getresponse().read()
        conn.close()

        assert _wait_until(lambda: len(server.access_log_records()) == 3)
        records = server.access_log_records()
        assert len(records) == 3
        outcomes = [r["outcome"] for r in records]
        assert outcomes == ["ok", "not_found", "blocked_traversal"]
        for record in records:
            assert record["client"] == LOOPBACK
            for key in ("time", "method", "path", "status", "bytes_sent"):
                assert key in record

    def test_access_log_is_valid_json_lines_and_never_empty_on_a_served_request(self, running_server):
        server, _root = running_server
        conn = server.connection()
        conn.request("GET", "/file.txt")
        conn.getresponse().read()
        conn.close()

        # Wait for the log to have CONTENT, not merely to exist. The handler
        # opens the file and appends the record in two separate steps, so
        # waiting on existence alone is a real race: this failed under
        # `pytest -n auto` on CI (ansible-core 2.17 / Python 3.10 and 3.11)
        # having passed everywhere else, which is exactly how a timing race
        # presents. The sibling assertion above already waits on record count;
        # this one was the outlier.
        def nonblank_log_lines():
            if not server.access_log_path.exists():
                return []
            raw_text = server.access_log_path.read_text(encoding="utf-8")
            return [line for line in raw_text.splitlines() if line.strip()]

        assert _wait_until(lambda: len(nonblank_log_lines()) == 1)
        lines = nonblank_log_lines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["status"] == 200
        assert parsed["path"] == "/file.txt"

    def test_empty_file_serves_with_zero_length(self, running_server):
        server, _root = running_server
        conn = server.connection()
        conn.request("GET", "/empty.txt")
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 200
        assert body == b""
        assert resp.getheader("Content-Length") == "0"
        conn.close()

    def test_python_version_banner_is_not_advertised(self, running_server):
        server, _root = running_server
        conn = server.connection()
        conn.request("GET", "/file.txt")
        resp = conn.getresponse()
        resp.read()
        server_header = resp.getheader("Server") or ""
        assert "Python" not in server_header
        conn.close()


# ===========================================================================
# _format_bound_url
# ===========================================================================


class TestFormatBoundUrl:
    def test_ipv4_address(self):
        assert ho._format_bound_url("127.0.0.1", 8080) == "http://127.0.0.1:8080/"

    def test_ipv6_address_is_bracketed(self):
        assert ho._format_bound_url("::1", 8080) == "http://[::1]:8080/"


# ===========================================================================
# The startup self-test -- see the module docstring's point 4. Exercised here
# in-process (no fork, reusing the same real ThreadingHTTPServer fixture as
# TestRealServerRequestHandling above) so the pure success/failure logic of
# _run_self_test/_find_probe_file/_self_test_connect_host is covered fast and
# in isolation; TestSpawnSessionRealFork below covers the real forked daemon
# actually calling this at startup.
# ===========================================================================


class TestFindProbeFile:
    def test_returns_the_only_file(self, tmp_path):
        (tmp_path / "file.txt").write_bytes(b"x")
        assert ho._find_probe_file(tmp_path) == tmp_path / "file.txt"

    def test_sorted_order_is_stable_across_multiple_files(self, tmp_path):
        (tmp_path / "b.txt").write_bytes(b"b")
        (tmp_path / "a.txt").write_bytes(b"a")
        assert ho._find_probe_file(tmp_path) == tmp_path / "a.txt"

    def test_descends_into_nested_directories(self, tmp_path):
        nested = tmp_path / "nested" / "deeper"
        nested.mkdir(parents=True)
        (nested / "deep.txt").write_bytes(b"deep")
        assert ho._find_probe_file(tmp_path) == nested / "deep.txt"

    def test_empty_directory_returns_none(self, tmp_path):
        assert ho._find_probe_file(tmp_path) is None

    def test_directory_containing_only_a_subdirectory_with_no_files_returns_none(self, tmp_path):
        (tmp_path / "empty-subdir").mkdir()
        assert ho._find_probe_file(tmp_path) is None

    def test_a_symlinked_file_is_skipped_even_when_it_sorts_first(self, tmp_path):
        outside = tmp_path.parent / "outside-probe-target"
        outside.mkdir(exist_ok=True)
        (outside / "real.txt").write_bytes(b"OUTSIDE ROOT")
        served = tmp_path / "served"
        served.mkdir()
        (served / "a-alias.txt").symlink_to(outside / "real.txt")
        (served / "z-real.txt").write_bytes(b"inside root")
        assert ho._find_probe_file(served) == served / "z-real.txt"


class TestSelfTestConnectHost:
    def test_wildcard_ipv4_maps_to_loopback(self):
        assert ho._self_test_connect_host("0.0.0.0") == "127.0.0.1"  # noqa: S104 -- recognising the wildcard, not binding it.

    def test_wildcard_ipv6_maps_to_loopback(self):
        assert ho._self_test_connect_host("::") == "::1"

    def test_a_real_address_passes_through_unchanged(self):
        assert ho._self_test_connect_host("127.0.0.1") == "127.0.0.1"
        assert ho._self_test_connect_host("192.0.2.5") == "192.0.2.5"


class TestRunSelfTest:
    def test_succeeds_against_a_real_file_and_the_request_is_excluded_from_counters(self, running_server):
        server, root = running_server
        resolved_root = root.resolve(strict=True)
        assert server.state["request_count"] == 0

        ok, detail = ho._run_self_test(bind_address=LOOPBACK, port=server.port, root=resolved_root)
        assert ok is True
        assert "byte" in detail

        # The self-test's own request must never be mistaken for real client traffic.
        assert _wait_until(lambda: len(server.access_log_records()) == 1)
        assert server.state["request_count"] == 0
        assert server.state["bytes_served"] == 0
        record = server.access_log_records()[0]
        assert record["self_test"] is True

        # A real request afterward is counted exactly as before -- the exclusion is
        # per-request (tagged by the self-test's own header), not a blanket "first
        # request is free" rule that could also swallow a real one.
        conn = server.connection()
        conn.request("GET", "/file.txt")
        conn.getresponse().read()
        conn.close()
        # Wait on the access log itself, not just request_count -- the handler's finally
        # block updates the counter *then* appends the access-log record, so polling only
        # the counter is a real, if narrow, race on the second assertion (see _wait_until's
        # own docstring above for the identical lesson learned elsewhere in this file).
        assert _wait_until(lambda: len(server.access_log_records()) == 2)
        assert server.state["request_count"] == 1
        records = server.access_log_records()
        assert len(records) == 2
        assert records[1]["self_test"] is False

    def test_fails_when_nothing_is_listening_on_the_port(self, tmp_path):
        served = tmp_path / "served"
        served.mkdir()
        (served / "file.txt").write_bytes(b"x")

        probe_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe_sock.bind((LOOPBACK, 0))
        free_port = probe_sock.getsockname()[1]
        probe_sock.close()  # nothing listens on this port once closed

        ok, detail = ho._run_self_test(bind_address=LOOPBACK, port=free_port, root=served)
        assert ok is False
        assert "failed" in detail

    def test_a_wrong_response_body_is_a_failure_not_a_false_positive(self, running_server, monkeypatch):
        # If the handler ever served the wrong bytes for the probe path (a
        # confinement or Range regression, say), the self-test must not wave it
        # through just because *some* 200 came back.
        server, root = running_server
        resolved_root = root.resolve(strict=True)

        real_perform = ho._perform_self_test_request

        def _lying_perform(**kwargs):
            status, _body = real_perform(**kwargs)
            return status, b"not the real file contents"

        monkeypatch.setattr(ho, "_perform_self_test_request", _lying_perform)
        ok, detail = ho._run_self_test(bind_address=LOOPBACK, port=server.port, root=resolved_root)
        assert ok is False
        assert "expected" in detail

    def test_empty_root_falls_back_to_expecting_a_well_formed_404(self, running_server):
        server, root = running_server
        empty_root = root.parent / "genuinely-empty"
        empty_root.mkdir()
        ok, detail = ho._run_self_test(bind_address=LOOPBACK, port=server.port, root=empty_root)
        assert ok is True
        assert "no file to verify" in detail

    def test_empty_root_self_test_request_is_also_excluded_from_counters(self, running_server):
        server, root = running_server
        empty_root = root.parent / "genuinely-empty-2"
        empty_root.mkdir()
        assert server.state["request_count"] == 0
        ok, _detail = ho._run_self_test(bind_address=LOOPBACK, port=server.port, root=empty_root)
        assert ok is True
        assert _wait_until(lambda: len(server.access_log_records()) == 1)
        assert server.state["request_count"] == 0


# ===========================================================================
# spawn_session platform guard
# ===========================================================================


class TestSpawnSessionPlatformGuard:
    def test_raises_unsupported_capability_when_os_fork_is_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.delattr(ho.os, "fork", raising=False)
        config = ho.SessionConfig(session_id="abc", root=str(tmp_path), bind_address=LOOPBACK, port=0, lifetime_seconds=60, runtime_dir=str(tmp_path))
        with pytest.raises(UnsupportedCapabilityError):
            ho.spawn_session(config)


# ===========================================================================
# The real daemon: double fork, hard lifetime cap, SIGTERM stop. Every test
# here forks a real process bound to 127.0.0.1 only, and always cleans it up.
# ===========================================================================


@pytest.fixture
def served_root(tmp_path):
    root = tmp_path / "served"
    root.mkdir()
    (root / "file.txt").write_bytes(b"hello world")
    return ho.validate_root(str(root))


@pytest.fixture
def runtime_dir(tmp_path):
    return tmp_path / "runtime"


class _DaemonHandle:
    def __init__(self, pid: int, runtime_dir, session_id: str) -> None:
        self.pid = pid
        self.runtime_dir = runtime_dir
        self.session_id = session_id

    def state(self) -> dict | None:
        return ho.read_state(self.runtime_dir, self.session_id)

    def wait_for_serving(self, timeout: float = 5.0) -> dict:
        state = ho.wait_for_state(self.runtime_dir, self.session_id, until=lambda s: s.get("state") in (ho.STATE_SERVING, ho.STATE_ERROR), timeout=timeout)
        assert state is not None, "daemon never wrote a state file at all"
        return state


@pytest.fixture
def spawn(runtime_dir, served_root):
    spawned: list[_DaemonHandle] = []

    def _spawn(*, session_id: str = "test-session", lifetime_seconds: int = 60, port: int = 0, root=None, bind_address: str = LOOPBACK) -> _DaemonHandle:
        config = ho.SessionConfig(
            session_id=session_id,
            root=str(root or served_root),
            bind_address=bind_address,
            port=port,
            lifetime_seconds=lifetime_seconds,
            runtime_dir=str(runtime_dir),
        )
        pid = ho.spawn_session(config)
        handle = _DaemonHandle(pid, runtime_dir, session_id)
        spawned.append(handle)
        return handle

    yield _spawn

    for handle in spawned:
        if ho.is_pid_alive(handle.pid):
            ho.request_stop(handle.pid)
            ho.wait_for_exit(handle.pid, timeout=5.0)
        with contextlib.suppress(ProcessLookupError):
            os.kill(handle.pid, 0)


class TestSpawnSessionRealFork:
    def test_forked_daemon_reports_serving_with_a_real_bound_port(self, spawn):
        handle = spawn()
        state = handle.wait_for_serving()
        assert state["state"] == ho.STATE_SERVING
        assert isinstance(state["port"], int) and state["port"] > 0
        assert state["url"] == f"http://{LOOPBACK}:{state['port']}/"
        assert ho.is_pid_alive(handle.pid) is True

    def test_forked_daemon_actually_serves_a_real_http_request(self, spawn):
        handle = spawn()
        state = handle.wait_for_serving()
        conn = http.client.HTTPConnection(LOOPBACK, state["port"], timeout=5)
        conn.request("GET", "/file.txt")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        assert resp.status == 200
        assert body == b"hello world"

    def test_the_daemons_own_startup_self_test_is_never_visible_in_request_count(self, spawn):
        # Guards the exact confusion the fix's own docs call out: an operator
        # who checks request_count/bytes_served the instant they see a fresh
        # 'serving' receipt must see zeros, not 1, even though the daemon has
        # already issued (and this test does not repeat) one real GET against
        # itself to earn that receipt in the first place.
        handle = spawn()
        state = handle.wait_for_serving()
        assert state["state"] == ho.STATE_SERVING
        assert state["request_count"] == 0
        assert state["bytes_served"] == 0
        assert state["last_request_at"] is None

        conn = http.client.HTTPConnection(LOOPBACK, state["port"], timeout=5)
        conn.request("GET", "/file.txt")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        assert resp.status == 200

        # The handler's finally block (state update, access-log append) runs after the
        # response bytes are already on the wire -- see _wait_until's own docstring above
        # for why polling here, not a bare read, is what avoids a real timing race.
        assert _wait_until(lambda: (handle.state() or {}).get("request_count") == 1)
        final = handle.state()
        assert final["bytes_served"] == len(body)
        assert final["last_request_at"] is not None

    def test_binding_the_wildcard_address_is_confirmed_and_reachable_via_loopback(self, spawn):
        # A real, routable, non-loopback address cannot be exercised safely from
        # this suite -- see the module docstring's constraints on what these tests
        # may bind/connect to. 0.0.0.0 (bind to every interface) is nonetheless a
        # real, legitimate bind_address distinct from the loopback default, and
        # is exactly the case _self_test_connect_host exists to handle: the
        # daemon's own startup self-test cannot connect to "0.0.0.0" as a
        # destination, so it must reach itself via loopback for the session to
        # ever report serving at all. A real client reaching a wildcard-bound
        # server via 127.0.0.1 exercises that same path.
        handle = spawn(bind_address="0.0.0.0")  # noqa: S104 -- the wildcard bind this test exists to cover.
        state = handle.wait_for_serving()
        assert state["state"] == ho.STATE_SERVING
        assert state["bind_address"] == "0.0.0.0"  # noqa: S104 -- asserting on it, not binding it.

        conn = http.client.HTTPConnection(LOOPBACK, state["port"], timeout=5)
        conn.request("GET", "/file.txt")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        assert resp.status == 200
        assert body == b"hello world"

    def test_daemon_whose_serve_loop_never_actually_runs_reports_error_not_serving(self, spawn, monkeypatch):
        # This is the test that would have caught issue #2. It reproduces the
        # defect's exact outward shape through the real double-forked daemon,
        # not an in-process stand-in: a socket that is genuinely bound and
        # listening (so a TCP connect succeeds and the kernel queues it) with
        # nothing on the other end ever calling accept() on it -- "TCP connect
        # succeeded repeatedly ... 0 bytes received" from the original report.
        #
        # Patching serve_forever() to a no-op *before* spawn_session() forks is
        # what makes this reachable without depending on the exact, possibly
        # platform- or load-dependent trigger that produced the defect in the
        # wild (a diagnostic experiment run alongside this fix could not
        # reproduce a permanent hang via fork()+threading alone on this host --
        # see the investigation notes for what was and was not reproducible):
        # fork() duplicates the whole process, including this monkeypatch, at
        # the instant of the fork, so the daemon's listening socket is real and
        # open but its accept loop never runs, regardless of why that might
        # happen for real.
        #
        # Before the fix in _run_daemon, this would have hung the assertions
        # below on an ever-'starting'/'serving' state and a request that never
        # returns; see the mutation check for that shown directly against a
        # throwaway copy of the pre-fix code.
        monkeypatch.setattr(ho.http.server.ThreadingHTTPServer, "serve_forever", lambda self, **kwargs: None)
        handle = spawn(lifetime_seconds=30)

        state = ho.wait_for_state(
            handle.runtime_dir,
            handle.session_id,
            until=lambda s: s.get("state") in (ho.STATE_SERVING, ho.STATE_ERROR),
            timeout=_SELF_TEST_WAIT_TIMEOUT,
        )
        assert state is not None, "daemon never wrote a state file at all"
        assert state["state"] == ho.STATE_ERROR, f"a daemon whose accept loop never runs must report error, not serving -- got {state!r}"
        assert state["error_class"] == ErrorClass.CONNECTION
        assert "self-test" in (state["error"] or "")

        # And the process must not be left behind holding the port -- a failed
        # self-test has to tear the daemon down, not leave an unusable one alive.
        assert ho.wait_for_exit(handle.pid, timeout=5.0) is True

    def test_create_only_write_leaves_the_daemons_own_first_report_alone(self, spawn):
        # spawn_session()'s own fallback write is create-only (see
        # _write_state_if_absent) -- by the time it runs, the real daemon has
        # almost certainly already written at least "starting". This asserts the
        # pid in the persisted record is always the daemon's real pid, from
        # whichever writer actually won the race.
        handle = spawn()
        state = handle.wait_for_serving()
        assert state["pid"] == handle.pid

    def test_sigterm_produces_a_clean_stop_not_an_error(self, spawn):
        handle = spawn()
        handle.wait_for_serving()
        ho.request_stop(handle.pid)
        exited = ho.wait_for_exit(handle.pid, timeout=5.0)
        assert exited is True
        final = handle.state()
        assert final["state"] == ho.STATE_STOPPED
        assert final["stop_reason"] == "signal"
        assert final["error"] is None

    def test_hard_lifetime_cap_self_terminates_the_daemon_with_nobody_asking(self, spawn):
        # The primary safety property this whole module exists for: an
        # interrupted/crashed play must not be required for the server to stop.
        # Nothing in this test ever calls request_stop() -- the daemon's own
        # control loop must be what ends it.
        handle = spawn(lifetime_seconds=1)
        handle.wait_for_serving()
        assert ho.is_pid_alive(handle.pid) is True

        exited = ho.wait_for_exit(handle.pid, timeout=10.0)
        assert exited is True, "the daemon must self-terminate once lifetime_seconds elapses, with no SIGTERM sent"

        final = handle.state()
        assert final["state"] == ho.STATE_STOPPED
        assert final["stop_reason"] == "lifetime_expired"

    def test_a_generous_lifetime_does_not_terminate_early(self, spawn):
        handle = spawn(lifetime_seconds=60)
        handle.wait_for_serving()
        time.sleep(1.0)
        assert ho.is_pid_alive(handle.pid) is True
        assert handle.state()["state"] == ho.STATE_SERVING

    def test_binding_a_port_already_in_use_is_a_classified_connection_error(self, spawn):
        first = spawn()
        first_state = first.wait_for_serving()
        busy_port = first_state["port"]

        second = spawn(session_id="second-session", port=busy_port)
        second_state = ho.wait_for_state(
            second.runtime_dir, second.session_id, until=lambda s: s.get("state") in (ho.STATE_SERVING, ho.STATE_ERROR), timeout=5.0
        )
        assert second_state["state"] == ho.STATE_ERROR
        assert second_state["error_class"] == ErrorClass.CONNECTION

    def test_invalid_root_removed_between_validate_and_fork_is_a_protocol_error(self, spawn, runtime_dir, tmp_path):
        # validate_root() runs in the caller before the fork; this exercises the
        # daemon's own defensive re-resolve when the directory is gone by the time
        # the (already-forked) daemon actually looks at it.
        vanished = tmp_path / "vanishes"
        vanished.mkdir()
        resolved = ho.validate_root(str(vanished))
        vanished.rmdir()
        handle = spawn(root=resolved)
        state = handle.wait_for_serving()
        assert state["state"] == ho.STATE_ERROR
        assert state["error_class"] == ErrorClass.PROTOCOL
