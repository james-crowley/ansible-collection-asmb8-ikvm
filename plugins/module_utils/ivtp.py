# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""AMI's proprietary IVTP protocol: the KVM/console (video + keyboard/mouse)
channel's packet framing and handshake, for opening an ASMB8-iKVM BMC's
console-redirection listener (port 7578 by default) headlessly -- no Java, no
JRE, no vendor JViewer client.

Provenance
==========

This module's structure (pure header/body codec separated from a thin
socket-driving layer, mirroring how ``iusb.py`` separates IUSB framing from
its ``Transport``/``SocketTransport``) is carried over from that same file, in
turn derived from ``rd450x-console`` (``BadCoder1337/rd450x-console``,
MIT-licensed, Copyright (c) 2026 Anton Musin -- see ``licenses/MIT.txt``),
specifically:

* ``internal/kvm/ivtp.go`` -- the 8-byte little-endian header shape
  (``type`` u16, ``pktSize`` u32, ``status`` u16) and the general opcode list.
* ``internal/kvm/client.go`` -- the video-fragment reassembly convention
  (a 2-byte little-endian fragment-number prefix; bit 0x8000 marks the last
  fragment of a frame, the low 15 bits being zero marks the first) and the
  overall "read a header, dispatch on ``type``" read-loop shape.
* ``internal/kvm/session.go`` / ``transport.go`` -- the general TCP/TLS dial
  shape and the ``VALIDATE_VIDEO_SESSION`` token/IP/username packet concept.
* ``docs/kvm-protocol.md`` -- the human-readable protocol write-up this file's
  module docstring cross-checks against.

Where the decompiled AMI vendor client for THIS board's exact firmware
(``com.ami.kvm.jviewer.kvmpkts.*``, from ``JViewer.jar``, decompiled locally --
see ``docs/protocol-notes.md``) disagrees with ``rd450x-console``
(a different vendor board and firmware generation), the decompiled source for
this board wins, per this collection's standing policy of preferring
vendor-client ground truth over a third-party reference client for a different
board. Two disagreements were found and are called out at the point they
matter below:

1. ``rd450x-console``'s ``VALIDATE_VIDEO_SESSION`` body is 373 bytes (token
   129B + IP 65B + username 129B + a trailing 49-byte MAC field) and is
   preceded by a bodyless ``CONNECTION_COMPLETE`` (opcode 58) packet, sent in
   response to a ``SESSION_ACCEPTED`` (opcode 23) greeting whose format
   suggested a different reconnect-aware firmware revision.

   The decompiled ``com.ami.kvm.jviewer.kvmpkts.IVTPPktHdr`` for THIS board's
   client defines no opcode 57 (``KEEP_ALIVE``) or 58 (``CONNECTION_COMPLETE``)
   at all, and ``KVMClient.onControlMessage()``'s ``case 23`` handler
   (the ``SESSION_ACCEPTED`` reaction) calls straight into
   ``JViewerApp.OnsendWebsessionToken()``, which sends (optionally) a
   ``GET_WEB_TOKEN`` (21) packet and then ``VALIDATE_VIDEO_SESSION`` (18)
   directly -- no ``CONNECTION_COMPLETE`` in between. This matches the exact
   greeting this collection captured live against the target board (8 bytes:
   ``17 00 00 00 00 00 00 00`` -- type=23, pktSize=0, status=0, with no
   further exchange expected before the client speaks) far better than
   ``rd450x-console``'s reconnect-aware sequence does. :func:`open_channel`
   therefore implements the decompiled board's simpler sequence: greeting ->
   (optional GET_WEB_TOKEN) -> VALIDATE_VIDEO_SESSION -> ... -> RESUME_REDIRECTION.

   ``OnsendWebsessionToken()``'s body layout is 324 bytes (a 130-byte
   token-type+token field, not 129+1 with a leading type byte counted
   separately as ``rd450x-console`` has it -- the arithmetic nets out the
   same 130 bytes either way -- followed by a 65-byte IP field and a
   129-byte username field), with **no trailing MAC field at all**. This is
   what :data:`VIDEO_PACKET_SIZE` and :func:`build_validate_video_session`
   implement.

2. One byte-counting detail in that same decompiled method is treated here as
   a probable defect in the vendor client, NOT reproduced: the header's own
   ``pktSize`` field is written as 332 (the *total* wire length, header
   included) rather than 324 (the body length that actually follows the
   header, and the value every other packet-building method in the same
   decompiled class uses ``pktSize`` to mean). This function instead writes
   ``pktSize = 324``, matching this collection's own precedent in
   ``iusb.py``'s ``Header.marshal()`` for the identical situation (a checksum
   byte the decompiled client computes inconsistently, not reproduced,
   because the field is empirically unenforced by the BMC): using the
   self-consistent value is testable and safe *if* the BMC firmware parses
   this fixed-size packet by its own hardcoded length rather than trusting
   the client's stated ``pktSize`` -- plausible, given ``VIDEO_PACKET_SIZE``
   is documented as fixed, but **not been verified live**. This is the single
   biggest live-hardware verification target in this file; see the module's
   own "Assumptions requiring live-hardware confirmation" note carried in
   this collection's task history.

