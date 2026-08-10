# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Self-tests for the mock IVTP KVM/console server.

Two kinds of coverage, mirroring ``test_iusb_server.py``'s own split exactly:

* ``Test*`` classes that drive the mock directly over a raw socket, using
  this file's own tiny client-side helpers rather than any real client
  implementation -- these pin the mock's own wire behaviour (including every
  fault-injection mode) independent of whether a real client agrees with it.
* ``TestRealClientAgainstMock``, which drives this collection's own shipped
  IVTP client (``plugins/module_utils/ivtp.py``: ``SocketTransport``,
  ``open_channel``, ``capture_one_frame``) end to end against this mock,
  over a real loopback socket. This is the coverage
  ``docs/capability-matrix.md`` previously listed as entirely absent for
  ``asmb8_console``/``ivtp.py`` -- see that file's Tier 4 entry, and this
  mock's own module docstring for exactly what this class does and does not
  prove (Tier 2 self-consistency against a decompiled-only understanding of
  the wire format, never Tier 3 live-hardware confirmation).

No test in this file binds to anything other than 127.0.0.1, and none makes
an outbound network connection: every server here is a fresh
``IvtpMockServer`` on an ephemeral loopback port, and the "real client" class
drives that same loopback socket, never a real BMC.
"""

from __future__ import annotations

import socket
import struct
import threading

import pytest
from ivtp_server import (
    DEFAULT_EXPECTED_TOKEN,
    FRAG_LAST_BIT,
    HEADER_LEN,
    LIVE_GREETING_BYTES,
    OP_BLANK_SCREEN,
    OP_GET_WEB_TOKEN,
    OP_REFRESH_VIDEO_SCREEN,
    OP_RESUME_REDIRECTION,
    OP_SESSION_ACCEPTED,
    OP_STOP_SESSION_IMMEDIATE,
    OP_VALIDATE_VIDEO_SESSION,
    OP_VALIDATE_VIDEO_SESSION_RESPONSE,
    OP_VIDEO_FRAGMENT,
    SESSION_INVALID_VIDEO_TOKEN,
    SESSION_KVM_DISABLED,
    SESSION_VALID,
    STOP_KVM_DISCONNECT,
    STOP_TIMED_OUT,
    STOP_WEB_LOGOUT,
    TOKEN_TYPE_WEB_SESSION,
    VIDEO_PACKET_SIZE,
    Header,
    IvtpMockServer,
    ProtocolViolation,
)

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import ivtp
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import (
    AuthenticationError,
    InvalidStateError,
    TimeoutError_,
    UnsupportedCapabilityError,
)

TOKEN = "kvm-unit-test-token"  # obviously-fake, not a real credential
CLIENT_IP = "10.1.1.1"
USERNAME = "ansible"


# ==========================================================================
# Minimal client-side helpers -- deliberately NOT the real client. Just
# enough wire code to drive the mock directly for its own self-tests.
# ==========================================================================


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError(f"socket closed after {len(buf)}/{n} bytes")
        buf += chunk
    return bytes(buf)


def _recv_frame(sock: socket.socket) -> tuple[Header, bytes]:
    header = Header.parse(_recv_exact(sock, HEADER_LEN))
    body = _recv_exact(sock, header.pkt_size) if header.pkt_size else b""
    return header, body


def _connect(server: IvtpMockServer) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    sock.settimeout(5)
    return sock


def _build_validate_request(*, token: str = TOKEN, client_ip: str = CLIENT_IP, username: str = USERNAME) -> bytes:
    token_field = bytes([TOKEN_TYPE_WEB_SESSION]) + token.encode("ascii") + bytes(129 - len(token))
    ip_field = client_ip.encode("ascii") + bytes(65 - len(client_ip))
    username_field = username.encode("ascii") + bytes(129 - len(username))
    body = token_field + ip_field + username_field
    assert len(body) == VIDEO_PACKET_SIZE
    return Header(type=OP_VALIDATE_VIDEO_SESSION, pkt_size=len(body)).marshal() + body


@pytest.fixture
def server():
    with IvtpMockServer(expected_token=DEFAULT_EXPECTED_TOKEN) as srv:
        yield srv


# ==========================================================================
# Header codec
# ==========================================================================


class TestHeaderCodec:
    def test_round_trip(self):
        header = Header(type=25, pkt_size=4096, status=7)
        assert Header.parse(header.marshal()) == header

    def test_little_endian(self):
        header = Header(type=0x0102, pkt_size=0x01020304, status=0x0506)
        assert header.marshal() == bytes([0x02, 0x01, 0x04, 0x03, 0x02, 0x01, 0x06, 0x05])

    def test_short_header_is_a_protocol_violation(self):
        with pytest.raises(ProtocolViolation):
            Header.parse(b"\x17\x00\x00")

    def test_live_greeting_bytes_decode_to_session_accepted(self):
        header = Header.parse(LIVE_GREETING_BYTES)
        assert header.type == OP_SESSION_ACCEPTED == 23
        assert header.pkt_size == 0
        assert header.status == 0


# ==========================================================================
# Greeting on connect
# ==========================================================================


class TestGreetingOnConnect:
    def test_default_greeting_matches_the_live_captured_bytes(self, server):
        sock = _connect(server)
        try:
            raw = _recv_exact(sock, HEADER_LEN)
            assert raw == LIVE_GREETING_BYTES
        finally:
            sock.close()

    def test_custom_greeting_body_length_is_honoured(self):
        with IvtpMockServer(greeting_body=b"\x00" * 12) as srv:
            sock = _connect(srv)
            try:
                header, body = _recv_frame(sock)
                assert header.type == OP_SESSION_ACCEPTED
                assert header.pkt_size == 12
                assert len(body) == 12
            finally:
                sock.close()


# ==========================================================================
# Reading the client's handshake packets
# ==========================================================================


class TestRecvValidateVideoSession:
    def test_parses_token_ip_and_username(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)  # the greeting
            sock.sendall(_build_validate_request())
            request = server.recv_validate_video_session()
            assert request.token == TOKEN
            assert request.client_ip == CLIENT_IP
            assert request.username == USERNAME
            assert request.token_type == TOKEN_TYPE_WEB_SESSION
        finally:
            sock.close()

    def test_leading_get_web_token_is_tolerated_and_recorded(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            get_web_token = Header(type=OP_GET_WEB_TOKEN, pkt_size=len(TOKEN)).marshal() + TOKEN.encode("ascii")
            sock.sendall(get_web_token + _build_validate_request())
            request = server.recv_validate_video_session()
            assert server.received_get_web_token is True
            assert server.get_web_token_body == TOKEN.encode("ascii")
            assert request.token == TOKEN
        finally:
            sock.close()

    def test_no_get_web_token_is_recorded_as_absent(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            sock.sendall(_build_validate_request())
            server.recv_validate_video_session()
            assert server.received_get_web_token is False
            assert server.get_web_token_body is None
        finally:
            sock.close()

    def test_wrong_opcode_is_a_protocol_violation(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            sock.sendall(Header(type=OP_RESUME_REDIRECTION, pkt_size=0).marshal())
            with pytest.raises(ProtocolViolation):
                server.recv_validate_video_session()
        finally:
            sock.close()

    def test_wrong_body_length_is_a_protocol_violation(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            short_body = b"\x00" * 10
            sock.sendall(Header(type=OP_VALIDATE_VIDEO_SESSION, pkt_size=len(short_body)).marshal() + short_body)
            with pytest.raises(ProtocolViolation):
                server.recv_validate_video_session()
        finally:
            sock.close()


class TestRecvResumeAndRefresh:
    def test_recv_resume_redirection_accepts_the_right_opcode(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            sock.sendall(Header(type=OP_RESUME_REDIRECTION, pkt_size=0).marshal())
            server.recv_resume_redirection()  # must not raise
        finally:
            sock.close()

    def test_recv_resume_redirection_rejects_the_wrong_opcode(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            sock.sendall(Header(type=OP_REFRESH_VIDEO_SCREEN, pkt_size=0).marshal())
            with pytest.raises(ProtocolViolation):
                server.recv_resume_redirection()
        finally:
            sock.close()

    def test_recv_refresh_video_screen_accepts_the_right_opcode(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            sock.sendall(Header(type=OP_REFRESH_VIDEO_SCREEN, pkt_size=0).marshal())
            server.recv_refresh_video_screen()  # must not raise
        finally:
            sock.close()


# ==========================================================================
# Sending to the client
# ==========================================================================


class TestSendValidateResponse:
    def test_single_status_byte(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            server.send_validate_response(SESSION_VALID)
            header, body = _recv_frame(sock)
            assert header.type == OP_VALIDATE_VIDEO_SESSION_RESPONSE
            assert body == bytes([SESSION_VALID])
        finally:
            sock.close()

    def test_status_plus_sub_status(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            server.send_validate_response(SESSION_KVM_DISABLED, sub_status=0x42)
            _header, body = _recv_frame(sock)
            assert body == bytes([SESSION_KVM_DISABLED, 0x42])
        finally:
            sock.close()


class TestSendStopSessionAndFiller:
    def test_stop_session_carries_the_reason_in_the_status_field(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            server.send_stop_session(STOP_WEB_LOGOUT)
            header, body = _recv_frame(sock)
            assert header.type == OP_STOP_SESSION_IMMEDIATE
            assert header.status == STOP_WEB_LOGOUT
            assert body == b""
        finally:
            sock.close()

    def test_filler_packet_carries_an_arbitrary_opcode_and_body(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            server.send_filler_packet(OP_BLANK_SCREEN, b"\x01")
            header, body = _recv_frame(sock)
            assert header.type == OP_BLANK_SCREEN
            assert body == b"\x01"
        finally:
            sock.close()


class TestSendVideoFrame:
    def test_single_fragment_frame_sets_first_and_last_bits_together(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            frag_nums = server.send_video_frame(b"short-payload", fragment_size=1024)
            assert frag_nums == [FRAG_LAST_BIT]
            header, body = _recv_frame(sock)
            assert header.type == OP_VIDEO_FRAGMENT
            (frag_num,) = struct.unpack_from("<H", body, 0)
            assert frag_num == FRAG_LAST_BIT
            assert (frag_num & 0x7FFF) == 0  # also "first"
            assert body[2:] == b"short-payload"
        finally:
            sock.close()

    def test_multi_fragment_frame_marks_first_middle_and_last_correctly(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            payload = b"AAAA" + b"BBBB" + b"CC"
            frag_nums = server.send_video_frame(payload, fragment_size=4)
            assert frag_nums == [0, 1, 2 | FRAG_LAST_BIT]

            received = b""
            for expected_num in frag_nums:
                header, body = _recv_frame(sock)
                assert header.type == OP_VIDEO_FRAGMENT
                (frag_num,) = struct.unpack_from("<H", body, 0)
                assert frag_num == expected_num
                received += body[2:]
            assert received == payload
        finally:
            sock.close()

    def test_empty_payload_sends_one_empty_fragment(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            frag_nums = server.send_video_frame(b"", fragment_size=64)
            assert frag_nums == [FRAG_LAST_BIT]
            _header, body = _recv_frame(sock)
            assert body == struct.pack("<H", FRAG_LAST_BIT)
        finally:
            sock.close()


# ==========================================================================
# Fault injection
# ==========================================================================


class TestFaultInjection:
    def test_truncate_next_frame_to_below_header_len_sends_a_truncated_header(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)  # the greeting, unaffected
            server.faults.truncate_next_frame_to = 3
            server.send_validate_response(SESSION_VALID)
            raw = sock.recv(64)
            assert len(raw) == 3
            # One-shot: consumed after firing.
            assert server.faults.truncate_next_frame_to is None
        finally:
            sock.close()

    def test_truncate_next_frame_to_mid_body_sends_a_truncated_body(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            payload = b"0123456789ABCDEF"  # 16 bytes, comfortably longer than 1 fragment header
            server.faults.truncate_next_frame_to = HEADER_LEN + 4
            server.send_video_fragment(FRAG_LAST_BIT, payload)
            raw = sock.recv(64)
            assert len(raw) == HEADER_LEN + 4
        finally:
            sock.close()

    def test_lie_next_pkt_size_declares_more_than_is_actually_sent(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            server.faults.lie_next_pkt_size = 999
            server.send_validate_response(SESSION_VALID)
            header = Header.parse(_recv_exact(sock, HEADER_LEN))
            assert header.pkt_size == 999
            # Only the real (1-byte) body was actually sent, not the 999
            # declared -- consume exactly that one real byte, then prove no
            # more ever arrives by disconnecting and observing EOF.
            assert _recv_exact(sock, 1) == bytes([SESSION_VALID])
            server.disconnect()
            assert sock.recv(64) == b""
            assert server.faults.lie_next_pkt_size is None
        finally:
            sock.close()

    def test_disconnect_after_next_send_closes_the_connection(self, server):
        sock = _connect(server)
        try:
            _recv_frame(sock)
            server.faults.disconnect_after_next_send = True
            server.send_stop_session(STOP_WEB_LOGOUT)
            _recv_frame(sock)  # the STOP_SESSION_IMMEDIATE itself still arrives intact
            assert sock.recv(64) == b""  # then EOF: nothing more is ever sent
        finally:
            sock.close()


# ==========================================================================
# TestRealClientAgainstMock: drives plugins/module_utils/ivtp.py for real.
# ==========================================================================


class _ClientRun:
    """Runs a callable in a background thread, capturing its return value or
    the exception it raised, so the main thread (which plays the BMC side
    against the very same mock) can drive the conversation step by step."""

    def __init__(self, fn, /, *args, **kwargs) -> None:
        self.value = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(fn, args, kwargs), daemon=True)
        self._thread.start()

    def _run(self, fn, args, kwargs) -> None:
        try:
            self.value = fn(*args, **kwargs)
        except BaseException as exc:  # deliberately captured for the main thread to inspect, including EOFError
            self.error = exc

    def join(self, timeout: float = 5.0) -> None:
        self._thread.join(timeout=timeout)
        assert not self._thread.is_alive(), "client thread did not finish in time"


class TestRealClientAgainstMock:
    def _connect_transport(self, server: IvtpMockServer, *, timeout: float = 5.0) -> ivtp.SocketTransport:
        transport = ivtp.SocketTransport.connect("127.0.0.1", server.port, timeout=timeout)
        server.wait_for_connection(timeout=timeout)
        return transport

    def test_full_successful_handshake(self, server):
        transport = self._connect_transport(server)
        run = _ClientRun(ivtp.open_channel, transport, token=TOKEN, client_ip=CLIENT_IP, username=USERNAME, handshake_timeout=5.0)

        request = server.recv_validate_video_session()
        server.send_validate_response(SESSION_VALID)
        server.recv_resume_redirection()
        run.join()

        assert run.error is None
        facts = run.value
        assert facts.session_accepted is True
        assert facts.validate_status == SESSION_VALID
        assert facts.resumed is True
        # The mock, playing the BMC, saw exactly what the real client sent --
        # byte-exact proof the client and this mock agree on the wire shape.
        assert request.token == TOKEN
        assert request.client_ip == CLIENT_IP
        assert request.username == USERNAME
        assert server.received_get_web_token is True  # send_get_web_token defaults True

        transport.close()

    def test_send_get_web_token_false_is_honoured(self, server):
        transport = self._connect_transport(server)
        run = _ClientRun(
            ivtp.open_channel,
            transport,
            token=TOKEN,
            client_ip=CLIENT_IP,
            username=USERNAME,
            handshake_timeout=5.0,
            send_get_web_token=False,
        )

        server.recv_validate_video_session()
        assert server.received_get_web_token is False
        server.send_validate_response(SESSION_VALID)
        server.recv_resume_redirection()
        run.join()

        assert run.error is None
        transport.close()

    def test_raw_frame_capture_reassembles_a_fragmented_frame_byte_exact(self, server):
        transport = self._connect_transport(server)
        run = _ClientRun(ivtp.open_channel, transport, token=TOKEN, client_ip=CLIENT_IP, username=USERNAME, handshake_timeout=5.0)
        server.recv_validate_video_session()
        server.send_validate_response(SESSION_VALID)
        server.recv_resume_redirection()
        run.join()
        assert run.error is None

        frame_payload = b"the quick brown fox jumps over the lazy dog" * 3  # forces several fragments
        capture_run = _ClientRun(ivtp.capture_one_frame, transport, frame_timeout=5.0)
        server.recv_refresh_video_screen()
        frag_nums = server.send_video_frame(frame_payload, fragment_size=17)
        capture_run.join()

        assert capture_run.error is None
        assert capture_run.value == frame_payload
        assert len(frag_nums) > 1, "the test payload/fragment_size must actually force multiple fragments"

        transport.close()

    def test_failed_validate_status_is_classified(self, server):
        transport = self._connect_transport(server)
        run = _ClientRun(ivtp.open_channel, transport, token=TOKEN, client_ip=CLIENT_IP, username=USERNAME, handshake_timeout=5.0)
        server.recv_validate_video_session()
        server.send_validate_response(SESSION_INVALID_VIDEO_TOKEN)
        run.join()

        assert isinstance(run.error, AuthenticationError)
        transport.close()

    def test_unsolicited_stop_session_mid_handshake_is_classified(self, server):
        # The mock never sends VALIDATE_VIDEO_SESSION_RESPONSE at all here --
        # STOP_SESSION_IMMEDIATE arrives instead, while open_channel's read
        # loop is still waiting for that response.
        transport = self._connect_transport(server)
        run = _ClientRun(ivtp.open_channel, transport, token=TOKEN, client_ip=CLIENT_IP, username=USERNAME, handshake_timeout=5.0)
        server.recv_validate_video_session()
        server.send_stop_session(STOP_WEB_LOGOUT)
        run.join()

        assert isinstance(run.error, AuthenticationError)
        transport.close()

    def test_unsolicited_stop_session_mid_frame_is_classified(self, server):
        transport = self._connect_transport(server)
        run = _ClientRun(ivtp.open_channel, transport, token=TOKEN, client_ip=CLIENT_IP, username=USERNAME, handshake_timeout=5.0)
        server.recv_validate_video_session()
        server.send_validate_response(SESSION_VALID)
        server.recv_resume_redirection()
        run.join()
        assert run.error is None

        capture_run = _ClientRun(ivtp.capture_one_frame, transport, frame_timeout=5.0)
        server.recv_refresh_video_screen()
        server.send_video_fragment(0, b"first-half-only")  # first fragment, deliberately not last
        server.send_stop_session(STOP_TIMED_OUT)
        capture_run.join()

        assert isinstance(capture_run.error, TimeoutError_)
        transport.close()

    def test_stop_session_kvm_disconnect_reason_is_classified_as_invalid_state(self, server):
        transport = self._connect_transport(server)
        run = _ClientRun(ivtp.open_channel, transport, token=TOKEN, client_ip=CLIENT_IP, username=USERNAME, handshake_timeout=5.0)
        server.recv_validate_video_session()
        server.send_stop_session(STOP_KVM_DISCONNECT)
        run.join()

        assert isinstance(run.error, InvalidStateError)
        transport.close()

    def test_kvm_disabled_is_classified_as_unsupported_capability(self, server):
        transport = self._connect_transport(server)
        run = _ClientRun(ivtp.open_channel, transport, token=TOKEN, client_ip=CLIENT_IP, username=USERNAME, handshake_timeout=5.0)
        server.recv_validate_video_session()
        server.send_validate_response(SESSION_KVM_DISABLED)
        run.join()

        assert isinstance(run.error, UnsupportedCapabilityError)
        transport.close()

    def test_intervening_filler_packets_are_tolerated(self, server):
        transport = self._connect_transport(server)
        run = _ClientRun(ivtp.open_channel, transport, token=TOKEN, client_ip=CLIENT_IP, username=USERNAME, handshake_timeout=5.0)
        server.recv_validate_video_session()
        server.send_filler_packet(OP_BLANK_SCREEN)
        server.send_validate_response(SESSION_VALID)
        server.recv_resume_redirection()
        run.join()

        assert run.error is None
        transport.close()

    def test_truncated_header_raises_eof(self, server):
        transport = self._connect_transport(server)
        run = _ClientRun(ivtp.open_channel, transport, token=TOKEN, client_ip=CLIENT_IP, username=USERNAME, handshake_timeout=5.0)
        server.recv_validate_video_session()
        server.faults.truncate_next_frame_to = 3  # fewer than HEADER_LEN (8) bytes
        server.faults.disconnect_after_next_send = True
        server.send_validate_response(SESSION_VALID)
        run.join()

        assert isinstance(run.error, EOFError)
        transport.close()

    def test_truncated_body_raises_eof(self, server):
        transport = self._connect_transport(server)
        run = _ClientRun(ivtp.open_channel, transport, token=TOKEN, client_ip=CLIENT_IP, username=USERNAME, handshake_timeout=5.0)
        server.recv_validate_video_session()
        server.send_validate_response(SESSION_VALID)
        server.recv_resume_redirection()
        run.join()
        assert run.error is None

        capture_run = _ClientRun(ivtp.capture_one_frame, transport, frame_timeout=5.0)
        server.recv_refresh_video_screen()
        server.faults.truncate_next_frame_to = HEADER_LEN + 2  # header intact, body cut short
        server.faults.disconnect_after_next_send = True
        server.send_video_fragment(FRAG_LAST_BIT, b"0123456789ABCDEF")  # 16-byte body, declared in full
        capture_run.join()

        assert isinstance(capture_run.error, EOFError)
        transport.close()

    def test_pkt_size_disagreeing_with_bytes_actually_sent_raises_eof(self, server):
        transport = self._connect_transport(server)
        run = _ClientRun(ivtp.open_channel, transport, token=TOKEN, client_ip=CLIENT_IP, username=USERNAME, handshake_timeout=5.0)
        server.recv_validate_video_session()
        # Declares a pktSize far larger than the single real status byte that
        # actually follows, then disconnects -- open_channel's read_packet()
        # trusts the declared size and blocks for the promised (never
        # arriving) remainder, so the disconnect must surface as an EOF.
        server.faults.lie_next_pkt_size = 4096
        server.faults.disconnect_after_next_send = True
        server.send_validate_response(SESSION_VALID)
        run.join()

        assert isinstance(run.error, EOFError)
        transport.close()
