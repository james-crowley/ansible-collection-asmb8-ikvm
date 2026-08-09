# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Self-tests for the mock IUSB virtual-media (CD-ROM) server.

Two kinds of coverage:

* ``Test*`` classes that drive the mock directly over a raw socket, using
  this file's own tiny client-side helpers (``_send_auth``, ``_recv_frame``)
  rather than any real client implementation -- these pin the mock's wire
  behaviour (including every fault-injection mode) independent of whether a
  real client exists yet in this collection.
* ``TestRealClientAgainstMock``, which drives this collection's own shipped
  iUSB client (``plugins/module_utils/iusb.py``) end to end: full auth
  handshake, then a scripted SCSI sequence, with byte-exact assertions
  against a synthetic ISO built in a ``tmp_path`` fixture. It exists
  specifically to prove this mock is not merely internally consistent but
  actually speaks the protocol a real client implementation understands.
  This class always runs -- an earlier revision of this file instead drove a
  standalone, ad hoc proof-of-concept client, located via a hardcoded,
  machine-specific absolute path outside this repository, and skipped itself
  entirely when that path did not exist (i.e. in CI and in every fresh
  clone), which meant the mock's single most valuable check silently never
  ran anywhere it mattered.

No test in this file binds to anything other than 127.0.0.1, and none makes
an outbound network connection: every server here is a fresh
``IusbMockServer`` on an ephemeral loopback port, and the one "real client"
test drives that same loopback socket, never a real BMC.
"""

from __future__ import annotations

import socket
import struct
import threading
from pathlib import Path

import pytest
from iusb_server import (
    ACK_PAYLOAD_LEN,
    AUTH_OPCODE_OFFSET,
    AUTH_PAYLOAD_LEN,
    AUTH_STATUS_OFFSET,
    AUTH_TOKEN_OFFSET,
    AUTH_TOKEN_REJECTED_STATUS,
    CD_BLOCK_SIZE,
    CONN_ERR_IN_USE_5,
    CONN_OK,
    DEVICE_CDROM,
    FULL_READ_TOC_RESPONSE_LEN,
    HEADER_LEN,
    OP_ACK,
    OP_AUTH,
    SCSI_READ10,
    SCSI_READ_CAPACITY10,
    SCSI_READ_TOC,
    SCSI_REQUEST_SENSE,
    SCSI_TEST_UNIT_READY,
    SERVER_DEVICE_TYPE_BIT,
    SIGNATURE,
    DisconnectStep,
    Header,
    IdleStep,
    IusbMockServer,
    ProtocolViolation,
    ScsiStep,
    captured_install_script,
    eject_step,
    read10_step,
    read_capacity10_step,
    read_toc_step,
)
from iusb_server import test_unit_ready_step as tur_step  # aliased: pytest would otherwise collect this factory as a test itself

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import iusb
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import BmcBusyError, ConnectionError_

DEFAULT_TOKEN = "STOKEN-unit-test-token"  # obviously-fake, not a real credential


# --------------------------------------------------------------------------
# Minimal client-side helpers -- deliberately NOT a full client implementation.
# Just enough wire code to drive the mock directly for its own self-tests.
# --------------------------------------------------------------------------


def _build_auth_packet(token: str, *, device_type: int = DEVICE_CDROM, instance: int = 0) -> bytes:
    header = Header(
        major=1, minor=0, header_len_field=HEADER_LEN, data_packet_len=AUTH_PAYLOAD_LEN, device_type=device_type, protocol=1, direction=0x80, instance=instance
    )
    payload = bytearray(AUTH_PAYLOAD_LEN)
    payload[AUTH_OPCODE_OFFSET] = OP_AUTH
    payload[AUTH_STATUS_OFFSET] = 0  # token-type byte: 0 = web session
    token_bytes = token.encode("ascii")
    payload[AUTH_TOKEN_OFFSET : AUTH_TOKEN_OFFSET + len(token_bytes)] = token_bytes
    return header.marshal() + bytes(payload)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError(f"socket closed after {len(buf)}/{n} bytes")
        buf += chunk
    return bytes(buf)


def _recv_frame(sock: socket.socket) -> tuple[Header, bytes]:
    header_bytes = _recv_exact(sock, HEADER_LEN)
    header = Header.parse(header_bytes)
    payload = _recv_exact(sock, header.data_packet_len) if header.data_packet_len else b""
    return header, payload


def _connect(server: IusbMockServer) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    sock.settimeout(5)
    return sock


@pytest.fixture
def server():
    with IusbMockServer(expected_token=DEFAULT_TOKEN) as srv:
        yield srv


class TestHeaderCodec:
    def test_round_trip(self):
        header = Header(
            major=1,
            minor=0,
            header_len_field=32,
            data_packet_len=0x1234,
            device_type=5,
            protocol=1,
            direction=0x80,
            instance=3,
            sequence_number=99,
            reserved=b"\xaa\xbb\xcc\xdd",
        )
        buf = header.marshal()
        parsed = Header.parse(buf)
        assert parsed == header

    def test_data_packet_len_and_sequence_number_are_little_endian(self):
        header = Header(data_packet_len=0x00020000, sequence_number=0xCAFEBABE)
        buf = header.marshal()
        assert struct.unpack_from("<I", buf, 12)[0] == 0x00020000
        assert struct.unpack_from("<I", buf, 24)[0] == 0xCAFEBABE

    def test_signature_is_iusb_plus_four_spaces(self):
        assert SIGNATURE == b"IUSB    "
        assert len(SIGNATURE) == 8

    def test_parse_rejects_bad_signature(self):
        buf = bytearray(HEADER_LEN)
        buf[0:8] = b"NOTIUSB!"
        with pytest.raises(ProtocolViolation):
            Header.parse(bytes(buf))

    def test_parse_rejects_short_header(self):
        with pytest.raises(ProtocolViolation):
            Header.parse(bytes(10))


class TestAuthHandshake:
    def test_successful_auth_returns_55_byte_ack_with_status_1(self, server):
        sock = _connect(server)
        try:
            sock.sendall(_build_auth_packet(DEFAULT_TOKEN))
            header, payload = _recv_frame(sock)
            assert len(payload) == ACK_PAYLOAD_LEN
            assert header.data_packet_len == ACK_PAYLOAD_LEN
            assert payload[AUTH_OPCODE_OFFSET] == OP_ACK
            assert payload[AUTH_STATUS_OFFSET] == CONN_OK
            server.wait_for_handshake()
            assert server.auth_status_seen() == CONN_OK
        finally:
            sock.close()

    def test_ack_header_oddity_major_minor_pkthdrlen_are_zero(self, server):
        # VERIFIED LIVE oddity: unlike every other frame, the real board's
        # ACK header comes back with major/minor/packetHeaderLen all zero,
        # not 1/0/32. A mock that "fixed" this would hide a real interop bug.
        sock = _connect(server)
        try:
            sock.sendall(_build_auth_packet(DEFAULT_TOKEN))
            header, _payload = _recv_frame(sock)
            assert header.major == 0
            assert header.minor == 0
            assert header.header_len_field == 0
        finally:
            sock.close()

    def test_ack_device_type_ors_in_the_server_bit(self, server):
        sock = _connect(server)
        try:
            sock.sendall(_build_auth_packet(DEFAULT_TOKEN))
            header, _payload = _recv_frame(sock)
            assert header.device_type == (DEVICE_CDROM | SERVER_DEVICE_TYPE_BIT)
            assert header.device_type == 0x85
        finally:
            sock.close()

    def test_reserved_bytes_are_non_zero_garbage_by_default(self, server):
        # VERIFIED LIVE: real server frames carry non-zero reserved bytes.
        sock = _connect(server)
        try:
            sock.sendall(_build_auth_packet(DEFAULT_TOKEN))
            header, _payload = _recv_frame(sock)
            assert header.reserved != bytes(4)
        finally:
            sock.close()

    def test_zero_reserved_bytes_fault_is_available_for_contrast(self, server):
        server.faults.zero_reserved_bytes = True
        sock = _connect(server)
        try:
            sock.sendall(_build_auth_packet(DEFAULT_TOKEN))
            header, _payload = _recv_frame(sock)
            assert header.reserved == bytes(4)
        finally:
            sock.close()

    def test_wrong_token_is_rejected_with_a_status_distinct_from_success(self, server):
        sock = _connect(server)
        try:
            sock.sendall(_build_auth_packet("not-the-right-token"))
            _header, payload = _recv_frame(sock)
            assert payload[AUTH_STATUS_OFFSET] == AUTH_TOKEN_REJECTED_STATUS
            assert payload[AUTH_STATUS_OFFSET] != CONN_OK
            with pytest.raises(ProtocolViolation):
                server.wait_for_handshake()
        finally:
            sock.close()

    def test_force_auth_status_fault_overrides_a_correct_token_once(self, server):
        server.faults.force_auth_status = CONN_ERR_IN_USE_5
        sock = _connect(server)
        try:
            sock.sendall(_build_auth_packet(DEFAULT_TOKEN))
            _header, payload = _recv_frame(sock)
            assert payload[AUTH_STATUS_OFFSET] == CONN_ERR_IN_USE_5
        finally:
            sock.close()
        # One-shot: consumed after firing.
        assert server.faults.force_auth_status is None


class TestSingleSessionHazard:
    def test_second_concurrent_connection_is_refused(self, server):
        first = _connect(server)
        try:
            first.sendall(_build_auth_packet(DEFAULT_TOKEN))
            _recv_frame(first)
            server.wait_for_handshake()

            second = _connect(server)
            try:
                second.sendall(_build_auth_packet(DEFAULT_TOKEN))
                _header, payload = _recv_frame(second)
                assert payload[AUTH_STATUS_OFFSET] != CONN_OK
            finally:
                second.close()
        finally:
            first.close()

    def test_stale_held_slot_refuses_a_connection_with_no_real_session_behind_it(self, server):
        # Models the real board's no-server-side-timeout hazard: nothing is
        # actually connected, yet the slot is unavailable until reclaimed.
        server.hold_slot_without_connection(owner_ip="203.0.113.9")
        sock = _connect(server)
        try:
            sock.sendall(_build_auth_packet(DEFAULT_TOKEN))
            _header, payload = _recv_frame(sock)
            assert payload[AUTH_STATUS_OFFSET] != CONN_OK
        finally:
            sock.close()

    def test_releasing_a_stale_slot_lets_the_next_connection_succeed(self, server):
        server.hold_slot_without_connection()
        server.release_held_slot()
        sock = _connect(server)
        try:
            sock.sendall(_build_auth_packet(DEFAULT_TOKEN))
            _header, payload = _recv_frame(sock)
            assert payload[AUTH_STATUS_OFFSET] == CONN_OK
        finally:
            sock.close()


class TestScsiRequestDriving:
    def _authenticate(self, server: IusbMockServer) -> socket.socket:
        sock = _connect(server)
        sock.sendall(_build_auth_packet(DEFAULT_TOKEN))
        _recv_frame(sock)
        server.wait_for_handshake()
        return sock

    def test_test_unit_ready_request_has_verified_zero_xferlen(self, server):
        sock = self._authenticate(server)
        try:
            server.send_scsi_request(tur_step())
            _header, payload = _recv_frame(sock)
            assert payload[9] == SCSI_TEST_UNIT_READY
            assert struct.unpack_from("<I", payload, 0)[0] == 0
        finally:
            sock.close()

    def test_read_capacity10_request_has_verified_xferlen_8(self, server):
        sock = self._authenticate(server)
        try:
            server.send_scsi_request(read_capacity10_step())
            _header, payload = _recv_frame(sock)
            assert payload[9] == SCSI_READ_CAPACITY10
            assert struct.unpack_from("<I", payload, 0)[0] == 8
        finally:
            sock.close()

    def test_command_counter_is_non_sequential_by_default(self, server):
        sock = self._authenticate(server)
        try:
            counters = []
            for _attempt in range(3):
                server.send_scsi_request(tur_step())
                _header, payload = _recv_frame(sock)
                counters.append(struct.unpack_from("<I", payload, 4)[0])
            # VERIFIED LIVE example sequence.
            assert counters == [3, 7, 16]
            assert counters != sorted(counters[:1]) + [c + 1 for c in counters[:-1]]
        finally:
            sock.close()

    def test_sequential_command_counter_fault_is_available_for_contrast(self, server):
        server.faults.sequential_command_counters = True
        sock = self._authenticate(server)
        try:
            counters = []
            for _attempt in range(3):
                server.send_scsi_request(tur_step())
                _header, payload = _recv_frame(sock)
                counters.append(struct.unpack_from("<I", payload, 4)[0])
            assert counters == [counters[0], counters[0] + 1, counters[0] + 2]
        finally:
            sock.close()

    def test_read_toc_step_uses_the_read_toc_opcode(self, server):
        sock = self._authenticate(server)
        try:
            server.send_scsi_request(read_toc_step())
            _header, payload = _recv_frame(sock)
            assert payload[9] == SCSI_READ_TOC
        finally:
            sock.close()

    def test_eject_step_sets_exact_loej_byte_2(self, server):
        sock = self._authenticate(server)
        try:
            server.send_scsi_request(eject_step())
            _header, payload = _recv_frame(sock)
            assert payload[13] == 2
        finally:
            sock.close()

    def test_sequence_number_increments_and_is_echoed_by_a_well_behaved_reply(self, server):
        sock = self._authenticate(server)
        try:
            seq = server.send_scsi_request(tur_step())
            header, payload = _recv_frame(sock)
            assert header.sequence_number == seq
            # A well-behaved client echoes the envelope with the status set;
            # simulate that reply directly to exercise recv_response()'s
            # validation path.
            reply_header = Header(data_packet_len=len(payload), device_type=DEVICE_CDROM, protocol=1, direction=0x80, sequence_number=seq)
            sock.sendall(reply_header.marshal() + payload)
            response = server.recv_response(timeout=2)
            assert response.header.sequence_number == seq
        finally:
            sock.close()


class TestFaultInjection:
    def _authenticate(self, server: IusbMockServer) -> socket.socket:
        sock = _connect(server)
        sock.sendall(_build_auth_packet(DEFAULT_TOKEN))
        _recv_frame(sock)
        server.wait_for_handshake()
        return sock

    def test_truncated_frame_sends_fewer_bytes_than_declared(self, server):
        # Armed AFTER the handshake, deliberately: arming it earlier would
        # truncate the auth ACK itself instead of the SCSI request this test
        # means to corrupt.
        sock = self._authenticate(server)
        server.faults.truncate_next_frame_to = 10
        try:
            server.send_scsi_request(tur_step())
            sock.settimeout(0.5)
            data = sock.recv(4096)
            assert len(data) == 10
        finally:
            sock.close()

    def test_lie_about_data_packet_len_declares_more_than_it_sends(self, server):
        sock = self._authenticate(server)  # see note in test_truncated_frame_* above
        server.faults.lie_next_data_packet_len = 9999
        try:
            server.send_scsi_request(tur_step())
            header_bytes = _recv_exact(sock, HEADER_LEN)
            header = Header.parse(header_bytes)
            assert header.data_packet_len == 9999
            # The real payload sent is the normal 29-byte envelope, not 9999
            # bytes -- a client that trusts the declared length and tries to
            # read all of it will block/timeout, which is the point.
            sock.settimeout(0.3)
            with pytest.raises(TimeoutError):
                _recv_exact(sock, 9999)
        finally:
            sock.close()

    def test_disconnect_after_next_send_closes_immediately_after_the_frame(self, server):
        sock = self._authenticate(server)  # see note in test_truncated_frame_* above
        server.faults.disconnect_after_next_send = True
        try:
            server.send_scsi_request(tur_step())
            _header, _payload = _recv_frame(sock)  # the frame itself still arrives intact
            sock.settimeout(2)
            # The connection is now closed server-side: the next recv sees EOF.
            assert sock.recv(64) == b""
        finally:
            sock.close()

    def test_run_script_disconnect_step_closes_the_connection(self, server):
        sock = self._authenticate(server)
        try:
            server.run_script([DisconnectStep()])
            sock.settimeout(2)
            assert sock.recv(64) == b""
        finally:
            sock.close()

    def test_run_script_idle_step_sends_nothing_then_resumes(self, server):
        # Idle is normal, not failure: prove the mock can go quiet for a
        # while and then keep serving requests afterward. expect_response is
        # deliberately False here: this test's own socket is not a real
        # client and never answers, so it only proves the mock resumed
        # SENDING after the idle period, not a full round trip (that is what
        # TestScsiRequestDriving and the real-client tests below cover).
        sock = self._authenticate(server)
        try:
            results = server.run_script([IdleStep(0.3), tur_step(expect_response=False)])
            assert results == [None, None]
            _header, payload = _recv_frame(sock)
            assert payload[9] == SCSI_TEST_UNIT_READY
        finally:
            sock.close()


# --------------------------------------------------------------------------
# Driving the collection's own shipped iUSB client, end to end, over this mock.
# --------------------------------------------------------------------------


class TestRealClientAgainstMock:
    """Drives this collection's own shipped iUSB client
    (``plugins/module_utils/iusb.py``) against this mock, proving the mock is
    interoperable with a real client and not merely self-consistent.

    This class always runs -- no ``skipif``, no external checkout to locate.
    See this file's module docstring for why an earlier revision needed one.
    """

    def _make_iso(self, tmp_path: Path, *, blocks: int) -> Path:
        size = blocks * CD_BLOCK_SIZE
        data = bytes((i % 251) for i in range(size))
        path = tmp_path / "synthetic.iso"
        path.write_bytes(data)
        return path

    def _labeled_sector(self, lba: int) -> bytes:
        """One 2048-byte sector whose first 4 bytes ARE its own LBA (big
        endian), the rest filled with a byte derived from that same LBA.
        Unlike ``_make_iso``'s flat repeating-byte pattern, a read that lands
        on the wrong LBA shows up immediately as a mismatched label rather
        than as merely-differently-offset, still-plausible-looking noise."""
        sector = bytearray(CD_BLOCK_SIZE)
        sector[0:4] = lba.to_bytes(4, "big")
        sector[4:] = bytes([lba % 256]) * (CD_BLOCK_SIZE - 4)
        return bytes(sector)

    def _make_labeled_iso(self, tmp_path: Path, *, blocks: int) -> Path:
        data = b"".join(self._labeled_sector(lba) for lba in range(blocks))
        path = tmp_path / "labeled.iso"
        path.write_bytes(data)
        return path

    def test_full_auth_then_scripted_scsi_sequence_byte_exact(self, server, tmp_path):
        blocks = 4
        iso_path = self._make_iso(tmp_path, blocks=blocks)

        client_session = iusb.Session.connect(
            "127.0.0.1",
            server.port,
            DEFAULT_TOKEN,
            device_type=DEVICE_CDROM,
            timeout=5.0,
        )
        server.wait_for_handshake()
        assert server.auth_status_seen() == CONN_OK

        reader = iusb.FileReader.open(str(iso_path))
        device = iusb.CDROMDevice(reader)

        serve_error: list[Exception] = []

        def _serve():
            try:
                client_session.serve_forever(device)
            except Exception as exc:
                serve_error.append(exc)

        serve_thread = threading.Thread(target=_serve, daemon=True)
        serve_thread.start()
        try:
            responses = server.run_script(
                [
                    tur_step(),
                    read_capacity10_step(),
                    read10_step(0, 2),
                ],
                timeout=5.0,
            )
        finally:
            server.send_kill()
            serve_thread.join(timeout=5)
            reader.close()
            client_session.close()

        assert not serve_thread.is_alive()
        assert serve_error == []

        tur_response, capacity_response, read_response = responses

        # TEST_UNIT_READY: pure envelope echo, no appended data.
        assert len(tur_response.data()) == 0

        # READ_CAPACITY10: 8 bytes, last LBA + block size, both big-endian.
        capacity_data = capacity_response.data()
        assert len(capacity_data) == 8
        last_lba, block_size = struct.unpack(">II", capacity_data)
        assert block_size == CD_BLOCK_SIZE
        assert last_lba == blocks - 1

        # READ(10) of 2 blocks at LBA 0: byte-exact against the synthetic ISO.
        read_data = read_response.data()
        assert len(read_data) == 2 * CD_BLOCK_SIZE
        expected = bytes((i % 251) for i in range(2 * CD_BLOCK_SIZE))
        assert read_data == expected

    def test_wrong_token_raises_bmcbusyerror_in_the_real_client(self, server):
        # The shipped client's interpret_ack() classifies every rejected auth
        # status (this mock's token-rejected status included) as
        # errors.BmcBusyError, not a bespoke auth-only exception -- see
        # iusb.interpret_ack's docstring for why "wrong credentials" and
        # "device already in use" collapse to the same exception on this
        # board's protocol.
        with pytest.raises(BmcBusyError):
            iusb.Session.connect("127.0.0.1", server.port, "definitely-not-the-right-token", device_type=DEVICE_CDROM, timeout=5.0)

    def test_client_survives_an_idle_period_then_serves_a_late_request(self, server, tmp_path):
        iso_path = self._make_iso(tmp_path, blocks=1)
        # Deliberately short: Session.connect's timeout also becomes the
        # underlying socket's persistent read timeout for the whole session
        # (socket.create_connection sets it once and nothing resets it), so a
        # short value here is what makes the idle period below actually
        # exceed it and exercise the idle-vs-mid-frame-stall distinction that
        # iusb.SocketTransport.recv_exact / iusb.IdleTimeout implement (see
        # iusb.py's module docstring: this is the one confirmed bug fixed
        # relative to the ad hoc proof-of-concept this module was ported from).
        client_session = iusb.Session.connect("127.0.0.1", server.port, DEFAULT_TOKEN, device_type=DEVICE_CDROM, timeout=1.0)
        server.wait_for_handshake()

        reader = iusb.FileReader.open(str(iso_path))
        device = iusb.CDROMDevice(reader)
        serve_error: list[Exception] = []

        def _serve():
            try:
                client_session.serve_forever(device)
            except Exception as exc:
                serve_error.append(exc)

        serve_thread = threading.Thread(target=_serve, daemon=True)
        serve_thread.start()
        try:
            # A quiet period exceeding the client's own read timeout above --
            # healthy steady state on the real board, not a failure. Uses the
            # mock's own idle-injection step (IdleStep) rather than a real
            # multi-minute sleep, so the suite stays fast.
            responses = server.run_script([IdleStep(2.0), tur_step()], timeout=2.0)
        finally:
            server.send_kill()
            serve_thread.join(timeout=5)
            reader.close()
            client_session.close()

        assert serve_error == [], f"client's serve loop raised during/after the idle period: {serve_error}"
        assert responses[1] is not None

    def test_captured_install_opcode_sequence_two_phases_read_toc_recurs_across_all_allocations(self, server, tmp_path):
        """End-to-end guard for the ``CDROMDevice._read_toc`` allocation-length
        bug (see that method's docstring, and
        ``test_iusb.TestCDROMDeviceGolden.test_read_toc_honours_the_cdb_allocation_length``
        for the unit-level half of this regression pair). That unit test proves
        the emulator itself gets the allocation length right in isolation; this
        test drives the REAL shipped client through the actual two-phase opcode
        shape a full OS install produces -- via ``captured_install_script`` --
        so the bug it exposed can never reach hardware again undetected.

        Covers, in one real client/server exchange:

        * READ_TOC recurs (four times here), never just once, across all three
          allocation-length cases that matter: 0, 12 (the value that broke real
          hardware), and 64.
        * A bootloader phase issues no READ_TOC at all; only the OS phase (past
          the idle gap) does -- the structural asymmetry that hid the bug.
        * REQUEST_SENSE never appears anywhere in the script.
        * READ10 replies are byte-exact for both single-sector and multi-block,
          irregular-size requests, against a self-identifying synthetic ISO.
        * The client survives the phase gap without tearing down.
        """
        total_blocks = 40
        iso_path = self._make_labeled_iso(tmp_path, blocks=total_blocks)

        # Short: makes the idle gap below (a couple of seconds, not the real
        # ~2 real-minute pause) exceed the client's persistent socket read
        # timeout several times over, exercising the SAME idle-at-a-frame-
        # boundary path the client hit for real (130s of silence at a
        # bootloader menu) -- never a mid-frame stall, which is the
        # DIFFERENT, error-raising path exercised by
        # test_mid_frame_stall_raises_a_connection_error_not_swallowed_as_idle
        # below. Nothing here is a real multi-minute sleep.
        client_session = iusb.Session.connect("127.0.0.1", server.port, DEFAULT_TOKEN, device_type=DEVICE_CDROM, timeout=1.0)
        server.wait_for_handshake()
        assert server.auth_status_seen() == CONN_OK

        reader = iusb.FileReader.open(str(iso_path))
        device = iusb.CDROMDevice(reader)
        serve_error: list[Exception] = []

        def _serve():
            try:
                client_session.serve_forever(device)
            except Exception as exc:
                serve_error.append(exc)

        serve_thread = threading.Thread(target=_serve, daemon=True)
        serve_thread.start()

        script = captured_install_script(blocks=total_blocks, gap_seconds=2.0)
        try:
            responses = server.run_script(script, timeout=5.0)
        finally:
            server.send_kill()
            serve_thread.join(timeout=5)
            reader.close()
            client_session.close()

        assert not serve_thread.is_alive()
        assert serve_error == [], f"client's serve loop raised: {serve_error}"

        # -- structural shape of the script itself -----------------------
        gap_index = next(i for i, step in enumerate(script) if isinstance(step, IdleStep))
        boot_phase, os_phase = script[:gap_index], script[gap_index + 1 :]

        def is_opcode(step: object, opcode: int) -> bool:
            return isinstance(step, ScsiStep) and step.opcode == opcode

        assert not any(is_opcode(s, SCSI_READ_TOC) for s in boot_phase), "a bootloader never issues READ_TOC"
        toc_step_count = sum(is_opcode(s, SCSI_READ_TOC) for s in os_phase)
        assert toc_step_count >= 3, f"READ_TOC must recur, not fire once (saw {toc_step_count})"
        assert not any(is_opcode(s, SCSI_REQUEST_SENSE) for s in script), "REQUEST_SENSE must never appear in this script"

        # -- every READ_TOC reply, across every allocation case ----------
        # run_script() above already enforced each step's expected_data_len
        # (i.e. the requested allocation was honoured for 0, 12, AND 64).
        # Additionally check the 2-byte TOC data-length header itself: it
        # must report the FULL available length even when the body is
        # truncated, so an initiator that under-allocated can tell and retry.
        toc_responses = [r for step, r in zip(script, responses, strict=True) if is_opcode(step, SCSI_READ_TOC)]
        assert len(toc_responses) == toc_step_count
        for response in toc_responses:
            data = response.data()
            assert len(data) >= 2
            toc_length_field = int.from_bytes(data[0:2], "big")
            assert toc_length_field == FULL_READ_TOC_RESPONSE_LEN - 2, "the TOC length header must report the FULL length, even when truncated"

        # -- every READ10 reply, single-sector and multi-block ------------
        for step, response in zip(script, responses, strict=True):
            if not is_opcode(step, SCSI_READ10):
                continue
            lba = int.from_bytes(step.cdb_tail[1:5], "big")
            blocks_requested = int.from_bytes(step.cdb_tail[6:8], "big")
            data = response.data()
            assert len(data) == blocks_requested * CD_BLOCK_SIZE
            expected = b"".join(self._labeled_sector(lba + n) for n in range(blocks_requested))
            assert data == expected, f"READ10 at LBA {lba}/{blocks_requested} blocks returned the wrong sector data"

    def test_mid_frame_stall_raises_a_connection_error_not_swallowed_as_idle(self, server, tmp_path):
        """Contrast case for the phase-gap idle handling exercised above: this
        truncates a request frame mid-payload (the client's socket read gets
        SOME bytes, then nothing more ever arrives) rather than sending
        nothing at all between two complete frames. ``iusb.SocketTransport
        .recv_exact`` must raise ``ConnectionError_`` here, not swallow it as
        ``IdleTimeout`` -- see that method's docstring for the exact
        boundary. This is what proves the phase-gap test above is actually
        exercising the idle-at-a-frame-boundary branch and not merely
        tolerating any timeout whatsoever.
        """
        iso_path = self._make_iso(tmp_path, blocks=1)
        client_session = iusb.Session.connect("127.0.0.1", server.port, DEFAULT_TOKEN, device_type=DEVICE_CDROM, timeout=0.3)
        server.wait_for_handshake()

        reader = iusb.FileReader.open(str(iso_path))
        device = iusb.CDROMDevice(reader)
        serve_outcome: list[object] = []

        def _serve():
            try:
                serve_outcome.append(client_session.serve_forever(device))
            except Exception as exc:
                serve_outcome.append(exc)

        serve_thread = threading.Thread(target=_serve, daemon=True)
        serve_thread.start()
        try:
            # Armed after the handshake, deliberately (see the note in
            # TestFaultInjection): truncate to header-plus-2-bytes, so the
            # client's header read completes normally but its payload read
            # gets 2 of the 29 expected bytes and then nothing further --
            # mid-frame, not at a boundary.
            server.faults.truncate_next_frame_to = HEADER_LEN + 2
            server.send_scsi_request(tur_step())
            serve_thread.join(timeout=5)
        finally:
            reader.close()
            client_session.close()

        assert not serve_thread.is_alive()
        assert len(serve_outcome) == 1
        assert isinstance(serve_outcome[0], ConnectionError_), f"expected a mid-frame ConnectionError_, got {serve_outcome[0]!r}"
