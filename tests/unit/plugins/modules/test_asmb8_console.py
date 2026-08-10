# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_console.

Every test here replaces the network-facing seams (`build_asp_client`,
`resolve_local_ip`, `build_kvm_transport`, `ivtp.open_channel`,
`ivtp.capture_one_frame`) with fakes -- nothing here can reach a socket, let
alone any real BMC.

This module carries the IVTP console/KVM-session implementation that used to
live in `asmb8_redirection` before that module was rewritten to report
service enablement instead -- see `test_asmb8_redirection.py` for that
module's own (new) tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import ivtp
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import (
    AuthenticationError,
    ProtocolError,
)
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_console

#: `tests/integration/mock_servers` is not part of the `ansible_collections`
#: namespace (never shipped in the built collection artifact), so it needs
#: its own directory on `sys.path` -- mirrors
#: `test_asmb8_power_lifecycle.py`'s own identical arrangement.
_MOCK_SERVERS_DIR = str(Path(__file__).resolve().parents[3] / "integration" / "mock_servers")


@pytest.fixture(autouse=True)
def _mock_servers_importable():
    if _MOCK_SERVERS_DIR not in sys.path:
        sys.path.insert(0, _MOCK_SERVERS_DIR)


PASSWORD = "Sup3rSecretPassw0rd!"
TOKEN = "SuperSecretKvmTok"

BASE_ARGS = {
    "host": "10.0.0.5",
    "username": "admin",
    "password": PASSWORD,
}


def _set_module_args(args: dict) -> None:
    basic._ANSIBLE_ARGS = to_bytes(json.dumps({"ANSIBLE_MODULE_ARGS": args}))
    basic._ANSIBLE_PROFILE = "legacy"


class AnsibleExitJson(Exception):
    pass


class AnsibleFailJson(Exception):
    pass


def _exit_json(*_args, **kwargs):
    raise AnsibleExitJson(kwargs)


def _fail_json(*_args, **kwargs):
    raise AnsibleFailJson(kwargs)


@pytest.fixture(autouse=True)
def _patch_module_exit(monkeypatch):
    monkeypatch.setattr(basic.AnsibleModule, "exit_json", _exit_json)
    monkeypatch.setattr(basic.AnsibleModule, "fail_json", _fail_json)


def _run_ok(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleExitJson) as excinfo:
        asmb8_console.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_console.main()
    return excinfo.value.args[0]


