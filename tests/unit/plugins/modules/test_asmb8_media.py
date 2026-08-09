# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for the ``asmb8_media`` module's decision logic.

``media_session.spawn_session`` (the real fork -> asp login -> JNLP fetch -> iUSB
handshake -> serve path) is mocked throughout: it is exercised for real by the
integration test target against the mock iUSB server, another agent's
responsibility for this task. These tests are about the module's own logic --
idempotency, stale-session recovery, the always-run reclamation pass, option
validation, check-mode, credential/token safety, and error surfacing -- all of
which is fully exercisable without ever forking or opening a socket.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import media_session
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_media

PASSWORD = "Sup3rSecret!"

BASE_ARGS = {
    "host": "10.0.0.5",
    "username": "admin",
    "password": PASSWORD,
    "use_tls": False,
    "allow_insecure_transport": True,
}


class AnsibleExitJson(Exception):
    def __init__(self, kwargs):
        super().__init__("exit_json")
        self.kwargs = kwargs


class AnsibleFailJson(Exception):
    def __init__(self, kwargs):
        super().__init__("fail_json")
        self.kwargs = kwargs


def _set_module_args(args: dict) -> None:
    basic._ANSIBLE_ARGS = to_bytes(json.dumps({"ANSIBLE_MODULE_ARGS": args}))
    basic._ANSIBLE_PROFILE = "legacy"


def _exit_json(*_args, **kwargs):
    raise AnsibleExitJson(kwargs)


def _fail_json(*_args, **kwargs):
    raise AnsibleFailJson(kwargs)


@pytest.fixture(autouse=True)
def _patch_exit_and_fail(monkeypatch):
    monkeypatch.setattr(basic.AnsibleModule, "exit_json", _exit_json)
    monkeypatch.setattr(basic.AnsibleModule, "fail_json", _fail_json)


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "boot.iso"
    path.write_bytes(b"\x00" * 4096)
    return str(path)


@pytest.fixture
def runtime_dir(tmp_path):
    return str(tmp_path / "runtime")


def _attach_args(*, runtime_dir, image, **overrides) -> dict:
    args = dict(BASE_ARGS, image=image, state="attached", runtime_dir=runtime_dir)
    args.update(overrides)
    return args


def _endpoint(cd_port: int = 5120) -> str:
    return f"{BASE_ARGS['host']}:{cd_port}"


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])  # fixed argv, no shell
    proc.wait()
    return proc.pid


