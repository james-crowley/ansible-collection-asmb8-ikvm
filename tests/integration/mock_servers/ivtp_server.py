# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Deterministic mock IVTP KVM/console endpoint for integration testing.

Plays the **BMC** side of the wire protocol that ``plugins/module_utils/ivtp.py``
speaks over port 7578 (plaintext, on the target hardware's current
configuration) -- the mirror image of ``iusb_server.py``'s ``IusbMockServer``
for the virtual-media channel, and deliberately built to the same idiom:
standard library only, binds loopback only, exposes a scripted/driving API a
test calls step by step, and a small, self-contained fault-injection surface
rather than a monolithic "run everything" method.

Provenance and verification status -- READ THIS BEFORE TRUSTING ANYTHING HERE
-------------------------------------------------------------------------------

``iusb_server.py``'s own docstring can mark a default as ``VERIFIED LIVE``
because a real capture against the target board exists for the iUSB
protocol. **No such capture exists for IVTP beyond the bare 8-byte greeting**
(see ``docs/protocol-notes.md`` §7 and ``docs/capability-matrix.md``'s Tier 4
entry for ``asmb8_console``/``ivtp.py``). Every wire-format fact this file
bakes in -- the header shape, the handshake sequence, every opcode and status
value, the video-fragment bit convention -- is sourced from a **local
decompilation of the vendor's own JViewer client for this exact board**
(Tier 1 in the capability matrix: "verified against an authoritative source",
*not* "verified against real firmware"). This mock's entire purpose is to let
a test prove the real shipped client (``plugins/module_utils/ivtp.py``)
*self-consistently implements that decompiled understanding* -- i.e. that the
state machine in ``ivtp.open_channel``/``ivtp.capture_one_frame`` matches what
this file, built independently from the same decompiled source, expects on
the wire. That is Tier 2 evidence ("unit/mock tested"). It is **not**, and
must never be cited as, Tier 3 evidence that the real board actually behaves
this way. A mock that is internally consistent with a real client is not the
same claim as a live capture -- see this collection's own
``docs/capability-matrix.md`` for why that distinction matters everywhere in
this project, not just here.

