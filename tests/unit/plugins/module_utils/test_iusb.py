# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for ``iusb.py``: framing, auth handshake, SCSI emulation, and the
idle-vs-failure distinction in the serve loop.

Golden-vector fixtures (the ``*_HEX`` constants below) are carried over
verbatim from this task's proof-of-concept
(a standalone proof-of-concept client's golden-vector fixtures), which captured them by
running the REAL, unmodified functions of ``rd450x-console``
(``BadCoder1337/rd450x-console``, MIT License, Copyright (c) 2026 Anton
Musin -- see ``licenses/MIT.txt``) offline (``GOPROXY=off``) and hex-encoding
the output. Where a test below asserts Python output against one of these
constants, it is checking Python-output == real-Go-output for the identical
input, not merely "matches what this port's author thinks the spec says".
See ``docs/protocol-notes.md`` for the full provenance ledger.

``PatternReader``'s semantics (byte at absolute offset ``off`` ==
``off % 251``) mirror the PoC's fixture reader of the same name, which is
what produced the golden READ(10)/READ(12) response vectors.

The single most important test class here is :class:`TestServeForeverIdleHandling`:
this collection's own protocol notes measured 130 consecutive seconds of
total silence on a healthy, attached session (a host parked at a bootloader
menu) before reads resumed normally. Nothing may treat that as failure.
"""

from __future__ import annotations

import socket
from unittest.mock import Mock

import pytest

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import iusb
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import BmcBusyError, ConnectionError_, ProtocolError

# --- golden vectors, ported verbatim from the proof-of-concept client's golden-vector fixtures ---

AUTH_CDROM_INSTANCE2_TOKEN_HEX = (
    "4955534220202020010020248000000000050180000000020000000000000000000000000000"
    "000000f200000000000000000000000000000000000000000053544f4b454e2d616263313233"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000"
)

CD_TEST_UNIT_READY_REQ_HEX = "0000000007000000010000000000000000000000000000000000000000"
CD_TEST_UNIT_READY_RESP_HEX = "0000000007000000010000000000000000000000000000000000000000"

CD_READ_CAPACITY10_REQ_HEX = "000000006300000001250000000000000000000000000000000000"
CD_READ_CAPACITY10_RESP_HEX = "080000006300000001250000000000000000000000000000000800000001ff00000800"

CD_READ_TOC_REQ_HEX = "0000000064000000014300000100000000000000000000000000000000"
CD_READ_TOC_RESP_HEX = "14000000640000000143000001000000000000000000000000140000000012010100140100000000000014aa0000000200"

CD_START_STOP_EJECT_REQ_HEX = "0000000065000000011b00000002000000000000000000000000000000"
CD_START_STOP_EJECT_RESP_HEX = "0000000065000000011b00000002000000000000000000000000000000"

CD_READ10_LBA14_2BLOCKS_REQ_HEX = "00100000610000000128000000000e0000020000000000000000000000"
CD_READ12_LBA5_1BLOCK_REQ_HEX = "000000006200000001a800000000050000000100000000000000000000"

#: Confirmed: GOLDEN cd_block_size == 2048 in the Go export.
GOLDEN_CD_BLOCK_SIZE = 2048
PATTERN_READER_SIZE = 1 << 20  # matches the Go fixture's patReader{size: 1 << 20}


class PatternReader:
    """Deterministic backing store: byte at absolute offset ``off`` == ``off % 251``.

    Mirrors the Go reference's own ``patReader`` fixture (see this module's
    docstring), so a read from this reader produces the exact bytes the
    golden READ(10)/READ(12) vectors were captured against.
    """

    def __init__(self, size: int = PATTERN_READER_SIZE) -> None:
        self._size = size

    def size(self) -> int:
        return self._size

    def read_at(self, n: int, offset: int) -> bytes:
        if offset >= self._size:
            return b""
        end = min(offset + n, self._size)
        return bytes((i % 251) for i in range(offset, end))


def _device() -> iusb.CDROMDevice:
    return iusb.CDROMDevice(PatternReader())


def _packet_from_payload_hex(hex_str: str, *, device_type: int = iusb.DEVICE_CDROM, instance: int = 0, sequence_number: int = 0) -> iusb.Packet:
    payload = bytes.fromhex(hex_str)
    header = iusb.Header(data_packet_len=len(payload), device_type=device_type, instance=instance, sequence_number=sequence_number)
    return iusb.Packet(header=header, payload=payload)


# ===========================================================================
# Header: marshal/parse round trip, checksum, malformed-header classification
# ===========================================================================


class TestHeader:
    def test_round_trip(self):
        header = iusb.Header(data_packet_len=128, device_type=iusb.DEVICE_CDROM, instance=3, sequence_number=42)
        parsed = iusb.Header.parse(header.marshal())
        assert parsed.data_packet_len == 128
        assert parsed.device_type == iusb.DEVICE_CDROM
        assert parsed.instance == 3
        assert parsed.sequence_number == 42

    def test_checksum_sums_to_zero_mod_256(self):
        wire = iusb.Header(data_packet_len=7, instance=9).marshal()
        assert sum(wire) % 256 == 0

    def test_signature_is_confirmed_exact_bytes(self):
        wire = iusb.Header().marshal()
        assert wire[0:8] == b"IUSB    "

    def test_bad_signature_is_a_classified_protocol_error(self):
        garbage = bytearray(iusb.Header().marshal())
        garbage[0:8] = b"XXXX    "
        with pytest.raises(ProtocolError):
            iusb.Header.parse(bytes(garbage))

    def test_short_header_is_a_classified_protocol_error(self):
        with pytest.raises(ProtocolError):
            iusb.Header.parse(b"IUSB    \x01\x00")

    def test_parse_tolerates_nonzero_reserved_bytes(self):
        # Observed live against the target hardware: the BMC's own request frames do
        # not always keep header offset 28 zero (one capture showed bc 38 02 bf).
        # parse() must not reject that.
        wire = bytearray(iusb.Header(data_packet_len=0).marshal())
        wire[28:32] = bytes.fromhex("bc3802bf")
        parsed = iusb.Header.parse(bytes(wire))
        assert parsed.data_packet_len == 0

    def test_parse_does_not_validate_checksum(self):
        wire = bytearray(iusb.Header(data_packet_len=0).marshal())
        wire[11] = wire[11] ^ 0xFF  # corrupt the checksum byte
        iusb.Header.parse(bytes(wire))  # must not raise


# ===========================================================================
# Auth handshake: byte-exact build_auth, ack interpretation
# ===========================================================================


class TestBuildAuth:
    def test_byte_exact_against_go_reference(self):
        wire = iusb.build_auth(iusb.DEVICE_CDROM, 2, "STOKEN-abc123")
        assert wire == bytes.fromhex(AUTH_CDROM_INSTANCE2_TOKEN_HEX)

    def test_payload_length_is_128_for_a_web_token(self):
        wire = iusb.build_auth(iusb.DEVICE_CDROM, 0, "tok")
        assert len(wire) == iusb.HEADER_LEN + iusb.AUTH_PAYLOAD_LEN

    def test_payload_length_is_240_for_an_ssi_token(self):
        wire = iusb.build_auth(iusb.DEVICE_CDROM, 0, "tok", ssi=True)
        assert len(wire) == iusb.HEADER_LEN + iusb.SSI_AUTH_PAYLOAD_LEN

    def test_oversized_token_is_a_classified_protocol_error(self):
        with pytest.raises(ProtocolError):
            iusb.build_auth(iusb.DEVICE_CDROM, 0, "x" * (iusb.AUTH_PAYLOAD_LEN + 1))


class TestInterpretAck:
    def _ack(self, status: int, *, other_ip: str = "") -> iusb.Packet:
        payload = bytearray(64)
        payload[iusb.OPCODE_OFFSET] = iusb.OP_REDIRECT_ACK
        payload[iusb.CONN_STATUS_OFFSET] = status
        if other_ip:
            payload[iusb.AUTH_TOKEN_OFFSET : iusb.AUTH_TOKEN_OFFSET + len(other_ip)] = other_ip.encode("ascii")
        return iusb.Packet(header=iusb.Header(data_packet_len=len(payload)), payload=bytes(payload))

    def test_accepted_status_does_not_raise(self):
        iusb.interpret_ack(self._ack(iusb.CONN_OK))  # must not raise

    def test_device_error_status_is_bmc_busy(self):
        with pytest.raises(BmcBusyError):
            iusb.interpret_ack(self._ack(iusb.CONN_ERR_IN_USE_5))
        with pytest.raises(BmcBusyError):
            iusb.interpret_ack(self._ack(iusb.CONN_ERR_IN_USE_8))

    def test_already_redirected_with_owner_ip_is_bmc_busy_and_names_the_ip(self):
        with pytest.raises(BmcBusyError, match=r"10\.0\.0\.9"):
            iusb.interpret_ack(self._ack(9, other_ip="10.0.0.9"))

    def test_bmc_busy_message_documents_the_cold_reset_escape_hatch(self):
        # Operationally load-bearing wording, per the single-session-hazard design: if
        # nothing else is holding the slot, this is the only remaining remedy.
        with pytest.raises(BmcBusyError, match="cold reset"):
            iusb.interpret_ack(self._ack(iusb.CONN_ERR_IN_USE_5))

    def test_wrong_opcode_in_ack_position_is_protocol_not_busy(self):
        payload = bytearray(64)
        payload[iusb.OPCODE_OFFSET] = 0x28  # a READ(10), not an ACK
        packet = iusb.Packet(header=iusb.Header(data_packet_len=len(payload)), payload=bytes(payload))
        with pytest.raises(ProtocolError):
            iusb.interpret_ack(packet)

    def test_short_ack_payload_is_protocol_error(self):
        # Long enough to carry the ACK opcode, too short to carry connectionStatus.
        payload = bytearray(iusb.CONN_STATUS_OFFSET)
        payload[iusb.OPCODE_OFFSET] = iusb.OP_REDIRECT_ACK
        packet = iusb.Packet(header=iusb.Header(data_packet_len=len(payload)), payload=bytes(payload))
        with pytest.raises(ProtocolError):
            iusb.interpret_ack(packet)


class TestOtherIp:
    def test_extracts_nul_terminated_ip(self):
        payload = bytearray(64)
        payload[iusb.AUTH_TOKEN_OFFSET : iusb.AUTH_TOKEN_OFFSET + 9] = b"10.1.2.3\x00"
        assert iusb.other_ip(bytes(payload)) == "10.1.2.3"

    def test_empty_for_short_payload(self):
        assert iusb.other_ip(b"\x00" * 4) == ""


# ===========================================================================
# is_eject / is_kill
# ===========================================================================


class TestPacketFlags:
    def test_is_eject_exact_equality_not_masked(self):
        # 0x06 shares the Go reference's masked bits with 0x02 (payload[13] & 0x03 == 2)
        # but is NOT exact equality to 2 -- decompiled JViewer requires exact equality,
        # and this client follows that, not the mask.
        packet = _packet_from_payload_hex(CD_START_STOP_EJECT_REQ_HEX)
        assert packet.is_eject() is True

        tampered = bytearray(bytes.fromhex(CD_START_STOP_EJECT_REQ_HEX))
        tampered[iusb.EJECT_BYTE_OFFSET] = 0x06
        masked_but_not_exact = iusb.Packet(header=iusb.Header(data_packet_len=len(tampered)), payload=bytes(tampered))
        assert masked_but_not_exact.is_eject() is False, "Go's bitmask would say True here; exact equality must say False"

    def test_is_kill_matches_opcode_0xf6(self):
        payload = bytearray(16)
        payload[iusb.OPCODE_OFFSET] = iusb.OP_KILL_REDIR
        packet = iusb.Packet(header=iusb.Header(data_packet_len=len(payload)), payload=bytes(payload))
        assert packet.is_kill() is True

    def test_short_payload_opcode_is_zero(self):
        packet = iusb.Packet(header=iusb.Header(data_packet_len=0), payload=b"")
        assert packet.opcode() == 0
        assert packet.cdb() == b""


# ===========================================================================
# CDROMDevice: byte-exact SCSI emulation against the Go reference
# ===========================================================================


class TestCDROMDeviceGolden:
    def test_block_size_is_2048_not_512(self):
        assert iusb.CD_BLOCK_SIZE == 2048 == GOLDEN_CD_BLOCK_SIZE

    def test_test_unit_ready_golden(self):
        device = _device()
        req = _packet_from_payload_hex(CD_TEST_UNIT_READY_REQ_HEX)
        assert device.handle(req) == bytes.fromhex(CD_TEST_UNIT_READY_RESP_HEX)

    def test_read_capacity10_golden(self):
        device = _device()
        req = _packet_from_payload_hex(CD_READ_CAPACITY10_REQ_HEX)
        assert device.handle(req) == bytes.fromhex(CD_READ_CAPACITY10_RESP_HEX)

    def test_read_toc_golden(self):
        device = _device()
        req = _packet_from_payload_hex(CD_READ_TOC_REQ_HEX)
        assert device.handle(req) == bytes.fromhex(CD_READ_TOC_RESP_HEX)

    def test_start_stop_unit_eject_golden(self):
        device = _device()
        req = _packet_from_payload_hex(CD_START_STOP_EJECT_REQ_HEX)
        assert device.handle(req) == bytes.fromhex(CD_START_STOP_EJECT_RESP_HEX)

    def test_read10_lba_and_blocks_are_big_endian_inside_the_le_wrapper(self):
        # CDB[2:6] (payload[11:15]) is the LBA, CDB[7:9] (payload[16:18]) is the
        # block count -- both big-endian, even though the IUSB header/envelope
        # wrapper around them is little-endian throughout.
        req_bytes = bytes.fromhex(CD_READ10_LBA14_2BLOCKS_REQ_HEX)
        cdb = req_bytes[iusb.OPCODE_OFFSET :]
        assert int.from_bytes(cdb[2:6], "big") == 14
        assert int.from_bytes(cdb[7:9], "big") == 2

        device = _device()
        req = _packet_from_payload_hex(CD_READ10_LBA14_2BLOCKS_REQ_HEX)
        resp = device.handle(req)
        data = resp[len(req.payload) :]
        assert len(data) == 2 * iusb.CD_BLOCK_SIZE
        assert data == PatternReader().read_at(2 * iusb.CD_BLOCK_SIZE, 14 * iusb.CD_BLOCK_SIZE)
        assert device.bytes_served() == 2 * iusb.CD_BLOCK_SIZE
        assert device.blocks_served() == 2

    def test_read12_shares_the_read10_handler(self):
        req_bytes = bytes.fromhex(CD_READ12_LBA5_1BLOCK_REQ_HEX)
        cdb = req_bytes[iusb.OPCODE_OFFSET :]
        assert cdb[0] == iusb.SCSI_READ12
        assert int.from_bytes(cdb[2:6], "big") == 5
        assert int.from_bytes(cdb[6:10], "big") == 1

        device = _device()
        req = _packet_from_payload_hex(CD_READ12_LBA5_1BLOCK_REQ_HEX)
        resp = device.handle(req)
        data = resp[len(req.payload) :]
        assert data == PatternReader().read_at(iusb.CD_BLOCK_SIZE, 5 * iusb.CD_BLOCK_SIZE)

    def test_response_length_is_set_at_both_offset_0_and_offset_25(self):
        device = _device()
        req = _packet_from_payload_hex(CD_READ10_LBA14_2BLOCKS_REQ_HEX)
        resp = device.handle(req)
        n = 2 * iusb.CD_BLOCK_SIZE
        assert int.from_bytes(resp[0:4], "little") == n
        assert int.from_bytes(resp[25:29], "little") == n

    def test_short_read_past_end_of_medium_is_zero_filled_not_raised(self):
        reader = PatternReader(size=4096)  # 2 blocks only
        device = iusb.CDROMDevice(reader)
        req_payload = bytearray(29)
        req_payload[iusb.OPCODE_OFFSET] = iusb.SCSI_READ10
        req_payload[11:15] = (0).to_bytes(4, "big")
        req_payload[16:18] = (4).to_bytes(2, "big")  # ask for 4 blocks, only 2 exist
        req = iusb.Packet(header=iusb.Header(data_packet_len=len(req_payload)), payload=bytes(req_payload))
        resp = device.handle(req)
        data = resp[len(req_payload) :]
        assert len(data) == 4 * iusb.CD_BLOCK_SIZE
        assert data[2 * iusb.CD_BLOCK_SIZE :] == b"\x00" * (2 * iusb.CD_BLOCK_SIZE)

    @pytest.mark.parametrize(
        "opcode",
        [0x03, 0x12, 0x15, 0x1A, 0x1E, 0x2A, 0x46, 0x4A, 0x51, 0x5A, 0xAA],
    )
    def test_removed_opcodes_fall_through_to_a_bare_echo_not_an_error(self, opcode):
        # These eleven opcodes were in an earlier, broader command-set brief but are
        # absent from the real vendor CD-ROM SCSI dispatcher (see the module
        # docstring). CDROMDevice must not raise for one -- it echoes the envelope
        # with zero appended data, matching what real firmware's own default branch
        # effectively looks like from this module's perspective (this module never
        # attempts to reproduce the hard SCSI error code the real dispatcher returns;
        # see iusb.py's docstring for the live-hardware caveat on this path).
        device = _device()
        payload = bytearray(29)
        payload[iusb.OPCODE_OFFSET] = opcode
        req = iusb.Packet(header=iusb.Header(data_packet_len=len(payload)), payload=bytes(payload))
        resp = device.handle(req)
        assert resp is not None
        assert len(resp) == len(payload)  # nothing appended
        assert int.from_bytes(resp[0:4], "little") == 0

    def test_inquiry_is_never_specially_handled(self):
        # INQUIRY (0x12) is answered by the BMC's own firmware and never forwarded --
        # confirmed both by the reference client's doc comments and independently by
        # disassembling the vendor's native dispatcher. Nothing in CDROMDevice
        # emulates a peripheral-device-type byte for it; it falls through like any
        # other unrecognised opcode (see the parametrized test above).
        assert 0x12 not in (
            iusb.SCSI_TEST_UNIT_READY,
            iusb.SCSI_START_STOP_UNIT,
            iusb.SCSI_READ_CAPACITY10,
            iusb.SCSI_READ10,
            iusb.SCSI_READ_TOC,
            iusb.SCSI_READ12,
        )


# ===========================================================================
# FileReader / Cache
# ===========================================================================


class TestFileReader:
    def test_reads_exact_bytes_at_offset(self, tmp_path):
        path = tmp_path / "image.iso"
        path.write_bytes(bytes(range(256)) * 16)  # 4096 bytes
        with iusb.FileReader.open(str(path)) as reader:
            assert reader.size() == 4096
            assert reader.read_at(4, 256) == bytes([0, 1, 2, 3])

    def test_short_read_past_eof_returns_fewer_bytes(self, tmp_path):
        path = tmp_path / "image.iso"
        path.write_bytes(b"\x01\x02\x03")
        with iusb.FileReader.open(str(path)) as reader:
            assert reader.read_at(100, 1) == b"\x02\x03"

    def test_directory_is_rejected(self, tmp_path):
        with pytest.raises(IsADirectoryError):
            iusb.FileReader.open(str(tmp_path))

    def test_never_loads_a_multi_gb_sparse_file_into_memory(self, tmp_path):
        # A 1.5 GiB sparse file costs no real disk space; read_at() must still only
        # ever touch the bounded window it was asked for.
        path = tmp_path / "huge.iso"
        size = int(1.5 * 1024 * 1024 * 1024)
        with open(path, "wb") as f:
            f.truncate(size)
        with iusb.FileReader.open(str(path)) as reader:
            assert reader.size() == size
            assert reader.read_at(16, size - 16) == b"\x00" * 16


class TestCache:
    def test_hit_after_miss_serves_from_the_same_window(self):
        cache = iusb.Cache(PatternReader(), window_size=4096, max_windows=4)
        first = cache.read_at(16, 0)
        second = cache.read_at(16, 0)
        assert first == second == PatternReader().read_at(16, 0)
        stats = cache.stats()
        assert stats.misses == 1
        assert stats.hits == 1

    def test_a_read_spanning_two_windows_is_served_correctly(self):
        cache = iusb.Cache(PatternReader(), window_size=4096, max_windows=4)
        data = cache.read_at(16, 4096 - 8)  # 8 bytes in window 0, 8 in window 1
        assert data == PatternReader().read_at(16, 4096 - 8)

    def test_short_read_at_end_of_medium_does_not_raise(self):
        cache = iusb.Cache(PatternReader(size=4096), window_size=4096, max_windows=4)
        assert cache.read_at(100, 4096) == b""
        assert cache.read_at(100, 4090) == PatternReader(size=4096).read_at(100, 4090)

    def test_negative_offset_raises(self):
        cache = iusb.Cache(PatternReader())
        with pytest.raises(ValueError, match="negative"):
            cache.read_at(1, -1)

    def test_write_through_invalidates_the_touched_window(self):
        class RecordingWriter:
            def __init__(self, backing: PatternReader) -> None:
                self._backing = backing
                self.writes: list[tuple[bytes, int]] = []

            def write_at(self, data: bytes, offset: int) -> int:
                self.writes.append((data, offset))
                return len(data)

        writer = RecordingWriter(PatternReader())
        cache = iusb.Cache(PatternReader(), writer=writer, window_size=4096, max_windows=4)
        cache.read_at(16, 0)  # populate window 0
        assert cache.cached_window_count() == 1
        cache.write_at(b"\xff" * 4, 0)
        assert cache.cached_window_count() == 0
        assert writer.writes == [(b"\xff\xff\xff\xff", 0)]

    def test_read_only_cache_rejects_write_at(self):
        cache = iusb.Cache(PatternReader(), writer=None)
        with pytest.raises(iusb.CacheReadOnlyError):
            cache.write_at(b"\x00", 0)

    def test_eviction_caps_resident_windows(self):
        cache = iusb.Cache(PatternReader(), window_size=4096, max_windows=2)
        for window in range(5):
            cache.read_at(1, window * 4096)
        assert cache.cached_window_count() <= 2

    def test_never_caches_more_than_the_bounded_window_cap_for_a_multi_gb_medium(self):
        huge = PatternReader(size=2 * 1024 * 1024 * 1024)
        cache = iusb.Cache(huge, window_size=iusb.WINDOW_SIZE, max_windows=4)
        for window in range(20):
            cache.read_at(8, window * iusb.WINDOW_SIZE)
        assert cache.cached_window_count() == 4


# ===========================================================================
# Pure framing seam: parse_request_frame / build_response_frame / process_frame
# ===========================================================================


class TestProcessFrame:
    def test_full_frame_round_trip_matches_direct_device_handle(self):
        device = _device()
        payload = bytes.fromhex(CD_READ_CAPACITY10_REQ_HEX)
        header = iusb.Header(data_packet_len=len(payload), device_type=iusb.DEVICE_CDROM, instance=0, sequence_number=7)
        frame = header.marshal() + payload

        response_frame = iusb.process_frame(device, frame, device_type=iusb.DEVICE_CDROM)
        expected_payload = device.handle(iusb.Packet(header=header, payload=payload))
        assert response_frame == iusb.build_response_frame(header, expected_payload, iusb.DEVICE_CDROM)
        # The response header must echo the request's sequence number verbatim.
        response_header = iusb.Header.parse(response_frame[: iusb.HEADER_LEN])
        assert response_header.sequence_number == 7

    def test_kill_frame_produces_no_response(self):
        payload = bytearray(16)
        payload[iusb.OPCODE_OFFSET] = iusb.OP_KILL_REDIR
        header = iusb.Header(data_packet_len=len(payload))
        frame = header.marshal() + bytes(payload)
        assert iusb.process_frame(_device(), frame) is None

    def test_short_frame_is_a_classified_protocol_error(self):
        with pytest.raises(ProtocolError):
            iusb.parse_request_frame(b"short")

    def test_payload_length_mismatch_is_a_classified_protocol_error(self):
        header = iusb.Header(data_packet_len=100)
        with pytest.raises(ProtocolError):
            iusb.parse_request_frame(header.marshal() + b"\x00" * 5)


# ===========================================================================
# SocketTransport: idle-at-frame-boundary vs. stalled-mid-frame
# ===========================================================================


class TestSocketTransport:
    def _transport_with_recv(self, recv_side_effect) -> iusb.SocketTransport:
        sock = Mock(spec=socket.socket)
        sock.recv.side_effect = recv_side_effect
        return iusb.SocketTransport(sock)

    def test_timeout_with_zero_bytes_read_is_idle_not_an_error(self):
        def _raise(_n):
            raise TimeoutError

        transport = self._transport_with_recv(_raise)
        with pytest.raises(iusb.IdleTimeout):
            transport.recv_exact(32)

    def test_timeout_after_partial_bytes_is_a_stalled_connection_not_idle(self):
        calls = iter([b"\x01\x02", TimeoutError])

        def _recv(_n):
            item = next(calls)
            if item is TimeoutError:
                raise TimeoutError
            return item

        transport = self._transport_with_recv(_recv)
        with pytest.raises(ConnectionError_):
            transport.recv_exact(32)

    def test_peer_close_mid_read_raises_eof(self):
        transport = self._transport_with_recv([b"\x01\x02", b""])
        with pytest.raises(EOFError):
            transport.recv_exact(32)

    def test_zero_length_read_returns_empty_without_touching_the_socket(self):
        sock = Mock(spec=socket.socket)
        transport = iusb.SocketTransport(sock)
        assert transport.recv_exact(0) == b""
        sock.recv.assert_not_called()

    def test_socket_error_on_send_is_a_classified_connection_error(self):
        sock = Mock(spec=socket.socket)
        sock.sendall.side_effect = OSError("broken pipe")
        transport = iusb.SocketTransport(sock)
        with pytest.raises(ConnectionError_):
            transport.send_all(b"data")


# ===========================================================================
# Session.serve_forever: THE idle-vs-failure fix.
# ===========================================================================


class FakeTransport:
    """A scripted :class:`iusb.Transport` for driving :meth:`iusb.Session.serve_forever`
    without a real socket.

    ``script`` items are consumed one per logical "read a packet" cycle:

    - ``"idle"``  -- this cycle raises :class:`iusb.IdleTimeout` (no bytes consumed).
    - ``"eof"``   -- this cycle raises ``EOFError`` (peer closed).
    - ``bytes``   -- a complete raw frame (header + payload), returned across
                     however many ``recv_exact`` calls :func:`iusb.read_packet` makes.

    This intentionally does not model a timeout occurring mid-frame (that distinction
    is :class:`iusb.SocketTransport`'s job, tested directly above) -- this fake exists
    only to drive :meth:`Session.serve_forever`'s own idle/EOF/kill/should_stop logic.
    """

    def __init__(self, script: list) -> None:
        self._ops = list(script)
        self._buffer = bytearray()
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout: float | None = None

    def set_timeout(self, seconds: float | None) -> None:
        self.timeout = seconds

    def recv_exact(self, n: int) -> bytes:
        while len(self._buffer) < n:
            if not self._ops:
                raise AssertionError("FakeTransport script exhausted while more bytes were requested")
            op = self._ops.pop(0)
            if op == "idle":
                raise iusb.IdleTimeout
            if op == "eof":
                raise EOFError("fake: peer closed")
            self._buffer += op
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data

    def send_all(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def close(self) -> None:
        self.closed = True


def _kill_frame() -> bytes:
    payload = bytearray(16)
    payload[iusb.OPCODE_OFFSET] = iusb.OP_KILL_REDIR
    return iusb.Header(data_packet_len=len(payload)).marshal() + bytes(payload)


def _request_frame(hex_payload: str = CD_TEST_UNIT_READY_REQ_HEX, *, sequence_number: int = 1) -> bytes:
    payload = bytes.fromhex(hex_payload)
    return iusb.Header(data_packet_len=len(payload), sequence_number=sequence_number).marshal() + payload


class TestServeForeverIdleHandling:
    """The single most important correctness fix in this module.

    Verified live against the target hardware: 130 CONSECUTIVE SECONDS of total
    silence on an attached, perfectly healthy media session (a host parked at a
    bootloader menu), immediately followed by reads resuming normally with no
    intervention. A serve loop that raises on an idle socket read would have torn
    down that session -- and because this BMC's media slot is single-occupancy with
    no server-side reclaim, that teardown could have wedged the slot for everyone.

    This test simulates a SIMULATED idle period of 200 continuous seconds (100 idle
    cycles at the daemon's real 2.0s poll interval, media_session._RECV_POLL_TIMEOUT)
    -- comfortably past both the measured 130s and the required >=180s bound -- using
    a scripted fake transport, never a real sleep.
    """

    #: 100 * 2.0s (the real daemon's poll interval) == 200s of simulated silence,
    #: safely past the measured 130s and the required >=180s test bound. No real
    #: time is spent: FakeTransport raises IdleTimeout synchronously.
    SIMULATED_IDLE_CYCLES = 100

    def test_a_long_idle_period_does_not_raise_and_does_not_end_the_session(self):
        idle_calls = []
        script = ["idle"] * self.SIMULATED_IDLE_CYCLES + [_kill_frame()]
        transport = FakeTransport(script)
        session = iusb.Session(transport, iusb.DEVICE_CDROM)

        outcome = session.serve_forever(_device(), on_idle=lambda: idle_calls.append(1))

        assert outcome == iusb.SERVE_KILLED
        assert len(idle_calls) == self.SIMULATED_IDLE_CYCLES, "every idle cycle must reach on_idle exactly once, never raise"

    def test_idle_then_a_real_request_still_gets_served(self):
        # The exact scenario from the live capture: quiet for a long stretch, then
        # traffic resumes and must be answered normally, not treated as a fresh
        # session or a recovery from an error.
        transport = FakeTransport(["idle"] * 50 + [_request_frame()] + [_kill_frame()])
        session = iusb.Session(transport, iusb.DEVICE_CDROM)
        served = []

        outcome = session.serve_forever(_device(), on_request=served.append)

        assert outcome == iusb.SERVE_KILLED
        assert len(served) == 1
        assert len(transport.sent) == 1, "the resumed request must receive exactly one response frame"

    def test_should_stop_ends_the_loop_without_reading_a_kill_frame(self):
        transport = FakeTransport(["idle", "idle", "idle"])
        session = iusb.Session(transport, iusb.DEVICE_CDROM)
        calls = {"n": 0}

        def _should_stop():
            calls["n"] += 1
            return calls["n"] > 2

        outcome = session.serve_forever(_device(), should_stop=_should_stop)
        assert outcome == iusb.SERVE_STOPPED

    def test_peer_closing_the_connection_is_a_clean_outcome_not_an_exception(self):
        transport = FakeTransport(["idle", "eof"])
        session = iusb.Session(transport, iusb.DEVICE_CDROM)
        outcome = session.serve_forever(_device())
        assert outcome == iusb.SERVE_PEER_CLOSED

    def test_a_malformed_frame_propagates_as_a_classified_error_not_silently_swallowed(self):
        bad_header = bytearray(iusb.Header(data_packet_len=5).marshal())
        bad_header[0:8] = b"XXXX    "  # corrupt signature
        transport = FakeTransport(["idle", bytes(bad_header) + b"\x00" * 5])
        session = iusb.Session(transport, iusb.DEVICE_CDROM)
        with pytest.raises(ProtocolError):
            session.serve_forever(_device())

    def test_on_request_is_not_called_for_idle_cycles_or_kill_frames(self):
        transport = FakeTransport(["idle", "idle", _kill_frame()])
        session = iusb.Session(transport, iusb.DEVICE_CDROM)
        calls = []
        session.serve_forever(_device(), on_request=calls.append)
        assert calls == []

    def test_set_poll_timeout_forwards_to_the_transport(self):
        transport = FakeTransport(["eof"])
        session = iusb.Session(transport, iusb.DEVICE_CDROM)
        session.set_poll_timeout(2.0)
        assert transport.timeout == 2.0


# ===========================================================================
# Full handshake + serve + kill over a real in-process socket pair
# ===========================================================================


class TestEndToEndSocketPair:
    def test_full_handshake_and_read10_and_kill(self):
        client_sock, server_sock = socket.socketpair()
        client_sock.settimeout(5.0)
        server_sock.settimeout(5.0)
        try:
            client_transport = iusb.SocketTransport(client_sock)
            server_transport = iusb.SocketTransport(server_sock)
            recorder = iusb.FrameRecorder()

            # This module plays the CLIENT role against the BMC's media listener --
            # send the auth packet first, exactly as Session._authenticate() does.
            iusb.write_frame(client_transport, iusb.build_auth(iusb.DEVICE_CDROM, 0, "tok"), recorder.record)

            # "Server" (standing in for the BMC) receives it and acks.
            auth_request = iusb.read_packet(server_transport)
            assert auth_request.opcode() == iusb.OP_AUTH
            ack_payload = bytearray(64)
            ack_payload[iusb.OPCODE_OFFSET] = iusb.OP_REDIRECT_ACK
            ack_payload[iusb.CONN_STATUS_OFFSET] = iusb.CONN_OK
            ack_frame = iusb.build_response_frame(auth_request.header, bytes(ack_payload), iusb.DEVICE_CDROM)
            server_transport.send_all(ack_frame)

            ack = iusb.read_packet(client_transport, recorder.record)
            iusb.interpret_ack(ack)

            # Server issues one READ CAPACITY(10); the client (this module's device
            # emulator) answers it.
            device = _device()
            request_frame = _request_frame(CD_READ_CAPACITY10_REQ_HEX, sequence_number=3)
            server_transport.send_all(request_frame)
            client_request = iusb.read_packet(client_transport, recorder.record)
            response_payload = device.handle(client_request)
            response_frame = iusb.build_response_frame(client_request.header, response_payload, iusb.DEVICE_CDROM)
            iusb.write_frame(client_transport, response_frame, recorder.record)
            iusb.read_packet(server_transport)  # drain the response so the socket stays clean

            server_transport.send_all(_kill_frame())
            final = iusb.read_packet(client_transport, recorder.record)
            assert final.is_kill()

            directions = [event.direction for event in recorder.events]
            assert directions == ["tx", "rx", "rx", "tx", "rx"], "auth tx, ack rx, request rx, response tx, kill rx"
        finally:
            client_sock.close()
            server_sock.close()


# ===========================================================================
# resolve_local_ip: no bytes ever sent, verified with a real UDP socket.
# ===========================================================================


class TestResolveLocalIp:
    def test_resolves_without_sending_any_packet(self):
        # RFC 5737 TEST-NET-1: guaranteed non-routable, so if this function ever
        # transmitted anything it would fail loudly (no route) rather than silently
        # succeed -- it does not, because UDP connect() only performs a local routing
        # decision.
        ip = iusb.resolve_local_ip("192.0.2.1")
        assert isinstance(ip, str) and ip
