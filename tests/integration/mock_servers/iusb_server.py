# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Deterministic mock IUSB virtual-media (CD-ROM) endpoint for integration testing.

There is exactly one real ASMB8-iKVM board in the world, and it is currently
unreachable (see the top-level task this file was written under and
CONTRIBUTING.md) -- so this mock is the only way to regression-test
``asmb8_media``'s virtual-media client against anything at all. Everything in
this file plays the **BMC side** of the wire protocol: it drives SCSI-wrapped
IUSB requests AT a connected client and validates the client's replies, which
is the mirror image of ``plugins/module_utils/asp.py`` (an HTTP *client*).

Standard library only, no ``requests`` dependency needed here: this is a raw
TCP socket protocol (see the header layout below), matching the real board and
matching the fact that the collection's own iUSB client (once it exists) must
add no runtime dependency either -- see ``requirements.txt``'s note on that.

Provenance and verification status
-----------------------------------

Every wire-format fact this file bakes in as *default* behaviour is stated as
either:

``VERIFIED LIVE``
    Captured directly against the real board on 2026-08-08, by the maintainer
    who owns access to that hardware -- see the field-by-field table in the
    module docstring's originating task. This mock reproduces these exactly,
    including the ones that look like bugs (the auth-ACK header's
    major/minor/packetHeaderLen bytes coming back zero; the non-zero
    "reserved" header bytes; the non-sequential command-counter field). A
    mock that "fixes" a verified real quirk hides a real interop bug rather
    than catching it.

``ASSUMED, NOT VERIFIED``
    Extrapolated from a verified fact for a case that was not itself directly
    captured (for example: the SCSI-request device-type OR-in of 0x80 is
    verified for the BMC's *request* frames; this mock applies the same OR to
    the ACK frame too, on the theory that both are "frames the server sends",
    but no live capture of an ACK frame specifically has confirmed that).
    Marked at the point it is used; treat these as this mock's own choice, not
    as evidence of real board behaviour.

