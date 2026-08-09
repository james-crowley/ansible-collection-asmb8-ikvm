# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Standalone runner: start an :class:`asp_server.AspMockServer`, report its
connection info, then idle until asked to stop.

Mirrors ``run_wsman_mock.py``'s shape exactly, for the same reason: used only
by a future ``tests/integration/targets/*`` playbook (started as a detached
background process via ``nohup ... & echo $!``) to drive the real
``AspClient``/``asmb8_media`` (once it exists) against a deterministic fixture
over an actual TCP connection. Never imported by collection code and never
shipped in the built collection artifact.

No fault-injection control channel exists here either, for the same reasoning
``run_wsman_mock.py`` documents: everything this mock's ``AspFaultConfig``
offers is either reachable through the mock's own stateful behaviour (a wrong
password fails login on the first request) or is a start-up property of the
endpoint (whether it hangs before answering at all).
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
    parser.add_argument("--mock-servers-dir", required=True, help="Directory containing asp_server.py")
    parser.add_argument("--ready-file", required=True, help="Path this script writes {pid, port} to once listening")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="test-password-not-real")  # fixture default, not a real credential
    parser.add_argument(
        "--single-port-enabled",
        action="store_true",
        default=True,
        help="Report the single-port JNLP wiring mode (the default: no -cdport/-fdport/-hdport arguments)",
    )
    parser.add_argument(
        "--dedicated-ports",
        action="store_true",
        help="Report the dedicated-port JNLP wiring mode instead of single-port",
    )
    parser.add_argument(
        "--jnlp-unescaped-ampersand",
        action="store_true",
        help="Include one JNLP <argument> value with an unescaped & (regression fixture for the client's regex-based parser)",
    )
    parser.add_argument(
        "--hang-before-response",
        action="store_true",
        help="Complete the TCP handshake on every request but never answer, standing in for a saturated worker pool (bmc_busy)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    sys.path.insert(0, args.mock_servers_dir)
    from asp_server import AspMockServer  # local import: only resolvable after the sys.path insert above

    server = AspMockServer(username=args.username, password=args.password)
    server.state.single_port_enabled = not args.dedicated_ports
    server.state.jnlp_include_unescaped_ampersand = args.jnlp_unescaped_ampersand
    # A start-up property of this endpoint, not a per-request switch -- see
    # module docstring on why there is no control channel here.
    server.faults.hang_before_response = args.hang_before_response
    server.start()

    _write_json_atomic(args.ready_file, {"pid": os.getpid(), "port": server.port})

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
