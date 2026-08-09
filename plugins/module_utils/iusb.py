# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""AMI's proprietary IUSB protocol: framing, auth handshake, and a read-only
virtual CD-ROM SCSI/MMC emulator, for serving a local ISO to an ASMB8-iKVM
BMC's virtual-media listener (port 5120 by default).

Ported from a standalone proof-of-concept client (a private development tree, not shipped, in this
task's working tree: ``iusb_header.py``, ``iusb_auth.py``, ``iusb_scsi.py``,
``iusb_cache.py``, ``iusb_reader.py``, ``iusb_session.py``) which was itself
ported from ``rd450x-console`` (``BadCoder1337/rd450x-console``,
MIT-licensed -- see ``licenses/MIT.txt``), a third-party Go client for a
related AMI MegaRAC board, and cross-checked field-by-field against decompiled
AMI JViewer sources and a direct disassembly of the vendor's native SCSI
dispatcher. See ``docs/protocol-notes.md`` for the full
provenance ledger, every disagreement between those sources and which one
won, and the still-open live-hardware verification targets.

**This module has been validated against the real target hardware** (see this
collection's task history): a Proxmox ISO was booted end-to-end over exactly
this wire format. Two facts earned that confidence and are treated as fixed
by this module rather than as something a future change gets to "clean up":

* The SCSI/MMC command set is exactly six opcodes -- ``TEST UNIT READY``
  (0x00), ``START STOP UNIT`` (0x1B), ``READ CAPACITY(10)`` (0x25),
  ``READ(10)`` (0x28), ``READ TOC`` (0x43), ``READ(12)`` (0xA8) -- at a
  2048-byte block size. INQUIRY is never forwarded: the BMC's own firmware
  answers it. See :class:`CDROMDevice`.
* **Idle is not failure.** A live, attached session can go quiet for minutes
  at a time -- verified directly: 130 consecutive seconds of total silence
  while a host sat at a bootloader menu, followed by reads resuming normally.
  Nothing in this module ever treats a socket read timing out, by itself, as
  an error. See :class:`IdleTimeout` and :meth:`Session.serve_forever`, which
  is the one piece of this file with no PoC/Go/JViewer precedent at all -- the
  PoC's equivalent loop got this wrong (raised ``TimeoutError`` on an idle
  socket, which would tear down a single-occupancy session that was about to
  be needed again) and this module exists specifically to fix that.