One deliberate, flagged departure from the decompiled source is carried over
from ``ivtp.py`` itself: ``VALIDATE_VIDEO_SESSION``'s body is 324 bytes, and
this mock enforces that self-consistent length (matching what the real
client actually writes into the packet's own ``pktSize`` header field),
**not** the 332-byte total-wire-length value the decompiled vendor client's
own packet-building method inconsistently writes for this one packet type.
See ``plugins/module_utils/ivtp.py``'s module docstring, disagreement 2, for
the full reasoning -- this remains the single biggest live-hardware
verification target in this collection's IVTP support, and nothing in this
mock changes that; it only lets a test observe that the client and this mock
agree on the same (unverified) choice.
"""

from __future__ import annotations

import dataclasses
import secrets
import socket
import struct
import threading
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Wire format constants -- all [decompiled vendor client] per this module's
# own docstring, unless noted otherwise. Named to match
# com.ami.kvm.jviewer.kvmpkts.IVTPPktHdr's own constants, mirroring
# plugins/module_utils/ivtp.py's own OP_* naming exactly, but defined here
# independently (this file imports nothing from the collection's own plugin
# tree) so a bug shared between the two would not silently cancel out.
# --------------------------------------------------------------------------

HEADER_LEN = 8
_HEADER_STRUCT = struct.Struct("<HIH")

OP_HID_PKT = 1
OP_REFRESH_VIDEO_SCREEN = 5
OP_RESUME_REDIRECTION = 6
OP_STOP_SESSION_IMMEDIATE = 8
OP_BLANK_SCREEN = 9
OP_ENCRYPTION_STATUS = 14
OP_VALIDATE_VIDEO_SESSION = 18
OP_VALIDATE_VIDEO_SESSION_RESPONSE = 19
OP_GET_WEB_TOKEN = 21
OP_SESSION_ACCEPTED = 23
OP_VIDEO_FRAGMENT = 25

#: VALIDATE_VIDEO_SESSION_RESPONSE status byte (KVMClient's own named constants).
SESSION_INVALID = 0
SESSION_VALID = 1
SESSION_KVM_DISABLED = 2
SESSION_INVALID_VIDEO_TOKEN = 3
SESSION_INVALID_CDROM_TOKEN = 4
SESSION_INVALID_FLOPPY_TOKEN = 5

#: STOP_SESSION_IMMEDIATE reason byte (KVMClient's own named constants).
STOP_WEB_LOGOUT = 7
STOP_LICENSE_EXPIRED = 8
STOP_TIMED_OUT = 9
STOP_KVM_DISCONNECT = 10

#: VIDEO_FRAGMENT's 2-byte little-endian fragment-number prefix convention.
#: Independently corroborated by the MIT reference implementation's own
#: read loop for a different board -- see ivtp.py's module docstring.
FRAG_LAST_BIT = 0x8000
FRAG_NUM_MASK = 0x7FFF

#: VALIDATE_VIDEO_SESSION body layout -- see this module's own docstring for
#: why this mock uses 324 (self-consistent), not the decompiled client's own
#: inconsistent 332, as the value it expects/enforces.
VIDEO_TOKEN_FIELD_LEN = 130  # 1 type byte + 129 bytes of zero-padded ASCII token.
VIDEO_IP_FIELD_LEN = 65
VIDEO_USERNAME_FIELD_LEN = 129
VIDEO_PACKET_SIZE = VIDEO_TOKEN_FIELD_LEN + VIDEO_IP_FIELD_LEN + VIDEO_USERNAME_FIELD_LEN  # 324

TOKEN_TYPE_WEB_SESSION = 0

#: The exact 8 bytes captured live against the target board (docs/protocol-notes.md
#: §7): an unsolicited SESSION_ACCEPTED greeting, no body. This IS Tier 3
#: evidence -- the one live-captured IVTP fact -- which is why this mock's
#: default greeting reproduces it exactly rather than being an arbitrary choice.
LIVE_GREETING_BYTES = bytes.fromhex("1700000000000000")

DEFAULT_EXPECTED_TOKEN = "kvmtoken-mock-test"  # obviously-fake fixture default, not a real credential


class ProtocolViolation(Exception):
    """The peer (or this mock's own caller) violated the wire framing this mock enforces."""


# --------------------------------------------------------------------------
# Header codec
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Header:
    """An 8-byte IVTP packet header: type (u16 LE), pktSize (u32 LE), status (u16 LE)."""

    type: int
    pkt_size: int = 0
    status: int = 0

    def marshal(self) -> bytes:
        return _HEADER_STRUCT.pack(self.type & 0xFFFF, self.pkt_size & 0xFFFFFFFF, self.status & 0xFFFF)

    @staticmethod
    def parse(data: bytes) -> Header:
        if len(data) < HEADER_LEN:
            raise ProtocolViolation(f"ivtp: short header ({len(data)} bytes, need {HEADER_LEN})")
        type_, pkt_size, status = _HEADER_STRUCT.unpack_from(data, 0)
        return Header(type=type_, pkt_size=pkt_size, status=status)


# --------------------------------------------------------------------------
# VALIDATE_VIDEO_SESSION (18) body parsing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedValidateRequest:
    """A decoded VALIDATE_VIDEO_SESSION body, as this mock (playing the BMC) sees it."""

    token_type: int
    token: str
    client_ip: str
    username: str


def _strip_nul_padding(field_bytes: bytes) -> str:
    nul = field_bytes.find(0)
    if nul >= 0:
        field_bytes = field_bytes[:nul]
    return field_bytes.decode("ascii", errors="replace")


def parse_validate_video_session_body(body: bytes) -> ParsedValidateRequest:
    """Parse a client's VALIDATE_VIDEO_SESSION body against the 324-byte layout
    this mock enforces (see this module's docstring for the 324-vs-332 note)."""
    if len(body) != VIDEO_PACKET_SIZE:
        raise ProtocolViolation(f"ivtp mock: VALIDATE_VIDEO_SESSION body is {len(body)} bytes, expected {VIDEO_PACKET_SIZE}")
    token_type = body[0]
    token = _strip_nul_padding(body[1:VIDEO_TOKEN_FIELD_LEN])
    ip_end = VIDEO_TOKEN_FIELD_LEN + VIDEO_IP_FIELD_LEN
    client_ip = _strip_nul_padding(body[VIDEO_TOKEN_FIELD_LEN:ip_end])
    username = _strip_nul_padding(body[ip_end : ip_end + VIDEO_USERNAME_FIELD_LEN])
    return ParsedValidateRequest(token_type=token_type, token=token, client_ip=client_ip, username=username)


# --------------------------------------------------------------------------
# Fault injection
# --------------------------------------------------------------------------


@dataclass
class IvtpFaultConfig:
    """Every fault-injection knob this mock understands. All one-shot: each
    fires once, on the very next frame this mock sends, then resets itself --
    matching how a test uses them ("the next frame should be broken this
    way"), and mirroring ``iusb_server.IusbFaultConfig``'s own convention.
    """

    #: Truncate the very next frame this mock sends to exactly this many
    #: bytes (of the full header+body), rather than the real length. A value
    #: below :data:`HEADER_LEN` models a truncated *header*; a value at or
    #: above it but below the frame's real total length models a truncated
    #: *body*. Pair with ``disconnect_after_next_send`` for a realistic "the
    #: BMC cut me off mid-frame" scenario -- otherwise the connection stays
    #: open and a real client's blocking read simply waits for bytes that
    #: were never coming, rather than observing the disconnect.
    truncate_next_frame_to: int | None = None

    #: Declare a different ``pktSize`` in the header of the very next frame
    #: than the number of body bytes actually sent -- "a frame whose pktSize
    #: disagrees with the bytes actually sent." Pair with
    #: ``disconnect_after_next_send`` to force a real client's blocking read
    #: (which trusts the declared ``pktSize`` and waits for exactly that many
    #: body bytes) to observe an EOF rather than hang forever.
    lie_next_pkt_size: int | None = None

    #: Close the connection immediately after sending the next frame.
    disconnect_after_next_send: bool = False


# --------------------------------------------------------------------------
# Socket helpers
# --------------------------------------------------------------------------


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    if n == 0:
        return b""
    chunks = bytearray()
    while len(chunks) < n:
        chunk = sock.recv(n - len(chunks))
        if not chunk:
            raise ProtocolViolation(f"ivtp mock: connection closed after {len(chunks)}/{n} bytes")
        chunks += chunk
    return bytes(chunks)


_ACCEPT_POLL_SECONDS = 0.25
_SHUTDOWN_JOIN_SECONDS = 5.0


class IvtpMockServer:
    """Threaded mock IVTP KVM/console endpoint, playing the BMC.

    Use as a context manager::

        with IvtpMockServer(expected_token="tok") as server:
            transport = ivtp.SocketTransport.connect("127.0.0.1", server.port, timeout=5.0)
            server.wait_for_connection()
            request = server.recv_validate_video_session()
            server.send_validate_response(SESSION_VALID)
            server.recv_resume_redirection()
            server.send_video_frame(b"one complete raw frame")

    Binds an ephemeral TCP port on 127.0.0.1 only. Accepts exactly one
    connection per server instance (the real board's `kvm` service tolerates
    several concurrent sessions per ``docs/protocol-notes.md`` -- see that
    file's note that even this capacity claim is unsourced -- but nothing
    about this collection's own client, or the tests this mock exists to
    support, needs more than one connection at a time to prove the handshake
    state machine is self-consistent).

    Sends the greeting (``SESSION_ACCEPTED``) automatically the moment a
    connection is accepted, matching the real board's own unsolicited-greeting
    behaviour (see ``open_channel``'s docstring: the greeting is the first
    thing the client ever reads, before it has sent anything at all).
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        expected_token: str = DEFAULT_EXPECTED_TOKEN,
        greeting_body: bytes = b"",
    ) -> None:
        self.host = host
        self.expected_token = expected_token
        self.greeting_body = greeting_body

        self.faults = IvtpFaultConfig()

        self.port: int | None = None
        self._listen_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._conn: socket.socket | None = None
        self._stop_event = threading.Event()
        self._connected_event = threading.Event()
        self._accept_error: Exception | None = None

        #: Populated by :meth:`recv_validate_video_session`, for a test to
        #: inspect without re-deriving it.
        self.received_get_web_token: bool = False
        self.get_web_token_body: bytes | None = None
        self.last_validate_request: ParsedValidateRequest | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> IvtpMockServer:
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        raw_sock.bind((self.host, 0))
        raw_sock.listen(1)
        raw_sock.settimeout(_ACCEPT_POLL_SECONDS)
        self.port = raw_sock.getsockname()[1]
        self._listen_sock = raw_sock
        self._accept_thread = threading.Thread(target=self._accept_loop, name="ivtp-mock-accept", daemon=True)
        self._accept_thread.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()
        for sock in (self._conn, self._listen_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=_SHUTDOWN_JOIN_SECONDS)
        self._listen_sock = None
        self._accept_thread = None

    def __enter__(self) -> IvtpMockServer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                conn, _addr = self._listen_sock.accept()  # type: ignore[union-attr]
            except TimeoutError:
                continue
            except OSError:
                return
            self._conn = conn
            try:
                self.send_greeting(self.greeting_body)
            except (ProtocolViolation, OSError) as exc:
                self._accept_error = exc
            self._connected_event.set()
            return  # single-connection mock: one accept is all this server ever does.

    def wait_for_connection(self, timeout: float = 5.0) -> None:
        if not self._connected_event.wait(timeout):
            raise TimeoutError("ivtp mock: no client connected in time")
        if self._accept_error is not None:
            raise self._accept_error

    # -- reading from the client ----------------------------------------------

    def recv_client_packet(self, timeout: float = 5.0) -> tuple[Header, bytes]:
        """Read exactly one packet the client sent. Does not interpret it --
        a test or a higher-level helper below decides what to do with the
        opcode."""
        if self._conn is None:
            raise ProtocolViolation("ivtp mock: no connection to read from")
        self._conn.settimeout(timeout)
        try:
            header = Header.parse(_recv_exact(self._conn, HEADER_LEN))
            body = _recv_exact(self._conn, header.pkt_size) if header.pkt_size else b""
        finally:
            self._conn.settimeout(None)
        return header, body

    def recv_validate_video_session(self, timeout: float = 5.0) -> ParsedValidateRequest:
        """Read packets from the client until VALIDATE_VIDEO_SESSION arrives,
        tolerating (and recording) an optional leading GET_WEB_TOKEN -- mirrors
        the sequence ``ivtp.open_channel`` actually sends. Raises
        :class:`ProtocolViolation` if neither opcode is what arrives."""
        header, body = self.recv_client_packet(timeout)
        self.received_get_web_token = False
        self.get_web_token_body = None
        if header.type == OP_GET_WEB_TOKEN:
            self.received_get_web_token = True
            self.get_web_token_body = body
            header, body = self.recv_client_packet(timeout)
        if header.type != OP_VALIDATE_VIDEO_SESSION:
            raise ProtocolViolation(f"ivtp mock: expected VALIDATE_VIDEO_SESSION (18), got opcode {header.type}")
        parsed = parse_validate_video_session_body(body)
        self.last_validate_request = parsed
        return parsed

    def recv_resume_redirection(self, timeout: float = 5.0) -> None:
        header, _body = self.recv_client_packet(timeout)
        if header.type != OP_RESUME_REDIRECTION:
            raise ProtocolViolation(f"ivtp mock: expected RESUME_REDIRECTION (6), got opcode {header.type}")

    def recv_refresh_video_screen(self, timeout: float = 5.0) -> None:
        header, _body = self.recv_client_packet(timeout)
        if header.type != OP_REFRESH_VIDEO_SCREEN:
            raise ProtocolViolation(f"ivtp mock: expected REFRESH_VIDEO_SCREEN (5), got opcode {header.type}")

    # -- sending to the client -------------------------------------------------

    def send_greeting(self, body: bytes = b"") -> None:
        """BMC -> client, unsolicited: SESSION_ACCEPTED (23). Sent automatically
        on accept (see the class docstring); exposed as its own method so a
        test can also send a second one, or call it with fault injection armed."""
        self._send_frame(Header(type=OP_SESSION_ACCEPTED, pkt_size=len(body)), body)

    def send_validate_response(self, status: int, sub_status: int | None = None) -> None:
        body = bytes([status & 0xFF]) if sub_status is None else bytes([status & 0xFF, sub_status & 0xFF])
        self._send_frame(Header(type=OP_VALIDATE_VIDEO_SESSION_RESPONSE, pkt_size=len(body)), body)

    def send_stop_session(self, reason: int) -> None:
        """BMC -> client, unsolicited, at any point: STOP_SESSION_IMMEDIATE (8)."""
        self._send_frame(Header(type=OP_STOP_SESSION_IMMEDIATE, pkt_size=0, status=reason), b"")

    def send_filler_packet(self, opcode: int, body: bytes = b"") -> None:
        """A push the real client tolerates and discards (LED state, encryption
        status, bandwidth probes, ...) -- for a test proving intervening
        packets do not derail the handshake or the frame-capture read loop."""
        self._send_frame(Header(type=opcode, pkt_size=len(body)), body)

    def send_video_fragment(self, frag_num: int, data: bytes) -> None:
        """Send exactly one VIDEO_FRAGMENT packet, low-level -- for a test that
        wants to interleave something else (e.g. an unsolicited
        STOP_SESSION_IMMEDIATE) between fragments of the same frame."""
        body = struct.pack("<H", frag_num & 0xFFFF) + data
        self._send_frame(Header(type=OP_VIDEO_FRAGMENT, pkt_size=len(body)), body)

    def send_video_frame(self, payload: bytes, *, fragment_size: int = 64) -> list[int]:
        """Split ``payload`` into one or more VIDEO_FRAGMENT packets and send
        them in order, following the fragment-number bit convention
        (``FragNumReader``/``FragReader`` -- see this module's docstring):
        low 15 bits ``0`` marks the first fragment, bit 0x8000 marks the last.
        Returns the list of fragment numbers actually sent, in order."""
        chunks = [payload[i : i + fragment_size] for i in range(0, len(payload), fragment_size)] or [b""]
        frag_nums: list[int] = []
        for index, chunk in enumerate(chunks):
            is_last = index == len(chunks) - 1
            frag_num = index  # index 0 has low-15 bits == 0 -> "first"; any nonzero index -> not first.
            if is_last:
                frag_num |= FRAG_LAST_BIT
            self.send_video_fragment(frag_num, chunk)
            frag_nums.append(frag_num)
        return frag_nums

    def disconnect(self) -> None:
        """Abruptly close the active connection, mid-conversation."""
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None

    # -- fault-injected frame transmission -----------------------------------

    def _send_frame(self, header: Header, body: bytes) -> None:
        """Send one frame, applying and consuming any armed one-shot frame faults."""
        if self._conn is None:
            raise ProtocolViolation("ivtp mock: no connection to send on")

        send_header = header
        if self.faults.lie_next_pkt_size is not None:
            lied_size = self.faults.lie_next_pkt_size
            self.faults.lie_next_pkt_size = None
            send_header = dataclasses.replace(header, pkt_size=lied_size)

        frame = send_header.marshal() + body

        if self.faults.truncate_next_frame_to is not None:
            truncate_to = self.faults.truncate_next_frame_to
            self.faults.truncate_next_frame_to = None
            frame = frame[:truncate_to]

        self._conn.sendall(frame)

        if self.faults.disconnect_after_next_send:
            self.faults.disconnect_after_next_send = False
            self.disconnect()

    def generate_test_token(self, length: int = 16) -> str:
        """Convenience: a random token of the shape real ``-kvmtoken`` values
        take (ASCII alphanumeric). Not used internally; mirrors
        ``iusb_server.IusbMockServer.generate_test_token`` exactly."""
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        return "".join(secrets.choice(alphabet) for _position in range(length))


__all__ = [
    "DEFAULT_EXPECTED_TOKEN",
    "FRAG_LAST_BIT",
    "FRAG_NUM_MASK",
    "HEADER_LEN",
    "LIVE_GREETING_BYTES",
    "OP_BLANK_SCREEN",
    "OP_ENCRYPTION_STATUS",
    "OP_GET_WEB_TOKEN",
    "OP_HID_PKT",
    "OP_REFRESH_VIDEO_SCREEN",
    "OP_RESUME_REDIRECTION",
    "OP_SESSION_ACCEPTED",
    "OP_STOP_SESSION_IMMEDIATE",
    "OP_VALIDATE_VIDEO_SESSION",
    "OP_VALIDATE_VIDEO_SESSION_RESPONSE",
    "OP_VIDEO_FRAGMENT",
    "SESSION_INVALID",
    "SESSION_INVALID_CDROM_TOKEN",
    "SESSION_INVALID_FLOPPY_TOKEN",
    "SESSION_INVALID_VIDEO_TOKEN",
    "SESSION_KVM_DISABLED",
    "SESSION_VALID",
    "STOP_KVM_DISCONNECT",
    "STOP_LICENSE_EXPIRED",
    "STOP_TIMED_OUT",
    "STOP_WEB_LOGOUT",
    "TOKEN_TYPE_WEB_SESSION",
    "VIDEO_IP_FIELD_LEN",
    "VIDEO_PACKET_SIZE",
    "VIDEO_TOKEN_FIELD_LEN",
    "VIDEO_USERNAME_FIELD_LEN",
    "Header",
    "IvtpFaultConfig",
    "IvtpMockServer",
    "ParsedValidateRequest",
    "ProtocolViolation",
    "parse_validate_video_session_body",
]