def _contains_secret(value: object, secret: str) -> bool:
    """Recursively search a JSON-shaped structure for ``secret`` as a substring anywhere."""
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(_contains_secret(k, secret) or _contains_secret(v, secret) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item, secret) for item in value)
    return False


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self.timeouts: list[float | None] = []

    def send_all(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def set_timeout(self, seconds: float | None) -> None:
        self.timeouts.append(seconds)

    def close(self) -> None:
        self.closed = True

    def recv_exact(self, n: int) -> bytes:  # pragma: no cover - not exercised once open_channel/capture_one_frame are mocked.
        raise AssertionError("recv_exact should never be called: ivtp.open_channel/capture_one_frame are mocked in these tests")


def _wire_no_network(monkeypatch, *, transport: _FakeTransport | None = None, token: str = TOKEN, kvm_secure: bool = False, client_ip: str = "10.1.1.1"):
    transport = transport or _FakeTransport()
    monkeypatch.setattr(asmb8_console, "resolve_token_and_security", lambda params: (token, kvm_secure, client_ip))
    monkeypatch.setattr(asmb8_console, "build_kvm_transport", lambda params, *, kvm_secure: transport)
    return transport


CANNED_FACTS = ivtp.ChannelFacts(
    session_accepted=True,
    greeting_body_len=0,
    validate_status=ivtp.SESSION_VALID,
    validate_status_name="valid_session",
    validate_sub_status=None,
    resumed=True,
)


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_console.argument_spec()["password"]["no_log"] is True

    def test_token_is_no_log(self):
        assert asmb8_console.argument_spec()["token"]["no_log"] is True

    def test_capture_choices_and_default(self):
        spec = asmb8_console.argument_spec()["capture"]
        assert set(spec["choices"]) == {"handshake_only", "raw_frame", "decoded_frame"}
        assert spec["default"] == "handshake_only"

    def test_kvm_port_defaults_to_7578(self):
        assert asmb8_console.argument_spec()["kvm_port"]["default"] == 7578

    def test_kvm_secure_has_no_forced_default(self):
        # No "default" key at all: None until the caller (or the JNLP fetch) sets it, per the
        # module description's note that TLS is governed by this flag explicitly, never inferred.
        assert "default" not in asmb8_console.argument_spec()["kvm_secure"]

    def test_send_get_web_token_defaults_true(self):
        assert asmb8_console.argument_spec()["send_get_web_token"]["default"] is True

    def test_handshake_and_frame_timeouts_have_sane_defaults(self):
        spec = asmb8_console.argument_spec()
        assert spec["handshake_timeout"]["default"] == 15
        assert spec["frame_timeout"]["default"] == 20


class TestDecodedFrameAlwaysFailsHonestly:
    def test_decoded_frame_fails_with_unsupported_capability(self, monkeypatch):
        transport = _wire_no_network(monkeypatch)
        result = _run_fail(dict(BASE_ARGS, capture="decoded_frame"))
        assert result["error_class"] == "unsupported_capability"
        assert transport.sent == []  # nothing was ever opened.

    def test_decoded_frame_fails_even_in_check_mode(self, monkeypatch):
        _wire_no_network(monkeypatch)
        result = _run_fail(dict(BASE_ARGS, capture="decoded_frame", _ansible_check_mode=True))
        assert result["error_class"] == "unsupported_capability"

    def test_decoded_frame_never_calls_resolve_token_and_security(self, monkeypatch):
        spy = Mock(side_effect=AssertionError("must not be called for capture=decoded_frame"))
        monkeypatch.setattr(asmb8_console, "resolve_token_and_security", spy)
        _run_fail(dict(BASE_ARGS, capture="decoded_frame"))
        spy.assert_not_called()

    def test_decoded_frame_does_not_fabricate_a_placeholder_frame(self, monkeypatch):
        _wire_no_network(monkeypatch)
        result = _run_fail(dict(BASE_ARGS, capture="decoded_frame"))
        assert "frame" not in result or result.get("frame") is None

    def test_decoded_frame_never_reaches_a_real_listening_kvm_port(self):
        """Stronger version of the seam-mocked tests above: point O(kvm_port)
        at a REAL, live ``IvtpMockServer`` on loopback -- reachable, correctly
        speaking the handshake, and ready to accept -- rather than a
        monkeypatched seam, and confirm the connection attempt this module
        would otherwise make simply never happens. Nothing here is monkey-
        patched: if C(capture=decoded_frame)'s early-return in C(main())
        were ever accidentally moved after C(resolve_token_and_security())
        or C(build_kvm_transport())'s call sites, this test -- unlike the
        seam-mocked ones above -- would actually observe the resulting
        connection and fail.
        """
        from ivtp_server import IvtpMockServer

        with IvtpMockServer() as server:
            result = _run_fail(dict(BASE_ARGS, host="127.0.0.1", kvm_port=server.port, capture="decoded_frame"))
            assert result["error_class"] == "unsupported_capability"
            with pytest.raises(TimeoutError):
                server.wait_for_connection(timeout=0.2)


class TestCheckMode:
    def test_handshake_only_check_mode_touches_no_network(self, monkeypatch):
        spy = Mock(side_effect=AssertionError("check_mode must never resolve a token or open a connection"))
        monkeypatch.setattr(asmb8_console, "resolve_token_and_security", spy)
        monkeypatch.setattr(asmb8_console, "build_kvm_transport", spy)
        result = _run_ok(dict(BASE_ARGS, _ansible_check_mode=True))
        assert result["changed"] is False
        assert result["channel"] is None
        assert result["frame"] is None
        assert result["operation"]["observed"] is None
        spy.assert_not_called()

    def test_raw_frame_check_mode_requires_output_path(self, monkeypatch):
        _wire_no_network(monkeypatch)
        result = _run_fail(dict(BASE_ARGS, capture="raw_frame", _ansible_check_mode=True))
        assert "output_path" in result["msg"]

    def test_raw_frame_check_mode_validates_output_path_parent_directory(self, monkeypatch, tmp_path):
        _wire_no_network(monkeypatch)
        missing_parent = tmp_path / "does-not-exist" / "frame.raw"
        result = _run_fail(dict(BASE_ARGS, capture="raw_frame", output_path=str(missing_parent), _ansible_check_mode=True))
        assert result["error_class"] == "protocol"

    def test_raw_frame_check_mode_with_valid_output_path_succeeds_without_network(self, monkeypatch, tmp_path):
        spy = Mock(side_effect=AssertionError("check_mode must never touch the network"))
        monkeypatch.setattr(asmb8_console, "resolve_token_and_security", spy)
        output_path = tmp_path / "frame.raw"
        result = _run_ok(dict(BASE_ARGS, capture="raw_frame", output_path=str(output_path), _ansible_check_mode=True))
        assert result["changed"] is False
        assert not output_path.exists()
        spy.assert_not_called()

    def test_output_path_required_if_missing_for_raw_frame_even_outside_check_mode(self, monkeypatch):
        _wire_no_network(monkeypatch)
        result = _run_fail(dict(BASE_ARGS, capture="raw_frame"))
        assert "output_path" in result["msg"]

    def test_token_requires_kvm_secure_via_required_by(self, monkeypatch):
        _wire_no_network(monkeypatch)
        result = _run_fail(dict(BASE_ARGS, token=TOKEN))
        assert "kvm_secure" in result["msg"]


class TestHandshakeOnlyCapture:
    def test_returns_channel_facts_and_never_writes_a_frame(self, monkeypatch):
        _wire_no_network(monkeypatch)
        monkeypatch.setattr(ivtp, "open_channel", Mock(return_value=CANNED_FACTS))
        result = _run_ok(dict(BASE_ARGS))

        assert result["changed"] is False
        assert result["capture"] == "handshake_only"
        assert result["channel"]["validate_status"] == ivtp.SESSION_VALID
        assert result["channel"]["resumed"] is True
        assert result["frame"] is None
        assert result["operation"]["schema"] == "asmb8-ikvm-operation/v1"
        assert result["operation"]["action"] == "asmb8_console.capture"
        assert result["operation"]["changed"] is False
        assert result["operation"]["error_class"] is None

    def test_best_effort_closes_and_stops_the_session(self, monkeypatch):
        transport = _wire_no_network(monkeypatch)
        monkeypatch.setattr(ivtp, "open_channel", Mock(return_value=CANNED_FACTS))
        _run_ok(dict(BASE_ARGS))
        assert transport.closed is True
        assert transport.sent == [ivtp.build_stop_session()]

    def test_cleanup_happens_even_when_open_channel_fails(self, monkeypatch):
        transport = _wire_no_network(monkeypatch)
        monkeypatch.setattr(ivtp, "open_channel", Mock(side_effect=AuthenticationError("rejected", operation="test")))
        result = _run_fail(dict(BASE_ARGS))
        assert result["error_class"] == "authentication"
        assert transport.closed is True

    def test_endpoint_uses_kvm_port_not_the_web_port(self, monkeypatch):
        _wire_no_network(monkeypatch)
        monkeypatch.setattr(ivtp, "open_channel", Mock(return_value=CANNED_FACTS))
        result = _run_ok(dict(BASE_ARGS, kvm_port=9999))
        assert result["operation"]["endpoint"] == "10.0.0.5:9999"

    def test_client_username_default_is_passed_to_open_channel(self, monkeypatch):
        _wire_no_network(monkeypatch)
        spy = Mock(return_value=CANNED_FACTS)
        monkeypatch.setattr(ivtp, "open_channel", spy)
        monkeypatch.setattr(asmb8_console, "default_client_username", lambda: "detected-os-user")
        _run_ok(dict(BASE_ARGS))
        assert spy.call_args.kwargs["username"] == "detected-os-user"

    def test_client_username_override_is_used_verbatim(self, monkeypatch):
        _wire_no_network(monkeypatch)
        spy = Mock(return_value=CANNED_FACTS)
        monkeypatch.setattr(ivtp, "open_channel", spy)
        _run_ok(dict(BASE_ARGS, client_username="custom-user"))
        assert spy.call_args.kwargs["username"] == "custom-user"

    def test_send_get_web_token_option_is_threaded_through(self, monkeypatch):
        _wire_no_network(monkeypatch)
        spy = Mock(return_value=CANNED_FACTS)
        monkeypatch.setattr(ivtp, "open_channel", spy)
        _run_ok(dict(BASE_ARGS, send_get_web_token=False))
        assert spy.call_args.kwargs["send_get_web_token"] is False


class TestRawFrameCapture:
    def test_writes_raw_undecoded_bytes_to_output_path(self, monkeypatch, tmp_path):
        _wire_no_network(monkeypatch)
        monkeypatch.setattr(ivtp, "open_channel", Mock(return_value=CANNED_FACTS))
        monkeypatch.setattr(ivtp, "capture_one_frame", Mock(return_value=b"raw-encoded-bytes"))
        output_path = tmp_path / "frame.raw"

        result = _run_ok(dict(BASE_ARGS, capture="raw_frame", output_path=str(output_path)))

        assert result["frame"]["decoded"] is False
        assert result["frame"]["bytes_written"] == len(b"raw-encoded-bytes")
        assert result["frame"]["output_path"] == str(output_path)
        assert output_path.read_bytes() == b"raw-encoded-bytes"

    def test_output_file_is_mode_0600(self, monkeypatch, tmp_path):
        _wire_no_network(monkeypatch)
        monkeypatch.setattr(ivtp, "open_channel", Mock(return_value=CANNED_FACTS))
        monkeypatch.setattr(ivtp, "capture_one_frame", Mock(return_value=b"data"))
        output_path = tmp_path / "frame.raw"

        _run_ok(dict(BASE_ARGS, capture="raw_frame", output_path=str(output_path)))

        assert (output_path.stat().st_mode & 0o777) == 0o600

    def test_frame_timeout_is_threaded_through(self, monkeypatch, tmp_path):
        _wire_no_network(monkeypatch)
        monkeypatch.setattr(ivtp, "open_channel", Mock(return_value=CANNED_FACTS))
        spy = Mock(return_value=b"data")
        monkeypatch.setattr(ivtp, "capture_one_frame", spy)
        output_path = tmp_path / "frame.raw"

        _run_ok(dict(BASE_ARGS, capture="raw_frame", output_path=str(output_path), frame_timeout=42))

        assert spy.call_args.kwargs["frame_timeout"] == 42.0

    def test_capture_failure_does_not_leave_a_partial_file_from_a_prior_run(self, monkeypatch, tmp_path):
        _wire_no_network(monkeypatch)
        monkeypatch.setattr(ivtp, "open_channel", Mock(return_value=CANNED_FACTS))
        monkeypatch.setattr(ivtp, "capture_one_frame", Mock(side_effect=ProtocolError("frame too large", operation="test")))
        output_path = tmp_path / "frame.raw"

        result = _run_fail(dict(BASE_ARGS, capture="raw_frame", output_path=str(output_path)))

        assert result["error_class"] == "protocol"
        assert not output_path.exists()


class TestResolveTokenAndSecurity:
    def test_token_supplied_without_kvm_secure_raises_protocol_error(self, monkeypatch):
        monkeypatch.setattr(asmb8_console, "resolve_local_ip", lambda host: "10.1.1.1")
        with pytest.raises(ProtocolError):
            asmb8_console.resolve_token_and_security({"host": "10.0.0.5", "token": TOKEN, "kvm_secure": None})

    def test_token_supplied_with_kvm_secure_is_used_as_is(self, monkeypatch):
        monkeypatch.setattr(asmb8_console, "resolve_local_ip", lambda host: "10.1.1.1")
        token, kvm_secure, client_ip = asmb8_console.resolve_token_and_security({"host": "10.0.0.5", "token": TOKEN, "kvm_secure": True})
        assert token == TOKEN
        assert kvm_secure is True
        assert client_ip == "10.1.1.1"

    def test_no_token_mints_one_via_login_and_allocate_media_session(self, monkeypatch):
        monkeypatch.setattr(asmb8_console, "resolve_local_ip", lambda host: "10.1.1.1")
        fake_asp = Mock()
        fake_jnlp = Mock(kvm_token="minted-token", kvm_secure=True)
        fake_asp.allocate_media_session.return_value = fake_jnlp
        monkeypatch.setattr(asmb8_console, "build_asp_client", lambda params: fake_asp)

        params = dict(BASE_ARGS, kvm_secure=None, token=None)
        token, kvm_secure, client_ip = asmb8_console.resolve_token_and_security(params)

        fake_asp.login.assert_called_once()
        fake_asp.allocate_media_session.assert_called_once_with(client_ip="10.1.1.1", secure=None)
        assert token == "minted-token"
        assert kvm_secure is True
        assert client_ip == "10.1.1.1"

    def test_explicit_kvm_secure_overrides_what_the_jnlp_reported(self, monkeypatch):
        monkeypatch.setattr(asmb8_console, "resolve_local_ip", lambda host: "10.1.1.1")
        fake_asp = Mock()
        fake_jnlp = Mock(kvm_token="minted-token", kvm_secure=True)
        fake_asp.allocate_media_session.return_value = fake_jnlp
        monkeypatch.setattr(asmb8_console, "build_asp_client", lambda params: fake_asp)

        params = dict(BASE_ARGS, kvm_secure=False, token=None)
        _token, kvm_secure, _client_ip = asmb8_console.resolve_token_and_security(params)

        assert kvm_secure is False  # caller's explicit False wins over the JNLP's reported True.

    def test_missing_token_in_jnlp_response_is_a_classified_protocol_error(self, monkeypatch):
        monkeypatch.setattr(asmb8_console, "resolve_local_ip", lambda host: "10.1.1.1")
        fake_asp = Mock()
        fake_jnlp = Mock(kvm_token=None, kvm_secure=False)
        fake_asp.allocate_media_session.return_value = fake_jnlp
        monkeypatch.setattr(asmb8_console, "build_asp_client", lambda params: fake_asp)

        params = dict(BASE_ARGS, kvm_secure=None, token=None)
        with pytest.raises(ProtocolError):
            asmb8_console.resolve_token_and_security(params)


class TestNoSecretLeakage:
    def test_password_and_token_never_appear_in_a_successful_result(self, monkeypatch):
        _wire_no_network(monkeypatch, token=TOKEN)
        monkeypatch.setattr(ivtp, "open_channel", Mock(return_value=CANNED_FACTS))
        result = _run_ok(dict(BASE_ARGS, token=TOKEN, kvm_secure=False))
        assert not _contains_secret(result, PASSWORD)
        assert not _contains_secret(result, TOKEN)

    def test_password_and_token_never_appear_in_a_failure_result(self, monkeypatch):
        _wire_no_network(monkeypatch, token=TOKEN)
        monkeypatch.setattr(ivtp, "open_channel", Mock(side_effect=AuthenticationError(f"rejected token {TOKEN}", operation="test", secrets=[TOKEN])))
        result = _run_fail(dict(BASE_ARGS, token=TOKEN, kvm_secure=False))
        assert not _contains_secret(result, PASSWORD)
        assert not _contains_secret(result, TOKEN)

    def test_password_never_appears_when_minting_a_fresh_token(self, monkeypatch):
        monkeypatch.setattr(asmb8_console, "resolve_local_ip", lambda host: "10.1.1.1")
        fake_asp = Mock()
        fake_jnlp = Mock(kvm_token="fresh-minted-token", kvm_secure=False)
        fake_asp.allocate_media_session.return_value = fake_jnlp
        monkeypatch.setattr(asmb8_console, "build_asp_client", lambda params: fake_asp)
        transport = _FakeTransport()
        monkeypatch.setattr(asmb8_console, "build_kvm_transport", lambda params, *, kvm_secure: transport)
        monkeypatch.setattr(ivtp, "open_channel", Mock(return_value=CANNED_FACTS))

        result = _run_ok(dict(BASE_ARGS))

        assert not _contains_secret(result, PASSWORD)
        assert not _contains_secret(result, "fresh-minted-token")