Everywhere this file cites an exact byte offset or opcode number, it is from
the decompiled ``com.ami.kvm.jviewer.kvmpkts.IVTPPktHdr``/``KVMClient``/
``HeaderReader``/``FragNumReader``/``FragReader``/``CtrlReader`` classes,
unless a comment says otherwise. Nothing in this file was invented to "fill a
gap" -- where the decompiled source and the Go reference are both silent (the
exact behaviour of a session-slot-exhaustion rejection, whether
``GET_WEB_TOKEN`` is actually required, the client-username field's real
effect), this file says so explicitly rather than guessing at a status code
or requirement that has not been sourced from anywhere.
"""

from __future__ import annotations

import socket as _socket
import ssl as _ssl
import struct
from dataclasses import dataclass
from typing import Protocol

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import (
    AuthenticationError,
    ConnectionError_,
    IkvmError,
    InvalidStateError,
    ProtocolError,
    RemoteOperationError,
    TimeoutError_,
    TlsValidationError,
    UnsupportedCapabilityError,
)

# --- Header ------------------------------------------------------------------

#: 8-byte little-endian header: type (u16), pktSize (u32), status (u16).
#: Confirmed byte-for-byte against a live greeting captured from the target
#: board: ``17 00 00 00 00 00 00 00`` decodes as type=0x17 (23,
#: SESSION_ACCEPTED), pktSize=0, status=0 -- see TestHeader's greeting test.
HEADER_LEN = 8
_HEADER_STRUCT = struct.Struct("<HIH")


@dataclass(frozen=True, slots=True)
class Header:
    """A decoded IVTP packet header. See :data:`HEADER_LEN`'s docstring for the wire shape."""

    type: int
    pkt_size: int = 0
    status: int = 0

    def marshal(self) -> bytes:
        return _HEADER_STRUCT.pack(self.type & 0xFFFF, self.pkt_size & 0xFFFFFFFF, self.status & 0xFFFF)

    @staticmethod
    def parse(data: bytes) -> Header:
        if len(data) < HEADER_LEN:
            raise ProtocolError(f"ivtp: short header: {len(data)} bytes (need {HEADER_LEN})", operation="ivtp.parse_header")
        type_, pkt_size, status = _HEADER_STRUCT.unpack_from(data, 0)
        return Header(type=type_, pkt_size=pkt_size, status=status)


# --- Opcodes -----------------------------------------------------------------
#
# Named exactly after com.ami.kvm.jviewer.kvmpkts.IVTPPktHdr's own constants
# (IVTP_* -> OP_*), for this board's exact decompiled client. Every opcode
# AMI's own client defines is listed here for reference even though this
# module only builds/parses the subset a headless handshake-and-one-frame
# client needs -- the rest are recognised by number so a read loop can skip
# their body cleanly rather than treating an unimplemented-but-real opcode as
# a protocol error.

OP_HID_PKT = 1
OP_SET_BANDWIDTH = 2
OP_SET_FPS = 3
OP_PAUSE_REDIRECTION = 4
OP_REFRESH_VIDEO_SCREEN = 5
OP_RESUME_REDIRECTION = 6
OP_SET_COMPRESSION_TYPE = 7
OP_STOP_SESSION_IMMEDIATE = 8
OP_BLANK_SCREEN = 9
OP_GET_USB_MOUSE_MODE = 10
OP_GET_FULL_SCREEN = 11
OP_ENABLE_ENCRYPTION = 12
OP_DISABLE_ENCRYPTION = 13
OP_ENCRYPTION_STATUS = 14
OP_INITIAL_ENCRYPTION_STATUS = 15
OP_BW_DETECT_REQ = 16
OP_BW_DETECT_RESP = 17
OP_VALIDATE_VIDEO_SESSION = 18
OP_VALIDATE_VIDEO_SESSION_RESPONSE = 19
OP_GET_KEYBD_LED = 20
OP_GET_WEB_TOKEN = 21
OP_MAX_SESSION_CLOSING = 22
OP_SESSION_ACCEPTED = 23
OP_MEDIA_STATE = 24
OP_VIDEO_FRAGMENT = 25
OP_WEB_PREVIEWER_SESSION = 26
OP_WEB_PREVIEWER_CAPTURE_STATUS = 27
OP_SET_MOUSE_MODE = 28
OP_KVM_SHARING = 32
OP_KVM_SOCKET_STATUS = 33
OP_POWER_STATUS = 34
OP_POWER_CONTROL_REQUEST = 35
OP_POWER_CONTROL_RESPONSE = 36
OP_CONF_SERVICE_STATUS = 37
OP_MOUSE_MEDIA_INFO = 38
OP_GET_ACTIVE_CLIENTS = 39
OP_GET_USER_MACRO = 40
OP_SET_USER_MACRO = 41
OP_IPMI_REQUEST_PKT = 48
OP_IPMI_RESPONSE_PKT = 49
OP_SET_NEXT_MASTER = 50
OP_DISPLAY_LOCK_SET = 51
OP_DISPLAY_CONTROL_STATUS = 52
OP_MEDIA_LICENSE_STATUS = 53
OP_KVM_DISCONNECT = 54
OP_SET_KBD_LANG = 55
OP_MEDIA_FREE_INSTANCE_STATUS = 56

