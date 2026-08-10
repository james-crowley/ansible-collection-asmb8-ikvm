# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for the ``asmb8_http_origin`` module's decision logic.

``http_origin.spawn_session`` (the real fork -> bind -> serve path) is mocked
throughout here, the same way ``test_asmb8_media.py`` mocks
``media_session.spawn_session`` -- the real fork/bind/serve path, including
the hard lifetime cap and path confinement, is exercised for real (loopback
only, no BMC or lab host involved) in ``tests/unit/plugins/module_utils/
test_http_origin.py``. These tests are about this module's own logic:
idempotency for both O(state=started) and O(state=stopped), stale-session
recovery, option validation, check-mode, and error surfacing.
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

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import http_origin
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_http_origin

MOD_UTILS = "ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.http_origin"


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
def served_path(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    (served / "file.txt").write_bytes(b"hello")
    return str(served)


@pytest.fixture
def runtime_dir(tmp_path):
    return str(tmp_path / "runtime")


def _start_args(*, runtime_dir, path, **overrides) -> dict:
    args = {"state": "started", "path": path, "runtime_dir": runtime_dir}
    args.update(overrides)
    return args


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])  # fixed argv, no shell
    proc.wait()
    return proc.pid


class TestStartFreshSession:
    def test_spawns_and_reports_changed_true(self, runtime_dir, served_path):
        _set_module_args(_start_args(runtime_dir=runtime_dir, path=served_path))
        serving_state = {
            "session_id": "will-be-overwritten",
            "pid": 4242,
            "state": "serving",
            "error": None,
            "url": "http://127.0.0.1:8080/",
            "port": 8080,
            "root": served_path,
            "request_count": 0,
            "bytes_served": 0,
        }
        with (
            patch(f"{MOD_UTILS}.spawn_session", return_value=4242) as spawn,
            patch(f"{MOD_UTILS}.wait_for_state", return_value=serving_state),
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_http_origin.main()
        result = excinfo.value.kwargs
        assert result["changed"] is True
        assert result["session_state"] == "serving"
        assert result["pid"] == 4242
        assert result["url"] == "http://127.0.0.1:8080/"
        assert result["port"] == 8080
        assert result.get("session_id")
        spawn.assert_called_once()
        config = spawn.call_args.args[0]
        assert config.bind_address == http_origin.DEFAULT_BIND_ADDRESS
        assert config.lifetime_seconds == http_origin.DEFAULT_LIFETIME_SECONDS

    def test_receipt_is_nested_under_operation_not_spread_at_top_level(self, runtime_dir, served_path):
        _set_module_args(_start_args(runtime_dir=runtime_dir, path=served_path))
        serving_state = {"session_id": "x", "pid": 4242, "state": "serving", "error": None, "url": "http://127.0.0.1:1/", "port": 1, "root": served_path}
        with (
            patch(f"{MOD_UTILS}.spawn_session", return_value=4242),
            patch(f"{MOD_UTILS}.wait_for_state", return_value=serving_state),
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_http_origin.main()
        result = excinfo.value.kwargs
        for moved_field in ("schema", "action", "endpoint", "previous", "desired", "observed"):
            assert moved_field not in result, f"{moved_field!r} must not be spread at the top level; it belongs under operation"
        assert result["operation"]["schema"] == "asmb8-ikvm-operation/v1"
        assert result["operation"]["action"] == "asmb8_http_origin.start"
        assert result["operation"]["error_class"] is None

    def test_generated_session_id_is_returned_for_a_later_stop(self, runtime_dir, served_path):
        _set_module_args(_start_args(runtime_dir=runtime_dir, path=served_path))
        with (
            patch(f"{MOD_UTILS}.spawn_session", return_value=1),
            patch(f"{MOD_UTILS}.wait_for_state", return_value={"state": "serving", "pid": 1, "port": 1, "url": "http://x/"}),
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_http_origin.main()
        session_id = excinfo.value.kwargs["session_id"]
        assert isinstance(session_id, str) and len(session_id) == 32

    def test_daemon_reporting_error_fails_the_module_with_its_own_error_class(self, runtime_dir, served_path):
        _set_module_args(_start_args(runtime_dir=runtime_dir, path=served_path))
        error_state = {"state": "error", "error": "failed to bind 127.0.0.1:80: [Errno 13] Permission denied", "error_class": "connection", "pid": 99}
        with (
            patch(f"{MOD_UTILS}.spawn_session", return_value=99),
            patch(f"{MOD_UTILS}.wait_for_state", return_value=error_state),
        ):
            with pytest.raises(AnsibleFailJson) as excinfo:
                asmb8_http_origin.main()
        assert excinfo.value.kwargs["error_class"] == "connection"
        assert "Permission denied" in excinfo.value.kwargs["msg"]

    def test_daemon_reporting_an_unclassified_error_falls_back_to_protocol(self, runtime_dir, served_path):
        _set_module_args(_start_args(runtime_dir=runtime_dir, path=served_path))
        error_state = {"state": "error", "error": "something odd", "pid": 99}  # no error_class key at all
        with (
            patch(f"{MOD_UTILS}.spawn_session", return_value=99),
            patch(f"{MOD_UTILS}.wait_for_state", return_value=error_state),
        ):
            with pytest.raises(AnsibleFailJson) as excinfo:
                asmb8_http_origin.main()
        assert excinfo.value.kwargs["error_class"] == "protocol"

    def test_custom_bind_address_and_lifetime_reach_the_session_config(self, runtime_dir, served_path):
        _set_module_args(_start_args(runtime_dir=runtime_dir, path=served_path, bind_address="192.0.2.5", lifetime_seconds=60, port=9000))
        with (
            patch(f"{MOD_UTILS}.spawn_session", return_value=1) as spawn,
            patch(f"{MOD_UTILS}.wait_for_state", return_value={"state": "serving", "pid": 1, "port": 9000, "url": "http://192.0.2.5:9000/"}),
        ):
            with pytest.raises(AnsibleExitJson):
                asmb8_http_origin.main()
        config = spawn.call_args.args[0]
        assert config.bind_address == "192.0.2.5"
        assert config.lifetime_seconds == 60
        assert config.port == 9000


class TestStartIdempotency:
    def test_existing_live_session_is_not_respawned(self, runtime_dir, served_path):
        session_id = "existing-session"
        http_origin._write_state_atomic(
            runtime_dir, session_id, {"session_id": session_id, "pid": os.getpid(), "state": "serving", "error": None, "port": 1, "url": "http://x/"}
        )
        _set_module_args(_start_args(runtime_dir=runtime_dir, path=served_path, session_id=session_id))
        with patch(f"{MOD_UTILS}.spawn_session") as spawn:
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_http_origin.main()
        result = excinfo.value.kwargs
        assert result["changed"] is False
        assert result["session_id"] == session_id
        spawn.assert_not_called()

    def test_stale_session_file_is_recovered_and_a_fresh_one_spawned(self, runtime_dir, served_path):
        session_id = "stale-session"
        dead_pid = _dead_pid()
        http_origin._write_state_atomic(
            runtime_dir, session_id, {"session_id": session_id, "pid": dead_pid, "state": "serving", "error": None, "port": 1, "url": "http://x/"}
        )
        _set_module_args(_start_args(runtime_dir=runtime_dir, path=served_path, session_id=session_id))
        with (
            patch(f"{MOD_UTILS}.spawn_session", return_value=4242) as spawn,
            patch(f"{MOD_UTILS}.wait_for_state", return_value={"state": "serving", "pid": 4242, "port": 1, "url": "http://x/"}),
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_http_origin.main()
        result = excinfo.value.kwargs
        assert result["changed"] is True
        assert result["recovered_stale_session"] is True
        spawn.assert_called_once()

    def test_a_terminal_but_still_present_state_file_is_not_treated_as_live(self, runtime_dir, served_path):
        # A state file whose last report is 'stopped' or 'error' must never be mistaken
        # for a live session merely because its pid happens to still exist (e.g. pid
        # reuse, or the daemon exited but the record was not yet cleaned up).
        session_id = "terminal-session"
        http_origin._write_state_atomic(
            runtime_dir, session_id, {"session_id": session_id, "pid": os.getpid(), "state": "stopped", "error": None, "stop_reason": "signal"}
        )
        _set_module_args(_start_args(runtime_dir=runtime_dir, path=served_path, session_id=session_id))
        with (
            patch(f"{MOD_UTILS}.spawn_session", return_value=4242) as spawn,
            patch(f"{MOD_UTILS}.wait_for_state", return_value={"state": "serving", "pid": 4242, "port": 1, "url": "http://x/"}),
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_http_origin.main()
        assert excinfo.value.kwargs["changed"] is True
        spawn.assert_called_once()


class TestStartCheckMode:
    def test_never_spawns_but_still_validates_the_path(self, runtime_dir, served_path):
        args = dict(_start_args(runtime_dir=runtime_dir, path=served_path), _ansible_check_mode=True)
        _set_module_args(args)
        with patch(f"{MOD_UTILS}.spawn_session") as spawn:
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_http_origin.main()
        assert excinfo.value.kwargs["changed"] is True
        assert excinfo.value.kwargs["port"] is None
        assert excinfo.value.kwargs["url"] is None
        spawn.assert_not_called()

    def test_bad_path_still_fails_in_check_mode(self, runtime_dir, tmp_path):
        args = dict(_start_args(runtime_dir=runtime_dir, path=str(tmp_path / "does-not-exist")), _ansible_check_mode=True)
        _set_module_args(args)
        with patch(f"{MOD_UTILS}.spawn_session") as spawn:
            with pytest.raises(AnsibleFailJson) as excinfo:
                asmb8_http_origin.main()
        assert excinfo.value.kwargs["error_class"] == "protocol"
        spawn.assert_not_called()

    def test_a_file_not_a_directory_fails_in_check_mode(self, runtime_dir, tmp_path):
        path = tmp_path / "afile.txt"
        path.write_text("x")
        args = dict(_start_args(runtime_dir=runtime_dir, path=str(path)), _ansible_check_mode=True)
        _set_module_args(args)
        with pytest.raises(AnsibleFailJson) as excinfo:
            asmb8_http_origin.main()
        assert excinfo.value.kwargs["error_class"] == "protocol"

    def test_check_mode_never_forks_even_on_a_platform_without_fork(self, runtime_dir, served_path, monkeypatch):
        monkeypatch.delattr(http_origin.os, "fork", raising=False)
        args = dict(_start_args(runtime_dir=runtime_dir, path=served_path), _ansible_check_mode=True)
        _set_module_args(args)
        with pytest.raises(AnsibleExitJson) as excinfo:
            asmb8_http_origin.main()
        assert excinfo.value.kwargs["changed"] is True

    def test_check_mode_on_an_existing_live_session_reports_no_change(self, runtime_dir, served_path):
        session_id = "existing-session"
        http_origin._write_state_atomic(
            runtime_dir, session_id, {"session_id": session_id, "pid": os.getpid(), "state": "serving", "error": None, "port": 1, "url": "http://x/"}
        )
        args = dict(_start_args(runtime_dir=runtime_dir, path=served_path, session_id=session_id), _ansible_check_mode=True)
        _set_module_args(args)
        with patch(f"{MOD_UTILS}.spawn_session") as spawn:
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_http_origin.main()
        assert excinfo.value.kwargs["changed"] is False
        spawn.assert_not_called()


class TestOptionValidation:
    def test_missing_path_is_rejected_by_the_argument_spec(self, runtime_dir):
        args = {"state": "started", "runtime_dir": runtime_dir}
        _set_module_args(args)
        with pytest.raises((AnsibleFailJson, SystemExit)):
            asmb8_http_origin.main()

    def test_stopped_without_session_id_is_rejected_by_the_argument_spec(self, runtime_dir):
        args = {"state": "stopped", "runtime_dir": runtime_dir}
        _set_module_args(args)
        with pytest.raises((AnsibleFailJson, SystemExit)):
            asmb8_http_origin.main()

    def test_invalid_state_choice_is_rejected_by_the_argument_spec(self, runtime_dir, served_path):
        args = {"state": "attached", "path": served_path, "runtime_dir": runtime_dir}
        _set_module_args(args)
        with pytest.raises((AnsibleFailJson, SystemExit)):
            asmb8_http_origin.main()

    def test_default_bind_address_is_loopback_not_every_interface(self):
        spec = asmb8_http_origin._argument_spec()
        assert spec["bind_address"]["default"] == "127.0.0.1"

    def test_default_port_is_zero_meaning_pick_a_free_one(self):
        spec = asmb8_http_origin._argument_spec()
        assert spec["port"]["default"] == 0

    def test_there_is_no_way_to_disable_the_lifetime_cap(self):
        spec = asmb8_http_origin._argument_spec()
        assert spec["lifetime_seconds"]["type"] == "int"
        assert isinstance(spec["lifetime_seconds"]["default"], int)
        assert spec["lifetime_seconds"]["default"] > 0


class TestStop:
    def test_no_existing_session_is_a_no_op(self, runtime_dir):
        args = {"state": "stopped", "session_id": "never-existed", "runtime_dir": runtime_dir}
        _set_module_args(args)
        with pytest.raises(AnsibleExitJson) as excinfo:
            asmb8_http_origin.main()
        result = excinfo.value.kwargs
        assert result["changed"] is False
        assert result["session_state"] == "unknown"

    def test_live_session_is_stopped_and_state_file_removed(self, runtime_dir):
        session_id = "live-session"
        http_origin._write_state_atomic(
            runtime_dir, session_id, {"session_id": session_id, "pid": 4242, "state": "serving", "error": None, "bind_address": "127.0.0.1", "port": 1}
        )
        args = {"state": "stopped", "session_id": session_id, "runtime_dir": runtime_dir}
        _set_module_args(args)
        with (
            patch(f"{MOD_UTILS}.is_pid_alive", return_value=True),
            patch(f"{MOD_UTILS}.request_stop") as request_stop,
            patch(f"{MOD_UTILS}.wait_for_exit", return_value=True),
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_http_origin.main()
        result = excinfo.value.kwargs
        assert result["changed"] is True
        request_stop.assert_called_once_with(4242)
        assert http_origin.read_state(runtime_dir, session_id) is None

    def test_stopping_an_already_stopped_origin_is_changed_false_not_an_error(self, runtime_dir):
        # The idempotency property the task calls out explicitly: two real,
        # sequential state=stopped calls for the same session_id -- the first
        # actually stops a live one, the second finds nothing left to stop.
        session_id = "twice-stopped"
        http_origin._write_state_atomic(
            runtime_dir, session_id, {"session_id": session_id, "pid": 4242, "state": "serving", "error": None, "bind_address": "127.0.0.1", "port": 1}
        )
        args = {"state": "stopped", "session_id": session_id, "runtime_dir": runtime_dir}
        _set_module_args(args)
        with (
            patch(f"{MOD_UTILS}.is_pid_alive", return_value=True),
            patch(f"{MOD_UTILS}.request_stop"),
            patch(f"{MOD_UTILS}.wait_for_exit", return_value=True),
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_http_origin.main()
        assert excinfo.value.kwargs["changed"] is True
        assert http_origin.read_state(runtime_dir, session_id) is None

        # Second call: no state file at all now -- must be changed=false, not a failure.
        _set_module_args(args)
        with pytest.raises(AnsibleExitJson) as excinfo:
            asmb8_http_origin.main()
        result = excinfo.value.kwargs
        assert result["changed"] is False
        assert result["session_state"] == "unknown"

    def test_stale_session_reports_no_change_but_cleans_up(self, runtime_dir):
        session_id = "stale-session"
        dead_pid = _dead_pid()
        http_origin._write_state_atomic(
            runtime_dir, session_id, {"session_id": session_id, "pid": dead_pid, "state": "serving", "error": None, "bind_address": "127.0.0.1", "port": 1}
        )
        args = {"state": "stopped", "session_id": session_id, "runtime_dir": runtime_dir}
        _set_module_args(args)
        with pytest.raises(AnsibleExitJson) as excinfo:
            asmb8_http_origin.main()
        result = excinfo.value.kwargs
        assert result["changed"] is False
        assert result["recovered_stale_session"] is True
        assert http_origin.read_state(runtime_dir, session_id) is None

    def test_check_mode_never_signals_a_live_session(self, runtime_dir):
        session_id = "live-session"
        http_origin._write_state_atomic(
            runtime_dir, session_id, {"session_id": session_id, "pid": 4242, "state": "serving", "error": None, "bind_address": "127.0.0.1", "port": 1}
        )
        args = {"state": "stopped", "session_id": session_id, "runtime_dir": runtime_dir, "_ansible_check_mode": True}
        _set_module_args(args)
        with (
            patch(f"{MOD_UTILS}.is_pid_alive", return_value=True),
            patch(f"{MOD_UTILS}.request_stop") as request_stop,
        ):
            with pytest.raises(AnsibleExitJson) as excinfo:
                asmb8_http_origin.main()
        assert excinfo.value.kwargs["changed"] is True
        request_stop.assert_not_called()
        assert http_origin.read_state(runtime_dir, session_id) is not None

    def test_check_mode_stopping_an_already_stopped_origin_reports_no_change(self, runtime_dir):
        args = {"state": "stopped", "session_id": "never-existed", "runtime_dir": runtime_dir, "_ansible_check_mode": True}
        _set_module_args(args)
        with pytest.raises(AnsibleExitJson) as excinfo:
            asmb8_http_origin.main()
        assert excinfo.value.kwargs["changed"] is False


class TestIndeterminateStartTimeout:
    """A daemon still alive but unconfirmed at start_timeout must not be reported as a
    success, must be classified as an indeterminate timeout, and must not be torn down.
    """

    def _start(self, monkeypatch, runtime_dir, served_path, *, polled_state, pid_alive):
        monkeypatch.setattr(http_origin, "spawn_session", lambda *a, **k: 4242)
        monkeypatch.setattr(http_origin, "wait_for_state", lambda *a, **k: polled_state)
        monkeypatch.setattr(http_origin, "is_pid_alive", lambda pid: pid_alive)
        _set_module_args(_start_args(runtime_dir=runtime_dir, path=served_path, session_id="sess-liveness"))

    def test_live_daemon_still_starting_at_timeout_is_an_indeterminate_timeout(self, monkeypatch, runtime_dir, served_path):
        self._start(monkeypatch, runtime_dir, served_path, polled_state={"session_id": "sess-liveness", "state": "starting", "pid": 4242}, pid_alive=True)
        with pytest.raises(AnsibleFailJson) as excinfo:
            asmb8_http_origin.main()
        result = excinfo.value.kwargs
        assert result["error_class"] == "timeout"
        assert result["indeterminate"] is True
        assert result.get("changed") is not True
        assert "operation" not in result

    def test_the_message_says_re_probe_not_retry_and_names_the_session(self, monkeypatch, runtime_dir, served_path):
        self._start(monkeypatch, runtime_dir, served_path, polled_state={"session_id": "sess-liveness", "state": "starting", "pid": 4242}, pid_alive=True)
        with pytest.raises(AnsibleFailJson) as excinfo:
            asmb8_http_origin.main()
        msg = excinfo.value.kwargs["msg"].lower()
        assert "re-probe" in msg
        assert "sess-liveness" in excinfo.value.kwargs["msg"]

    def test_the_unconfirmed_session_is_not_torn_down(self, monkeypatch, runtime_dir, served_path):
        self._start(monkeypatch, runtime_dir, served_path, polled_state={"session_id": "sess-liveness", "state": "starting", "pid": 4242}, pid_alive=True)
        with patch(f"{MOD_UTILS}.request_stop") as request_stop:
            with pytest.raises(AnsibleFailJson):
                asmb8_http_origin.main()
        request_stop.assert_not_called()

    def test_a_dead_daemon_with_no_verdict_is_a_settled_protocol_failure_not_indeterminate(self, monkeypatch, runtime_dir, served_path):
        self._start(monkeypatch, runtime_dir, served_path, polled_state={"session_id": "sess-liveness", "state": "starting", "pid": 4242}, pid_alive=False)
        with pytest.raises(AnsibleFailJson) as excinfo:
            asmb8_http_origin.main()
        result = excinfo.value.kwargs
        assert result["error_class"] == "protocol"
        assert result.get("indeterminate") is not True

    def test_a_serving_session_is_still_a_success(self, monkeypatch, runtime_dir, served_path):
        self._start(
            monkeypatch,
            runtime_dir,
            served_path,
            polled_state={"session_id": "sess-liveness", "state": "serving", "pid": 4242, "port": 1, "url": "http://x/", "error": None},
            pid_alive=True,
        )
        with pytest.raises(AnsibleExitJson) as excinfo:
            asmb8_http_origin.main()
        result = excinfo.value.kwargs
        assert result["changed"] is True
        assert result["session_state"] == "serving"
        assert result["operation"]["error_class"] is None


class TestBuildSessionConfig:
    def test_carries_the_documented_fields(self, served_path):
        params = {"bind_address": "127.0.0.1", "port": 8080, "lifetime_seconds": 3600, "runtime_dir": "/tmp/x"}  # noqa: S108 - shape only, never used.
        config = asmb8_http_origin.build_session_config(params, session_id="abc", root=served_path)
        assert config.session_id == "abc"
        assert config.root == served_path
        assert config.bind_address == "127.0.0.1"
        assert config.port == 8080
        assert config.lifetime_seconds == 3600
