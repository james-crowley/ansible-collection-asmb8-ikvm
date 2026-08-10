# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for the state-file/process-lifecycle/reclamation primitives in
``media_session.py``.

Mostly scoped to logic that does not require a real fork or a real network
connection -- state file read/write/atomicity, pid liveness, bounded waits,
image validation, and the single-session reclamation scan. The *fork* half of
the daemon path (``spawn_session``'s double fork, and a real iUSB handshake
over a real socket) is exercised for real by the integration test target
against the mock iUSB server, which is another agent's responsibility for
this task -- forking a real detached daemon is exactly the kind of thing that
is invisible to a mocked test and easy to get subtly wrong.

:class:`TestRunDaemonIdleHandling` is the exception, and deliberately so:
what ``_run_daemon`` does when its iUSB session goes quiet is the single most
important correctness property in this whole collection (see this module's
own module docstring and ``iusb.Session.serve_forever``'s docstring) --
verified live at 130 continuous seconds of silence on a healthy session, so
it is driven here frame by frame with the real ``iusb`` classes underneath a
fake ``AspClient``/network layer, not mocked away.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time

import pytest

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import iusb, media_session
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import ErrorClass, ProtocolError, UnsupportedCapabilityError

#: Documentation-only endpoint address (RFC 5737 TEST-NET-1). Nothing here connects.
EXAMPLE_HOST = "192.0.2.10"
EXAMPLE_ENDPOINT = f"{EXAMPLE_HOST}:5120"


def _config(*, session_id: str, runtime_dir, image: str, cd_port: int = 5120) -> media_session.SessionConfig:
    return media_session.SessionConfig(
        session_id=session_id,
        host=EXAMPLE_HOST,
        port=443,
        use_tls=False,
        allow_insecure_transport=True,
        validate_certs=True,
        ca_path=None,
        tls_fingerprint=None,
        timeout=30,
        connect_timeout=1,
        cd_port=cd_port,
        instance=0,
        image=image,
        runtime_dir=str(runtime_dir),
    )


# ===========================================================================
# State file plumbing
# ===========================================================================


class TestStateFileRoundTrip:
    def test_missing_file_reads_as_none(self, tmp_path):
        assert media_session.read_state(tmp_path, "no-such-session") is None

    def test_write_then_read_round_trips(self, tmp_path):
        data = {"session_id": "abc", "pid": 123, "state": "attached"}
        media_session._write_state_atomic(tmp_path, "abc", data)
        assert media_session.read_state(tmp_path, "abc") == data

    def test_corrupt_file_reads_as_none_not_an_exception(self, tmp_path):
        media_session.state_file_path(tmp_path, "abc").write_text("{not json", encoding="utf-8")
        assert media_session.read_state(tmp_path, "abc") is None

    def test_non_dict_json_reads_as_none(self, tmp_path):
        media_session.state_file_path(tmp_path, "abc").write_text("[1, 2, 3]", encoding="utf-8")
        assert media_session.read_state(tmp_path, "abc") is None

    def test_write_is_atomic_no_temp_file_left_behind(self, tmp_path):
        media_session._write_state_atomic(tmp_path, "abc", {"state": "attached"})
        assert list(tmp_path.glob("*.tmp")) == []

    def test_remove_state_deletes_state_and_log_files(self, tmp_path):
        media_session._write_state_atomic(tmp_path, "abc", {"state": "attached"})
        media_session.log_file_path(tmp_path, "abc").write_text("log line\n", encoding="utf-8")
        media_session.remove_state(tmp_path, "abc")
        assert not media_session.state_file_path(tmp_path, "abc").exists()
        assert not media_session.log_file_path(tmp_path, "abc").exists()

    def test_remove_state_is_silent_when_nothing_exists(self, tmp_path):
        media_session.remove_state(tmp_path, "never-existed")  # must not raise

    def test_list_session_ids_on_missing_directory_is_empty(self, tmp_path):
        assert media_session.list_session_ids(tmp_path / "does-not-exist") == []

    def test_list_session_ids_finds_every_state_file(self, tmp_path):
        media_session._write_state_atomic(tmp_path, "a", {"state": "attached"})
        media_session._write_state_atomic(tmp_path, "b", {"state": "attached"})
        assert set(media_session.list_session_ids(tmp_path)) == {"a", "b"}


class TestStateFilePermissions:
    def test_state_file_is_owner_only(self, tmp_path):
        media_session._write_state_atomic(tmp_path, "sess-perm", {"state": "attached"})
        mode = stat.S_IMODE(media_session.state_file_path(tmp_path, "sess-perm").stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_state_file_is_owner_only_even_in_a_lax_pre_existing_directory(self, tmp_path):
        lax = tmp_path / "shared"
        lax.mkdir(mode=0o777)
        media_session._write_state_atomic(lax, "sess-lax", {"state": "attached"})
        mode = stat.S_IMODE(media_session.state_file_path(lax, "sess-lax").stat().st_mode)
        assert not mode & stat.S_IROTH
        assert not mode & stat.S_IRGRP

    def test_credentials_and_tokens_never_reach_the_state_file(self, tmp_path):
        secret_password = "Sup3rSecret!"
        secret_token = "STOKEN-abcdef0123456789"
        media_session._write_state_atomic(tmp_path, "sess-cred", {"session_id": "sess-cred", "state": "attached", "host": EXAMPLE_HOST})
        raw = media_session.state_file_path(tmp_path, "sess-cred").read_text()
        assert secret_password not in raw
        assert secret_token not in raw


class TestInitialStateRecord:
    def test_record_carries_every_documented_field(self):
        record = media_session._initial_state(session_id="abc", endpoint=EXAMPLE_ENDPOINT, pid=4242, image="/srv/x.iso")
        for key in (
            "session_id",
            "pid",
            "endpoint",
            "state",
            "error",
            "error_class",
            "image",
            "bytes_read",
            "sectors_served",
            "last_request_at",
            "started_at",
            "updated_at",
            "idle_polls",
            "idle_poll_interval_seconds",
            "current_idle_streak",
            "last_idle_streak",
        ):
            assert key in record, f"missing documented field {key!r}"
        assert record["state"] == media_session.STATE_STARTING
        assert record["error_class"] is None
        assert record["bytes_read"] == 0
        assert record["sectors_served"] == 0
        assert record["last_request_at"] is None
        assert record["idle_polls"] == 0
        assert record["idle_poll_interval_seconds"] == media_session._RECV_POLL_TIMEOUT
        assert record["current_idle_streak"] is None
        assert record["last_idle_streak"] is None

    def test_never_contains_a_password_or_token_shaped_field(self):
        record = media_session._initial_state(session_id="abc", endpoint=EXAMPLE_ENDPOINT, pid=4242, image="/srv/x.iso")
        assert "password" not in record
        assert "token" not in record
        assert "kvm_token" not in record

    def test_create_only_write_does_not_clobber_a_daemons_report(self, tmp_path):
        daemon_report = {
            "session_id": "abc",
            "pid": 4242,
            "endpoint": EXAMPLE_ENDPOINT,
            "state": media_session.STATE_ERROR,
            "error": "iusb auth rejected",
            "error_class": ErrorClass.BMC_BUSY,
        }
        media_session._write_state_atomic(tmp_path, "abc", daemon_report)
        wrote = media_session._write_state_if_absent(
            tmp_path, "abc", media_session._initial_state(session_id="abc", endpoint=EXAMPLE_ENDPOINT, pid=4242, image="x")
        )
        assert wrote is False
        assert media_session.read_state(tmp_path, "abc") == daemon_report

    def test_create_only_write_does_create_when_the_daemon_wrote_nothing(self, tmp_path):
        record = media_session._initial_state(session_id="abc", endpoint=EXAMPLE_ENDPOINT, pid=4242, image="x")
        assert media_session._write_state_if_absent(tmp_path, "abc", record) is True
        assert media_session.read_state(tmp_path, "abc") == record

    def test_create_only_write_is_owner_only(self, tmp_path):
        media_session._write_state_if_absent(tmp_path, "abc", media_session._initial_state(session_id="abc", endpoint=EXAMPLE_ENDPOINT, pid=1, image="x"))
        mode = stat.S_IMODE(media_session.state_file_path(tmp_path, "abc").stat().st_mode)
        assert mode == 0o600


class TestCloseIdleStreak:
    """Unit tests for the pure `_close_idle_streak` helper, isolated from the
    real daemon loop (see `TestRunDaemonIdleHandling` below for that).
    """

    def test_no_open_streak_is_a_noop(self):
        state = {"current_idle_streak": None, "last_idle_streak": None}
        media_session._close_idle_streak(state, now="2026-01-01T00:00:10+00:00")
        assert state["current_idle_streak"] is None
        assert state["last_idle_streak"] is None

    def test_an_open_streak_is_moved_to_last_idle_streak_with_an_ended_at(self):
        state = {
            "current_idle_streak": {"started_at": "2026-01-01T00:00:00+00:00", "polls": 3, "seconds": 6.0},
            "last_idle_streak": None,
        }
        media_session._close_idle_streak(state, now="2026-01-01T00:00:06+00:00")
        assert state["current_idle_streak"] is None
        assert state["last_idle_streak"] == {
            "started_at": "2026-01-01T00:00:00+00:00",
            "polls": 3,
            "seconds": 6.0,
            "ended_at": "2026-01-01T00:00:06+00:00",
        }

    def test_closing_a_streak_overwrites_whatever_last_idle_streak_previously_held(self):
        state = {
            "current_idle_streak": {"started_at": "t2", "polls": 1, "seconds": 2.0},
            "last_idle_streak": {"started_at": "t0", "ended_at": "t1", "polls": 5, "seconds": 10.0},
        }
        media_session._close_idle_streak(state, now="t3")
        assert state["last_idle_streak"]["started_at"] == "t2"
        assert state["last_idle_streak"]["ended_at"] == "t3"


# ===========================================================================
# pid liveness / stop signalling / bounded waits
# ===========================================================================


class TestIsPidAlive:
    def test_self_pid_is_alive(self):
        assert media_session.is_pid_alive(os.getpid()) is True

    def test_none_is_not_alive(self):
        assert media_session.is_pid_alive(None) is False

    @pytest.mark.parametrize("bad_pid", [0, -1, -999])
    def test_non_positive_pid_is_not_alive_and_never_reaches_os_kill(self, bad_pid, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise AssertionError("os.kill must not be called for a non-positive pid")

        monkeypatch.setattr(media_session.os, "kill", _boom)
        assert media_session.is_pid_alive(bad_pid) is False

    def test_dead_pid_is_not_alive(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])  # fixed argv, no shell
        proc.wait()
        assert media_session.is_pid_alive(proc.pid) is False


class TestRequestStopAndWaitForExit:
    @pytest.fixture
    def sleeper(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # fixed argv, no shell
        yield proc
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        proc.wait()

    def test_request_stop_terminates_a_live_process(self, sleeper):
        assert media_session.is_pid_alive(sleeper.pid) is True
        media_session.request_stop(sleeper.pid)
        sleeper.wait(timeout=5.0)
        assert media_session.is_pid_alive(sleeper.pid) is False

    def test_request_stop_on_dead_pid_is_a_no_op(self, sleeper):
        sleeper.kill()
        sleeper.wait()
        media_session.request_stop(sleeper.pid)  # must not raise

    @pytest.mark.parametrize("bad_pid", [None, 0, -1])
    def test_request_stop_rejects_non_positive_pid_before_os_kill(self, bad_pid, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise AssertionError("os.kill must not be called for a non-positive/None pid")

        monkeypatch.setattr(media_session.os, "kill", _boom)
        media_session.request_stop(bad_pid)

    def test_wait_for_exit_times_out_on_a_still_live_process(self, sleeper):
        assert media_session.wait_for_exit(sleeper.pid, timeout=0.2, poll_interval=0.05) is False


class TestWaitForState:
    def test_returns_immediately_when_predicate_already_satisfied(self, tmp_path):
        media_session._write_state_atomic(tmp_path, "abc", {"state": "attached"})
        start = time.monotonic()
        result = media_session.wait_for_state(tmp_path, "abc", until=lambda s: s["state"] == "attached", timeout=5.0, poll_interval=0.05)
        assert result == {"state": "attached"}
        assert time.monotonic() - start < 1.0

    def test_times_out_returning_the_last_observed_state(self, tmp_path):
        media_session._write_state_atomic(tmp_path, "abc", {"state": "connecting"})
        result = media_session.wait_for_state(tmp_path, "abc", until=lambda s: s["state"] == "attached", timeout=0.2, poll_interval=0.05)
        assert result == {"state": "connecting"}

    def test_observes_a_transition_that_happens_mid_wait(self, tmp_path):
        media_session._write_state_atomic(tmp_path, "abc", {"state": "connecting"})

        def _flip_soon():
            time.sleep(0.1)
            media_session._write_state_atomic(tmp_path, "abc", {"state": "attached"})

        thread = threading.Thread(target=_flip_soon)
        thread.start()
        result = media_session.wait_for_state(tmp_path, "abc", until=lambda s: s["state"] == "attached", timeout=5.0, poll_interval=0.02)
        thread.join()
        assert result == {"state": "attached"}

    def test_missing_state_file_never_satisfies_and_times_out_with_none(self, tmp_path):
        result = media_session.wait_for_state(tmp_path, "no-such-session", until=lambda s: True, timeout=0.2, poll_interval=0.05)
        assert result is None


# ===========================================================================
# validate_image
# ===========================================================================


class TestValidateImage:
    def test_valid_image_passes(self, tmp_path):
        path = tmp_path / "boot.iso"
        path.write_bytes(b"\x00" * 4096)
        media_session.validate_image(str(path))  # must not raise

    def test_missing_image_raises_protocol_error(self, tmp_path):
        with pytest.raises(ProtocolError):
            media_session.validate_image(str(tmp_path / "missing.iso"))

    def test_directory_raises_protocol_error(self, tmp_path):
        with pytest.raises(ProtocolError):
            media_session.validate_image(str(tmp_path))


# ===========================================================================
# The single-session hazard: reclamation.
# ===========================================================================


class TestFindConflictingSessions:
    def test_no_state_files_at_all_is_empty(self, tmp_path):
        assert media_session.find_conflicting_sessions(tmp_path, EXAMPLE_ENDPOINT) == []

    def test_finds_a_session_against_the_same_endpoint(self, tmp_path):
        media_session._write_state_atomic(tmp_path, "other", {"session_id": "other", "endpoint": EXAMPLE_ENDPOINT, "pid": 1, "state": "attached"})
        conflicts = media_session.find_conflicting_sessions(tmp_path, EXAMPLE_ENDPOINT)
        assert [c["session_id"] for c in conflicts] == ["other"]

    def test_ignores_a_session_against_a_different_endpoint(self, tmp_path):
        media_session._write_state_atomic(tmp_path, "elsewhere", {"session_id": "elsewhere", "endpoint": "198.51.100.5:5120", "pid": 1, "state": "attached"})
        assert media_session.find_conflicting_sessions(tmp_path, EXAMPLE_ENDPOINT) == []

    def test_excludes_the_named_session_id(self, tmp_path):
        media_session._write_state_atomic(tmp_path, "self", {"session_id": "self", "endpoint": EXAMPLE_ENDPOINT, "pid": 1, "state": "attached"})
        assert media_session.find_conflicting_sessions(tmp_path, EXAMPLE_ENDPOINT, exclude_session_id="self") == []

    def test_a_stale_conflicting_session_is_still_found(self, tmp_path):
        dead_pid = _dead_pid()
        media_session._write_state_atomic(tmp_path, "stale", {"session_id": "stale", "endpoint": EXAMPLE_ENDPOINT, "pid": dead_pid, "state": "attached"})
        conflicts = media_session.find_conflicting_sessions(tmp_path, EXAMPLE_ENDPOINT)
        assert [c["session_id"] for c in conflicts] == ["stale"]

    def test_multiple_different_session_ids_against_different_endpoints_are_all_independent(self, tmp_path):
        # Sessions for OTHER boards must never be touched by a reclamation pass scoped
        # to one endpoint.
        media_session._write_state_atomic(tmp_path, "a", {"session_id": "a", "endpoint": EXAMPLE_ENDPOINT, "pid": 1, "state": "attached"})
        media_session._write_state_atomic(tmp_path, "b", {"session_id": "b", "endpoint": "198.51.100.5:5120", "pid": 1, "state": "attached"})
        conflicts = media_session.find_conflicting_sessions(tmp_path, EXAMPLE_ENDPOINT)
        assert [c["session_id"] for c in conflicts] == ["a"]


class TestReclaimConflictingSessions:
    """This is the "eject/reset before insert" step: always-run, not a fallback."""

    def test_reclaims_a_live_conflicting_session(self, tmp_path):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            media_session._write_state_atomic(
                tmp_path, "held-by-someone-else", {"session_id": "held-by-someone-else", "endpoint": EXAMPLE_ENDPOINT, "pid": proc.pid, "state": "attached"}
            )
            reclaimed = media_session.reclaim_conflicting_sessions(tmp_path, EXAMPLE_ENDPOINT, exclude_session_id="fresh", stop_timeout=5.0)
            assert reclaimed == ["held-by-someone-else"]
            proc.wait(timeout=5.0)
            assert media_session.is_pid_alive(proc.pid) is False
            assert media_session.read_state(tmp_path, "held-by-someone-else") is None
        finally:
            with pytest.raises(ProcessLookupError):
                # Confirms the process really is gone rather than merely unresponsive --
                # if this does NOT raise, the test above did not actually prove reclamation.
                os.kill(proc.pid, 0)

    def test_reclaims_and_removes_a_stale_conflicting_session_without_sending_sigterm(self, tmp_path, monkeypatch):
        # is_pid_alive() itself legitimately probes with os.kill(pid, 0) (signal 0, no
        # actual signal delivered) -- what must never happen for an already-dead pid is
        # request_stop()'s real SIGTERM.
        dead_pid = _dead_pid()
        media_session._write_state_atomic(tmp_path, "stale", {"session_id": "stale", "endpoint": EXAMPLE_ENDPOINT, "pid": dead_pid, "state": "attached"})

        def _boom(*_args, **_kwargs):
            raise AssertionError("a stale (dead) session must never be sent SIGTERM")

        monkeypatch.setattr(media_session, "request_stop", _boom)
        reclaimed = media_session.reclaim_conflicting_sessions(tmp_path, EXAMPLE_ENDPOINT, exclude_session_id="fresh")
        assert reclaimed == ["stale"]
        assert media_session.read_state(tmp_path, "stale") is None

    def test_never_touches_the_excluded_session_id(self, tmp_path):
        media_session._write_state_atomic(tmp_path, "fresh", {"session_id": "fresh", "endpoint": EXAMPLE_ENDPOINT, "pid": os.getpid(), "state": "attached"})
        reclaimed = media_session.reclaim_conflicting_sessions(tmp_path, EXAMPLE_ENDPOINT, exclude_session_id="fresh")
        assert reclaimed == []
        assert media_session.read_state(tmp_path, "fresh") is not None

    def test_never_touches_a_session_against_a_different_endpoint(self, tmp_path):
        media_session._write_state_atomic(
            tmp_path, "unrelated", {"session_id": "unrelated", "endpoint": "198.51.100.5:5120", "pid": os.getpid(), "state": "attached"}
        )
        reclaimed = media_session.reclaim_conflicting_sessions(tmp_path, EXAMPLE_ENDPOINT, exclude_session_id="fresh")
        assert reclaimed == []
        assert media_session.read_state(tmp_path, "unrelated") is not None

    def test_no_conflicts_is_a_no_op(self, tmp_path):
        assert media_session.reclaim_conflicting_sessions(tmp_path, EXAMPLE_ENDPOINT, exclude_session_id="fresh") == []


# ===========================================================================
# spawn_session platform guard
# ===========================================================================


class TestSpawnSessionPlatformGuard:
    def test_raises_unsupported_capability_when_os_fork_is_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.delattr(media_session.os, "fork", raising=False)
        config = _config(session_id="abc", runtime_dir=tmp_path, image=str(tmp_path / "x.iso"))
        with pytest.raises(UnsupportedCapabilityError):
            media_session.spawn_session(config, username="admin", password="test-password-not-real")


# ===========================================================================
# _run_daemon: the real serve loop, idle handling, and outcome classification.
#
# The network layer (AspClient login/allocate_media_session, iusb.Session.connect)
# is faked; everything from there down -- iusb.Session.serve_forever,
# iusb.CDROMDevice, the real image file -- is real.
# ===========================================================================


class _FakeJnlpSession:
    def __init__(self, token: str = "STOKEN-fake-not-real") -> None:  # noqa: S107 - test fixture, not a real credential.
        self.kvm_token = token


class _FakeAspClient:
    """Stands in for asp.AspClient -- and only for it. Everything below the
    login/JNLP fetch (the actual iUSB session) stays real.
    """

    instances: list[_FakeAspClient] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.login_called = False
        self.allocate_calls: list[str] = []
        type(self).instances.append(self)

    def login(self) -> None:
        self.login_called = True

    def allocate_media_session(self, *, client_ip: str, secure: bool | None = None) -> _FakeJnlpSession:
        self.allocate_calls.append(client_ip)
        return _FakeJnlpSession()


class _ScriptedTransport:
    """Drives iusb.Session over a scripted byte stream, standing in for a real
    socket the same way test_iusb.py's FakeTransport does, but wired in via
    iusb.Session.from_transport so the auth handshake ALSO runs for real.
    """

    def __init__(self, script: list) -> None:
        self._ops = list(script)
        self._buffer = bytearray()
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout = None

    def set_timeout(self, seconds) -> None:
        self.timeout = seconds

    def recv_exact(self, n: int) -> bytes:
        while len(self._buffer) < n:
            if not self._ops:
                raise AssertionError("script exhausted while more bytes were requested")
            op = self._ops.pop(0)
            if op == "idle":
                raise iusb.IdleTimeout
            if op == "eof":
                raise EOFError("fake: peer closed")
            self._buffer += op
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data

    def send_all(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def close(self) -> None:
        self.closed = True


def _ack_frame(req_header: iusb.Header) -> bytes:
    payload = bytearray(64)
    payload[iusb.OPCODE_OFFSET] = iusb.OP_REDIRECT_ACK
    payload[iusb.CONN_STATUS_OFFSET] = iusb.CONN_OK
    return iusb.build_response_frame(req_header, bytes(payload), iusb.DEVICE_CDROM)


def _kill_frame() -> bytes:
    payload = bytearray(16)
    payload[iusb.OPCODE_OFFSET] = iusb.OP_KILL_REDIR
    return iusb.Header(data_packet_len=len(payload)).marshal() + bytes(payload)


def _read_capacity_frame(*, sequence_number: int = 1) -> bytes:
    payload = bytes.fromhex("000000006300000001250000000000000000000000000000000000")
    return iusb.Header(data_packet_len=len(payload), sequence_number=sequence_number).marshal() + payload


class TestRunDaemonIdleHandling:
    """The highest-consequence behaviour in this whole daemon.

    ``_run_daemon`` drives ``iusb.Session.serve_forever`` for real (only the network
    dial -- ``AspClient`` and the transport -- is faked). This class asserts the
    property the task's own hardware capture demands directly: a long idle period
    inside the daemon's real loop must not raise, must not end the session, and must
    keep the state file's ``updated_at`` moving while ``last_request_at`` stays put.
    """

    @pytest.fixture
    def harness(self, tmp_path, monkeypatch):
        image_path = tmp_path / "boot.iso"
        image_path.write_bytes(b"\x00" * (4 * iusb.CD_BLOCK_SIZE))
        runtime_dir = tmp_path / "runtime"

        monkeypatch.setattr(media_session, "_redirect_std_fds", lambda _log_path: None)
        monkeypatch.setattr(media_session.signal, "signal", lambda _signum, _handler: None)
        monkeypatch.setattr(media_session, "_stop_flag", False)
        monkeypatch.setattr(media_session, "AspClient", _FakeAspClient)
        monkeypatch.setattr(media_session, "resolve_local_ip", lambda _host: "203.0.113.1")
        _FakeAspClient.instances = []

        transports: list[_ScriptedTransport] = []

        def run(script: list, *, session_id: str = "idle-session") -> dict:
            transport = _ScriptedTransport(script)
            transports.append(transport)
            monkeypatch.setattr(iusb.Session, "connect", classmethod(lambda cls, *a, **k: cls.from_transport(transport, "STOKEN-fake-not-real")))
            config = _config(session_id=session_id, runtime_dir=runtime_dir, image=str(image_path))
            media_session._run_daemon(config, "admin", "test-password-not-real")
            return media_session.read_state(runtime_dir, session_id)

        run.runtime_dir = runtime_dir  # type: ignore[attr-defined]
        run.transports = transports  # type: ignore[attr-defined]
        return run

    def _handshake(self) -> list:
        """The auth request + ACK exchange every script needs before serve_forever runs."""
        auth_header = iusb.Header(data_packet_len=iusb.AUTH_PAYLOAD_LEN)
        return [_ack_frame(auth_header)]

    #: 100 simulated idle cycles, matching test_iusb.py's own bound: comfortably past
    #: the measured 130s of live silence and the required >=180s test bound, with the
    #: real 2.0s daemon poll interval (media_session._RECV_POLL_TIMEOUT). No real
    #: sleep occurs -- the scripted transport raises IdleTimeout synchronously.
    SIMULATED_IDLE_CYCLES = 100

    def test_a_long_idle_period_does_not_raise_and_the_session_stays_attached(self, harness):
        # The daemon must consume every single idle cycle without raising or exiting
        # early, and only then report a clean, deliberate outcome once a real signal
        # (here, a kill frame) actually arrives.
        script = self._handshake() + ["idle"] * self.SIMULATED_IDLE_CYCLES + [_kill_frame()]
        final = harness(script)

        assert final["state"] == media_session.STATE_DETACHED
        assert final["error"] == "BMC sent a redirection-terminate (opcode 0xF6)"

    def test_idle_then_a_real_request_is_served_and_counted(self, harness):
        script = self._handshake() + ["idle"] * 50 + [_read_capacity_frame(sequence_number=9)] + [_kill_frame()]
        final = harness(script)
        assert final["state"] == media_session.STATE_DETACHED
        assert final["sectors_served"] == 0  # READ CAPACITY(10) reports capacity; it does not itself transfer sectors
        assert final["last_request_at"] is not None

    def test_updated_at_advances_on_idle_while_last_request_at_does_not(self, harness, monkeypatch):
        # The operator-facing contract this test pins: updated_at is a heartbeat (moves
        # on every poll, idle or not); last_request_at only moves on real traffic. A
        # caller must be able to tell "idle" from "dead" using exactly these two fields.
        ticks = iter([f"2026-01-01T00:00:{i:02d}+00:00" for i in range(10)])
        monkeypatch.setattr(media_session, "_now_iso", lambda: next(ticks))

        script = [*self._handshake(), "idle", "idle", "idle", _kill_frame()]
        harness(script)
        # Re-read is not enough here (only the final state survives) -- assert via the
        # persisted file's own timestamps being from the *later* part of the tick
        # sequence than the state written right after attach, proving _persist() ran
        # again during the idle stretch.
        final = media_session.read_state(harness.runtime_dir, "idle-session")
        assert final["updated_at"] > final["started_at"]
        assert final["last_request_at"] is None  # never any real traffic in this script

    def test_idle_streak_accumulates_polls_and_seconds_while_quiet(self, harness):
        script = self._handshake() + ["idle"] * 5 + [_kill_frame()]
        final = harness(script)
        # The streak is still open at kill time (a kill frame is not a real
        # SCSI request, so it never closes it via _on_request) -- see
        # _close_idle_streak's own docstring: a normal serve_forever exit
        # closes whatever was open, so this shows up as last_idle_streak, not
        # current_idle_streak, in the FINAL persisted state.
        assert final["current_idle_streak"] is None
        assert final["last_idle_streak"]["polls"] == 5
        assert final["last_idle_streak"]["seconds"] == 5 * media_session._RECV_POLL_TIMEOUT
        assert final["last_idle_streak"]["started_at"] is not None
        assert final["last_idle_streak"]["ended_at"] is not None
        assert final["idle_polls"] == 5

    def test_a_real_request_closes_the_streak_into_last_idle_streak(self, harness):
        script = self._handshake() + ["idle"] * 3 + [_read_capacity_frame(sequence_number=9)] + [_kill_frame()]
        final = harness(script)
        # last_idle_streak reflects the pre-request quiet stretch (3 polls),
        # not anything after the request resumed traffic.
        assert final["last_idle_streak"]["polls"] == 3
        assert final["idle_polls"] == 3
        # current_idle_streak was cleared by the request and never reopened
        # (the kill frame arrives immediately after, with no further idle).
        assert final["current_idle_streak"] is None

    def test_last_idle_streak_is_not_clobbered_by_a_second_shorter_quiet_stretch(self, harness):
        # A second, SHORTER idle stretch after new traffic must not silently
        # look identical to the first, longer one in a naive read of the
        # final state -- last_idle_streak always reflects the MOST RECENT
        # closed streak, which is exactly what a post-mortem needs, not
        # necessarily the longest one ever seen.
        script = self._handshake() + ["idle"] * 10 + [_read_capacity_frame(sequence_number=9)] + ["idle"] * 2 + [_kill_frame()]
        final = harness(script)
        assert final["last_idle_streak"]["polls"] == 2
        assert final["idle_polls"] == 12  # lifetime total across both streaks

    def test_idle_polls_is_a_lifetime_counter_not_reset_by_a_request(self, harness):
        script = self._handshake() + ["idle"] * 4 + [_read_capacity_frame(sequence_number=9)] + ["idle"] * 6 + [_kill_frame()]
        final = harness(script)
        assert final["idle_polls"] == 10

    def test_no_idle_at_all_leaves_both_streak_fields_null(self, harness):
        script = [*self._handshake(), _read_capacity_frame(sequence_number=9), _kill_frame()]
        final = harness(script)
        assert final["current_idle_streak"] is None
        assert final["last_idle_streak"] is None
        assert final["idle_polls"] == 0

    def test_a_wire_fault_leaves_the_idle_streak_open_not_force_closed(self, harness):
        # The exception path (any IkvmError escaping serve_forever without a
        # real request ever having succeeded first) deliberately does NOT
        # call _close_idle_streak -- see that function's own docstring on why
        # "idle right up until this failure" is itself useful forensic
        # signal, not something to erase.
        bad_header = bytearray(iusb.Header(data_packet_len=0).marshal())
        bad_header[0:8] = b"XXXX    "
        script = [*self._handshake(), "idle", "idle", bytes(bad_header)]
        final = harness(script)
        assert final["state"] == media_session.STATE_ERROR
        assert final["current_idle_streak"] is not None
        assert final["current_idle_streak"]["polls"] == 2
        assert final["last_idle_streak"] is None

    def test_peer_closing_the_connection_is_recorded_as_detached_not_error(self, harness):
        script = [*self._handshake(), "idle", "eof"]
        final = harness(script)
        assert final["state"] == media_session.STATE_DETACHED
        assert final["error"] == "connection closed by peer"
        assert final["error_class"] is None

    def test_local_stop_flag_is_recorded_as_a_clean_detach(self, harness, monkeypatch):
        # _stop_flag is a module global the real SIGTERM handler sets. Simulate that
        # happening mid-serve by flipping it directly after a few idle polls, via a
        # counting wrapper around recv_exact -- the scripted transport itself only
        # understands "idle"/"eof"/bytes, not a callback hook.
        monkeypatch.setattr(media_session, "_stop_flag", False)

        state = {"n": 0}
        real_recv_exact = _ScriptedTransport.recv_exact

        def _counting_recv_exact(self, n):
            if self._ops and self._ops[0] == "idle":
                state["n"] += 1
                if state["n"] > 3:
                    media_session._stop_flag = True
            return real_recv_exact(self, n)

        monkeypatch.setattr(_ScriptedTransport, "recv_exact", _counting_recv_exact)
        script = self._handshake() + ["idle"] * 10
        final = harness(script)
        assert final["state"] == media_session.STATE_DETACHED
        assert final["error"] is None

    def test_malformed_frame_after_attach_is_a_classified_protocol_error(self, harness):
        bad_header = bytearray(iusb.Header(data_packet_len=0).marshal())
        bad_header[0:8] = b"XXXX    "
        script = [*self._handshake(), "idle", bytes(bad_header)]
        final = harness(script)
        assert final["state"] == media_session.STATE_ERROR
        assert final["error_class"] == ErrorClass.PROTOCOL

    def test_auth_rejection_is_bmc_busy_not_a_generic_protocol_error(self, harness, monkeypatch):
        def _rejecting_connect(cls, *_args, **_kwargs):
            payload = bytearray(64)
            payload[iusb.OPCODE_OFFSET] = iusb.OP_REDIRECT_ACK
            payload[iusb.CONN_STATUS_OFFSET] = iusb.CONN_ERR_IN_USE_5
            auth_header = iusb.Header(data_packet_len=iusb.AUTH_PAYLOAD_LEN)
            transport = _ScriptedTransport([iusb.build_response_frame(auth_header, bytes(payload), iusb.DEVICE_CDROM)])
            return cls.from_transport(transport, "STOKEN-fake-not-real")

        monkeypatch.setattr(iusb.Session, "connect", classmethod(_rejecting_connect))
        image_path = harness.runtime_dir.parent / "boot.iso"
        config = _config(session_id="busy-session", runtime_dir=harness.runtime_dir, image=str(image_path))
        media_session._run_daemon(config, "admin", "test-password-not-real")
        final = media_session.read_state(harness.runtime_dir, "busy-session")
        assert final["state"] == media_session.STATE_ERROR
        assert final["error_class"] == ErrorClass.BMC_BUSY
        assert "cold reset" in final["error"]

    def test_the_daemon_claims_the_state_file_before_it_opens_the_image(self, harness, monkeypatch):
        seen = []
        real_open = iusb.FileReader.open

        def _spy(path):
            seen.append(media_session.read_state(harness.runtime_dir, "idle-session"))
            return real_open(path)

        monkeypatch.setattr(iusb.FileReader, "open", staticmethod(_spy))
        harness([*self._handshake(), _kill_frame()])
        assert seen and seen[0] is not None
        assert seen[0]["state"] == media_session.STATE_STARTING
        assert seen[0]["pid"] == os.getpid()

    def test_the_media_token_never_reaches_the_state_file(self, harness):
        harness(self._handshake() + ["idle"] * 3 + [_kill_frame()])
        raw = media_session.state_file_path(harness.runtime_dir, "idle-session").read_text()
        assert "STOKEN-fake-not-real" not in raw
        assert "test-password-not-real" not in raw

    def test_asp_client_is_used_to_log_in_and_allocate_a_media_session(self, harness):
        harness([*self._handshake(), _kill_frame()])
        assert len(_FakeAspClient.instances) == 1
        client = _FakeAspClient.instances[0]
        assert client.login_called is True
        assert client.allocate_calls == ["203.0.113.1"]

    def test_an_unlabelled_password_embedded_in_a_raw_exception_is_still_scrubbed(self, harness, monkeypatch):
        # errors.redact()'s generic patterns only catch a *labelled* secret
        # (`password: ...`); a raw, non-IkvmError exception whose message happens to
        # embed the password with no separator at all (e.g. a third-party library
        # echoing constructor arguments) is exactly the shape those patterns cannot
        # catch. _fail()'s extra-secrets backstop (exact literal substring removal)
        # must catch it anyway.
        class _ExplodingAspClient(_FakeAspClient):
            def login(self) -> None:
                raise RuntimeError("connect failed for admin using secret value test-password-not-real right here")

        monkeypatch.setattr(media_session, "AspClient", _ExplodingAspClient)
        final = harness([*self._handshake(), _kill_frame()], session_id="password-leak-check")
        assert final["state"] == media_session.STATE_ERROR
        assert "test-password-not-real" not in final["error"]
        raw = media_session.state_file_path(harness.runtime_dir, "password-leak-check").read_text()
        assert "test-password-not-real" not in raw


def _dead_pid() -> int:
    """A pid guaranteed not to be alive, for stale-session tests."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])  # fixed argv, no shell
    proc.wait()
    return proc.pid


class TestJsonSerializability:
    def test_state_written_by_the_daemon_shape_is_json_safe_and_credential_free(self, tmp_path):
        state = media_session._initial_state(session_id="abc", endpoint=EXAMPLE_ENDPOINT, pid=123, image="/srv/boot.iso")
        state["state"] = "attached"
        state["bytes_read"] = 4096
        state["sectors_served"] = 2
        media_session._write_state_atomic(tmp_path, "abc", state)
        raw = media_session.state_file_path(tmp_path, "abc").read_text(encoding="utf-8")
        assert "password" not in raw.lower()
        assert json.loads(raw) == state


class TestGenerateSessionId:
    def test_generates_a_plausible_unique_hex_id(self):
        first = media_session.generate_session_id()
        second = media_session.generate_session_id()
        assert first != second
        assert len(first) == 32
        int(first, 16)


class TestSignalHandlerSetsStopFlag:
    def test_handle_sigterm_sets_the_module_global(self, monkeypatch):
        monkeypatch.setattr(media_session, "_stop_flag", False)
        media_session._handle_sigterm(signal.SIGTERM, None)
        assert media_session._stop_flag is True