#: NOT defined by this board's decompiled client at all -- see this module's
#: docstring, disagreement 1. rd450x-console (a different board/firmware) uses
#: 57/58 for KEEP_ALIVE/CONNECTION_COMPLETE; this board's client sends neither.
#: Kept here, commented out rather than assigned, so a future reader searching
#: for "57" or "58" finds this note instead of silence.
# OP_KEEP_ALIVE = 57            # rd450x-console only; absent from this board's client.
# OP_CONNECTION_COMPLETE = 58   # rd450x-console only; absent from this board's client.

TOKEN_TYPE_WEB_SESSION = 0  # IVTPPktHdr.WEB_SESSION_TOKEN
TOKEN_TYPE_SSI = 1  # IVTPPktHdr.SSI_SESSION_TOKEN -- not used by this collection.

# --- VALIDATE_VIDEO_SESSION (18) body -----------------------------------------
#
# Field widths per com.ami.kvm.jviewer.gui.JViewerApp.OnsendWebsessionToken();
# see this module's docstring, disagreement 1, for the byte-offset derivation
# and why this differs from rd450x-console's 373-byte/MAC-bearing shape.

_TOKEN_FIELD_LEN = 130  # 1 type byte + 129 bytes of zero-padded ASCII token.
_IP_FIELD_LEN = 65
_USERNAME_FIELD_LEN = 129
#: Body length following the header for VALIDATE_VIDEO_SESSION: 130+65+129=324.
#: This collection writes this same value into the header's pktSize field
#: (see disagreement 2 above) rather than reproducing the decompiled client's
#: 332 -- unverified against live hardware, flagged explicitly.
VIDEO_PACKET_SIZE = _TOKEN_FIELD_LEN + _IP_FIELD_LEN + _USERNAME_FIELD_LEN


def _fixed_ascii(value: str, width: int, *, field: str, operation: str) -> bytes:
    """Encode ``value`` as ASCII, zero-padded to exactly ``width`` bytes."""
    encoded = value.encode("ascii")
    if len(encoded) > width:
        raise ProtocolError(
            f"ivtp: {field} ({len(encoded)} bytes) does not fit in its {width}-byte fixed-width field",
            operation=operation,
        )
    return encoded + bytes(width - len(encoded))


def build_validate_video_session(*, token: str, client_ip: str, username: str, token_type: int = TOKEN_TYPE_WEB_SESSION) -> bytes:
    """Build the type=18 VALIDATE_VIDEO_SESSION packet: header + 324-byte body.

    ``token`` is the ``-kvmtoken`` minted by fetching ``jviewer.jnlp`` (see
    ``asp.py``'s ``allocate_media_session``) -- the same token virtual media
    uses, per this collection's task brief; NOT a bare ``getsessiontoken.asp``
    result, which is observed live to be useless. This function is a pure
    byte-codec: obtaining the token is the caller's job.

    See this module's docstring for the field layout and the two points where
    this deliberately does not reproduce the decompiled vendor client
    byte-for-byte.
    """
    operation = "ivtp.build_validate_video_session"
    token_field = bytes([token_type & 0xFF]) + _fixed_ascii(token, _TOKEN_FIELD_LEN - 1, field="token", operation=operation)
    ip_field = _fixed_ascii(client_ip, _IP_FIELD_LEN, field="client_ip", operation=operation)
    username_field = _fixed_ascii(username, _USERNAME_FIELD_LEN, field="username", operation=operation)
    body = token_field + ip_field + username_field
    if len(body) != VIDEO_PACKET_SIZE:
        # An internal shape invariant, not caller input -- the three fixed-width
        # fields above must sum to exactly VIDEO_PACKET_SIZE. Raised rather than
        # asserted because `assert` is stripped under `python -O`, which would
        # silently stop checking it in exactly the deployments least able to
        # debug a malformed handshake packet.
        raise AssertionError(f"ivtp: VALIDATE_VIDEO_SESSION body is {len(body)} bytes, expected {VIDEO_PACKET_SIZE}")
    header = Header(type=OP_VALIDATE_VIDEO_SESSION, pkt_size=len(body), status=0)
    return header.marshal() + body


def build_get_web_token(token: str) -> bytes:
    """Build the type=21 GET_WEB_TOKEN packet: header + the raw token bytes, unpadded.

    Sent by the decompiled vendor client (``JViewerApp.OnsendWebsessionToken()``)
    immediately before VALIDATE_VIDEO_SESSION, but only on the
    ``JViewer.isjviewerapp()`` code path (the standalone desktop client, as
    opposed to a browser-embedded "web previewer"). This headless client is
    functionally closest to that standalone path, so :func:`open_channel`
    sends this by default -- but whether the BMC actually requires it, or
    merely tolerates it, has not been confirmed against live hardware; see
    ``open_channel``'s ``send_get_web_token`` parameter.
    """
    body = token.encode("ascii")
    header = Header(type=OP_GET_WEB_TOKEN, pkt_size=len(body), status=0)
    return header.marshal() + body