class TestAttachFreshSession:
    def test_spawns_and_reports_changed_true(self, runtime_dir, image):
        _set_module_args(_attach_args(runtime_dir=runtime_dir, image=image))
        attached_state = {"session_id": "will-be-overwritten", "pid": 4242, "state": "attached", "error": None, "bytes_read": 0}
        with (
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.spawn_session", return_value=4242) as spawn,
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.wait_for_state", return_value=attached_state),
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_media.main()
        result = excinfo.value.kwargs
        assert result["changed"] is True
        assert result["session_state"] == "attached"
        assert result["pid"] == 4242
        assert result["bytes_read"] == 0
        assert result.get("session_id")
        assert result["reclaimed_sessions"] == []
        spawn.assert_called_once()
        config = spawn.call_args.args[0]
        assert config.cd_port == 5120
        assert config.image == image

    def test_receipt_is_nested_under_operation_not_spread_at_top_level(self, runtime_dir, image):
        _set_module_args(_attach_args(runtime_dir=runtime_dir, image=image))
        attached_state = {"session_id": "x", "pid": 4242, "state": "attached", "error": None, "bytes_read": 512}
        with (
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.spawn_session", return_value=4242),
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.wait_for_state", return_value=attached_state),
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_media.main()
        result = excinfo.value.kwargs
        for moved_field in ("schema", "action", "endpoint", "previous", "desired", "observed"):
            assert moved_field not in result, f"{moved_field!r} must not be spread at the top level; it belongs under operation"
        assert result["operation"]["schema"] == "asmb8-ikvm-operation/v1"
        assert result["operation"]["action"] == "asmb8_media.attach"
        assert result["operation"]["endpoint"] == _endpoint()
        assert result["operation"]["error_class"] is None

    def test_generated_session_id_is_returned_for_a_later_detach(self, runtime_dir, image):
        _set_module_args(_attach_args(runtime_dir=runtime_dir, image=image))
        with (
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.spawn_session", return_value=1),
            patch(
                "ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.wait_for_state",
                return_value={"state": "attached", "pid": 1, "bytes_read": 0},
            ),
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_media.main()
        session_id = excinfo.value.kwargs["session_id"]
        assert isinstance(session_id, str) and len(session_id) == 32

    def test_daemon_reporting_error_fails_the_module_with_its_own_error_class(self, runtime_dir, image):
        _set_module_args(_attach_args(runtime_dir=runtime_dir, image=image))
        error_state = {"state": "error", "error": "BMC rejected the media session", "error_class": "bmc_busy", "pid": 99, "bytes_read": 0}
        with (
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.spawn_session", return_value=99),
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.wait_for_state", return_value=error_state),
        ):
            with pytest.raises(AnsibleFailJson) as excinfo:
                asmb8_media.main()
        assert excinfo.value.kwargs["error_class"] == "bmc_busy"
        assert "BMC rejected the media session" in excinfo.value.kwargs["msg"]

    def test_daemon_reporting_an_unclassified_error_falls_back_to_protocol(self, runtime_dir, image):
        _set_module_args(_attach_args(runtime_dir=runtime_dir, image=image))
        error_state = {"state": "error", "error": "something odd", "pid": 99, "bytes_read": 0}  # no error_class key at all
        with (
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.spawn_session", return_value=99),
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.wait_for_state", return_value=error_state),
        ):
            with pytest.raises(AnsibleFailJson) as excinfo:
                asmb8_media.main()
        assert excinfo.value.kwargs["error_class"] == "protocol"


class TestAttachIdempotency:
    def test_existing_live_session_is_not_respawned(self, runtime_dir, image):
        session_id = "existing-session"
        media_session._write_state_atomic(
            runtime_dir, session_id, {"session_id": session_id, "pid": os.getpid(), "state": "attached", "error": None, "bytes_read": 0}
        )
        _set_module_args(_attach_args(runtime_dir=runtime_dir, image=image, session_id=session_id))
        with patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.spawn_session") as spawn:
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_media.main()
        result = excinfo.value.kwargs
        assert result["changed"] is False
        assert result["session_id"] == session_id
        spawn.assert_not_called()

    def test_stale_session_file_is_recovered_and_a_fresh_one_spawned(self, runtime_dir, image):
        session_id = "stale-session"
        dead_pid = _dead_pid()
        media_session._write_state_atomic(
            runtime_dir, session_id, {"session_id": session_id, "pid": dead_pid, "state": "attached", "error": None, "bytes_read": 0}
        )
        _set_module_args(_attach_args(runtime_dir=runtime_dir, image=image, session_id=session_id))
        with (
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.spawn_session", return_value=4242) as spawn,
            patch(
                "ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.wait_for_state",
                return_value={"state": "attached", "pid": 4242, "bytes_read": 0},
            ),
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_media.main()
        result = excinfo.value.kwargs
        assert result["changed"] is True
        assert result["recovered_stale_session"] is True
        spawn.assert_called_once()


class TestSingleSessionReclamation:
    """The "eject/reset before insert" step: always run, never a fallback."""

    def test_a_conflicting_live_session_is_reclaimed_before_attaching(self, runtime_dir, image):
        other_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            media_session._write_state_atomic(
                runtime_dir,
                "someone-elses-session",
                {"session_id": "someone-elses-session", "endpoint": _endpoint(), "pid": other_proc.pid, "state": "attached"},
            )
            _set_module_args(_attach_args(runtime_dir=runtime_dir, image=image, session_id="fresh-session"))
            with (
                patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.spawn_session", return_value=4242),
                patch(
                    "ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.wait_for_state",
                    return_value={"state": "attached", "pid": 4242, "bytes_read": 0},
                ),
            ):
                with pytest.raises(AnsibleExitJson) as excinfo:
                    asmb8_media.main()
            result = excinfo.value.kwargs
            assert result["reclaimed_sessions"] == ["someone-elses-session"]
            # The reclaimed session's process was actually asked to stop, not merely
            # forgotten about.
            other_proc.wait(timeout=5.0)
            assert media_session.is_pid_alive(other_proc.pid) is False
            assert media_session.read_state(runtime_dir, "someone-elses-session") is None
        finally:
            with pytest.raises(ProcessLookupError):
                os.kill(other_proc.pid, 0)

    def test_reclamation_never_touches_a_session_for_a_different_endpoint(self, runtime_dir, image):
        media_session._write_state_atomic(
            runtime_dir,
            "different-board",
            {"session_id": "different-board", "endpoint": "198.51.100.9:5120", "pid": os.getpid(), "state": "attached"},
        )
        _set_module_args(_attach_args(runtime_dir=runtime_dir, image=image, session_id="fresh-session"))
        with (
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.spawn_session", return_value=4242),
            patch(
                "ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.wait_for_state",
                return_value={"state": "attached", "pid": 4242, "bytes_read": 0},
            ),
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_media.main()
        assert excinfo.value.kwargs["reclaimed_sessions"] == []
        assert media_session.read_state(runtime_dir, "different-board") is not None

    def test_reclamation_runs_even_when_there_is_nothing_to_reclaim(self, runtime_dir, image):
        _set_module_args(_attach_args(runtime_dir=runtime_dir, image=image))
        with (
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.spawn_session", return_value=1),
            patch(
                "ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.wait_for_state",
                return_value={"state": "attached", "pid": 1, "bytes_read": 0},
            ),
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_media.main()
        assert excinfo.value.kwargs["reclaimed_sessions"] == []

    def test_check_mode_reports_what_would_be_reclaimed_but_signals_nothing(self, runtime_dir, image):
        media_session._write_state_atomic(
            runtime_dir,
            "someone-elses-session",
            {"session_id": "someone-elses-session", "endpoint": _endpoint(), "pid": os.getpid(), "state": "attached"},
        )
        args = dict(_attach_args(runtime_dir=runtime_dir, image=image, session_id="fresh-session"), _ansible_check_mode=True)
        _set_module_args(args)
        with (
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.request_stop") as request_stop,
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.spawn_session") as spawn,
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_media.main()
        assert excinfo.value.kwargs["reclaimed_sessions"] == ["someone-elses-session"]
        request_stop.assert_not_called()
        spawn.assert_not_called()
        # check mode must not have actually removed the other session's state.
        assert media_session.read_state(runtime_dir, "someone-elses-session") is not None


class TestAttachCheckMode:
    def test_never_spawns_but_still_validates_the_image(self, runtime_dir, image):
        args = dict(_attach_args(runtime_dir=runtime_dir, image=image), _ansible_check_mode=True)
        _set_module_args(args)
        with patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.spawn_session") as spawn:
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_media.main()
        assert excinfo.value.kwargs["changed"] is True
        spawn.assert_not_called()

    def test_bad_image_path_still_fails_in_check_mode(self, runtime_dir, tmp_path):
        args = dict(_attach_args(runtime_dir=runtime_dir, image=str(tmp_path / "does-not-exist.iso")), _ansible_check_mode=True)
        _set_module_args(args)
        with patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.spawn_session") as spawn:
            with pytest.raises(AnsibleFailJson) as excinfo:
                asmb8_media.main()
        assert excinfo.value.kwargs["error_class"] == "protocol"
        spawn.assert_not_called()

    def test_check_mode_never_forks_even_on_a_platform_without_fork(self, runtime_dir, image, monkeypatch):
        monkeypatch.delattr(media_session.os, "fork", raising=False)
        args = dict(_attach_args(runtime_dir=runtime_dir, image=image), _ansible_check_mode=True)
        _set_module_args(args)
        with pytest.raises(AnsibleExitJson) as excinfo:
            asmb8_media.main()
        assert excinfo.value.kwargs["changed"] is True


class TestOptionValidation:
    def test_missing_image_is_rejected_by_the_argument_spec(self, runtime_dir):
        args = dict(BASE_ARGS, state="attached", runtime_dir=runtime_dir)
        _set_module_args(args)
        with pytest.raises((AnsibleFailJson, SystemExit)):
            asmb8_media.main()

    def test_detach_without_session_id_is_rejected_by_the_argument_spec(self, runtime_dir):
        args = dict(BASE_ARGS, state="detached", runtime_dir=runtime_dir)
        _set_module_args(args)
        with pytest.raises((AnsibleFailJson, SystemExit)):
            asmb8_media.main()

    def test_insecure_transport_without_acknowledgement_is_refused(self, runtime_dir, image):
        args = dict(_attach_args(runtime_dir=runtime_dir, image=image))
        args["allow_insecure_transport"] = False
        _set_module_args(args)
        with pytest.raises(AnsibleFailJson) as excinfo:
            asmb8_media.main()
        assert excinfo.value.kwargs["error_class"] == "tls_validation"


class TestDetach:
    def test_no_existing_session_is_a_no_op(self, runtime_dir):
        args = dict(BASE_ARGS, state="detached", session_id="never-existed", runtime_dir=runtime_dir)
        _set_module_args(args)
        with pytest.raises(AnsibleExitJson) as excinfo:
            asmb8_media.main()
        result = excinfo.value.kwargs
        assert result["changed"] is False
        assert result["session_state"] == "unknown"

    def test_live_session_is_stopped_and_state_file_removed(self, runtime_dir):
        session_id = "live-session"
        media_session._write_state_atomic(runtime_dir, session_id, {"session_id": session_id, "pid": 4242, "state": "attached", "error": None, "bytes_read": 0})
        args = dict(BASE_ARGS, state="detached", session_id=session_id, runtime_dir=runtime_dir)
        _set_module_args(args)
        with (
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.is_pid_alive", return_value=True),
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.request_stop") as request_stop,
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.wait_for_exit", return_value=True),
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_media.main()
        result = excinfo.value.kwargs
        assert result["changed"] is True
        request_stop.assert_called_once_with(4242)
        assert media_session.read_state(runtime_dir, session_id) is None

    def test_stale_session_reports_no_change_but_cleans_up(self, runtime_dir):
        session_id = "stale-session"
        dead_pid = _dead_pid()
        media_session._write_state_atomic(
            runtime_dir, session_id, {"session_id": session_id, "pid": dead_pid, "state": "attached", "error": None, "bytes_read": 0}
        )
        args = dict(BASE_ARGS, state="detached", session_id=session_id, runtime_dir=runtime_dir)
        _set_module_args(args)
        with pytest.raises(AnsibleExitJson) as excinfo:
            asmb8_media.main()
        result = excinfo.value.kwargs
        assert result["changed"] is False
        assert result["recovered_stale_session"] is True
        assert media_session.read_state(runtime_dir, session_id) is None

    def test_check_mode_never_signals_a_live_session(self, runtime_dir):
        session_id = "live-session"
        media_session._write_state_atomic(runtime_dir, session_id, {"session_id": session_id, "pid": 4242, "state": "attached", "error": None, "bytes_read": 0})
        args = dict(BASE_ARGS, state="detached", session_id=session_id, runtime_dir=runtime_dir, _ansible_check_mode=True)
        _set_module_args(args)
        with (
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.is_pid_alive", return_value=True),
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.request_stop") as request_stop,
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_media.main()
        assert excinfo.value.kwargs["changed"] is True
        request_stop.assert_not_called()
        assert media_session.read_state(runtime_dir, session_id) is not None


class TestIndeterminateAttachTimeout:
    """A daemon still alive but unconfirmed at attach_timeout must not be reported as
    a success, must be classified as an indeterminate timeout, and must not be torn
    down -- see the module docstring's O(attach_timeout) contract.
    """

    def _attach(self, monkeypatch, runtime_dir, image, *, polled_state, pid_alive):
        monkeypatch.setattr(media_session, "spawn_session", lambda *a, **k: 4242)
        monkeypatch.setattr(media_session, "wait_for_state", lambda *a, **k: polled_state)
        monkeypatch.setattr(media_session, "is_pid_alive", lambda pid: pid_alive)
        _set_module_args(_attach_args(runtime_dir=runtime_dir, image=image, session_id="sess-liveness"))

    def test_live_daemon_still_connecting_at_timeout_is_an_indeterminate_timeout(self, monkeypatch, runtime_dir, image):
        self._attach(monkeypatch, runtime_dir, image, polled_state={"session_id": "sess-liveness", "state": "connecting", "pid": 4242}, pid_alive=True)
        with pytest.raises(AnsibleFailJson) as excinfo:
            asmb8_media.main()
        result = excinfo.value.kwargs
        assert result["error_class"] == "timeout"
        assert result["indeterminate"] is True
        assert result.get("changed") is not True
        assert "operation" not in result

    def test_the_message_says_re_probe_not_retry_and_names_the_session(self, monkeypatch, runtime_dir, image):
        self._attach(monkeypatch, runtime_dir, image, polled_state={"session_id": "sess-liveness", "state": "connecting", "pid": 4242}, pid_alive=True)
        with pytest.raises(AnsibleFailJson) as excinfo:
            asmb8_media.main()
        msg = excinfo.value.kwargs["msg"].lower()
        assert "re-probe" in msg
        assert "sess-liveness" in excinfo.value.kwargs["msg"]
        assert "not detached" in msg or "was not detached" in msg

    def test_the_unconfirmed_session_is_not_torn_down(self, monkeypatch, runtime_dir, image):
        self._attach(monkeypatch, runtime_dir, image, polled_state={"session_id": "sess-liveness", "state": "connecting", "pid": 4242}, pid_alive=True)
        with patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.request_stop") as request_stop:
            with pytest.raises(AnsibleFailJson):
                asmb8_media.main()
        request_stop.assert_not_called()

    def test_a_dead_daemon_with_no_verdict_is_a_settled_protocol_failure_not_indeterminate(self, monkeypatch, runtime_dir, image):
        self._attach(monkeypatch, runtime_dir, image, polled_state={"session_id": "sess-liveness", "state": "connecting", "pid": 4242}, pid_alive=False)
        with pytest.raises(AnsibleFailJson) as excinfo:
            asmb8_media.main()
        result = excinfo.value.kwargs
        assert result["error_class"] == "protocol"
        assert result.get("indeterminate") is not True

    def test_an_attached_session_is_still_a_success(self, monkeypatch, runtime_dir, image):
        self._attach(
            monkeypatch,
            runtime_dir,
            image,
            polled_state={"session_id": "sess-liveness", "state": "attached", "pid": 4242, "bytes_read": 0, "error": None},
            pid_alive=True,
        )
        with pytest.raises(AnsibleExitJson) as excinfo:
            asmb8_media.main()
        result = excinfo.value.kwargs
        assert result["changed"] is True
        assert result["session_state"] == "attached"
        assert result["operation"]["error_class"] is None


class TestCredentialAndTokenSafety:
    """Never let a token or password reach a message, receipt, or state file."""

    def test_password_never_appears_in_a_successful_result(self, runtime_dir, image):
        _set_module_args(_attach_args(runtime_dir=runtime_dir, image=image))
        with (
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.spawn_session", return_value=1),
            patch(
                "ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.wait_for_state",
                return_value={"state": "attached", "pid": 1, "bytes_read": 0},
            ),
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_media.main()
        assert PASSWORD not in json.dumps(excinfo.value.kwargs)

    def test_module_never_reintroduces_the_password_into_a_daemon_error_result(self, runtime_dir, image):
        # media_session._fail() is what actually redacts the daemon's own error text
        # before it ever reaches disk (see test_media_session.py's
        # test_an_unlabelled_password_embedded_in_a_raw_exception_is_still_scrubbed
        # for that backstop exercised end-to-end); what this test pins is a narrower
        # but still load-bearing property at THIS module's own boundary: nothing in
        # asmb8_media.py's own message-building ever interpolates params["password"]
        # into a msg=/fail_json() call, so even a daemon-reported state that is
        # already clean stays clean once this module has formatted its own message
        # around it.
        _set_module_args(_attach_args(runtime_dir=runtime_dir, image=image))
        error_state = {
            "state": "error",
            "error": "the BMC rejected the media session (see log for detail)",
            "error_class": "authentication",
            "pid": 1,
            "bytes_read": 0,
        }
        with (
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.spawn_session", return_value=1),
            patch("ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.media_session.wait_for_state", return_value=error_state),
        ):
            with pytest.raises(AnsibleFailJson) as excinfo:
                asmb8_media.main()
        assert PASSWORD not in json.dumps(excinfo.value.kwargs)
        assert excinfo.value.kwargs["error_class"] == "authentication"

    def test_password_argument_spec_entry_is_no_log(self):
        spec = asmb8_media._argument_spec()
        assert spec["password"]["no_log"] is True

    def test_no_option_carries_a_raw_media_token(self):
        # There is no "token" option at all: the module never accepts or exposes one,
        # since the token is minted internally by the daemon and never leaves it.
        assert "token" not in asmb8_media._argument_spec()
        assert "kvm_token" not in asmb8_media._argument_spec()


class TestBuildSessionConfig:
    def test_carries_the_documented_fields_and_no_credential(self, image):
        params = dict(
            BASE_ARGS,
            image=image,
            port=443,
            validate_certs=True,
            ca_path=None,
            tls_fingerprint=None,
            timeout=30,
            connect_timeout=10,
            cd_port=5120,
            instance=0,
            runtime_dir="/tmp/x",  # noqa: S108 - not actually used; only shape is asserted.
        )
        config = asmb8_media.build_session_config(params, session_id="abc")
        assert config.session_id == "abc"
        assert config.image == image
        assert config.cd_port == 5120
        assert not hasattr(config, "password")
        assert not hasattr(config, "username")
        assert not hasattr(config, "token")
