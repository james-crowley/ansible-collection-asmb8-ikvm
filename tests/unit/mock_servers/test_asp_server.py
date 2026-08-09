# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Self-tests for the mock ``.asp`` RPC server.

Two kinds of coverage, same split as the sibling ``james_crowley.intel_amt``
collection's ``test_wsman_server.py``:

* Direct HTTP coverage (``Test*`` classes other than the last) that drives the
  mock with a plain ``requests`` call and inspects the raw response -- proving
  the mock behaves like the real ``.asp`` surface on the wire, independent of
  any client's interpretation of it.
* ``TestRealAspClientAgainstMock``, which drives the collection's own
  ``AspClient`` against the mock over a real socket. A hand-rolled
  ``requests.post`` proves what the mock puts on the wire; it cannot prove
  ``AspClient`` reads it correctly -- in particular, that it raises
  ``AuthenticationError`` on a ``Failure_Login_*`` cookie rather than trusting
  the HTTP 200. That is the single most important behaviour in this whole
  fixture, so it is the one exercised through the real client, not just
  asserted against the mock's raw body.

No test in this file binds to anything other than 127.0.0.1, and none makes an
outbound network connection: every server here is a fresh ``AspMockServer`` on
an ephemeral loopback port, torn down at the end of its own test.
"""

from __future__ import annotations

import re
import socket

import pytest
import requests
from asp_server import (
    DEFAULT_FAILURE_MARKER,
    KVM_TOKEN_LENGTH,
    SESSION_COOKIE_LENGTH,
    AspMockServer,
)

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import AspClient, parse_jnlp_arguments
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import AuthenticationError, BmcBusyError

USERNAME = "admin"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def asp_server():
    with AspMockServer(username=USERNAME, password=PASSWORD) as server:
        yield server


def _login(server: AspMockServer, *, username: str = USERNAME, password: str = PASSWORD) -> requests.Response:
    return requests.post(
        f"{server.base_url}/rpc/WEBSES/create.asp",
        data={"WEBVAR_USERNAME": username, "WEBVAR_PASSWORD": password},
        timeout=5,
    )


class TestCreateAsp:
    def test_correct_credentials_return_http_200_with_a_35_char_session_cookie(self, asp_server):
        response = _login(asp_server)
        assert response.status_code == 200
        match = re.search(r"'SESSION_COOKIE':'([^']*)'", response.text)
        assert match is not None
        cookie = match.group(1)
        assert len(cookie) == SESSION_COOKIE_LENGTH
        assert not cookie.startswith("Failure_Login")

    def test_bad_credentials_return_http_200_never_401(self, asp_server):
        # CRITICAL, VERIFIED LIVE (PR #40, nesvet/nojava-ipmi-kvm): this is
        # the trap a status-code-only client falls into.
        response = _login(asp_server, password="wrong")
        assert response.status_code == 200
        assert f"'SESSION_COOKIE':'{DEFAULT_FAILURE_MARKER}'" in response.text

    def test_successful_login_updates_state_for_a_later_request_to_observe(self, asp_server):
        _login(asp_server)
        assert asp_server.state.session_cookie is not None
        assert len(asp_server.state.session_cookie) == SESSION_COOKIE_LENGTH

    def test_failed_login_does_not_clobber_a_previously_established_session_cookie(self, asp_server):
        _login(asp_server)
        first_cookie = asp_server.state.session_cookie
        _login(asp_server, password="wrong")
        assert asp_server.state.session_cookie == first_cookie

    def test_forced_failure_marker_overrides_correct_credentials_once(self, asp_server):
        asp_server.faults.force_login_failure_marker = "Failure_Login_Max_Users"
        response = _login(asp_server)
        assert "'SESSION_COOKIE':'Failure_Login_Max_Users'" in response.text
        # One-shot: the next attempt with the same correct credentials succeeds.
        response2 = _login(asp_server)
        assert "Failure_Login" not in response2.text


class TestGetSessionToken:
    def test_stoken_is_always_empty(self, asp_server):
        # VERIFIED LIVE: this endpoint never yields a usable token, regardless
        # of whether a session is active.
        _login(asp_server)
        response = requests.get(f"{asp_server.base_url}/rpc/getsessiontoken.asp", timeout=5)
        assert response.status_code == 200
        assert "'STOKEN':''" in response.text


class TestJnlp:
    def test_single_port_mode_has_no_dedicated_port_arguments(self, asp_server):
        _login(asp_server)
        response = requests.get(f"{asp_server.base_url}/Java/jviewer.jnlp", params={"EXTRNIP": "203.0.113.5", "JNLPSTR": "JViewer"}, timeout=5)
        assert response.status_code == 200
        arguments = parse_jnlp_arguments(response.text)
        assert arguments["singleportenabled"] == "1"
        assert "cdport" not in arguments
        assert "fdport" not in arguments
        assert "hdport" not in arguments
        assert arguments["kvmport"] == "443"

    def test_dedicated_ports_mode_reports_all_three_ports(self, asp_server):
        asp_server.state.single_port_enabled = False
        _login(asp_server)
        response = requests.get(f"{asp_server.base_url}/Java/jviewer.jnlp", timeout=5)
        arguments = parse_jnlp_arguments(response.text)
        assert arguments["singleportenabled"] == "0"
        assert arguments["cdport"] == str(asp_server.state.cd_port)
        assert arguments["fdport"] == str(asp_server.state.fd_port)
        assert arguments["hdport"] == str(asp_server.state.hd_port)

    def test_webcookie_is_byte_identical_to_the_session_cookie(self, asp_server):
        login_response = _login(asp_server)
        cookie = re.search(r"'SESSION_COOKIE':'([^']*)'", login_response.text).group(1)
        response = requests.get(f"{asp_server.base_url}/Java/jviewer.jnlp", timeout=5)
        arguments = parse_jnlp_arguments(response.text)
        assert arguments["webcookie"] == cookie

    def test_kvmtoken_is_a_distinct_16_char_value_from_the_session_cookie(self, asp_server):
        login_response = _login(asp_server)
        cookie = re.search(r"'SESSION_COOKIE':'([^']*)'", login_response.text).group(1)
        response = requests.get(f"{asp_server.base_url}/Java/jviewer.jnlp", timeout=5)
        arguments = parse_jnlp_arguments(response.text)
        assert len(arguments["kvmtoken"]) == KVM_TOKEN_LENGTH
        assert arguments["kvmtoken"] != cookie

    def test_unescaped_ampersand_variant_is_opt_in(self, asp_server):
        response_default = requests.get(f"{asp_server.base_url}/Java/jviewer.jnlp", timeout=5)
        assert "&" not in response_default.text.replace("&amp;", "")

        asp_server.state.jnlp_include_unescaped_ampersand = True
        response = requests.get(f"{asp_server.base_url}/Java/jviewer.jnlp", timeout=5)
        # A literal, unescaped & in an argument value -- exactly the shape
        # that breaks a strict XML parser and that asp.py's regex scanner is
        # built to tolerate.
        assert "<argument>a=1&b=2</argument>" in response.text
        arguments = parse_jnlp_arguments(response.text)
        assert arguments["note"] == "a=1&b=2"


class TestHostStatusAndHostCtl:
    """UNCONFIRMED shapes -- see asp_server.py's module docstring. These only
    check the plumbing (path resolves, method's own input field round-trips),
    never a specific response shape."""

    def test_hoststatus_answers_200(self, asp_server):
        response = requests.get(f"{asp_server.base_url}/rpc/hoststatus.asp", timeout=5)
        assert response.status_code == 200

    def test_hostctl_records_the_power_command_it_was_sent(self, asp_server):
        response = requests.post(f"{asp_server.base_url}/rpc/hostctl.asp", data={"WEBVAR_POWER_CMD": "reset"}, timeout=5)
        assert response.status_code == 200
        assert asp_server.state.last_power_command == "reset"


class TestHttp10NoKeepAlive:
    def test_response_is_http_1_0_and_closes_the_connection(self, asp_server):
        sock = socket.create_connection(("127.0.0.1", asp_server.port), timeout=5)
        try:
            sock.sendall(b"GET /rpc/hoststatus.asp HTTP/1.1\r\nHost: mock\r\n\r\n")
            first_chunk = sock.recv(64)
            assert first_chunk.startswith(b"HTTP/1.0")
            # Read until the peer closes: an HTTP/1.0, no-keep-alive server
            # closes after one response rather than waiting for another
            # request on the same connection.
            sock.settimeout(5)
            remaining = first_chunk
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                remaining += chunk
            assert b"hoststatus" in remaining.lower() or len(remaining) > 0
        finally:
            sock.close()


class TestFaultInjectionBmcBusy:
    """The single highest-value fault this mock implements: the real board
    completing the TCP handshake and then never answering at all."""

    def test_hang_before_response_sends_no_bytes_within_a_bounded_wait(self, asp_server):
        # The mock's own hang must outlast the client's read timeout below --
        # otherwise the mock's bounded give-up (a testing convenience; see
        # AspFaultConfig.hang_seconds) closes the connection first, and the
        # client would see a clean EOF instead of proving "no bytes arrive".
        asp_server.faults.hang_seconds = 2.0
        asp_server.faults.hang_before_response = True
        sock = socket.create_connection(("127.0.0.1", asp_server.port), timeout=5)
        try:
            sock.sendall(b"GET /rpc/hoststatus.asp HTTP/1.0\r\nHost: mock\r\n\r\n")
            sock.settimeout(0.3)
            with pytest.raises(TimeoutError):
                sock.recv(64)
        finally:
            sock.close()
            asp_server.faults.hang_before_response = False

    def test_hang_is_persistent_not_one_shot(self, asp_server):
        # Distinguishes this fault from force_login_failure_marker: it
        # describes what the endpoint IS for as long as a test wants that.
        asp_server.faults.hang_seconds = 2.0
        asp_server.faults.hang_before_response = True
        for _attempt in range(2):
            sock = socket.create_connection(("127.0.0.1", asp_server.port), timeout=5)
            try:
                sock.sendall(b"GET /rpc/hoststatus.asp HTTP/1.0\r\nHost: mock\r\n\r\n")
                sock.settimeout(0.3)
                with pytest.raises(TimeoutError):
                    sock.recv(64)
            finally:
                sock.close()
        asp_server.faults.hang_before_response = False


class TestRealAspClientAgainstMock:
    """Drives the collection's own AspClient against this mock over a real
    socket -- the only way to prove the client reads what the mock sends,
    not just that the mock sends something plausible."""

    def _client(self, server: AspMockServer, **overrides) -> AspClient:
        kwargs = {
            "host": "127.0.0.1",
            "port": server.port,
            "username": USERNAME,
            "password": PASSWORD,
            "use_tls": False,
            "allow_insecure_transport": True,
            "max_retries": 0,
        }
        kwargs.update(overrides)
        return AspClient(**kwargs)

    def test_successful_login_round_trips_through_the_real_client(self, asp_server):
        client = self._client(asp_server)
        cookie = client.login()
        assert len(cookie) == SESSION_COOKIE_LENGTH
        assert cookie == asp_server.state.session_cookie

    def test_bad_credentials_raise_authentication_error_not_a_silent_200(self, asp_server):
        # This is the whole point of the mock: prove the real client does not
        # fall into the HTTP-200-on-bad-credentials trap.
        client = self._client(asp_server, password="wrong")
        with pytest.raises(AuthenticationError):
            client.login()

    def test_get_session_token_returns_none_through_the_real_client(self, asp_server):
        client = self._client(asp_server)
        client.login()
        assert client.get_session_token() is None

    def test_allocate_media_session_single_port_mode(self, asp_server):
        client = self._client(asp_server)
        client.login()
        session = client.allocate_media_session(client_ip="203.0.113.5")
        assert session.port_mode == "single_port"
        assert session.web_cookie == asp_server.state.session_cookie
        assert session.kvm_token == asp_server.state.kvm_token

    def test_allocate_media_session_dedicated_ports_mode(self, asp_server):
        asp_server.state.single_port_enabled = False
        client = self._client(asp_server)
        client.login()
        session = client.allocate_media_session(client_ip="203.0.113.5")
        assert session.port_mode == "dedicated_ports"
        assert session.cd_port == asp_server.state.cd_port
        assert session.fd_port == asp_server.state.fd_port
        assert session.hd_port == asp_server.state.hd_port

    def test_hang_before_response_eventually_raises_bmc_busy_error(self, asp_server):
        asp_server.faults.hang_seconds = 5.0
        asp_server.faults.hang_before_response = True
        # A short read timeout so the client gives up well before the mock's
        # own bound -- the mock's bound only exists to keep the test suite
        # from leaking a thread forever, per asp_server.py's own comment.
        client = self._client(asp_server, timeout=0.3, connect_timeout=2, max_retries=0)
        with pytest.raises(BmcBusyError) as exc_info:
            client.get_session_token()
        assert exc_info.value.to_result()["indeterminate"] is True
        asp_server.faults.hang_before_response = False