def _bodyless(opcode: int, *, status: int = 0) -> bytes:
    return Header(type=opcode, pkt_size=0, status=status).marshal()


def build_resume_redirection() -> bytes:
    """Build the bodyless type=6 RESUME_REDIRECTION packet.

    Sent once VALIDATE_VIDEO_SESSION_RESPONSE reports VALID_SESSION -- per
    ``KVMClient.resumeRedirection()`` and ``docs/kvm-protocol.md``'s handshake
    diagram, this is what starts the video stream.
    """
    return _bodyless(OP_RESUME_REDIRECTION)


def build_refresh_video_screen() -> bytes:
    """Build the bodyless type=5 REFRESH_VIDEO_SCREEN packet: request a full-screen redraw.

    Sent by :func:`capture_one_frame` immediately after subscribing, as a
    best-effort way to provoke a frame promptly rather than waiting for
    on-screen activity -- the ASPEED video engine this protocol fronts is
    delta/change-driven (see ``docs/kvm-protocol.md``'s note on SKIP/delta
    tile coding), so an idle host might otherwise never send one within a
    bounded timeout. Not sourced as *required*; sending it is a harmless,
    client-initiated request either way.
    """
    return _bodyless(OP_REFRESH_VIDEO_SCREEN)


def build_stop_session(status: int = 0) -> bytes:
    """Build the bodyless type=8 STOP_SESSION_IMMEDIATE packet, for a clean client-initiated close."""
    return _bodyless(OP_STOP_SESSION_IMMEDIATE, status=status)


# --- VALIDATE_VIDEO_SESSION_RESPONSE (19) -------------------------------------
#
# Status byte values per com.ami.kvm.jviewer.kvmpkts.KVMClient's own named
# int constants (INVALID_SESSION, VALID_SESSION, KVM_DISABLED, ...).

SESSION_INVALID = 0
SESSION_VALID = 1
SESSION_KVM_DISABLED = 2
SESSION_INVALID_VIDEO_TOKEN = 3
SESSION_INVALID_CDROM_TOKEN = 4
SESSION_INVALID_FLOPPY_TOKEN = 5

_VALIDATE_STATUS_NAMES = {
    SESSION_INVALID: "invalid_session",
    SESSION_VALID: "valid_session",
    SESSION_KVM_DISABLED: "kvm_disabled",
    SESSION_INVALID_VIDEO_TOKEN: "invalid_video_session_token",
    SESSION_INVALID_CDROM_TOKEN: "invalid_cdrom_session_token",
    SESSION_INVALID_FLOPPY_TOKEN: "invalid_floppy_session_token",
}


def validate_status_name(status: int) -> str:
    return _VALIDATE_STATUS_NAMES.get(status, f"unknown({status})")


def parse_validate_video_session_response(body: bytes) -> tuple[int, int | None]:
    """Parse a VALIDATE_VIDEO_SESSION_RESPONSE body: byte0 status, byte1 (if present) a sub-status.

    Mirrors ``KVMClient.onControlMessage()``'s ``case 19``: it always reads
    ``m_ctrlMsg.get()`` for the status byte, and reads a second byte only when
    ``pktSize > 1``. The decompiled client never names what the second byte
    means; this function returns it verbatim as ``sub_status`` rather than
    guessing.
    """
    if not body:
        raise ProtocolError(
            "ivtp: VALIDATE_VIDEO_SESSION_RESPONSE body is empty (expected at least 1 status byte)",
            operation="ivtp.parse_validate_video_session_response",
        )
    status = body[0]
    sub_status = body[1] if len(body) > 1 else None
    return status, sub_status


def validate_status_error(status: int, *, endpoint: str | None = None) -> IkvmError:
    """Classify a non-VALID VALIDATE_VIDEO_SESSION_RESPONSE status as this collection's own error taxonomy."""
    name = validate_status_name(status)
    operation = "ivtp.open_channel"
    if status == SESSION_KVM_DISABLED:
        return UnsupportedCapabilityError(
            f"BMC reports the KVM/video channel is disabled for this session (status={status} {name})",
            endpoint=endpoint,
            operation=operation,
        )
    if status in (SESSION_INVALID, SESSION_INVALID_VIDEO_TOKEN, SESSION_INVALID_CDROM_TOKEN, SESSION_INVALID_FLOPPY_TOKEN):
        return AuthenticationError(
            f"BMC rejected the KVM/video session token (status={status} {name})",
            endpoint=endpoint,
            operation=operation,
        )
    return ProtocolError(
        f"BMC returned an unrecognised VALIDATE_VIDEO_SESSION_RESPONSE status {status} ({name})",
        endpoint=endpoint,
        operation=operation,
    )