Everywhere the original PoC raised a bare ``ValueError``/``EOFError`` for a
wire-level fault, this port raises this collection's own
:class:`errors.IkvmError` subclasses instead, per this collection's
convention that every failure is classified. ``IdleTimeout`` remains a plain,
undocumented-to-callers control-flow marker: it must never escape
:meth:`Session.serve_forever` to reach a caller, so it has no place in the
``ErrorClass`` taxonomy.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import socket
import struct
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple, Protocol

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import (
    BmcBusyError,
    ConnectionError_,
    ProtocolError,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

# --- Framing constants -------------------------------------------------------

HEADER_LEN = 32
SIGNATURE = b"IUSB    "  # "IUSB" + 4 spaces, exactly 8 bytes.

IUSB_MAJOR = 1
IUSB_MINOR = 0

#: Device types (IUSBHeader.deviceType / createCDROMHeader). Confirmed from the
#: decompiled CDROMRedir.SendAuth_SessionToken, and *also* from FloppyRedir and
#: HarddiskRedir, which both call the SAME IUSBHeader.createCDROMHeader(...)
#: (deviceType hardcoded to 5) for their own auth packets. deviceType is 5 for
#: the AUTH packet regardless of which device (CD/FD/HD) is being redirected --
#: the port number, not this header byte, is what selects the device
#: server-side. Only CD-ROM is implemented here, so this is the only value
#: this module ever sends.
DEVICE_CDROM = 5

#: Default per-device TLS/plaintext TCP ports on the BMC. Only CD (5120) is
#: exercised by this collection; FD/HD ports are kept for reference/parity
#: with the reference client and are not used by anything in this module.
PORT_CD = 5120
PORT_FD = 5122
PORT_HD = 5123

#: Opcodes carried in the SCSI payload at OPCODE_OFFSET. The 0xF* values are
#: AMI's own redirection-control opcodes, confirmed against decompiled
#: IUSBSCSI.OPCODE_EJECT=27/OPCODE_KILL_REDIR=246 and
#: CDROMRedir.DEVICE_REDIRECTION_ACK=241/AUTH_CMD=242.
OP_AUTH = 0xF2  # client -> server: session-token authentication
OP_REDIRECT_ACK = 0xF1  # server -> client: redirection acknowledgement
OP_KILL_REDIR = 0xF6  # server -> client: terminate this redirection
OP_START_STOP_UNIT = 0x1B  # standard SCSI START STOP UNIT (eject request)

# Offsets inside the IUSB payload (after the 32-byte header). The SCSI CDB
# begins at OPCODE_OFFSET, so opcode == cdb[0] and the eject byte == cdb[4].
ENV_OFF_DATA_LEN = 0  # payload[0:4]  = transfer/data length, u32 LE
#: payload[25:29] = response length the BMC actually forwards to the host --
#: verified live against the target hardware (READ(10) traffic for LBA 0, 1,
#: 16-18, the El Torito boot catalog at LBA 4660, and multi-block reads up to
#: 16 blocks / 32 KiB all round-tripped correctly once this offset, not just
#: offset 0, carried the response length).
ENV_OFF_RESP_LEN = 25
OPCODE_OFFSET = 9  # payload[9]  = SCSI CDB byte 0 (opcode)
EJECT_BYTE_OFFSET = 13  # payload[13] = SCSI CDB byte 4 (START STOP UNIT loej)
CONN_STATUS_OFFSET = 30  # payload[30] = connectionStatus in an ACK / token-type in auth
AUTH_TOKEN_OFFSET = 31  # payload[31:] = web session token (kvmtoken) in an auth packet
AUTH_PAYLOAD_LEN = 128  # web-session-token (type 0) auth payload length
SSI_AUTH_PAYLOAD_LEN = 240  # SSI-token (type 1) auth payload length

# Connection-status values reported in an ACK's CONN_STATUS_OFFSET byte.
CONN_OK = 1
CONN_ERR_IN_USE_5 = 5
CONN_ERR_IN_USE_8 = 8

# The six SCSI/MMC opcodes the real vendor CD-ROM SCSI dispatcher recognizes --
# confirmed by disassembling the vendor's own native SCSI dispatcher (see this
# module's docstring). Block size is 2048, not 512; there is no WRITE opcode
# anywhere in the dispatch table for either device profile (CD-ROM is
# read-only by construction here, not by a runtime flag).
CD_BLOCK_SIZE = 2048
SCSI_TEST_UNIT_READY = 0x00
SCSI_START_STOP_UNIT = 0x1B
SCSI_READ_CAPACITY10 = 0x25
SCSI_READ10 = 0x28
SCSI_READ_TOC = 0x43
SCSI_READ12 = 0xA8

#: AMI's own redirection-control opcodes (0xF0-0xFF) ride in the same SCSI
#: opcode byte slot but are not SCSI at all -- 0xF1 ack/0xF2 auth are consumed
#: by the handshake before Session.serve_forever ever calls into
#: CDROMDevice.handle, and 0xF6 kill is intercepted by serve_forever itself
#: (Packet.is_kill()) before dispatch. This constant exists purely so an
#: unexpected control opcode that somehow reaches handle() gets a harmless,
#: silent envelope echo instead of the "unhandled opcode" warning log --
#: believed unreachable in practice for the CD-ROM channel (the disassembly
#: shows a hard error, not an ack, for anything outside the six opcodes
#: above); flagged as a live-hardware verification target if it is ever hit.
AMI_CONTROL_OPCODE_MIN = 0xF0

#: The BMC's own MAX_READ_SIZE is 128 KiB (0x20000, per the decompiled
#: CDROMRedir.MAX_READ_SIZE constant); cap accepted payload length a little
#: above that so a corrupt/hostile length field cannot wedge this process into
#: a huge allocation.
MAX_PACKET_PAYLOAD = 0x20000 + 4096

#: How long Session.connect()/SocketTransport.connect() wait for the initial
#: TCP handshake and the auth ACK. NOT the idle-poll timeout used once a
#: session is serving -- see Session.set_poll_timeout.
DEFAULT_DIAL_TIMEOUT = 15.0

# --- windowed cache tuning ---------------------------------------------------

#: Aligned fetch granularity, larger than the BMC's 128 KiB single-request cap
#: so sequential ISO9660/El-Torito access (the observed live access pattern)
#: mostly serves out of one cached window instead of one backing read per SCSI
#: command.
WINDOW_SIZE = 512 * 1024
#: LRU capacity in windows -> a 32 MiB cap regardless of how large the backing
#: ISO is (verified in this collection's tests at both 200 MiB and 1.5 GiB
#: sparse-file scale: neither Cache nor FileReader ever loads more than one
#: bounded window at a time).
DEFAULT_MAX_WINDOWS = 64


@dataclasses.dataclass
class Header:
    """A decoded IUSB packet header. Multi-byte fields are little-endian on the
    wire; ``data_packet_len`` is the framing length (payload bytes following
    the header) -- confirmed little-endian by both the Go reference
    (``binary.LittleEndian``) and the decompiled Java
    (``ByteOrder.LITTLE_ENDIAN`` buffers, per ``CDROMRedir.setBufferEndianness``).
    """

    major: int = IUSB_MAJOR
    minor: int = IUSB_MINOR
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

    def marshal(self) -> bytes:
        """Serialize to the 32-byte wire form, filling in the checksum byte so
        the receiver's sum over the 32 header bytes is zero (mod 256).

        This deliberately does NOT reproduce the decompiled JViewer's own
        checksum-as-sent, which turns out to be contingent on native
        ``ByteBuffer`` capacity and reuse history rather than a clean "sum
        these N bytes" rule (see ``docs/protocol-notes.md``
        s2.1 for the full trace). This keeps the Go reference's clean,
        self-consistent, testable behavior instead: checksum over exactly the
        32 header bytes, computed last. Safe because the checksum is
        empirically unenforced by the BMC (confirmed live: this module has
        booted a real ISO with this checksum) and :meth:`parse` does not
        validate it either, for the same reason.
        """
        b = bytearray(HEADER_LEN)
        b[0:8] = SIGNATURE
        b[8] = self.major & 0xFF
        b[9] = self.minor & 0xFF
        b[10] = HEADER_LEN
        # b[11] checksum filled in below.
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
        # b[28:32] reserved. Left zero on send -- but see parse()'s note that
        # the BMC's OWN request frames do not always keep this zero, so this
        # module never asserts it is.
        checksum = (-sum(b)) & 0xFF
        b[11] = checksum
        return bytes(b)

    @staticmethod
    def parse(b: bytes) -> Header:
        """Decode a 32-byte IUSB header. Validates the signature but not the
        checksum (see :meth:`marshal`'s docstring for why there is nothing
        meaningful to validate there).

        Does not validate the reserved bytes at offset 28 either: observed
        live against the target hardware, the BMC's own request frames do not
        always keep them zero (one capture showed ``bc 38 02 bf``). This
        parser tolerates both, and so must anything built on top of it.
        """
        if len(b) < HEADER_LEN:
            raise ProtocolError(f"iusb: short header: {len(b)} bytes", operation="iusb.parse_header")
        if b[0:8] != SIGNATURE:
            raise ProtocolError(f"iusb: bad signature {b[0:8]!r}", operation="iusb.parse_header")
        (data_packet_len,) = struct.unpack_from("<I", b, 12)
        (sequence_number,) = struct.unpack_from("<I", b, 24)
        return Header(
            major=b[8],
            minor=b[9],
            data_packet_len=data_packet_len,
            server_caps=b[16],
            device_type=b[17],
            protocol=b[18],
            direction=b[19],
            device_number=b[20],
            interface_number=b[21],
            client_data=b[22],
            instance=b[23],
            sequence_number=sequence_number,
        )


@dataclasses.dataclass
class Packet:
    """A decoded IUSB packet: a header plus its payload (the SCSI envelope).

    ``sequence_number`` (header offset 24) is what a client must echo
    verbatim on its response -- verified live: the SCSI envelope's own
    ``cmdctr`` field (payload offset 4) is non-sequential (observed 3, 7, 16)
    and must NOT be used for request/response pairing. Nothing in this module
    reads ``cmdctr`` for that purpose; :func:`build_response_frame` keys off
    the header's sequence number only.
    """

    header: Header
    payload: bytes = b""

    def opcode(self) -> int:
        """Return the SCSI/redirection opcode (``payload[OPCODE_OFFSET]``), or
        0 if the payload is too short to carry one.
        """
        if len(self.payload) <= OPCODE_OFFSET:
            return 0
        return self.payload[OPCODE_OFFSET]

    def cdb(self) -> bytes:
        """Return the SCSI CDB embedded in the IUSB payload (starting at
        OPCODE_OFFSET), or ``b""`` if the payload is too short.
        """
        if len(self.payload) <= OPCODE_OFFSET:
            return b""
        return self.payload[OPCODE_OFFSET:]

    def is_eject(self) -> bool:
        """Report whether this is a START STOP UNIT eject request.

        Exact equality to 2 at the loej byte, NOT a bitmask -- decompiled
        ``CDROMRedir.run()``'s ``iUSBSCSI.Lba == 2`` check, not the Go
        reference's ``payload[13] & 0x03 == 2``. A hypothetical loej byte of
        0x06 would read as eject under the masked comparison but not under
        this one. Unverified live: no capture has confirmed which byte
        value(s) a real eject actually sends (0x02 is what the SCSI spec's
        own loej/power-condition encoding implies), so this remains a
        live-hardware test target.
        """
        return self.opcode() == OP_START_STOP_UNIT and len(self.payload) > EJECT_BYTE_OFFSET and self.payload[EJECT_BYTE_OFFSET] == 2

    def is_kill(self) -> bool:
        """Report whether the BMC asked to terminate the redirection."""
        return self.opcode() == OP_KILL_REDIR


# --- auth handshake -----------------------------------------------------------


def build_auth(device_type: int, instance: int, token: str, ssi: bool = False) -> bytes:
    """Assemble the IUSB session-token authentication packet: a 32-byte
    header plus a 128-byte (web session token) or 240-byte (SSI token)
    payload.

    ``token`` is the ``-kvmtoken`` minted by fetching ``jviewer.jnlp`` (see
    ``asp.py``'s ``allocate_media_session``) -- NOT the output of a bare
    ``getsessiontoken.asp`` call, which is observed live to return an EMPTY
    ``STOKEN`` and is useless here. This function takes the token as a plain
    parameter; obtaining it is out of scope for this module by design, which
    keeps this module a pure, network-free protocol codec.
    """
    payload_len = SSI_AUTH_PAYLOAD_LEN if ssi else AUTH_PAYLOAD_LEN
    header = Header(
        major=IUSB_MAJOR,
        minor=IUSB_MINOR,
        data_packet_len=payload_len,
        device_type=device_type,
        protocol=1,
        direction=128,
        instance=instance,
    )
    header_bytes = header.marshal()

    payload = bytearray(payload_len)
    payload[OPCODE_OFFSET] = OP_AUTH
    payload[CONN_STATUS_OFFSET] = 0  # token-type byte: 0 = web session, 1 = SSI
    token_bytes = token.encode("ascii")
    end = AUTH_TOKEN_OFFSET + len(token_bytes)
    if end > payload_len:
        raise ProtocolError(
            f"iusb: token ({len(token_bytes)} bytes) does not fit in the {payload_len}-byte auth payload starting at offset {AUTH_TOKEN_OFFSET}",
            operation="iusb.build_auth",
        )
    payload[AUTH_TOKEN_OFFSET:end] = token_bytes
    return header_bytes + bytes(payload)


def other_ip(payload: bytes) -> str:
    """Extract the NUL/whitespace-trimmed owner IP an ACK carries at
    AUTH_TOKEN_OFFSET when the device is already redirected (JViewer's
    ``m_otherIP``).
    """
    if len(payload) <= AUTH_TOKEN_OFFSET:
        return ""
    raw = payload[AUTH_TOKEN_OFFSET:]
    nul = raw.find(0)
    if nul >= 0:
        raw = raw[:nul]
    return raw.decode("ascii", errors="replace").strip()


def interpret_ack(packet: Packet, *, endpoint: str | None = None) -> None:
    """Validate a redirection ACK packet (opcode 0xF1). Raises on anything but
    "accepted" (connectionStatus == 1).

    Every rejection this BMC can report at this point in the handshake is
    raised as :class:`errors.BmcBusyError`, deliberately, not a generic
    protocol failure: this board allows exactly one active media/iUSB
    session and has no server-side timeout to reclaim an abandoned one (see
    ``media_session.py``'s module docstring and ``ErrorClass.BMC_BUSY``), so
    a rejected auth overwhelmingly means a stale session still holds the
    slot rather than a wire-format bug. The one exception is an unexpected
    opcode in the ACK's place, which is a real protocol-level confusion (the
    handshake sequence itself broke down) and is raised as
    :class:`errors.ProtocolError` instead.
    """
    if packet.opcode() != OP_REDIRECT_ACK:
        raise ProtocolError(
            f"iusb: expected redirection ACK (0x{OP_REDIRECT_ACK:02X}), got opcode 0x{packet.opcode():02X}",
            endpoint=endpoint,
            operation="iusb.authenticate",
        )
    if len(packet.payload) <= CONN_STATUS_OFFSET:
        raise ProtocolError(f"iusb: ACK payload too short ({len(packet.payload)} bytes)", endpoint=endpoint, operation="iusb.authenticate")
    status = packet.payload[CONN_STATUS_OFFSET]
    if status == CONN_OK:
        return

    owner_ip = other_ip(packet.payload)
    detail = f" It reports being held by {owner_ip}." if owner_ip else ""
    raise BmcBusyError(
        f"BMC rejected the media session (connectionStatus={status}).{detail} This BMC allows exactly one active "
        "iUSB/media session and has no server-side timeout to reclaim an abandoned one, so this is very often a "
        "stale session left over from a previous attach, not a protocol failure. If a previous session_id for "
        "this endpoint is known, detach it first (state=detached); this module also always attempts to reclaim "
        "any session it can find recorded for this endpoint before every attach. If no software reclamation "
        "clears it, a BMC cold reset (`ipmitool mc reset cold`, or the pyghmi equivalent) is the operator's "
        "escape hatch -- it does not affect host power.",
        endpoint=endpoint,
        operation="iusb.authenticate",
    )


# --- ISO backing store: Reader protocol, FileReader, windowed Cache --------


class Reader(Protocol):
    """The backing store for a redirected image: random-access byte ranges of
    a fixed-size medium. Implementations: :class:`FileReader` and, wrapping
    one, :class:`Cache`.

    ``read_at(n, offset)`` simply returns up to ``n`` bytes, possibly fewer at
    end-of-medium, with no exception -- callers (:class:`CDROMDevice`,
    :class:`Cache`) are written to zero-fill or otherwise handle a short
    result themselves.
    """

    def read_at(self, n: int, offset: int) -> bytes: ...

    def size(self) -> int: ...


class FileReader:
    """Serves a local ISO image file directly via seek+read. Never loads the
    file into memory: ``read_at()`` always performs exactly one bounded
    ``seek()`` + ``read(n)``, regardless of file size.

    CD-ROM is read-only-only in this module (see the module docstring): there
    is no write path here at all, unlike a hypothetical floppy/HD reader.
    """

    def __init__(self, path: str) -> None:
        if os.path.isdir(path):
            raise IsADirectoryError(f"iusb: {path} is a directory, not an image")
        self._f = open(path, "rb")
        self._size = os.fstat(self._f.fileno()).st_size

    @classmethod
    def open(cls, path: str) -> FileReader:
        return cls(path)

    def read_at(self, n: int, offset: int) -> bytes:
        if n <= 0 or offset < 0:
            return b""
        self._f.seek(offset)
        return self._f.read(n)

    def size(self) -> int:
        return self._size

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> FileReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class CacheReadOnlyError(Exception):
    """Raised by :meth:`Cache.write_at` when constructed without a writer.

    Not part of ``errors.IkvmError``: this is a generic, reusable-utility
    invariant (see :class:`Cache`'s docstring), not a BMC wire/protocol
    failure. Every :class:`Cache` this module actually constructs is built
    with ``writer=None`` (CD-ROM has no write opcode at all -- see the module
    docstring), so this is never reachable through the CD-ROM serve path;
    it exists because :class:`Cache` is kept as a faithful, independently
    useful port rather than trimmed to only what CD-ROM needs today.
    """


@dataclasses.dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    fetched_bytes: int = 0


class Cache:
    """A windowed, read-ahead LRU in front of a :class:`Reader`. Satisfies the
    ``Reader`` protocol itself, so it drops directly between
    :class:`CDROMDevice` and a slow backing.

    The BMC requests at most 128 KiB per SCSI read, but ISO9660/El-Torito
    access is largely sequential (verified live: LBA 0, 1, 16-18, then the
    boot catalog at 4660, then ascending multi-block reads), so a miss fetches
    a larger aligned :data:`WINDOW_SIZE` window from the backing and serves
    subsequent reads from memory. Bounded to :data:`DEFAULT_MAX_WINDOWS`
    windows so a multi-GB ISO is never pulled fully into memory.
    """

    def __init__(
        self,
        reader: Reader,
        writer: Reader | None = None,
        window_size: int = WINDOW_SIZE,
        max_windows: int = DEFAULT_MAX_WINDOWS,
    ) -> None:
        self._r = reader
        self._w = writer
        self._window_size = window_size
        self._max_windows = max_windows
        self._size = reader.size()
        self._lock = threading.Lock()
        self._windows: OrderedDict[int, bytes] = OrderedDict()  # MRU at the end
        self._hits = 0
        self._misses = 0
        self._fetched = 0

    def size(self) -> int:
        return self._size

    def read_at(self, n: int, offset: int) -> bytes:
        """Fill up to ``n`` bytes starting at ``offset`` from cached windows,
        fetching any missing aligned window from the backing. Returns fewer
        than ``n`` bytes at/after end-of-medium (never raises for a short
        read).
        """
        if offset < 0:
            raise ValueError("iusb: negative read offset")
        if offset >= self._size:
            return b""
        end = min(offset + n, self._size)

        out = bytearray()
        pos = offset
        while pos < end:
            win_start = pos - (pos % self._window_size)
            win = self._window(win_start)
            in_win = pos - win_start
            if in_win >= len(win):
                break  # window short of window_size (end-of-medium) -> stop
            chunk_len = min(len(win) - in_win, end - pos)
            if chunk_len == 0:
                break
            out += win[in_win : in_win + chunk_len]
            pos += chunk_len
        return bytes(out)

    def _window(self, start: int) -> bytes:
        with self._lock:
            if start in self._windows:
                self._windows.move_to_end(start)
                self._hits += 1
                return self._windows[start]
            self._misses += 1

        want = self._window_size
        if start + want > self._size:
            want = self._size - start
        data = self._r.read_at(want, start)

        with self._lock:
            self._fetched += len(data)
            if start in self._windows:
                # A concurrent fetch may have populated this window meanwhile;
                # prefer the existing entry so all readers share one bytes object.
                self._windows.move_to_end(start)
                return self._windows[start]
            self._windows[start] = data
            self._windows.move_to_end(start)
            while len(self._windows) > self._max_windows:
                self._windows.popitem(last=False)  # evict least-recently-used
            return data

    def write_at(self, data: bytes, offset: int) -> int:
        """Write through to the backing and invalidate any cached windows the
        write touches. Raises :class:`CacheReadOnlyError` if constructed with
        no writer -- see that class's docstring for why this path is never
        exercised by the CD-ROM serve path.
        """
        if self._w is None:
            raise CacheReadOnlyError("iusb: cache backing is read-only")
        n = self._w.write_at(data, offset)
        self._invalidate(offset, len(data))
        return n

    def _invalidate(self, offset: int, length: int) -> None:
        if length <= 0:
            return
        end = offset + length
        with self._lock:
            w = offset - (offset % self._window_size)
            while w < end:
                self._windows.pop(w, None)
                w += self._window_size

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(self._hits, self._misses, self._fetched)

    def cached_window_count(self) -> int:
        """Number of windows currently resident -- lets a test prove memory
        stays bounded rather than growing with the medium size.
        """
        with self._lock:
            return len(self._windows)


# --- SCSI/MMC emulation -------------------------------------------------------


class CDROMDevice:
    """Emulates a read-only virtual CD-ROM's SCSI/MMC command set (see the
    module docstring for the six confirmed opcodes), answering the BMC's
    IUSB-wrapped SCSI requests over a local ISO image via a ``Reader``-shaped
    backing (typically a :class:`Cache` in front of a :class:`FileReader`).

    ``handle(req)`` is a pure function of its input beyond its own byte/sector
    counters: no socket I/O, nothing that depends on wall-clock time. That is
    what lets it be driven directly by tests (including byte-exact golden
    vectors) and by :func:`process_frame` without a transport in the loop at
    all.
    """

    def __init__(self, reader: Reader) -> None:
        self._r = reader
        n = reader.size() // CD_BLOCK_SIZE
        self.last_lba = (n - 1) if n > 0 else 0
        self.n_bytes = 0
        self.n_blocks = 0

    def bytes_served(self) -> int:
        return self.n_bytes

    def blocks_served(self) -> int:
        return self.n_blocks

    def handle(self, req: Packet) -> bytes | None:
        """Dispatch one IUSB SCSI request, returning the response payload (or
        ``None`` to send no reply, matching a payload too short to carry an
        opcode at all).
        """
        cdb = req.cdb()
        if not cdb:
            return None
        op = cdb[0]

        if op == SCSI_TEST_UNIT_READY:
            return self._build_response(req, b"")

        if op == SCSI_START_STOP_UNIT:
            # Status-only ack. Eject *detection* is the caller's job
            # (Packet.is_eject) -- this only needs to answer the command so
            # the host does not stall waiting for a reply.
            return self._build_response(req, b"")

        if op == SCSI_READ_CAPACITY10:
            return self._build_response(req, self._read_capacity())

        if op == SCSI_READ10:
            lba = int.from_bytes(cdb[2:6], "big")
            blocks = int.from_bytes(cdb[7:9], "big")
            return self._build_response(req, self._read(lba, blocks))

        if op == SCSI_READ12:
            lba = int.from_bytes(cdb[2:6], "big")
            blocks = int.from_bytes(cdb[6:10], "big")
            return self._build_response(req, self._read(lba, blocks))

        if op == SCSI_READ_TOC:
            return self._build_response(req, self._read_toc(cdb))

        if op >= AMI_CONTROL_OPCODE_MIN:
            return self._build_response(req, b"")

        logger.warning("iusb: unhandled SCSI opcode 0x%02X (replying empty)", op)
        return self._build_response(req, b"")

    # -- response framing -----------------------------------------------

    def _build_response(self, req: Packet, data: bytes) -> bytes:
        """Wrap SCSI response data in the IUSB response payload: echo the
        request's command envelope verbatim, append the SCSI data-in bytes,
        and set the appended-byte-count at BOTH envelope offset 0 and offset
        25 (:data:`ENV_OFF_RESP_LEN` -- see that constant's docstring for why
        offset 25 alone is load-bearing, verified live).
        """
        env = req.payload
        out = bytearray(env)
        out += data
        n = len(data)
        if len(out) >= ENV_OFF_DATA_LEN + 4:
            out[ENV_OFF_DATA_LEN : ENV_OFF_DATA_LEN + 4] = n.to_bytes(4, "little")
        if len(out) >= ENV_OFF_RESP_LEN + 4:
            out[ENV_OFF_RESP_LEN : ENV_OFF_RESP_LEN + 4] = n.to_bytes(4, "little")
        return bytes(out)

    # -- SCSI command bodies ----------------------------------------------

    def _read(self, lba: int, blocks: int) -> bytes:
        """Return ``blocks * CD_BLOCK_SIZE`` bytes starting at ``lba``,
        zero-filling any short read past end-of-medium.
        """
        n = blocks * CD_BLOCK_SIZE
        off = lba * CD_BLOCK_SIZE
        data = self._r.read_at(n, off)
        buf = bytearray(n)
        buf[: len(data)] = data
        self.n_bytes += len(data)
        self.n_blocks += blocks
        return bytes(buf)

    def _read_capacity(self) -> bytes:
        """READ CAPACITY(10) response: last LBA and block size, both
        big-endian, 8 bytes total -- byte-exact verified against real
        firmware.
        """
        return self.last_lba.to_bytes(4, "big") + CD_BLOCK_SIZE.to_bytes(4, "big")

    def _read_toc(self, cdb: bytes) -> bytes:
        """Minimal single-data-track TOC (formatted, MSF=0). ``cdb`` is unused
        for now (this does not yet branch on the CDB's format field) but kept
        for future MSF-format handling.
        """

        def track(no: int, lba: int) -> bytes:
            d = bytearray(8)
            d[1] = 0x14  # ADR/control: data track
            d[2] = no
            d[4:8] = lba.to_bytes(4, "big")
            return bytes(d)

        body = track(1, 0) + track(0xAA, self.last_lba + 1)
        out = bytearray(4 + len(body))
        out[0:2] = (len(body) + 2).to_bytes(2, "big")
        out[2] = 1  # first track
        out[3] = 1  # last track
        out[4:] = body
        return bytes(out)


# --- pure framing functions (no transport) -----------------------------------


def parse_request_frame(frame: bytes) -> Packet:
    """Parse one raw IUSB frame (header + exactly ``header.data_packet_len``
    payload bytes, nothing more) into a :class:`Packet`. Raises
    :class:`errors.ProtocolError` if the frame is short or the trailing
    payload length does not match the header.
    """
    if len(frame) < HEADER_LEN:
        raise ProtocolError(f"iusb: frame shorter than the {HEADER_LEN}-byte header", operation="iusb.parse_request_frame")
    header = Header.parse(frame[:HEADER_LEN])
    payload = frame[HEADER_LEN:]
    if len(payload) != header.data_packet_len:
        raise ProtocolError(
            f"iusb: frame payload is {len(payload)} bytes, header says {header.data_packet_len}",
            operation="iusb.parse_request_frame",
        )
    return Packet(header=header, payload=payload)


def build_response_frame(req_header: Header, payload: bytes, device_type: int) -> bytes:
    """Build the raw response frame for a request header + response payload:
    an IUSB header (echoing the request's instance and sequence number,
    direction=128) followed by the payload.
    """
    resp_header = Header(
        data_packet_len=len(payload),
        device_type=device_type,
        protocol=1,
        direction=128,
        instance=req_header.instance,
        sequence_number=req_header.sequence_number,
    )
    return resp_header.marshal() + payload


class Handler(Protocol):
    """Anything with a ``handle(req) -> bytes | None`` method can drive a
    :class:`Session` -- :class:`CDROMDevice` satisfies this structurally.
    """

    def handle(self, req: Packet) -> bytes | None: ...


def process_frame(handler: Handler, frame: bytes, device_type: int = DEVICE_CDROM) -> bytes | None:
    """Pure function: one raw request frame in, one raw response frame out
    (or ``None`` if no response should be sent -- a kill packet, or a handler
    that returns ``None``). No transport, no socket, no shared state beyond
    the handler's own bookkeeping.

    This is the differential-testing seam: feed it a frame captured from the
    Go reference implementation (or a real packet capture) and diff the
    return value against the corresponding captured response, byte for byte.
    """
    req = parse_request_frame(frame)
    if req.is_kill():
        return None
    payload = handler.handle(req)
    if payload is None:
        return None
    return build_response_frame(req.header, payload, device_type)


# --- transport-driving Session ------------------------------------------------

FrameHook = Callable[[str, bytes], None]  # (direction "tx"|"rx", raw frame bytes)


class FrameEvent(NamedTuple):
    direction: str  # "tx" (this client sent it) or "rx" (this client received it)
    data: bytes


class FrameRecorder:
    """An in-memory, in-order trace of every raw IUSB frame sent/received.

    Pass ``recorder.record`` as a :class:`Session`'s ``frame_hook`` (or to
    :func:`read_packet`/:func:`write_frame` directly) to capture every frame
    a session sends or receives, verbatim, in order -- used by this
    collection's own tests to assert an exact frame sequence for a full
    auth-then-serve exchange.
    """

    def __init__(self) -> None:
        self.events: list[FrameEvent] = []

    def record(self, direction: str, data: bytes) -> None:
        self.events.append(FrameEvent(direction, bytes(data)))

    def to_hex_lines(self) -> list[str]:
        return [f"{e.direction} {e.data.hex()}" for e in self.events]


class IdleTimeout(Exception):
    """Raised internally by :class:`SocketTransport` when the socket's poll
    timeout elapses with zero bytes read for the *current* logical read (a
    fresh packet header, not a payload already in flight).

    This is the single most important distinction in this module (see the
    module docstring and ``media_session.py``'s serve loop): an idle socket
    at a frame boundary is a perfectly healthy attached session -- verified
    live at 130 continuous seconds of silence -- and must never surface as a
    failure. It must never escape :meth:`Session.serve_forever`; every caller
    of :func:`read_packet` that is not that loop should let this propagate as
    a bug report, not a session teardown.

    Contrast with a timeout that occurs *mid-frame* (some header or payload
    bytes already received for this read): that is a genuinely stalled
    connection, not idle, and :class:`SocketTransport.recv_exact` raises
    :class:`errors.ConnectionError_` for that case instead of this one.
    """


class Transport(Protocol):
    """The minimum a byte-stream carrier must support for IUSB framing.
    :class:`SocketTransport` is the only implementation here.
    """

    def recv_exact(self, n: int) -> bytes: ...

    def send_all(self, data: bytes) -> None: ...

    def set_timeout(self, seconds: float | None) -> None: ...

    def close(self) -> None: ...


class SocketTransport:
    """Transport backed by a plain blocking TCP socket. Dedicated-port vmedia
    is a bare ``socket.connect((host, port))`` with no preamble -- IUSB
    framing begins at byte 0 of the connection (confirmed against decompiled
    ``PacketMaster.connectVmedianonssl()``, and against the live target board,
    where ports 5120/5122/5123/7578 are bound only on demand after a JNLP
    fetch -- a closed/refused port before that is not this module's problem
    to solve, see ``asp.py``'s ``allocate_media_session``).
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    @classmethod
    def connect(cls, host: str, port: int, timeout: float = DEFAULT_DIAL_TIMEOUT) -> SocketTransport:
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except TimeoutError as exc:
            raise ConnectionError_(f"iusb: timed out connecting to {host}:{port}", endpoint=f"{host}:{port}", operation="iusb.connect") from exc
        except OSError as exc:
            raise ConnectionError_(f"iusb: could not connect to {host}:{port}: {exc}", endpoint=f"{host}:{port}", operation="iusb.connect") from exc
        return cls(sock)

    def set_timeout(self, seconds: float | None) -> None:
        self._sock.settimeout(seconds)

    def recv_exact(self, n: int) -> bytes:
        """Read exactly ``n`` bytes, or raise.

        A socket timeout with zero bytes read so far for this call is idle
        (raises :class:`IdleTimeout`); a socket timeout after some bytes have
        already arrived is a stalled connection, not idle (raises
        :class:`errors.ConnectionError_`). See :class:`IdleTimeout`'s
        docstring for why that split matters.
        """
        if n == 0:
            return b""
        chunks = bytearray()
        while len(chunks) < n:
            try:
                chunk = self._sock.recv(n - len(chunks))
            except TimeoutError as exc:
                if not chunks:
                    raise IdleTimeout from exc
                raise ConnectionError_(f"iusb: connection stalled mid-frame after {len(chunks)} of {n} bytes", operation="iusb.read_packet") from exc
            except OSError as exc:
                raise ConnectionError_(f"iusb: socket error while reading: {exc}", operation="iusb.read_packet") from exc
            if not chunk:
                raise EOFError("iusb: connection closed while reading")
            chunks += chunk
        return bytes(chunks)

    def send_all(self, data: bytes) -> None:
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise ConnectionError_(f"iusb: socket error while writing: {exc}", operation="iusb.write_frame") from exc

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()


def read_packet(t: Transport, frame_hook: FrameHook | None = None) -> Packet:
    """Read one IUSB packet: the 32-byte header, then
    ``header.data_packet_len`` payload bytes.

    Lets :class:`IdleTimeout` and ``EOFError`` (peer closed) propagate
    unchanged -- callers (:meth:`Session.serve_forever`) are what decide
    which of those is a healthy pause versus a real end of session.
    """
    hdr_bytes = t.recv_exact(HEADER_LEN)
    header = Header.parse(hdr_bytes)
    if header.data_packet_len > MAX_PACKET_PAYLOAD:
        raise ProtocolError(f"iusb: implausible payload length {header.data_packet_len}", operation="iusb.read_packet")
    payload = t.recv_exact(header.data_packet_len) if header.data_packet_len else b""
    if frame_hook is not None:
        frame_hook("rx", hdr_bytes + payload)
    return Packet(header=header, payload=payload)


def write_frame(t: Transport, frame: bytes, frame_hook: FrameHook | None = None) -> None:
    t.send_all(frame)
    if frame_hook is not None:
        frame_hook("tx", frame)


#: Outcomes :meth:`Session.serve_forever` can return -- see that method's
#: docstring. Every one of them is a *normal*, non-exceptional end of the
#: serve loop; a wire-level fault is raised instead (see that method).
SERVE_STOPPED = "stopped"
SERVE_PEER_CLOSED = "peer_closed"
SERVE_KILLED = "killed"


class Session:
    """One device redirection: a :class:`Transport` that has completed the
    IUSB auth handshake and is ready to serve SCSI requests.
    """

    def __init__(
        self,
        transport: Transport,
        device_type: int,
        instance: int = 0,
        frame_hook: FrameHook | None = None,
    ) -> None:
        self.transport = transport
        self.device_type = device_type
        self.instance = instance
        self.frame_hook = frame_hook

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        token: str,
        device_type: int = DEVICE_CDROM,
        instance: int = 0,
        timeout: float = DEFAULT_DIAL_TIMEOUT,
        frame_hook: FrameHook | None = None,
    ) -> Session:
        """Dial the vmedia port and authenticate. ``token`` is the
        ``-kvmtoken`` minted by the JNLP fetch -- this method does not fetch
        or mint a token itself (see :func:`build_auth`'s docstring).
        """
        transport = SocketTransport.connect(host, port, timeout=timeout)
        return cls._authenticate(transport, device_type, instance, token, frame_hook, endpoint=f"{host}:{port}")

    @classmethod
    def from_transport(
        cls,
        transport: Transport,
        token: str,
        device_type: int = DEVICE_CDROM,
        instance: int = 0,
        frame_hook: FrameHook | None = None,
    ) -> Session:
        """Authenticate over an already-connected :class:`Transport`. This is
        the seam tests use to drive a full handshake over an in-process
        socket pair with no network involved.
        """
        return cls._authenticate(transport, device_type, instance, token, frame_hook, endpoint=None)

    @classmethod
    def _authenticate(
        cls,
        transport: Transport,
        device_type: int,
        instance: int,
        token: str,
        frame_hook: FrameHook | None,
        *,
        endpoint: str | None,
    ) -> Session:
        auth_pkt = build_auth(device_type, instance, token)
        write_frame(transport, auth_pkt, frame_hook)
        try:
            ack = read_packet(transport, frame_hook)
        except IdleTimeout as exc:
            # The ACK is expected immediately; an idle-socket read here means
            # the BMC never answered the auth attempt at all, which is a
            # connection-level fault, not the "healthy pause between SCSI
            # requests" case IdleTimeout exists to describe post-attach.
            raise ConnectionError_("iusb: no response to the authentication packet", endpoint=endpoint, operation="iusb.authenticate") from exc
        interpret_ack(ack, endpoint=endpoint)
        return cls(transport, device_type, instance, frame_hook)

    def set_poll_timeout(self, seconds: float) -> None:
        """Set the transport's socket timeout used by :meth:`serve_forever` to
        poll for a stop request between requests. This is a responsiveness
        knob for SIGTERM/``should_stop``, never a deadline on how long a
        session may legitimately stay idle -- see :meth:`serve_forever`.
        """
        self.transport.set_timeout(seconds)

    def serve_forever(
        self,
        handler: Handler,
        *,
        should_stop: Callable[[], bool] | None = None,
        on_idle: Callable[[], None] | None = None,
        on_request: Callable[[Packet], None] | None = None,
    ) -> str:
        """Service SCSI requests with NO upper bound on how long the session
        may sit idle between them.

        Returns one of :data:`SERVE_STOPPED` (``should_stop()`` returned
        True), :data:`SERVE_PEER_CLOSED` (the transport hit EOF), or
        :data:`SERVE_KILLED` (the BMC sent opcode 0xF6). All three are normal
        exits, not failures -- the caller decides what state that means for a
        media session (see ``media_session.py``).

        A wire-level fault (malformed frame, a stalled mid-frame read, a
        socket error, an unexpected ACK-shaped opcode reaching here) is
        raised as one of ``errors.IkvmError``'s subclasses and propagates out
        of this method uncaught; only :class:`IdleTimeout` is intercepted
        here, and only to call ``on_idle`` and keep looping.

        This is the fix for the one confirmed defect in the PoC this module
        was ported from: its serve loop raised ``TimeoutError`` on an idle
        socket, which would have torn down a single-occupancy session that
        was healthy and about to be needed again. Verified live: 130
        consecutive seconds of total silence on an attached session (a host
        parked at a bootloader menu) followed by reads resuming normally, so
        there is no idle duration this method may safely treat as failure --
        the poll timeout set by :meth:`set_poll_timeout` is purely how often
        ``should_stop``/``on_idle`` get a chance to run, never a deadline.
        """
        while True:
            if should_stop is not None and should_stop():
                return SERVE_STOPPED
            try:
                req = read_packet(self.transport, self.frame_hook)
            except IdleTimeout:
                if on_idle is not None:
                    on_idle()
                continue
            except EOFError:
                return SERVE_PEER_CLOSED
            if req.is_kill():
                return SERVE_KILLED
            if on_request is not None:
                on_request(req)
            payload = handler.handle(req)
            if payload is None:
                continue
            frame = build_response_frame(req.header, payload, self.device_type)
            write_frame(self.transport, frame, self.frame_hook)

    def close(self) -> None:
        self.transport.close()


def resolve_local_ip(host: str, port: int = 80) -> str:
    """Return this controller's outbound IP address toward ``host``, for the
    ``EXTRNIP`` argument ``asp.py``'s ``allocate_media_session`` needs to mint
    a KVM/media token.

    Uses a UDP socket's ``connect()``, which performs a local routing-table
    lookup only -- ``connect()`` on ``SOCK_DGRAM`` never transmits a packet by
    itself, so this makes no network request of any kind to ``host``. Any
    reachable, non-loopback ``host`` (including one that refuses every real
    connection) resolves correctly, because nothing is actually sent.
    """
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as probe:
        probe.connect((host, port))
        return probe.getsockname()[0]


__all__: Iterable[str] = [
    "AMI_CONTROL_OPCODE_MIN",
    "AUTH_PAYLOAD_LEN",
    "AUTH_TOKEN_OFFSET",
    "CD_BLOCK_SIZE",
    "CONN_ERR_IN_USE_5",
    "CONN_ERR_IN_USE_8",
    "CONN_OK",
    "CONN_STATUS_OFFSET",
    "DEFAULT_DIAL_TIMEOUT",
    "DEFAULT_MAX_WINDOWS",
    "DEVICE_CDROM",
    "EJECT_BYTE_OFFSET",
    "ENV_OFF_DATA_LEN",
    "ENV_OFF_RESP_LEN",
    "HEADER_LEN",
    "MAX_PACKET_PAYLOAD",
    "OPCODE_OFFSET",
    "OP_AUTH",
    "OP_KILL_REDIR",
    "OP_REDIRECT_ACK",
    "OP_START_STOP_UNIT",
    "PORT_CD",
    "PORT_FD",
    "PORT_HD",
    "SCSI_READ10",
    "SCSI_READ12",
    "SCSI_READ_CAPACITY10",
    "SCSI_READ_TOC",
    "SCSI_START_STOP_UNIT",
    "SCSI_TEST_UNIT_READY",
    "SERVE_KILLED",
    "SERVE_PEER_CLOSED",
    "SERVE_STOPPED",
    "SSI_AUTH_PAYLOAD_LEN",
    "WINDOW_SIZE",
    "CDROMDevice",
    "Cache",
    "CacheReadOnlyError",
    "CacheStats",
    "FileReader",
    "FrameEvent",
    "FrameHook",
    "FrameRecorder",
    "Handler",
    "Header",
    "IdleTimeout",
    "Packet",
    "Reader",
    "Session",
    "SocketTransport",
    "Transport",
    "build_auth",
    "build_response_frame",
    "interpret_ack",
    "other_ip",
    "parse_request_frame",
    "process_frame",
    "read_packet",
    "resolve_local_ip",
    "write_frame",
]
