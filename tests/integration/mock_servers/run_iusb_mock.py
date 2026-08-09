# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Standalone runner: start an :class:`iusb_server.IusbMockServer`, then drive a
scripted SCSI exchange against whatever client connects to it, and report the outcome.

Mirrors ``run_ider_mock.py``'s shape and reasoning exactly, for the same
reason: :class:`iusb_server.IusbMockServer` plays the **BMC**, which is the
side that actively issues SCSI commands, so something has to call
``run_script`` against the live connection once a client (started as a
separate, detached process by whatever calls this) has connected and
authenticated. That "something" is this script's own main thread, blocking on
``wait_for_handshake()`` and then running the fixed sequence below.

The outcome is written into the same ready file the connection info came from
(``handshake``, ``read_ok``, ``read_bytes_len``, ``error``), which a calling
playbook or test harness polls for.

No integration target exists yet that invokes this script (the collection's
own iUSB virtual-media client, ``asmb8_media``, has not been written) -- it is
provided now, alongside the mock server it drives, so that target can be added
later without also needing a new fixture-driving script. Fault injection
follows ``run_ider_mock.py``'s convention: start-up flags only, never a
control channel reachable mid-session.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path


def _write_json_atomic(path: str, data: dict) -> None:
    target = Path(path)
    tmp = target.with_name(f"{target.name}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, target)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock-servers-dir", required=True, help="Directory containing iusb_server.py")
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--expected-token", required=True, help="The -kvmtoken value a connecting client must present")
    parser.add_argument("--handshake-timeout", type=float, default=20.0)
    parser.add_argument("--action-timeout", type=float, default=10.0)
    parser.add_argument(
        "--script",
        choices=("probe", "read10", "eject"),
        default="probe",
        help=(
            "probe: TEST_UNIT_READY then READ_CAPACITY10 (the exact VERIFIED LIVE exchange this mock's "
            "defaults were captured from). read10: probe, then READ(10) of 2 blocks at LBA 16. "
            "eject: probe, then a START_STOP_UNIT eject request"
        ),
    )
    parser.add_argument("--read-lba", type=int, default=16)
    parser.add_argument("--read-blocks", type=int, default=2)
    parser.add_argument(
        "--simulate-stale-slot",
        action="store_true",
        help="Hold the single media slot from start-up, standing in for an abandoned prior session the client must reclaim",
    )
    return parser.parse_args()


def _build_script(args: argparse.Namespace):
    from iusb_server import eject_step, read10_step, read_capacity10_step, test_unit_ready_step

    steps = [test_unit_ready_step(), read_capacity10_step()]
    if args.script == "read10":
        steps.append(read10_step(args.read_lba, args.read_blocks))
    elif args.script == "eject":
        steps.append(eject_step())
    return steps


def main() -> None:
    args = _parse_args()
    sys.path.insert(0, args.mock_servers_dir)
    from iusb_server import IusbMockServer  # local import: only resolvable after the sys.path insert above

    server = IusbMockServer(expected_token=args.expected_token).start()
    if args.simulate_stale_slot:
        server.hold_slot_without_connection()

    info = {
        "pid": os.getpid(),
        "port": server.port,
        "handshake": False,
        "read_ok": None,
        "read_bytes_len": None,
        "error": None,
    }
    _write_json_atomic(args.ready_file, info)

    try:
        server.wait_for_handshake(timeout=args.handshake_timeout)
        info["handshake"] = True
        _write_json_atomic(args.ready_file, info)

        steps = _build_script(args)
        responses = server.run_script(steps, timeout=args.action_timeout)
        info["read_ok"] = True
        last = responses[-1]
        info["read_bytes_len"] = len(last.data()) if last is not None else 0
        server.send_kill()
    except Exception as exc:
        info["read_ok"] = False
        info["error"] = f"iusb mock script failed: {exc}"
    finally:
        _write_json_atomic(args.ready_file, info)

    _idle_until_sigterm()
    server.stop()


def _idle_until_sigterm() -> None:
    state = {"stop": False}

    def _handle_sigterm(_signum: int, _frame: object) -> None:
        state["stop"] = True

    signal.signal(signal.SIGTERM, _handle_sigterm)
    while not state["stop"]:
        signal.pause()


if __name__ == "__main__":
    main()