# --- STOP_SESSION_IMMEDIATE (8) -----------------------------------------------
#
# Status (reason) values per KVMClient's own named int constants
# (STOP_SESSION_CONF_CHANGE, STOP_SESSION_WEB_LOGOUT, ...) and the dialog
# branches in onControlMessage()'s case 8.

STOP_GENERIC = 2  # Shares its numeric value with SESSION_KVM_DISABLED by coincidence in the vendor source; unrelated field.
STOP_CONF_CHANGE = 5
STOP_WEB_LOGOUT = 7
STOP_LICENSE_EXPIRED = 8
STOP_TIMED_OUT = 9
STOP_KVM_DISCONNECT = 10

_STOP_REASON_NAMES = {
    STOP_GENERIC: "generic",
    STOP_CONF_CHANGE: "conf_change",
    STOP_WEB_LOGOUT: "web_logout",
    STOP_LICENSE_EXPIRED: "license_expired",
    STOP_TIMED_OUT: "timed_out",
    STOP_KVM_DISCONNECT: "kvm_disconnect",
}


def stop_reason_name(status: int) -> str:
    return _STOP_REASON_NAMES.get(status, f"unknown({status})")


def stop_session_error(status: int, *, endpoint: str | None = None) -> IkvmError:
    """Classify an unsolicited STOP_SESSION_IMMEDIATE (opcode 8) as this collection's own error taxonomy."""
    name = stop_reason_name(status)
    operation = "ivtp.open_channel"
    if status == STOP_WEB_LOGOUT:
        return AuthenticationError(
            f"BMC stopped the KVM session: the underlying web session was logged out (status={status} {name})",
            endpoint=endpoint,
            operation=operation,
        )
    if status == STOP_LICENSE_EXPIRED:
        return UnsupportedCapabilityError(
            f"BMC stopped the KVM session: KVM licensing has expired (status={status} {name})",
            endpoint=endpoint,
            operation=operation,
        )
    if status == STOP_TIMED_OUT:
        return TimeoutError_(
            f"BMC stopped the KVM session: server-side inactivity timeout (status={status} {name}). This is "
            "the BMC's own 1800s KVM session timeout, distinct from this module's own connect/handshake timeouts.",
            endpoint=endpoint,
            operation=operation,
        )
    if status == STOP_KVM_DISCONNECT:
        return InvalidStateError(
            f"BMC stopped the KVM session: another client requested a KVM disconnect (status={status} {name})",
            endpoint=endpoint,
            operation=operation,
        )
    return RemoteOperationError(
        f"BMC stopped the KVM session (status={status} {name})",
        endpoint=endpoint,
        operation=operation,
    )


# --- VIDEO_FRAGMENT (25) ------------------------------------------------------
#
# Body shape per com.ami.kvm.jviewer.kvmpkts.FragNumReader/FragReader: a
# 2-byte little-endian fragment-number prefix, then the fragment's raw data.
# fragNum's low 15 bits (fragNum & 0x7FFF) == 0 marks the FIRST fragment of a
# new frame (FragReader.initialize()); bit 0x8000 marks the LAST fragment,
# after which the accumulated bytes are a complete, still-encoded frame
# (FragReader.read(), "if 0 != (m_fragNum & 0x8000)"). Cross-checked against
# rd450x-console's client.go readLoop(), which implements the identical
# convention independently -- this is one of the strongest-corroborated facts
# in this file.

_FRAG_NUM_STRUCT = struct.Struct("<H")
_FRAG_LAST_BIT = 0x8000
_FRAG_NUM_MASK = 0x7FFF

#: The decompiled FragReader allocates a fixed 9,216,000-byte reassembly
#: buffer (``new byte[9216000]``) -- used here as the hard cap on a
#: reassembled frame, so a malformed/hostile stream (huge declared size, or an
#: endless run of non-terminal fragments) cannot grow memory without bound.
MAX_FRAME_BYTES = 9_216_000


def parse_video_fragment(body: bytes) -> tuple[int, bool, bool, bytes]:
    """Parse a VIDEO_FRAGMENT body: ``(frag_num, is_first, is_last, data)``."""
    if len(body) < 2:
        raise ProtocolError(
            f"ivtp: VIDEO_FRAGMENT body ({len(body)} bytes) is shorter than the 2-byte fragment-number prefix",
            operation="ivtp.parse_video_fragment",
        )
    (frag_num,) = _FRAG_NUM_STRUCT.unpack_from(body, 0)
    return frag_num, (frag_num & _FRAG_NUM_MASK) == 0, bool(frag_num & _FRAG_LAST_BIT), body[2:]