Nothing here should be read as authoritative for a value not explicitly
marked one of the above two ways.
"""

from __future__ import annotations

import dataclasses
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Wire format constants -- see module docstring for verification status.
# --------------------------------------------------------------------------

HEADER_LEN = 32
SIGNATURE = b"IUSB    "  # "IUSB" + 4 spaces, exactly 8 bytes.

#: VERIFIED LIVE: the client sends 0x05 (CD-ROM) as deviceType; the real
#: server ORs in 0x80 on every frame it sends back.
DEVICE_CDROM = 0x05
SERVER_DEVICE_TYPE_BIT = 0x80

#: Redirection-control opcodes (not SCSI) that ride in the same opcode slot.
OP_AUTH = 0xF2
OP_ACK = 0xF1
OP_KILL = 0xF6

#: VERIFIED LIVE: a second AMI redirection-control opcode observed exactly
#: once in a full-install opcode census (see ``captured_install_script``
#: below), distinct from the ack/auth/kill opcodes already modelled above.
#: Not SCSI at all -- it rides the same >=0xF0 control-opcode range that
#: ``iusb.CDROMDevice.handle`` bare-echoes rather than treating as an error
#: (see that method's ``AMI_CONTROL_OPCODE_MIN`` branch).
OP_AMI_CTRL_F3 = 0xF3

#: The six SCSI/MMC opcodes the real vendor CD-ROM dispatcher recognises
#: (disassembly-verified by a companion PoC; see docs/protocol-notes.md if/when
#: it exists, and docs/protocol-notes.md for the account this
#: mock was built against). INQUIRY (0x12) is deliberately absent: real
#: firmware answers it itself and never forwards it, so this mock never sends
#: it as a scripted request.
SCSI_TEST_UNIT_READY = 0x00
SCSI_START_STOP_UNIT = 0x1B
SCSI_READ_CAPACITY10 = 0x25
SCSI_READ10 = 0x28
SCSI_READ_TOC = 0x43
SCSI_READ12 = 0xA8

#: VERIFIED, by absence: the real vendor CD-ROM SCSI dispatcher's six
#: recognised opcodes (see ``iusb.py``'s module docstring) do NOT include
#: REQUEST SENSE. No scripted sequence in this file issues it. A test that
#: asserts its absence across a whole scripted conversation is asserting the
#: reasoning that let the allocation-length TOC bug (see
#: ``captured_install_script``) go unnoticed: had the client's replies
#: actually been malformed in a way the host *noticed*, a real initiator
#: would have asked for sense data to find out why, and this opcode is what
#: that ask looks like.
SCSI_REQUEST_SENSE = 0x03

CD_BLOCK_SIZE = 2048

#: Auth payload layout (VERIFIED LIVE).
AUTH_OPCODE_OFFSET = 9
AUTH_STATUS_OFFSET = 30  # aka "connectionStatus" / token-type byte
AUTH_TOKEN_OFFSET = 31
AUTH_PAYLOAD_LEN = 128  # web-session-token auth payload length
ACK_PAYLOAD_LEN = 55  # VERIFIED LIVE: the ACK's dataPacketLen

#: SCSI envelope layout (VERIFIED LIVE).
ENV_OFF_DATA_LEN = 0
ENV_OFF_CMD_COUNTER = 4
ENV_OFF_MARKER = 8
ENV_OFF_OPCODE = 9
ENV_OFF_EJECT_BYTE = 13
ENV_OFF_RESP_LEN = 25
ENV_MARKER_BYTE = 0x01
DEFAULT_ENVELOPE_LEN = 29  # every captured real request envelope was this length

#: ACK connectionStatus values. 1 is VERIFIED LIVE ("success"); the client's
#: own ack_status()-style logic (see the proof-of-concept client's auth layer) treats
#: 5/8 as a device error and anything else as "already redirected" (reading an
#: owner IP that may or may not be present). 5/8 themselves are ported from the
#: decompiled JViewer sources cited by that PoC, not independently captured
#: against THIS board -- so they are ASSUMED, NOT VERIFIED for the ASMB8
#: specifically, but are the best available stand-in for "device busy".
CONN_OK = 1
CONN_ERR_IN_USE_5 = 5
CONN_ERR_IN_USE_8 = 8

#: ASSUMED, NOT VERIFIED: no live capture exists of what status byte the real
#: board sends for a *rejected token* specifically (as opposed to a busy
#: device slot). This mock's own choice of a value distinct from 1/5/8 so a
#: test can tell "bad credentials" apart from "device busy" -- do not cite
#: this as observed firmware behaviour.
AUTH_TOKEN_REJECTED_STATUS = 0x02

#: VERIFIED LIVE: non-zero garbage observed in the header's 4 "reserved" bytes
#: on real server frames (an example capture was ``bc 38 02 bf``). This mock
#: reproduces non-zero reserved bytes by default -- a client that silently
#: assumes "reserved means zero" needs to fail here, not on the one
#: unreachable board.
LIVE_OBSERVED_RESERVED_GARBAGE = bytes.fromhex("bc3802bf")

#: VERIFIED LIVE: the command-counter field (SCSI envelope offset 4) is
#: non-sequential; the exact example values observed were 3, then 7, then 16.
#: This cycle of increments reproduces exactly that: 3, 3+4=7, 7+9=16, then
#: continues non-sequentially rather than reverting to +1 steps.
_COUNTER_START = 3
_COUNTER_INCREMENTS = (4, 9, 13, 7, 11, 5)

DEFAULT_EXPECTED_TOKEN = "STOKEN-mock-test"  # obviously-fake fixture default, not a real credential


class ProtocolViolation(Exception):
    """The peer (or this mock's own caller) violated the wire framing this mock enforces."""


class SlotHeldError(Exception):
    """Raised by the driving API when a caller tries to use a connection that never authenticated."""


# --------------------------------------------------------------------------
# Header codec
# --------------------------------------------------------------------------


@dataclass
class Header:
    """A 32-byte IUSB packet header. All multi-byte fields are little-endian.

    ``major``/``minor``/``header_len_field`` are ordinarily 1/0/32, but are
    kept as plain settable fields (rather than constants) specifically so
    :meth:`IusbMockServer` can reproduce the VERIFIED LIVE ACK oddity where the
    real board sends all three as zero.
    """

    major: int = 1
    minor: int = 0
    header_len_field: int = HEADER_LEN
    checksum: int = 0
    data_packet_len: int = 0
    server_caps: int = 0
    device_type: int = 0
    protocol: int = 1
    direction: int = 0
    device_number: int = 0
    interface_number: int = 0
    client_data: int = 0
    instance: int = 0
    sequence_number: int = 0
    reserved: bytes = b""

    def marshal(self) -> bytes:
        b = bytearray(HEADER_LEN)
        b[0:8] = SIGNATURE
        b[8] = self.major & 0xFF
        b[9] = self.minor & 0xFF
        b[10] = self.header_len_field & 0xFF
        b[11] = self.checksum & 0xFF
        struct.pack_into("<I", b, 12, self.data_packet_len & 0xFFFFFFFF)
        b[16] = self.server_caps & 0xFF
        b[17] = self.device_type & 0xFF
        b[18] = self.protocol & 0xFF
        b[19] = self.direction & 0xFF
        b[20] = self.device_number & 0xFF
        b[21] = self.interface_number & 0xFF
        b[22] = self.client_data & 0xFF
        b[23] = self.instance & 0xFF
        struct.pack_into("<I", b, 24, self.sequence_number & 0xFFFFFFFF)
        b[28:32] = (self.reserved + bytes(4))[:4]
        return bytes(b)

    @staticmethod
    def parse(data: bytes) -> Header:
        if len(data) < HEADER_LEN:
            raise ProtocolViolation(f"iusb: short header ({len(data)} bytes, need {HEADER_LEN})")
        if data[0:8] != SIGNATURE:
            raise ProtocolViolation(f"iusb: bad signature {data[0:8]!r}")
        (data_packet_len,) = struct.unpack_from("<I", data, 12)
        (sequence_number,) = struct.unpack_from("<I", data, 24)
        return Header(
            major=data[8],
            minor=data[9],
            header_len_field=data[10],
            checksum=data[11],
            data_packet_len=data_packet_len,
            server_caps=data[16],
            device_type=data[17],
            protocol=data[18],
            direction=data[19],
            device_number=data[20],
            interface_number=data[21],
            client_data=data[22],
            instance=data[23],
            sequence_number=sequence_number,
            reserved=bytes(data[28:32]),
        )


def _le32(value: int) -> bytes:
    return (value & 0xFFFFFFFF).to_bytes(4, "little")


def _read32le(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


# --------------------------------------------------------------------------
# Scripted SCSI conversation steps
# --------------------------------------------------------------------------


@dataclass
class ScsiStep:
    """One request the mock will send at the client, and how to validate the reply.

    ``transfer_length`` is the envelope-offset-0 value the mock's *request*
    carries -- VERIFIED LIVE for TEST_UNIT_READY (0) and READ_CAPACITY10 (8);
    every other opcode's default here is this mock's own reasonable-looking
    choice (ASSUMED, NOT VERIFIED) and is freely overridable.
    """

    opcode: int
    cdb_tail: bytes = b""
    transfer_length: int = 0
    command_counter: int | None = None  # None -> use the server's auto non-sequential counter
    instance: int = 0
    envelope_len: int = DEFAULT_ENVELOPE_LEN
    expect_response: bool = True
    #: Expected appended data length for :meth:`IusbMockServer.recv_response`'s
    #: built-in validation, or ``None`` to skip that specific check (the
    #: sequence-echo / self-consistency checks always run regardless).
    expected_data_len: int | None = None


@dataclass
class IdleStep:
    """Send nothing for ``seconds``. Idle is normal on this board once the host
    finishes probing -- this step exists so a test can prove a client survives
    a quiet period rather than erroring out on it."""

    seconds: float


@dataclass
class DisconnectStep:
    """Abruptly close the connection, mid-conversation."""


def _cdb10_tail(lba: int, blocks: int) -> bytes:
    """CDB bytes 1..9 (i.e. everything after the opcode) for READ(10) / a
    10-byte CDB shape: reserved, LBA (4 bytes BE), reserved, transfer length
    (2 bytes BE), control. VERIFIED LIVE layout (LBA/length fields are
    big-endian inside the little-endian IUSB wrapper)."""
    return bytes([0x00]) + lba.to_bytes(4, "big") + bytes([0x00]) + blocks.to_bytes(2, "big") + bytes([0x00])


def _cdb12_tail(lba: int, blocks: int) -> bytes:
    """CDB bytes 1..11 for READ(12): reserved, LBA (4 BE), transfer length (4 BE), group, control."""
    return bytes([0x00]) + lba.to_bytes(4, "big") + blocks.to_bytes(4, "big") + bytes([0x00, 0x00])


def _cdb_read_toc_tail(alloc: int) -> bytes:
    """CDB bytes 1..9 for READ TOC: MSF bit, three reserved bytes, format byte,
    three more reserved bytes, allocation length (2 bytes BE at CDB[7:9]),
    control. This is the exact field the real bug (see
    ``iusb.CDROMDevice._read_toc``'s docstring) ignored: the initiator's
    stated budget for the TOC response, big-endian, at CDB offset 7."""
    tail = bytearray(9)
    tail[6:8] = alloc.to_bytes(2, "big")
    return bytes(tail)


def test_unit_ready_step(**overrides) -> ScsiStep:
    """VERIFIED LIVE: xferlen 0."""
    defaults = {"opcode": SCSI_TEST_UNIT_READY, "transfer_length": 0, "expected_data_len": 0}
    defaults.update(overrides)
    return ScsiStep(**defaults)


def read_capacity10_step(**overrides) -> ScsiStep:
    """VERIFIED LIVE: xferlen 8; response is 8 bytes (last LBA + block size, both big-endian)."""
    defaults = {"opcode": SCSI_READ_CAPACITY10, "transfer_length": 8, "expected_data_len": 8}
    defaults.update(overrides)
    return ScsiStep(**defaults)


def read10_step(lba: int, blocks: int, **overrides) -> ScsiStep:
    data_len = blocks * CD_BLOCK_SIZE
    defaults = {"opcode": SCSI_READ10, "cdb_tail": _cdb10_tail(lba, blocks), "transfer_length": data_len, "expected_data_len": data_len}
    defaults.update(overrides)
    return ScsiStep(**defaults)


def read12_step(lba: int, blocks: int, **overrides) -> ScsiStep:
    data_len = blocks * CD_BLOCK_SIZE
    defaults = {"opcode": SCSI_READ12, "cdb_tail": _cdb12_tail(lba, blocks), "transfer_length": data_len, "expected_data_len": data_len}
    defaults.update(overrides)
    return ScsiStep(**defaults)


#: The full, untruncated READ TOC response length this mock's single-track
#: TOC body always produces (4-byte header + two 8-byte track descriptors) --
#: see ``iusb.CDROMDevice._read_toc``. Exposed here so a caller can reason
#: about ``alloc`` without re-deriving this number.
FULL_READ_TOC_RESPONSE_LEN = 20


def read_toc_step(alloc: int = 0, **overrides) -> ScsiStep:
    """READ TOC. ``alloc`` is the CDB's allocation length (CDB[7:9], big
    endian -- see :func:`_cdb_read_toc_tail`), the exact field whose value
    determines how much of the full TOC response a well-behaved client may
    return. This is the field the real bug ignored entirely (see
    ``iusb.CDROMDevice._read_toc``'s docstring), and the reason the previous
    golden vector -- alloc 0, "no limit stated" -- could not catch it.

    ``expected_data_len`` is derived from ``alloc`` to match SCSI's own rule:
    a short allocation truncates the response to exactly ``alloc`` bytes; an
    allocation at or above the full response length changes nothing. Override
    it explicitly to probe a client that gets this wrong.
    """
    if 0 < alloc < FULL_READ_TOC_RESPONSE_LEN:
        expected = alloc
    else:
        expected = FULL_READ_TOC_RESPONSE_LEN
    defaults = {
        "opcode": SCSI_READ_TOC,
        "cdb_tail": _cdb_read_toc_tail(alloc),
        "transfer_length": FULL_READ_TOC_RESPONSE_LEN,
        "expected_data_len": expected,
    }
    defaults.update(overrides)
    return ScsiStep(**defaults)


def ami_control_step(opcode: int = OP_AMI_CTRL_F3, **overrides) -> ScsiStep:
    """A non-SCSI AMI control opcode (>= 0xF0) riding mid-session -- e.g. the
    captured 0xF3 frame (see ``captured_install_script``). Real firmware's
    dispatcher answers every opcode in this range with a bare envelope echo
    and no appended data (see ``iusb.CDROMDevice.handle``'s
    ``AMI_CONTROL_OPCODE_MIN`` branch), never an error; this step exists so a
    scripted sequence can include one and prove a real client agrees."""
    defaults = {"opcode": opcode, "transfer_length": 0, "expected_data_len": 0}
    defaults.update(overrides)
    return ScsiStep(**defaults)


def start_stop_unit_step(*, eject: bool, **overrides) -> ScsiStep:
    """START/STOP UNIT. ``eject=True`` sets CDB[4] (envelope offset 13) to
    exactly 2 -- VERIFIED LIVE: exact equality, not a bitmask (see the
    the proof-of-concept client's eject-detection docstring for the
    decompiled-vs-Go-reference disagreement this pins to the decompiled
    side)."""
    tail = bytearray(4)
    tail[3] = 2 if eject else 0  # tail[3] == CDB[4] == envelope offset 13
    defaults = {"opcode": SCSI_START_STOP_UNIT, "cdb_tail": bytes(tail), "transfer_length": 0, "expected_data_len": 0}
    defaults.update(overrides)
    return ScsiStep(**defaults)


def eject_step(**overrides) -> ScsiStep:
    return start_stop_unit_step(eject=True, **overrides)


#: LBAs a real bootloader probed, in order, before any filesystem driver was
#: involved: the ISO9660 primary volume descriptor's usual neighbourhood (0,
#: 1) and a path-table/root-directory pair (16, 17), immediately followed by
#: the El Torito boot catalog and the boot image it points at. VERIFIED LIVE
#: shape (see ``captured_install_script``'s docstring); the exact catalog/
#: image LBAs vary per ISO build and are this mock's own stand-in values.
BOOT_PROBE_LBAS: tuple[int, ...] = (0, 1, 16, 17)
EL_TORITO_CATALOG_LBA = 20
EL_TORITO_BOOT_IMAGE_LBA = 21

#: Minimum medium size (in 2048-byte blocks) ``captured_install_script``'s
#: fixed LBA layout touches -- its highest access is ``read10_step(30, 8)``,
#: reaching LBA 37.
MIN_CAPTURED_INSTALL_BLOCKS = 40


def captured_install_script(*, blocks: int, gap_seconds: float = 2.0) -> list[ScsiStep | IdleStep]:
    """Reproduce the two-phase opcode shape captured from one real, full OS
    install against the target hardware -- the trace that exposed the
    ``CDROMDevice._read_toc`` allocation-length bug (see that method's
    docstring). Opcode totals over that one install: TEST_UNIT_READY 132,
    READ_CAPACITY10 7, READ10 16451, READ_TOC 58, the AMI ack opcode 1, and
    the AMI 0xF3 control opcode 1. Two structural facts this reproduces:

    1. A bootloader phase of single-sector READ10 probes issues NO READ_TOC
       at all, followed by an idle gap (a host sitting at a menu -- modelled
       with :class:`IdleStep` rather than a real multi-minute sleep), then an
       OS phase where READ_TOC recurs repeatedly, interleaved with
       TEST_UNIT_READY and multi-block READ10s of irregular size. That
       asymmetry is exactly why the bug hid: firmware-stage booting never
       exercised the broken code path, and only a real OS tripped over it.
    2. READ_TOC recurs across all three allocation-length cases that matter:
       0 ("no limit stated" -- the old golden vector's case, which could not
       catch the bug), 12 (the EXACT value a real Linux initrd sent, and the
       value that broke real hardware), and 64 (over-generous -- must change
       nothing). A single occurrence of any one case under-tests it.

    ``blocks`` must cover every LBA this script touches (see
    :data:`MIN_CAPTURED_INSTALL_BLOCKS`); callers size their backing medium
    accordingly. No step here is :data:`SCSI_REQUEST_SENSE` -- see that
    constant's docstring for why its absence across this whole script is
    itself part of what a test asserts.
    """
    if blocks < MIN_CAPTURED_INSTALL_BLOCKS:
        raise ValueError(f"captured_install_script needs a medium of at least {MIN_CAPTURED_INSTALL_BLOCKS} blocks, got {blocks}")

    boot_phase: list[ScsiStep] = [
        test_unit_ready_step(),
        read_capacity10_step(),
        *(read10_step(lba, 1) for lba in BOOT_PROBE_LBAS),
        read10_step(EL_TORITO_CATALOG_LBA, 1),
        read10_step(EL_TORITO_BOOT_IMAGE_LBA, 1),
    ]

    os_phase: list[ScsiStep] = [
        test_unit_ready_step(),
        read_toc_step(alloc=0),  # "no limit stated" -- the case the old golden vector covered
        read10_step(22, 16),  # multi-block, at the BMC's observed 16-block ceiling
        read_toc_step(alloc=12),  # the EXACT allocation length that broke real hardware
        read10_step(38, 2),  # irregular size, at an extent boundary
        test_unit_ready_step(),
        read_toc_step(alloc=64),  # over-generous allocation: must change nothing
        read10_step(0, 1),
        ami_control_step(),  # the captured 0xF3 -- must not be treated as an error
        test_unit_ready_step(),
        read_toc_step(alloc=12),  # READ_TOC recurs -- a single occurrence under-tests it
        read10_step(30, 8),
    ]

    return [*boot_phase, IdleStep(gap_seconds), *os_phase]


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScsiResponse:
    """A parsed client reply to one scripted SCSI request."""

    header: Header
    payload: bytes

    @property
    def declared_data_len(self) -> int | None:
        """Envelope offset 0, as the client set it on this reply."""
        if len(self.payload) < ENV_OFF_DATA_LEN + 4:
            return None
        return _read32le(self.payload, ENV_OFF_DATA_LEN)

    @property
    def resp_len_field(self) -> int | None:
        """Envelope offset 25 -- VERIFIED LIVE as the field that matters for
        how many bytes get forwarded to the host."""
        if len(self.payload) < ENV_OFF_RESP_LEN + 4:
            return None
        return _read32le(self.payload, ENV_OFF_RESP_LEN)

    def data(self, envelope_len: int = DEFAULT_ENVELOPE_LEN) -> bytes:
        """The appended SCSI data-in bytes, i.e. everything after the echoed envelope."""
        return self.payload[envelope_len:]


@dataclass
class ValidationResult:
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def validate_response(step: ScsiStep, request_sequence_number: int, response: ScsiResponse) -> ValidationResult:
    """Check a client's reply against the request that produced it.

    Runs the four checks the task calls out explicitly: sequence echo,
    ``dataPacketLen`` self-consistency, the offset-25 response-length field,
    and (when ``step.expected_data_len`` was given) the actual appended data
    length.
    """
    problems: list[str] = []

    if response.header.sequence_number != request_sequence_number:
        problems.append(f"sequence number not echoed: expected {request_sequence_number}, got {response.header.sequence_number}")

    if response.header.data_packet_len != len(response.payload):
        problems.append(f"dataPacketLen ({response.header.data_packet_len}) does not match actual payload bytes received ({len(response.payload)})")

    data_len = len(response.data(step.envelope_len))

    if response.resp_len_field is not None and response.resp_len_field != data_len:
        problems.append(f"offset-25 response-length field ({response.resp_len_field}) does not match appended data length ({data_len})")

    if step.expected_data_len is not None and data_len != step.expected_data_len:
        problems.append(f"appended data length ({data_len}) does not match expected ({step.expected_data_len})")

    return ValidationResult(problems)


# --------------------------------------------------------------------------
# Fault injection
# --------------------------------------------------------------------------


@dataclass
class IusbFaultConfig:
    """Every fault-injection knob this mock understands.

    One-shot fields (documented individually) fire once, on the very next
    frame this mock sends, then reset themselves -- matching how a test uses
    them ("the next frame should be broken this way"). Persistent fields stay
    in effect until a test changes them back.
    """

    #: One-shot: override the ACK's connectionStatus byte for the very next
    #: auth attempt, regardless of whether the token matched. This is how a
    #: test exercises "wrong auth status" independent of credential logic.
    force_auth_status: int | None = None

    #: One-shot: truncate the very next frame this mock sends to exactly this
    #: many bytes (of the full header+payload), rather than the real length.
    #: Pair with :meth:`IusbMockServer.disconnect` immediately afterward for a
    #: realistic "the BMC cut me off mid-frame" scenario.
    truncate_next_frame_to: int | None = None

    #: One-shot: declare a different ``dataPacketLen`` in the header of the
    #: very next frame than the number of payload bytes actually sent --
    #: "a frame whose dataPacketLen lies about the real length".
    lie_next_data_packet_len: int | None = None

    #: One-shot: close the connection immediately after sending the next frame.
    disconnect_after_next_send: bool = False

    #: Persistent: use an all-zero reserved header field instead of the
    #: VERIFIED LIVE non-zero garbage. Default off, because non-zero IS the
    #: real behaviour -- this exists only to prove a test can tell the
    #: difference, e.g. while diagnosing a client that turns out to depend on
    #: reserved being zero.
    zero_reserved_bytes: bool = False

    #: Persistent: emit strictly sequential (0, 1, 2, ...) command-counter
    #: values instead of the VERIFIED LIVE non-sequential pattern. Same
    #: rationale as zero_reserved_bytes.
    sequential_command_counters: bool = False


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
            raise ProtocolViolation(f"iusb: connection closed after {len(chunks)}/{n} bytes")
        chunks += chunk
    return bytes(chunks)


#: How long the accept loop waits before re-checking the stop event.
_ACCEPT_POLL_SECONDS = 0.25
_SHUTDOWN_JOIN_SECONDS = 5.0


class IusbMockServer:
    """Threaded mock IUSB CD-ROM redirection endpoint, playing the BMC.

    Use as a context manager::

        with IusbMockServer(expected_token="tok") as server:
            connect_a_test_client(server.port, token="tok")
            server.wait_for_handshake()
            resp = server.run_script([test_unit_ready_step(), read_capacity10_step()])

    Binds an ephemeral TCP port on 127.0.0.1 only.

    **Single-session hazard.** The real board's cd-media service allows
    exactly one active session and has no server-side timeout to reclaim an
    abandoned one (see ``plugins/module_utils/errors.py``'s
    ``ErrorClass.BMC_BUSY`` docstring). This mock models that structurally: a
    second concurrent connection is answered with an in-use ACK and dropped,
    never becoming the active session. :meth:`hold_slot_without_connection` /
    :meth:`release_held_slot` let a test simulate a *stale* held slot -- one
    where no real connection exists at all -- so the eject-before-insert
    reclamation path can be exercised without needing two real sockets.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        expected_token: str = DEFAULT_EXPECTED_TOKEN,
        device_type: int = DEVICE_CDROM,
        auth_success_status: int = CONN_OK,
        auth_token_rejected_status: int = AUTH_TOKEN_REJECTED_STATUS,
        in_use_status: int = CONN_ERR_IN_USE_5,
        reserved_bytes: bytes = LIVE_OBSERVED_RESERVED_GARBAGE,
    ) -> None:
        self.host = host
        self.expected_token = expected_token
        self.device_type = device_type
        self.auth_success_status = auth_success_status
        self.auth_token_rejected_status = auth_token_rejected_status
        self.in_use_status = in_use_status
        self._reserved_bytes_value = reserved_bytes

        self.faults = IusbFaultConfig()

        self.port: int | None = None

        self._listen_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._handshake_thread: threading.Thread | None = None
        self._conn: socket.socket | None = None
        self._stop_event = threading.Event()

        self._slot_lock = threading.Lock()
        self._slot_held = False
        self._slot_owner_ip: str | None = None

        self._handshake_event = threading.Event()
        self._handshake_error: Exception | None = None
        self._auth_status_seen: int | None = None
        self._session_closed = threading.Event()

        self._header_sequence = 0
        self._command_counter = _COUNTER_START
        self._command_counter_increments_used = 0

    # -- lifecycle -------------------------------------------------------

    def start(self) -> IusbMockServer:
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        raw_sock.bind((self.host, 0))
        raw_sock.listen(2)
        raw_sock.settimeout(_ACCEPT_POLL_SECONDS)
        self.port = raw_sock.getsockname()[1]
        self._listen_sock = raw_sock
        self._accept_thread = threading.Thread(target=self._accept_loop, name="iusb-mock-accept", daemon=True)
        self._accept_thread.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()
        for _attempt in range(2):
            for sock in (self._conn, self._listen_sock):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            for thread in (self._handshake_thread, self._accept_thread):
                if thread is not None:
                    thread.join(timeout=_SHUTDOWN_JOIN_SECONDS)
            if not self._is_running():
                break
        self._listen_sock = None
        self._accept_thread = None
        self._handshake_thread = None

    def _is_running(self) -> bool:
        return any(t is not None and t.is_alive() for t in (self._accept_thread, self._handshake_thread))

    def __enter__(self) -> IusbMockServer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- single-session slot ---------------------------------------------

    def hold_slot_without_connection(self, owner_ip: str = "203.0.113.9") -> None:
        """Simulate a stale held slot: no real connection exists, but the next
        real connection attempt is refused as "already in use" anyway, exactly
        as the real board behaves toward an abandoned session it has no
        timeout to reclaim."""
        with self._slot_lock:
            self._slot_held = True
            self._slot_owner_ip = owner_ip

    def release_held_slot(self) -> None:
        """Simulate successful reclamation of a stale slot: the next
        connection attempt is free to become the active session."""
        with self._slot_lock:
            if self._conn is None:  # only clear if no real session holds it
                self._slot_held = False
                self._slot_owner_ip = None

    def is_slot_held(self) -> bool:
        with self._slot_lock:
            return self._slot_held

    # -- accept loop -------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                conn, _addr = self._listen_sock.accept()  # type: ignore[union-attr]
            except TimeoutError:
                continue
            except OSError:
                return

            with self._slot_lock:
                slot_free = not self._slot_held

            if not slot_free:
                threading.Thread(target=self._refuse_second_session, args=(conn,), name="iusb-mock-refuse", daemon=True).start()
                continue

            with self._slot_lock:
                self._slot_held = True
                self._slot_owner_ip = None
            self._conn = conn
            self._handshake_thread = threading.Thread(target=self._run_handshake, args=(conn,), name="iusb-mock-handshake", daemon=True)
            self._handshake_thread.start()
            # Bounded joins so shutdown notices the stop event promptly rather
            # than waiting on a handshake that may itself be waiting on a test.
            while self._handshake_thread.is_alive():
                self._handshake_thread.join(timeout=_ACCEPT_POLL_SECONDS)
                if self._stop_event.is_set():
                    return
            if self._stop_event.is_set():
                return

    def _refuse_second_session(self, conn: socket.socket) -> None:
        """Read a second connection's auth attempt and answer "already in use", per
        the single-session hazard. Never becomes the active session."""
        try:
            header_bytes = _recv_exact(conn, HEADER_LEN)
            header = Header.parse(header_bytes)
            _recv_exact(conn, header.data_packet_len)  # drain the auth payload; contents irrelevant here
            with self._slot_lock:
                owner_ip = self._slot_owner_ip
            ack_header, ack_payload = self._build_ack(status=self.in_use_status, request_header=header, owner_ip=owner_ip)
            conn.sendall(ack_header.marshal() + ack_payload)
        except (ProtocolViolation, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # -- handshake ---------------------------------------------------------

    def _run_handshake(self, conn: socket.socket) -> None:
        try:
            header_bytes = _recv_exact(conn, HEADER_LEN)
            header = Header.parse(header_bytes)
            payload = _recv_exact(conn, header.data_packet_len)

            if len(payload) <= AUTH_OPCODE_OFFSET or payload[AUTH_OPCODE_OFFSET] != OP_AUTH:
                raise ProtocolViolation(f"iusb: expected auth packet (opcode 0x{OP_AUTH:02X}), got payload {payload[:16]!r}")

            token_bytes = payload[AUTH_TOKEN_OFFSET:]
            nul = token_bytes.find(0)
            if nul >= 0:
                token_bytes = token_bytes[:nul]
            token = token_bytes.decode("ascii", errors="replace")

            if self.faults.force_auth_status is not None:
                status = self.faults.force_auth_status
                self.faults.force_auth_status = None
            elif token == self.expected_token:
                status = self.auth_success_status
            else:
                status = self.auth_token_rejected_status

            self._auth_status_seen = status
            ack_header, ack_payload = self._build_ack(status=status, request_header=header)
            self._send_frame(conn, ack_header, ack_payload)

            if status != self.auth_success_status:
                raise ProtocolViolation(f"iusb: auth rejected (status {status})")
        except (ProtocolViolation, OSError) as exc:
            self._handshake_error = exc
            self._handshake_event.set()
            self._release_session(conn)
            return
        self._handshake_event.set()

    def _release_session(self, conn: socket.socket) -> None:
        with self._slot_lock:
            if conn is self._conn:
                self._slot_held = False
                self._slot_owner_ip = None
        try:
            conn.close()
        except OSError:
            pass
        self._session_closed.set()

    def wait_for_handshake(self, timeout: float = 5.0) -> None:
        if not self._handshake_event.wait(timeout):
            raise TimeoutError("iusb handshake did not complete in time")
        if self._handshake_error is not None:
            raise self._handshake_error

    def auth_status_seen(self) -> int | None:
        """The connectionStatus byte this mock actually sent in its ACK, for a
        test to assert on directly rather than re-deriving it."""
        return self._auth_status_seen

    def session_closed(self, timeout: float = 5.0) -> bool:
        return self._session_closed.wait(timeout)

    # -- ACK construction ---------------------------------------------------

    def _server_device_type(self) -> int:
        """VERIFIED LIVE for SCSI request frames; ASSUMED, NOT VERIFIED that
        the same OR-in applies to the ACK frame -- see module docstring."""
        return self.device_type | SERVER_DEVICE_TYPE_BIT

    def _reserved(self) -> bytes:
        if self.faults.zero_reserved_bytes:
            return bytes(4)
        return self._reserved_bytes_value

    def _build_ack(self, *, status: int, request_header: Header, owner_ip: str | None = None) -> tuple[Header, bytes]:
        """Build the auth-ACK frame.

        VERIFIED LIVE oddity reproduced here on purpose: the header's
        major/minor/packetHeaderLen bytes come back as ZERO, not 1/0/32.
        """
        payload = bytearray(ACK_PAYLOAD_LEN)
        payload[AUTH_OPCODE_OFFSET] = OP_ACK
        payload[AUTH_STATUS_OFFSET] = status & 0xFF
        if owner_ip:
            ip_bytes = owner_ip.encode("ascii")
            end = min(AUTH_TOKEN_OFFSET + len(ip_bytes), ACK_PAYLOAD_LEN)
            payload[AUTH_TOKEN_OFFSET:end] = ip_bytes[: end - AUTH_TOKEN_OFFSET]

        header = Header(
            major=0,  # VERIFIED LIVE: zero, not 1.
            minor=0,  # VERIFIED LIVE: zero, not 0 (i.e. explicitly not the "normal" value either -- see below).
            header_len_field=0,  # VERIFIED LIVE: zero, not 32.
            checksum=0,
            data_packet_len=len(payload),
            device_type=self._server_device_type(),
            protocol=1,
            direction=0x00,  # server -> client, per the verified field table.
            instance=request_header.instance,
            sequence_number=request_header.sequence_number,
            reserved=self._reserved(),
        )
        return header, bytes(payload)

    # -- SCSI request driving -------------------------------------------------

    def _next_command_counter(self) -> int:
        if self.faults.sequential_command_counters:
            value = self._command_counter
            self._command_counter += 1
            return value
        if self._command_counter_increments_used == 0:
            value = self._command_counter
        else:
            increment = _COUNTER_INCREMENTS[(self._command_counter_increments_used - 1) % len(_COUNTER_INCREMENTS)]
            self._command_counter += increment
            value = self._command_counter
        self._command_counter_increments_used += 1
        return value

    def _build_envelope(self, step: ScsiStep, command_counter: int) -> bytes:
        payload = bytearray(step.envelope_len)
        payload[ENV_OFF_DATA_LEN : ENV_OFF_DATA_LEN + 4] = _le32(step.transfer_length)
        payload[ENV_OFF_CMD_COUNTER : ENV_OFF_CMD_COUNTER + 4] = _le32(command_counter)
        payload[ENV_OFF_MARKER] = ENV_MARKER_BYTE
        payload[ENV_OFF_OPCODE] = step.opcode & 0xFF
        tail = step.cdb_tail
        end = min(ENV_OFF_OPCODE + 1 + len(tail), step.envelope_len)
        payload[ENV_OFF_OPCODE + 1 : end] = tail[: end - (ENV_OFF_OPCODE + 1)]
        return bytes(payload)

    def send_scsi_request(self, step: ScsiStep) -> int:
        """Send one scripted SCSI request at the client. Returns the header
        sequence number used, so a caller can correlate it with the reply."""
        if self._conn is None:
            raise SlotHeldError("iusb mock: no authenticated connection to send a request on")
        counter = step.command_counter if step.command_counter is not None else self._next_command_counter()
        payload = self._build_envelope(step, counter)
        sequence_number = self._header_sequence
        self._header_sequence += 1
        header = Header(
            major=1,
            minor=0,
            header_len_field=HEADER_LEN,
            checksum=0,
            data_packet_len=len(payload),
            device_type=self._server_device_type(),
            protocol=1,
            direction=0x00,  # server -> client
            instance=step.instance,
            sequence_number=sequence_number,
            reserved=self._reserved(),
        )
        self._send_frame(self._conn, header, payload)
        return sequence_number

    def send_kill(self) -> None:
        """Send the 0xF6 redirection-kill frame, which ends the client's serve loop."""
        if self._conn is None:
            raise SlotHeldError("iusb mock: no authenticated connection to send a kill packet on")
        payload = bytearray(ENV_OFF_OPCODE + 1)
        payload[ENV_OFF_OPCODE] = OP_KILL
        header = Header(
            data_packet_len=len(payload),
            device_type=self._server_device_type(),
            protocol=1,
            direction=0x00,
            sequence_number=self._header_sequence,
            reserved=self._reserved(),
        )
        self._header_sequence += 1
        self._send_frame(self._conn, header, bytes(payload))

    def recv_response(self, timeout: float = 5.0) -> ScsiResponse:
        """Read one client reply frame and parse it. Does not itself validate
        against a request -- see :func:`validate_response`, which
        :meth:`run_script` calls automatically."""
        if self._conn is None:
            raise SlotHeldError("iusb mock: no authenticated connection to read a response from")
        self._conn.settimeout(timeout)
        try:
            header_bytes = _recv_exact(self._conn, HEADER_LEN)
            header = Header.parse(header_bytes)
            payload = _recv_exact(self._conn, header.data_packet_len)
        finally:
            self._conn.settimeout(None)
        return ScsiResponse(header=header, payload=payload)

    def idle(self, seconds: float) -> None:
        """Send nothing for ``seconds``. See :class:`IdleStep`."""
        time.sleep(seconds)

    def disconnect(self) -> None:
        """Abruptly close the active connection, mid-conversation."""
        if self._conn is not None:
            self._release_session(self._conn)
            self._conn = None

    def run_script(self, steps: list[ScsiStep | IdleStep | DisconnectStep], timeout: float = 5.0) -> list[ScsiResponse | None]:
        """Drive a scripted conversation, validating each SCSI reply as it
        arrives. Raises :class:`ProtocolViolation` on the first invalid reply;
        returns the list of parsed responses (``None`` for a step that did not
        expect one) if every step validated cleanly."""
        results: list[ScsiResponse | None] = []
        for step in steps:
            if isinstance(step, IdleStep):
                self.idle(step.seconds)
                results.append(None)
                continue
            if isinstance(step, DisconnectStep):
                self.disconnect()
                results.append(None)
                continue
            sequence_number = self.send_scsi_request(step)
            if not step.expect_response:
                results.append(None)
                continue
            response = self.recv_response(timeout=timeout)
            outcome = validate_response(step, sequence_number, response)
            if not outcome.ok:
                raise ProtocolViolation(f"iusb mock: client reply for opcode 0x{step.opcode:02X} failed validation: {'; '.join(outcome.problems)}")
            results.append(response)
        return results

    # -- fault-injected frame transmission -----------------------------------

    def _send_frame(self, conn: socket.socket, header: Header, payload: bytes) -> None:
        """Send one frame, applying and consuming any armed one-shot frame faults."""
        send_header = header
        if self.faults.lie_next_data_packet_len is not None:
            lied_len = self.faults.lie_next_data_packet_len
            self.faults.lie_next_data_packet_len = None
            send_header = dataclasses.replace(header, data_packet_len=lied_len)

        frame = send_header.marshal() + payload

        if self.faults.truncate_next_frame_to is not None:
            truncate_to = self.faults.truncate_next_frame_to
            self.faults.truncate_next_frame_to = None
            frame = frame[:truncate_to]

        conn.sendall(frame)

        if self.faults.disconnect_after_next_send:
            self.faults.disconnect_after_next_send = False
            self._release_session(conn)
            if conn is self._conn:
                self._conn = None

    def generate_test_token(self, length: int = 16) -> str:
        """Convenience: a random token of the shape real ``-kvmtoken`` values
        take (ASCII alphanumeric). Not used internally; provided so a test or
        a runner script can mint one without importing ``secrets`` itself."""
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        return "".join(secrets.choice(alphabet) for _position in range(length))
