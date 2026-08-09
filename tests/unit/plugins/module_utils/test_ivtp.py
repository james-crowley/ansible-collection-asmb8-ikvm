# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for ``ivtp.py``: header pack/unpack, every opcode/message type this
module implements, the handshake state machine (including every rejection
path), video-fragment reassembly, and one full handshake-plus-one-frame
exchange over ``socket.socketpair()``.

No test in this file makes a network request. The only sockets involved are
``socket.socketpair()`` (an in-process, kernel-mediated pipe with no network
interface at all) and a single localhost connect-to-a-closed-port probe used
to exercise :meth:`ivtp.SocketTransport.connect`'s failure path -- neither
touches any real BMC.
"""

from __future__ import annotations

import dataclasses
import socket
import struct
import threading

import pytest

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import ivtp
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import (
    AuthenticationError,
    ConnectionError_,
    InvalidStateError,
    ProtocolError,
    RemoteOperationError,
    TimeoutError_,
    UnsupportedCapabilityError,
)

#: The exact 8 bytes captured live against the target board: an unsolicited
#: SESSION_ACCEPTED (type=23/0x17) greeting, no body, no status.
LIVE_GREETING_HEX = "1700000000000000"

TOKEN = "STOKEN-abc1234567"  # 18 chars: deliberately not the real 16-char shape, to prove no length assumption leaks.


# ===========================================================================
# Header: the exact captured greeting, and pack/unpack round trip
# ===========================================================================


class TestHeader:
    def test_live_greeting_bytes_parse_to_session_accepted(self):
        header = ivtp.Header.parse(bytes.fromhex(LIVE_GREETING_HEX))
        assert header.type == ivtp.OP_SESSION_ACCEPTED == 23
        assert header.pkt_size == 0
        assert header.status == 0

    def test_marshal_produces_the_exact_greeting_bytes(self):
        # The reverse direction: building this exact header must reproduce the captured bytes
        # byte-for-byte, not merely parse equivalently.
        header = ivtp.Header(type=23, pkt_size=0, status=0)
        assert header.marshal() == bytes.fromhex(LIVE_GREETING_HEX)

    def test_round_trip_with_nonzero_fields(self):
        header = ivtp.Header(type=25, pkt_size=4096, status=7)
        parsed = ivtp.Header.parse(header.marshal())
        assert parsed == header

    def test_marshal_is_little_endian(self):
        header = ivtp.Header(type=0x0102, pkt_size=0x01020304, status=0x0506)
        wire = header.marshal()
        assert wire == bytes([0x02, 0x01, 0x04, 0x03, 0x02, 0x01, 0x06, 0x05])

    def test_short_header_is_a_classified_protocol_error(self):
        with pytest.raises(ProtocolError):
            ivtp.Header.parse(b"\x17\x00\x00")

    def test_header_len_constant_matches_the_struct_size(self):
        assert ivtp.HEADER_LEN == 8
        assert len(ivtp.Header(type=1).marshal()) == ivtp.HEADER_LEN

    def test_opcode_constants_match_the_decompiled_ivtppkthdr_table(self):
        # Spot-check a handful against com.ami.kvm.jviewer.kvmpkts.IVTPPktHdr's own values.
        assert ivtp.OP_HID_PKT == 1
        assert ivtp.OP_RESUME_REDIRECTION == 6
        assert ivtp.OP_STOP_SESSION_IMMEDIATE == 8
        assert ivtp.OP_VALIDATE_VIDEO_SESSION == 18
        assert ivtp.OP_VALIDATE_VIDEO_SESSION_RESPONSE == 19
        assert ivtp.OP_GET_WEB_TOKEN == 21
        assert ivtp.OP_SESSION_ACCEPTED == 23
        assert ivtp.OP_VIDEO_FRAGMENT == 25
        assert ivtp.OP_REFRESH_VIDEO_SCREEN == 5

    def test_opcodes_57_and_58_are_deliberately_not_defined(self):
        # See ivtp.py's module docstring, disagreement 1: this board's decompiled client defines
        # no KEEP_ALIVE(57)/CONNECTION_COMPLETE(58) at all, unlike rd450x-console.
        assert not hasattr(ivtp, "OP_KEEP_ALIVE")
        assert not hasattr(ivtp, "OP_CONNECTION_COMPLETE")


# ===========================================================================
# VALIDATE_VIDEO_SESSION (18): body layout, byte-exact
# ===========================================================================


class TestBuildValidateVideoSession:
    def test_total_length_matches_header_plus_video_packet_size(self):
        wire = ivtp.build_validate_video_session(token=TOKEN, client_ip="10.0.0.5", username="ansible")
        assert len(wire) == ivtp.HEADER_LEN + ivtp.VIDEO_PACKET_SIZE
        assert ivtp.VIDEO_PACKET_SIZE == 324

    def test_header_opcode_and_pkt_size(self):
        wire = ivtp.build_validate_video_session(token=TOKEN, client_ip="10.0.0.5", username="ansible")
        header = ivtp.Header.parse(wire[: ivtp.HEADER_LEN])
        assert header.type == ivtp.OP_VALIDATE_VIDEO_SESSION
        assert header.pkt_size == ivtp.VIDEO_PACKET_SIZE  # 324, not the decompiled client's 332 -- see module docstring.
        assert header.status == 0

    def test_token_type_byte_defaults_to_web_session(self):
        wire = ivtp.build_validate_video_session(token=TOKEN, client_ip="10.0.0.5", username="ansible")
        body = wire[ivtp.HEADER_LEN :]
        assert body[0] == ivtp.TOKEN_TYPE_WEB_SESSION == 0

    def test_token_bytes_and_zero_padding(self):
        wire = ivtp.build_validate_video_session(token=TOKEN, client_ip="10.0.0.5", username="ansible")
        body = wire[ivtp.HEADER_LEN :]
        token_field = body[1:130]
        assert token_field[: len(TOKEN)] == TOKEN.encode("ascii")
        assert token_field[len(TOKEN) :] == bytes(130 - 1 - len(TOKEN))

    def test_ip_field_offset_and_padding(self):
        wire = ivtp.build_validate_video_session(token=TOKEN, client_ip="10.0.0.5", username="ansible")
        body = wire[ivtp.HEADER_LEN :]
        ip_field = body[130:195]
        assert len(ip_field) == 65
        assert ip_field[:8] == b"10.0.0.5"
        assert ip_field[8:] == bytes(65 - 8)

    def test_username_field_offset_and_padding(self):
        wire = ivtp.build_validate_video_session(token=TOKEN, client_ip="10.0.0.5", username="ansible")
        body = wire[ivtp.HEADER_LEN :]
        username_field = body[195:324]
        assert len(username_field) == 129
        assert username_field[:7] == b"ansible"
        assert username_field[7:] == bytes(129 - 7)

    def test_token_type_ssi_is_selectable(self):
        wire = ivtp.build_validate_video_session(token=TOKEN, client_ip="10.0.0.5", username="ansible", token_type=ivtp.TOKEN_TYPE_SSI)
        body = wire[ivtp.HEADER_LEN :]
        assert body[0] == ivtp.TOKEN_TYPE_SSI == 1

    def test_oversized_token_is_a_classified_protocol_error_not_a_crash(self):
        huge_secret_token = "X" * 200
        with pytest.raises(ProtocolError) as excinfo:
            ivtp.build_validate_video_session(token=huge_secret_token, client_ip="10.0.0.5", username="ansible")
        assert huge_secret_token not in str(excinfo.value)

    def test_oversized_username_is_a_classified_protocol_error(self):
        with pytest.raises(ProtocolError):
            ivtp.build_validate_video_session(token=TOKEN, client_ip="10.0.0.5", username="x" * 200)

    def test_oversized_ip_is_a_classified_protocol_error(self):
        with pytest.raises(ProtocolError):
            ivtp.build_validate_video_session(token=TOKEN, client_ip="x" * 200, username="ansible")


# ===========================================================================
# GET_WEB_TOKEN (21)
# ===========================================================================


class TestBuildGetWebToken:
    def test_header_and_body(self):
        wire = ivtp.build_get_web_token(TOKEN)
        header = ivtp.Header.parse(wire[: ivtp.HEADER_LEN])
        assert header.type == ivtp.OP_GET_WEB_TOKEN
        assert header.pkt_size == len(TOKEN)
        body = wire[ivtp.HEADER_LEN :]
        assert body == TOKEN.encode("ascii")

    def test_body_is_not_padded_unlike_validate_video_session(self):
        wire = ivtp.build_get_web_token("short")
        assert len(wire) == ivtp.HEADER_LEN + len("short")


# ===========================================================================
# Bodyless control packets: RESUME_REDIRECTION, REFRESH_VIDEO_SCREEN, STOP_SESSION
# ===========================================================================


class TestBodylessPackets:
    def test_resume_redirection(self):
        wire = ivtp.build_resume_redirection()
        assert wire == ivtp.Header(type=ivtp.OP_RESUME_REDIRECTION, pkt_size=0, status=0).marshal()
        assert len(wire) == ivtp.HEADER_LEN

    def test_refresh_video_screen(self):
        wire = ivtp.build_refresh_video_screen()
        assert wire == ivtp.Header(type=ivtp.OP_REFRESH_VIDEO_SCREEN, pkt_size=0, status=0).marshal()

    def test_stop_session_default_status(self):
        wire = ivtp.build_stop_session()
        header = ivtp.Header.parse(wire)
        assert header.type == ivtp.OP_STOP_SESSION_IMMEDIATE
        assert header.status == 0

    def test_stop_session_custom_status(self):
        wire = ivtp.build_stop_session(status=ivtp.STOP_KVM_DISCONNECT)
        header = ivtp.Header.parse(wire)
        assert header.status == ivtp.STOP_KVM_DISCONNECT


# ===========================================================================
# VALIDATE_VIDEO_SESSION_RESPONSE (19): parsing and status classification
# ===========================================================================


class TestValidateVideoSessionResponse:
    def test_parse_single_status_byte(self):
        status, sub_status = ivtp.parse_validate_video_session_response(bytes([ivtp.SESSION_VALID]))
        assert status == ivtp.SESSION_VALID
        assert sub_status is None

    def test_parse_status_plus_sub_status(self):
        status, sub_status = ivtp.parse_validate_video_session_response(bytes([ivtp.SESSION_INVALID_VIDEO_TOKEN, 0x42]))
        assert status == ivtp.SESSION_INVALID_VIDEO_TOKEN
        assert sub_status == 0x42

    def test_empty_body_is_a_classified_protocol_error(self):
        with pytest.raises(ProtocolError):
            ivtp.parse_validate_video_session_response(b"")

    def test_valid_status_name(self):
        assert ivtp.validate_status_name(ivtp.SESSION_VALID) == "valid_session"

    def test_unknown_status_name_is_labelled_unknown(self):
        assert ivtp.validate_status_name(99) == "unknown(99)"

    @pytest.mark.parametrize(
        ("status", "expected_exception"),
        [
            (ivtp.SESSION_INVALID, AuthenticationError),
            (ivtp.SESSION_INVALID_VIDEO_TOKEN, AuthenticationError),
            (ivtp.SESSION_INVALID_CDROM_TOKEN, AuthenticationError),
            (ivtp.SESSION_INVALID_FLOPPY_TOKEN, AuthenticationError),
            (ivtp.SESSION_KVM_DISABLED, UnsupportedCapabilityError),
            (123, ProtocolError),
        ],
    )
    def test_validate_status_error_classification(self, status, expected_exception):
        err = ivtp.validate_status_error(status, endpoint="10.0.0.5:7578")
        assert isinstance(err, expected_exception)
        assert err.endpoint == "10.0.0.5:7578"

    def test_validate_status_error_never_embeds_a_token(self):
        # validate_status_error() takes no token parameter at all -- this asserts that
        # structurally, by checking the classified message never contains anything token-shaped.
        err = ivtp.validate_status_error(ivtp.SESSION_INVALID_VIDEO_TOKEN)
        assert TOKEN not in str(err)


# ===========================================================================
# STOP_SESSION_IMMEDIATE (8): reason classification
# ===========================================================================


class TestStopSessionError:
    @pytest.mark.parametrize(
        ("status", "expected_exception"),
        [
            (ivtp.STOP_WEB_LOGOUT, AuthenticationError),
            (ivtp.STOP_LICENSE_EXPIRED, UnsupportedCapabilityError),
            (ivtp.STOP_TIMED_OUT, TimeoutError_),
            (ivtp.STOP_KVM_DISCONNECT, InvalidStateError),
            (ivtp.STOP_CONF_CHANGE, RemoteOperationError),
            (ivtp.STOP_GENERIC, RemoteOperationError),
            (250, RemoteOperationError),
        ],
    )
    def test_stop_session_error_classification(self, status, expected_exception):
        err = ivtp.stop_session_error(status, endpoint="10.0.0.5:7578")
        assert isinstance(err, expected_exception)

    def test_stop_reason_name_unknown(self):
        assert ivtp.stop_reason_name(250) == "unknown(250)"


# ===========================================================================
# VIDEO_FRAGMENT (25): fragment-number bit convention, and reassembly
# ===========================================================================


class TestParseVideoFragment:
    def test_first_and_only_fragment(self):
        body = struct.pack("<H", 0x8000) + b"data"
        frag_num, is_first, is_last, data = ivtp.parse_video_fragment(body)
        assert frag_num == 0x8000
        assert is_first is True
        assert is_last is True
        assert data == b"data"

    def test_first_fragment_of_a_multi_fragment_frame(self):
        body = struct.pack("<H", 0) + b"part1"
        _frag_num, is_first, is_last, data = ivtp.parse_video_fragment(body)
        assert is_first is True
        assert is_last is False
        assert data == b"part1"

    def test_middle_fragment(self):
        body = struct.pack("<H", 5) + b"part2"
        _frag_num, is_first, is_last, _data = ivtp.parse_video_fragment(body)
        assert is_first is False
        assert is_last is False

    def test_last_fragment_of_a_multi_fragment_frame(self):
        body = struct.pack("<H", 0x8000 | 7) + b"tail"
        _frag_num, is_first, is_last, _data = ivtp.parse_video_fragment(body)
        assert is_first is False
        assert is_last is True

    def test_short_body_is_a_classified_protocol_error(self):
        with pytest.raises(ProtocolError):
            ivtp.parse_video_fragment(b"\x00")


class TestFrameReassembler:
    def test_single_fragment_frame(self):
        reassembler = ivtp.FrameReassembler()
        frame = reassembler.feed(0x8000, b"hello")
        assert frame == b"hello"

    def test_multi_fragment_frame_in_order(self):
        reassembler = ivtp.FrameReassembler()
        assert reassembler.feed(0, b"AAA") is None
        assert reassembler.feed(1, b"BBB") is None
        frame = reassembler.feed(0x8000 | 2, b"CCC")
        assert frame == b"AAABBBCCC"

    def test_new_first_fragment_resets_any_in_progress_buffer(self):
        reassembler = ivtp.FrameReassembler()
        assert reassembler.feed(0, b"stale") is None
        # A fresh "first fragment" arrives before the stale frame ever completed --
        # e.g. the BMC restarted a full-screen refresh mid-stream.
        frame = reassembler.feed(0x8000, b"fresh")
        assert frame == b"fresh"

    def test_reassembler_can_be_reused_across_frames(self):
        reassembler = ivtp.FrameReassembler()
        first = reassembler.feed(0x8000, b"frame1")
        second_partial = reassembler.feed(0, b"frame2-")
        second = reassembler.feed(0x8000 | 1, b"part2")
        assert first == b"frame1"
        assert second_partial is None
        assert second == b"frame2-part2"

    def test_oversized_frame_is_a_classified_protocol_error(self, monkeypatch):
        monkeypatch.setattr(ivtp, "MAX_FRAME_BYTES", 8)
        reassembler = ivtp.FrameReassembler()
        with pytest.raises(ProtocolError):
            reassembler.feed(0x8000, b"way too many bytes for the cap")


# ===========================================================================
# SocketTransport: read/write semantics over a fake, in-memory Transport-like
# socket, plus one real (localhost-only, no BMC involved) connect failure.
# ===========================================================================


class _FakeSocket:
    """A minimal stand-in for ``socket.socket`` driving :class:`ivtp.SocketTransport`."""

    def __init__(self, rx: bytes = b"", *, raise_on_recv: Exception | None = None):
        self._rx = rx
        self._pos = 0
        self.sent = bytearray()
        self._raise_on_recv = raise_on_recv
        self.closed = False

    def recv(self, n: int) -> bytes:
        if self._raise_on_recv is not None:
            raise self._raise_on_recv
        chunk = self._rx[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, seconds) -> None:  # matching socket.socket's own signature.
        self.timeout = seconds

    def close(self) -> None:
        self.closed = True


class TestSocketTransport:
    def test_recv_exact_returns_requested_bytes(self):
        transport = ivtp.SocketTransport(_FakeSocket(b"abcdef"))
        assert transport.recv_exact(3) == b"abc"
        assert transport.recv_exact(3) == b"def"

    def test_recv_exact_zero_bytes_short_circuits(self):
        transport = ivtp.SocketTransport(_FakeSocket(b""))
        assert transport.recv_exact(0) == b""

    def test_recv_exact_raises_eof_on_peer_close(self):
        transport = ivtp.SocketTransport(_FakeSocket(b"ab"))
        with pytest.raises(EOFError):
            transport.recv_exact(5)

    def test_recv_exact_wraps_timeout_as_timeout_error(self):
        transport = ivtp.SocketTransport(_FakeSocket(raise_on_recv=TimeoutError()))
        with pytest.raises(TimeoutError_):
            transport.recv_exact(4)

    def test_recv_exact_wraps_os_error_as_connection_error(self):
        transport = ivtp.SocketTransport(_FakeSocket(raise_on_recv=OSError("boom")))
        with pytest.raises(ConnectionError_):
            transport.recv_exact(4)

    def test_send_all_wraps_os_error_as_connection_error(self):
        class _RaisingSocket(_FakeSocket):
            def sendall(self, data: bytes) -> None:
                raise OSError("boom")

        transport = ivtp.SocketTransport(_RaisingSocket())
        with pytest.raises(ConnectionError_):
            transport.send_all(b"x")

    def test_close_is_idempotent_and_swallows_os_error(self):
        class _RaisingCloseSocket(_FakeSocket):
            def close(self) -> None:
                raise OSError("already closed")

        transport = ivtp.SocketTransport(_RaisingCloseSocket())
        transport.close()  # must not raise

    def test_connect_to_a_closed_local_port_is_a_classified_connection_error(self):
        # Bind a socket to get a genuinely free localhost port, then close it immediately so the
        # connect below is guaranteed to be refused -- localhost-only, no BMC or other network
        # host is ever contacted.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        _host, port = probe.getsockname()
        probe.close()

        with pytest.raises(ConnectionError_):
            ivtp.SocketTransport.connect("127.0.0.1", port, timeout=2.0)


# ===========================================================================
# read_packet / write_packet
# ===========================================================================


class _QueueTransport:
    """A :class:`ivtp.Transport` backed by an in-memory byte queue, for pure (socket-free) tests
    of the handshake state machine.
    """

    def __init__(self, rx: bytes = b"") -> None:
        self._rx = rx
        self._pos = 0
        self.sent: list[bytes] = []
        self.timeout: float | None = None

    def recv_exact(self, n: int) -> bytes:
        if n == 0:
            return b""
        chunk = self._rx[self._pos : self._pos + n]
        if len(chunk) < n:
            raise EOFError("out of fixture data")
        self._pos += n
        return chunk

    def send_all(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def set_timeout(self, seconds: float | None) -> None:
        self.timeout = seconds

    def close(self) -> None:
        pass


class TestReadPacket:
    def test_reads_header_and_body(self):
        payload = struct.pack("<H", 0x8000) + b"xyz"
        wire = ivtp.Header(type=ivtp.OP_VIDEO_FRAGMENT, pkt_size=len(payload)).marshal() + payload
        transport = _QueueTransport(wire)
        header, body = ivtp.read_packet(transport)
        assert header.type == ivtp.OP_VIDEO_FRAGMENT
        assert body == payload

    def test_bodyless_packet(self):
        wire = ivtp.Header(type=ivtp.OP_RESUME_REDIRECTION, pkt_size=0).marshal()
        transport = _QueueTransport(wire)
        header, body = ivtp.read_packet(transport)
        assert header.type == ivtp.OP_RESUME_REDIRECTION
        assert body == b""

    def test_implausible_pkt_size_is_a_classified_protocol_error(self):
        wire = ivtp.Header(type=ivtp.OP_VIDEO_FRAGMENT, pkt_size=ivtp.MAX_FRAME_BYTES + 1).marshal()
        transport = _QueueTransport(wire)
        with pytest.raises(ProtocolError):
            ivtp.read_packet(transport)


# ===========================================================================
# open_channel(): handshake state machine, every rejection path
# ===========================================================================


def _greeting(body: bytes = b"") -> bytes:
    return ivtp.Header(type=ivtp.OP_SESSION_ACCEPTED, pkt_size=len(body)).marshal() + body


def _validate_response(status: int, sub_status: int | None = None) -> bytes:
    body = bytes([status]) if sub_status is None else bytes([status, sub_status])
    return ivtp.Header(type=ivtp.OP_VALIDATE_VIDEO_SESSION_RESPONSE, pkt_size=len(body)).marshal() + body


class TestOpenChannel:
    def test_happy_path_sends_get_web_token_then_validate_then_resume(self):
        rx = _greeting() + _validate_response(ivtp.SESSION_VALID)
        transport = _QueueTransport(rx)
        facts = ivtp.open_channel(transport, token=TOKEN, client_ip="10.0.0.5", username="ansible")

        assert facts.session_accepted is True
        assert facts.validate_status == ivtp.SESSION_VALID
        assert facts.validate_status_name == "valid_session"
        assert facts.resumed is True
        assert facts.validate_sub_status is None

        # First send is GET_WEB_TOKEN (send_get_web_token defaults True), second is
        # VALIDATE_VIDEO_SESSION, third is the bodyless RESUME_REDIRECTION.
        assert len(transport.sent) == 3
        assert ivtp.Header.parse(transport.sent[0][: ivtp.HEADER_LEN]).type == ivtp.OP_GET_WEB_TOKEN
        assert ivtp.Header.parse(transport.sent[1][: ivtp.HEADER_LEN]).type == ivtp.OP_VALIDATE_VIDEO_SESSION
        assert transport.sent[2] == ivtp.build_resume_redirection()

    def test_send_get_web_token_false_skips_that_packet(self):
        rx = _greeting() + _validate_response(ivtp.SESSION_VALID)
        transport = _QueueTransport(rx)
        ivtp.open_channel(transport, token=TOKEN, client_ip="10.0.0.5", username="ansible", send_get_web_token=False)
        assert len(transport.sent) == 2
        assert ivtp.Header.parse(transport.sent[0][: ivtp.HEADER_LEN]).type == ivtp.OP_VALIDATE_VIDEO_SESSION

    def test_greeting_body_len_is_reported(self):
        rx = _greeting(b"\x00" * 48) + _validate_response(ivtp.SESSION_VALID)
        transport = _QueueTransport(rx)
        facts = ivtp.open_channel(transport, token=TOKEN, client_ip="10.0.0.5", username="ansible")
        assert facts.greeting_body_len == 48

    def test_sub_status_is_carried_through(self):
        rx = _greeting() + _validate_response(ivtp.SESSION_VALID, sub_status=0x7)
        transport = _QueueTransport(rx)
        facts = ivtp.open_channel(transport, token=TOKEN, client_ip="10.0.0.5", username="ansible")
        assert facts.validate_sub_status == 0x7

    def test_wrong_greeting_opcode_is_a_classified_protocol_error(self):
        wrong_greeting = ivtp.Header(type=ivtp.OP_BLANK_SCREEN, pkt_size=0).marshal()
        transport = _QueueTransport(wrong_greeting)
        with pytest.raises(ProtocolError):
            ivtp.open_channel(transport, token=TOKEN, client_ip="10.0.0.5", username="ansible")

    def test_invalid_token_is_rejected_as_authentication_error(self):
        rx = _greeting() + _validate_response(ivtp.SESSION_INVALID_VIDEO_TOKEN)
        transport = _QueueTransport(rx)
        with pytest.raises(AuthenticationError):
            ivtp.open_channel(transport, token=TOKEN, client_ip="10.0.0.5", username="ansible")

    def test_kvm_disabled_is_rejected_as_unsupported_capability(self):
        rx = _greeting() + _validate_response(ivtp.SESSION_KVM_DISABLED)
        transport = _QueueTransport(rx)
        with pytest.raises(UnsupportedCapabilityError):
            ivtp.open_channel(transport, token=TOKEN, client_ip="10.0.0.5", username="ansible")

    def test_unsolicited_stop_session_before_validate_response_is_classified(self):
        rx = _greeting() + ivtp.Header(type=ivtp.OP_STOP_SESSION_IMMEDIATE, pkt_size=0, status=ivtp.STOP_WEB_LOGOUT).marshal()
        transport = _QueueTransport(rx)
        with pytest.raises(AuthenticationError):
            ivtp.open_channel(transport, token=TOKEN, client_ip="10.0.0.5", username="ansible")

    def test_intervening_unrelated_packets_are_tolerated_and_skipped(self):
        # A BLANK_SCREEN push and an ENCRYPTION_STATUS push arrive between the greeting and the
        # validate response -- open_channel must not choke on either.
        blank = ivtp.Header(type=ivtp.OP_BLANK_SCREEN, pkt_size=0).marshal()
        enc_status = ivtp.Header(type=ivtp.OP_ENCRYPTION_STATUS, pkt_size=1, status=0).marshal() + b"\x00"
        rx = _greeting() + blank + enc_status + _validate_response(ivtp.SESSION_VALID)
        transport = _QueueTransport(rx)
        facts = ivtp.open_channel(transport, token=TOKEN, client_ip="10.0.0.5", username="ansible")
        assert facts.validate_status == ivtp.SESSION_VALID

    def test_no_token_reaches_the_returned_facts(self):
        rx = _greeting() + _validate_response(ivtp.SESSION_VALID)
        transport = _QueueTransport(rx)
        facts = ivtp.open_channel(transport, token=TOKEN, client_ip="10.0.0.5", username="ansible")
        field_names = {f.name for f in dataclasses.fields(facts)}
        assert not any("token" in name for name in field_names)
        assert TOKEN not in repr(facts)
        assert TOKEN not in str(facts)


# ===========================================================================
# capture_one_frame()
# ===========================================================================


class TestCaptureOneFrame:
    def test_reassembles_a_single_fragment_frame(self):
        frag = struct.pack("<H", 0x8000) + b"one-frame-of-bytes"
        rx = ivtp.Header(type=ivtp.OP_VIDEO_FRAGMENT, pkt_size=len(frag)).marshal() + frag
        transport = _QueueTransport(rx)
        frame = ivtp.capture_one_frame(transport, request_refresh=False)
        assert frame == b"one-frame-of-bytes"

    def test_sends_refresh_video_screen_by_default(self):
        frag = struct.pack("<H", 0x8000) + b"data"
        rx = ivtp.Header(type=ivtp.OP_VIDEO_FRAGMENT, pkt_size=len(frag)).marshal() + frag
        transport = _QueueTransport(rx)
        ivtp.capture_one_frame(transport)
        assert transport.sent == [ivtp.build_refresh_video_screen()]

    def test_request_refresh_false_sends_nothing(self):
        frag = struct.pack("<H", 0x8000) + b"data"
        rx = ivtp.Header(type=ivtp.OP_VIDEO_FRAGMENT, pkt_size=len(frag)).marshal() + frag
        transport = _QueueTransport(rx)
        ivtp.capture_one_frame(transport, request_refresh=False)
        assert transport.sent == []

    def test_reassembles_a_multi_fragment_frame_skipping_unrelated_packets(self):
        frag1 = struct.pack("<H", 0) + b"AAA"
        frag2 = struct.pack("<H", 0x8000 | 1) + b"BBB"
        blank = ivtp.Header(type=ivtp.OP_BLANK_SCREEN, pkt_size=0).marshal()
        rx = (
            blank
            + ivtp.Header(type=ivtp.OP_VIDEO_FRAGMENT, pkt_size=len(frag1)).marshal()
            + frag1
            + ivtp.Header(type=ivtp.OP_VIDEO_FRAGMENT, pkt_size=len(frag2)).marshal()
            + frag2
        )
        transport = _QueueTransport(rx)
        frame = ivtp.capture_one_frame(transport, request_refresh=False)
        assert frame == b"AAABBB"

    def test_stop_session_while_waiting_for_a_frame_is_classified(self):
        rx = ivtp.Header(type=ivtp.OP_STOP_SESSION_IMMEDIATE, pkt_size=0, status=ivtp.STOP_TIMED_OUT).marshal()
        transport = _QueueTransport(rx)
        with pytest.raises(TimeoutError_):
            ivtp.capture_one_frame(transport, request_refresh=False)


# ===========================================================================
# End-to-end: open_channel() + capture_one_frame() over a real socket.socketpair()
# ===========================================================================


def _recv_exact_raw(sock: socket.socket, n: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < n:
        chunk = sock.recv(n - len(chunks))
        if not chunk:
            raise EOFError("fake BMC: peer closed early")
        chunks += chunk
    return bytes(chunks)


def _fake_bmc(server_sock: socket.socket, *, validate_status: int, frame_payload: bytes) -> None:
    """Plays the BMC side of one full handshake, using raw socket I/O (not ivtp.SocketTransport,
    so this genuinely exercises two independent implementations of the same wire format talking
    to each other).
    """
    server_sock.sendall(ivtp.Header(type=ivtp.OP_SESSION_ACCEPTED, pkt_size=0).marshal())

    header = ivtp.Header.parse(_recv_exact_raw(server_sock, ivtp.HEADER_LEN))
    if header.type == ivtp.OP_GET_WEB_TOKEN:
        _recv_exact_raw(server_sock, header.pkt_size)  # discard the echoed token.
        header = ivtp.Header.parse(_recv_exact_raw(server_sock, ivtp.HEADER_LEN))
    assert header.type == ivtp.OP_VALIDATE_VIDEO_SESSION
    _recv_exact_raw(server_sock, header.pkt_size)

    server_sock.sendall(ivtp.Header(type=ivtp.OP_VALIDATE_VIDEO_SESSION_RESPONSE, pkt_size=1).marshal() + bytes([validate_status]))
    if validate_status != ivtp.SESSION_VALID:
        return

    header = ivtp.Header.parse(_recv_exact_raw(server_sock, ivtp.HEADER_LEN))
    assert header.type == ivtp.OP_RESUME_REDIRECTION

    header = ivtp.Header.parse(_recv_exact_raw(server_sock, ivtp.HEADER_LEN))
    assert header.type == ivtp.OP_REFRESH_VIDEO_SCREEN

    mid = len(frame_payload) // 2
    frag1 = struct.pack("<H", 0) + frame_payload[:mid]
    frag2 = struct.pack("<H", 0x8000 | 1) + frame_payload[mid:]
    server_sock.sendall(ivtp.Header(type=ivtp.OP_VIDEO_FRAGMENT, pkt_size=len(frag1)).marshal() + frag1)
    server_sock.sendall(ivtp.Header(type=ivtp.OP_VIDEO_FRAGMENT, pkt_size=len(frag2)).marshal() + frag2)


class TestEndToEndOverSocketpair:
    def test_full_handshake_and_one_frame_capture(self):
        client_sock, server_sock = socket.socketpair()
        frame_payload = b"the quick brown fox jumps over the lazy dog"
        thread = threading.Thread(target=_fake_bmc, args=(server_sock,), kwargs={"validate_status": ivtp.SESSION_VALID, "frame_payload": frame_payload})
        thread.start()
        try:
            transport = ivtp.SocketTransport(client_sock)
            facts = ivtp.open_channel(transport, token=TOKEN, client_ip="10.0.0.5", username="ansible", handshake_timeout=5.0)
            assert facts.validate_status == ivtp.SESSION_VALID
            assert facts.resumed is True

            frame = ivtp.capture_one_frame(transport, frame_timeout=5.0)
            assert frame == frame_payload
        finally:
            thread.join(timeout=5)
            client_sock.close()
            server_sock.close()

    def test_rejected_session_over_the_real_socket_pair(self):
        client_sock, server_sock = socket.socketpair()
        thread = threading.Thread(target=_fake_bmc, args=(server_sock,), kwargs={"validate_status": ivtp.SESSION_INVALID_VIDEO_TOKEN, "frame_payload": b""})
        thread.start()
        try:
            transport = ivtp.SocketTransport(client_sock)
            with pytest.raises(AuthenticationError):
                ivtp.open_channel(transport, token=TOKEN, client_ip="10.0.0.5", username="ansible", handshake_timeout=5.0)
        finally:
            thread.join(timeout=5)
            client_sock.close()
            server_sock.close()

    def test_no_token_appears_on_the_wire_in_cleartext_outside_the_expected_field(self):
        # The token DOES appear on the wire -- that is the whole point of the auth handshake --
        # but it must appear exactly once, inside the packets this module explicitly builds for
        # that purpose (GET_WEB_TOKEN's body and VALIDATE_VIDEO_SESSION's token field), never
        # smeared elsewhere or duplicated unexpectedly.
        client_sock, server_sock = socket.socketpair()
        frame_payload = b"frame"
        thread = threading.Thread(target=_fake_bmc, args=(server_sock,), kwargs={"validate_status": ivtp.SESSION_VALID, "frame_payload": frame_payload})
        thread.start()
        try:
            transport = ivtp.SocketTransport(client_sock)
            ivtp.open_channel(transport, token=TOKEN, client_ip="10.0.0.5", username="ansible", handshake_timeout=5.0)
            frame = ivtp.capture_one_frame(transport, frame_timeout=5.0)
            assert TOKEN not in frame.decode("ascii", errors="replace")
        finally:
            thread.join(timeout=5)
            client_sock.close()
            server_sock.close()