class FrameReassembler:
    """Accumulates VIDEO_FRAGMENT fragments into complete, still-encoded frames.

    Pure and socket-free: :meth:`feed` takes one fragment's ``(frag_num, data)``
    and returns the complete frame's bytes once the last fragment of it has
    arrived, or ``None`` while a frame is still in progress. The returned
    bytes are the RAW fragment stream concatenated in order -- the AMI/ASPEED
    VQ+JPEG(DCT)+RC4 video codec that would turn this into pixels is not
    implemented by this collection (see ``asmb8_redirection.py``'s
    DOCUMENTATION); this class's job ends at framing, exactly like
    ``HeaderReader``/``FragNumReader``/``FragReader`` in the decompiled
    client, which likewise hand a reassembled buffer off to a separate
    decoder this file does not reimplement.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, frag_num: int, data: bytes) -> bytes | None:
        is_first = (frag_num & _FRAG_NUM_MASK) == 0
        is_last = bool(frag_num & _FRAG_LAST_BIT)
        if is_first:
            self._buf = bytearray()
        if len(self._buf) + len(data) > MAX_FRAME_BYTES:
            raise ProtocolError(f"ivtp: reassembled video frame would exceed {MAX_FRAME_BYTES} bytes", operation="ivtp.capture_one_frame")
        self._buf += data
        if is_last:
            frame = bytes(self._buf)
            self._buf = bytearray()
            return frame
        return None


# --- Transport: the thin, replaceable socket-driving layer --------------------
#
# Kept separate from every pure function above, mirroring iusb.py's own
# Transport/SocketTransport split -- everything above this point can be
# exercised by a unit test with no socket at all; everything below drives one.


class Transport(Protocol):
    """The minimum a byte-stream carrier must support to drive this module's handshake/read loop."""

    def recv_exact(self, n: int) -> bytes:
        raise NotImplementedError

    def send_all(self, data: bytes) -> None:
        raise NotImplementedError

    def set_timeout(self, seconds: float | None) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class SocketTransport:
    """:class:`Transport` backed by a plain (optionally already TLS-wrapped) blocking socket.

    Deliberately takes an already-connected socket rather than dialing one
    itself: TLS *policy* for this collection's target BMC (cipher restriction,
    certificate trust mode) is owned by ``asp.py``'s ``TlsTrustPolicy`` /
    ``BMC_CIPHERS`` and must be reused, not reinvented here (per this
    collection's task brief) -- so the module that already imports
    ``asp.TlsTrustPolicy`` for the HTTP side builds the ``ssl.SSLContext`` (if
    any) and does the dial; this class only wraps the resulting socket-like
    object in the small, generic interface :meth:`open_channel`/
    :meth:`capture_one_frame` need. This keeps TLS policy in exactly one
    place in this collection.
    """

    def __init__(self, sock) -> None:  # sock is deliberately duck-typed: socket.socket or ssl.SSLSocket.
        self._sock = sock

    def set_timeout(self, seconds: float | None) -> None:
        self._sock.settimeout(seconds)

    def recv_exact(self, n: int) -> bytes:
        if n == 0:
            return b""
        chunks = bytearray()
        while len(chunks) < n:
            try:
                chunk = self._sock.recv(n - len(chunks))
            except TimeoutError as exc:
                raise TimeoutError_(
                    f"ivtp: timed out waiting for {n - len(chunks)} more byte(s) (got {len(chunks)} of {n})",
                    operation="ivtp.read_packet",
                    indeterminate=bool(chunks),
                ) from exc
            except OSError as exc:
                raise ConnectionError_(f"ivtp: socket error while reading: {exc}", operation="ivtp.read_packet") from exc
            if not chunk:
                raise EOFError("ivtp: connection closed while reading")
            chunks += chunk
        return bytes(chunks)

    def send_all(self, data: bytes) -> None:
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise ConnectionError_(f"ivtp: socket error while writing: {exc}", operation="ivtp.write_packet") from exc

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    @classmethod
    def connect(cls, host: str, port: int, *, timeout: float, ssl_context=None) -> SocketTransport:
        """Dial ``host:port`` and, if ``ssl_context`` is given, wrap the socket in TLS.

        ``ssl_context`` is expected to already carry this board's mandatory
        cipher/protocol restriction (see the class docstring) -- this method
        does not build or adjust one.
        """
        try:
            raw = _socket.create_connection((host, port), timeout=timeout)
        except TimeoutError as exc:
            raise ConnectionError_(f"ivtp: timed out connecting to {host}:{port}", endpoint=f"{host}:{port}", operation="ivtp.connect") from exc
        except OSError as exc:
            raise ConnectionError_(f"ivtp: could not connect to {host}:{port}: {exc}", endpoint=f"{host}:{port}", operation="ivtp.connect") from exc

        if ssl_context is None:
            return cls(raw)

        try:
            wrapped = ssl_context.wrap_socket(raw, server_hostname=host if ssl_context.check_hostname else None)
        except _ssl.SSLError as exc:
            raw.close()
            raise TlsValidationError(f"ivtp: TLS handshake with {host}:{port} failed: {exc}", endpoint=f"{host}:{port}", operation="ivtp.connect") from exc
        except OSError as exc:
            raw.close()
            raise ConnectionError_(f"ivtp: could not establish TLS with {host}:{port}: {exc}", endpoint=f"{host}:{port}", operation="ivtp.connect") from exc
        return cls(wrapped)


