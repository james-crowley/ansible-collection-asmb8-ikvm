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
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time

import pytest

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import http_origin as ho
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import ErrorClass, ProtocolError, UnsupportedCapabilityError

LOOPBACK = "127.0.0.1"


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
        assert _wait_until(server.access_log_path.exists)
        raw = server.access_log_path.read_text(encoding="utf-8")
        lines = [line for line in raw.splitlines() if line.strip()]
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

    def _spawn(*, session_id: str = "test-session", lifetime_seconds: int = 60, port: int = 0, root=None) -> _DaemonHandle:
        config = ho.SessionConfig(
            session_id=session_id,
            root=str(root or served_root),
            bind_address=LOOPBACK,
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