def read_packet(transport: Transport) -> tuple[Header, bytes]:
    """Read one IVTP packet: the 8-byte header, then exactly ``header.pkt_size`` body bytes.

    This TLV convention (``pktSize`` == body length following the header) is
    confirmed for every message a client RECEIVES by the decompiled
    ``CtrlReader``'s framing (it reads exactly ``m_pktHdr.pktSize`` bytes
    before calling back into ``onControlMessage()``). It is only the
    client's own OUTGOING VALIDATE_VIDEO_SESSION packet where this collection
    deliberately does not carry that convention through -- see this module's
    docstring.
    """
    header = Header.parse(transport.recv_exact(HEADER_LEN))
    if header.pkt_size > MAX_FRAME_BYTES:
        raise ProtocolError(f"ivtp: implausible pkt_size {header.pkt_size} in a received header (type={header.type})", operation="ivtp.read_packet")
    body = transport.recv_exact(header.pkt_size) if header.pkt_size else b""
    return header, body


def write_packet(transport: Transport, data: bytes) -> None:
    transport.send_all(data)


# --- Handshake state machine ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChannelFacts:
    """Everything :func:`open_channel` confirmed about a KVM/video session, with no secret-shaped field.

    Safe to pass directly as an :class:`models.OperationReceipt` ``observed``
    value -- unlike a token or password, nothing here identifies a credential,
    per this collection's rule that a receipt never carries one.
    """

    session_accepted: bool
    greeting_body_len: int
    validate_status: int
    validate_status_name: str
    validate_sub_status: int | None
    resumed: bool


def open_channel(
    transport: Transport,
    *,
    token: str,
    client_ip: str,
    username: str,
    handshake_timeout: float = 15.0,
    send_get_web_token: bool = True,
) -> ChannelFacts:
    """Drive the full IVTP handshake over an already-connected ``transport``.

    Sequence (see this module's docstring for why this differs from
    ``rd450x-console``'s reconnect-aware sequence):

    1. Read the BMC's unsolicited greeting -- expected to be SESSION_ACCEPTED
       (23). Confirmed live against the target board: exactly 8 bytes,
       ``17 00 00 00 00 00 00 00`` (pktSize=0, i.e. no active-client body).
    2. Optionally send GET_WEB_TOKEN (21) -- see :func:`build_get_web_token`'s
       docstring for why this is enabled by default but unconfirmed as
       *required*.
    3. Send VALIDATE_VIDEO_SESSION (18).
    4. Read packets, tolerating and discarding anything this function does
       not specifically care about (LED state, encryption-status pushes,
       bandwidth probes, ...), until either VALIDATE_VIDEO_SESSION_RESPONSE
       (19) or an unsolicited STOP_SESSION_IMMEDIATE (8) arrives.
    5. On a non-VALID validate status, raise the classified error from
       :func:`validate_status_error`. On STOP_SESSION_IMMEDIATE, raise the
       classified error from :func:`stop_session_error`.
    6. On VALID_SESSION, send RESUME_REDIRECTION (6) and return
       :class:`ChannelFacts`.

    Raises :class:`errors.ProtocolError` if the first packet is not
    SESSION_ACCEPTED at all -- a real protocol-level surprise, not merely a
    rejected session.
    """
    transport.set_timeout(handshake_timeout)

    header, body = read_packet(transport)
    if header.type != OP_SESSION_ACCEPTED:
        raise ProtocolError(
            f"ivtp: expected the BMC's greeting to be SESSION_ACCEPTED (23), got opcode {header.type} (pktSize={header.pkt_size}, status={header.status})",
            operation="ivtp.open_channel",
        )
    greeting_body_len = len(body)

    if send_get_web_token:
        write_packet(transport, build_get_web_token(token))
    write_packet(transport, build_validate_video_session(token=token, client_ip=client_ip, username=username))

    status: int | None = None
    sub_status: int | None = None
    while status is None:
        header, body = read_packet(transport)
        if header.type == OP_VALIDATE_VIDEO_SESSION_RESPONSE:
            status, sub_status = parse_validate_video_session_response(body)
        elif header.type == OP_STOP_SESSION_IMMEDIATE:
            raise stop_session_error(header.status)
        # Anything else (SESSION_ACCEPTED update, LED push, encryption-status
        # push, bandwidth probe, ...) is tolerated and its body already fully
        # consumed by read_packet(); keep waiting for the response we asked for.

    if status != SESSION_VALID:
        raise validate_status_error(status)

    write_packet(transport, build_resume_redirection())

    return ChannelFacts(
        session_accepted=True,
        greeting_body_len=greeting_body_len,
        validate_status=status,
        validate_status_name=validate_status_name(status),
        validate_sub_status=sub_status,
        resumed=True,
    )


def capture_one_frame(transport: Transport, *, frame_timeout: float = 20.0, request_refresh: bool = True) -> bytes:
    """Read packets until one complete VIDEO_FRAGMENT frame has been reassembled, and return its raw bytes.

    Must be called after :func:`open_channel` has already sent
    RESUME_REDIRECTION. The returned bytes are the concatenated, still-encoded
    (VQ+JPEG/DCT, optionally RC4-obfuscated) fragment stream for exactly one
    frame -- decoding it to pixels is out of scope for this collection (see
    :class:`FrameReassembler`'s docstring); callers must not present this as,
    or attempt to interpret this as, a viewable image.

    A timeout here (:class:`errors.TimeoutError_`) is not necessarily a
    protocol failure: the ASPEED video engine only sends fragments for
    changed content, so a genuinely idle/unchanged host screen may not
    produce a full frame within ``frame_timeout`` even with
    ``request_refresh=True``'s best-effort nudge. Raise ``frame_timeout`` or
    retry if this is hit against a host that is not actually idle.
    """
    transport.set_timeout(frame_timeout)
    if request_refresh:
        write_packet(transport, build_refresh_video_screen())

    reassembler = FrameReassembler()
    while True:
        header, body = read_packet(transport)
        if header.type == OP_VIDEO_FRAGMENT:
            frag_num, _is_first, _is_last, data = parse_video_fragment(body)
            frame = reassembler.feed(frag_num, data)
            if frame is not None:
                return frame
        elif header.type == OP_STOP_SESSION_IMMEDIATE:
            raise stop_session_error(header.status)
        # else: tolerate and discard (already consumed by read_packet()).


__all__ = [
    "HEADER_LEN",
    "MAX_FRAME_BYTES",
    "OP_BLANK_SCREEN",
    "OP_BW_DETECT_REQ",
    "OP_BW_DETECT_RESP",
    "OP_CONF_SERVICE_STATUS",
    "OP_DISABLE_ENCRYPTION",
    "OP_DISPLAY_CONTROL_STATUS",
    "OP_DISPLAY_LOCK_SET",
    "OP_ENABLE_ENCRYPTION",
    "OP_ENCRYPTION_STATUS",
    "OP_GET_ACTIVE_CLIENTS",
    "OP_GET_FULL_SCREEN",
    "OP_GET_KEYBD_LED",
    "OP_GET_USB_MOUSE_MODE",
    "OP_GET_USER_MACRO",
    "OP_GET_WEB_TOKEN",
    "OP_HID_PKT",
    "OP_INITIAL_ENCRYPTION_STATUS",
    "OP_IPMI_REQUEST_PKT",
    "OP_IPMI_RESPONSE_PKT",
    "OP_KVM_DISCONNECT",
    "OP_KVM_SHARING",
    "OP_KVM_SOCKET_STATUS",
    "OP_MAX_SESSION_CLOSING",
    "OP_MEDIA_FREE_INSTANCE_STATUS",
    "OP_MEDIA_LICENSE_STATUS",
    "OP_MEDIA_STATE",
    "OP_MOUSE_MEDIA_INFO",
    "OP_PAUSE_REDIRECTION",
    "OP_POWER_CONTROL_REQUEST",
    "OP_POWER_CONTROL_RESPONSE",
    "OP_POWER_STATUS",
    "OP_REFRESH_VIDEO_SCREEN",
    "OP_RESUME_REDIRECTION",
    "OP_SESSION_ACCEPTED",
    "OP_SET_BANDWIDTH",
    "OP_SET_COMPRESSION_TYPE",
    "OP_SET_FPS",
    "OP_SET_KBD_LANG",
    "OP_SET_MOUSE_MODE",
    "OP_SET_NEXT_MASTER",
    "OP_SET_USER_MACRO",
    "OP_STOP_SESSION_IMMEDIATE",
    "OP_VALIDATE_VIDEO_SESSION",
    "OP_VALIDATE_VIDEO_SESSION_RESPONSE",
    "OP_VIDEO_FRAGMENT",
    "OP_WEB_PREVIEWER_CAPTURE_STATUS",
    "OP_WEB_PREVIEWER_SESSION",
    "SESSION_INVALID",
    "SESSION_INVALID_CDROM_TOKEN",
    "SESSION_INVALID_FLOPPY_TOKEN",
    "SESSION_INVALID_VIDEO_TOKEN",
    "SESSION_KVM_DISABLED",
    "SESSION_VALID",
    "STOP_CONF_CHANGE",
    "STOP_GENERIC",
    "STOP_KVM_DISCONNECT",
    "STOP_LICENSE_EXPIRED",
    "STOP_TIMED_OUT",
    "STOP_WEB_LOGOUT",
    "TOKEN_TYPE_SSI",
    "TOKEN_TYPE_WEB_SESSION",
    "VIDEO_PACKET_SIZE",
    "ChannelFacts",
    "FrameReassembler",
    "Header",
    "SocketTransport",
    "Transport",
    "build_get_web_token",
    "build_refresh_video_screen",
    "build_resume_redirection",
    "build_stop_session",
    "build_validate_video_session",
    "capture_one_frame",
    "open_channel",
    "parse_validate_video_session_response",
    "parse_video_fragment",
    "read_packet",
    "stop_reason_name",
    "stop_session_error",
    "validate_status_error",
    "validate_status_name",
    "write_packet",
]
