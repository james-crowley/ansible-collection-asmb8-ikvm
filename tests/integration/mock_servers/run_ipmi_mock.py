# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Standalone runner: generate a genuinely importable fake ``pyghmi`` package,
backed by :mod:`ipmi_server`'s :class:`FakeIpmiBmc`, and report where it lives.

Deliberately NOT shaped like this directory's other two runners
(``run_asp_mock.py``, ``run_iusb_mock.py``): those start a long-lived listener
process a client dials into over a real socket, so they idle-until-SIGTERM to
keep that listener alive. This script starts nothing that listens for
anything -- see ``ipmi_server.py``'s module docstring on why this collection's
IPMI fixture is a same-process test double, not a wire responder -- so once it
has written the fake package and the initial state file, its job is done and
it exits immediately. There is no server for a caller to signal to stop.

What a caller does with the directory this script reports
------------------------------------------------------------

Put it FIRST on ``PYTHONPATH`` for whatever process should see the fake
``pyghmi`` instead of the real one -- an ``ansible-playbook`` invocation (via
that task's own ``environment:``, or inherited from the parent process's
environment for a nested invocation), a plain Python subprocess, or this
directory's own unit tests reaching in directly. Every ``import pyghmi`` in
that process then resolves to the generated package, and by extension every
``from pyghmi import exceptions``/``from pyghmi.ipmi import command`` --
including the one inside ``plugins/module_utils/ipmi.py`` itself -- resolves
to the SAME generated modules, consistently, for the lifetime of that one
process. This was verified directly, not assumed: a real, unmodified
``asmb8_power`` module, run through a real ``ansible-playbook`` process with
this mechanism wired up, returns this double's fixture state rather than
attempting a real RMCP+ session.

Cross-process state
---------------------

Every module invocation Ansible makes -- even three tasks in the same
playbook, even all ``delegate_to: localhost`` -- is its own OS process under
AnsiballZ. The generated package's ``Command`` therefore does not hold a
:class:`ipmi_server.FakeIpmiBmc` with only in-memory state: it is constructed
with ``sync_path`` pointed at a JSON file THIS script writes first (see
``--state-path``), so a later process's import of the same generated package
picks up whatever an earlier process's calls left behind -- the one-time boot
override reverting after a `asmb8_power` reset, in particular, depends on
this working across the `asmb8_boot` and `asmb8_power` invocations that arm
and consume it being genuinely separate processes.

No fault-injection control channel exists here, for the same reason
``run_asp_mock.py``'s own docstring gives for the same absence: every fault
this script's flags below can arrange is a start-up-time property of the
generated package, established once, before any real module process ever
imports it -- not a live channel a still-running listener could be told to
flip mid-session, because there is no still-running listener at all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path


def _write_json_atomic(path: str, data: dict) -> None:
    target = Path(path)
    tmp = target.with_name(f"{target.name}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, target)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock-servers-dir", required=True, help="Directory containing ipmi_server.py")
    parser.add_argument("--ready-file", required=True, help="Path this script writes {pid, shim_path, state_path} to once ready")
    parser.add_argument("--shim-dir", help="Directory to write the fake pyghmi package into. Defaults to a directory beside --ready-file")
    parser.add_argument("--state-path", help="Path for the cross-process state JSON file. Defaults to a file beside --ready-file")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="test-password-not-real")  # fixture default, not a real credential
    parser.add_argument("--initial-power-state", choices=("on", "off"), default="off")
    parser.add_argument(
        "--initial-boot-device",
        default="default",
        help="One of asmb8_boot's own device choices (network/floppy/hd/safe/optical/setup/default)",
    )
    parser.add_argument("--initial-boot-persist", action="store_true")
    parser.add_argument("--initial-boot-uefi", action="store_true")
    parser.add_argument("--mc-info", default=None, help="Override the get_mci() string this fixture reports; omit for its own default")
    parser.add_argument(
        "--omit-uefimode-when-default",
        action="store_true",
        help="Model the alternate, NOT-observed-on-this-board get_bootdev() default shape -- see ipmi_server.py's docstring",
    )
    parser.add_argument("--force-auth-failure", action="store_true", help="Every connect() raises the same message a wrong password would")
    parser.add_argument("--force-unreachable", action="store_true", help="Every connect() raises the same message an unanswering BMC would")
    parser.add_argument(
        "--force-generic-exception",
        default=None,
        help="Every connect() raises pyghmi.exceptions.IpmiException with this message (checked after the two flags above)",
    )
    parser.add_argument(
        "--force-power-wait-timeout",
        action="store_true",
        help="Every confirmable set_power(wait=...) raises the exact pyghmi confirmation-timeout message, after still applying the transition",
    )
    return parser.parse_args()


_PYGHMI_INIT = ""

_PYGHMI_EXCEPTIONS = '''\
"""Fake pyghmi.exceptions, generated by run_ipmi_mock.py. See ipmi_server.py's module docstring."""


class PyghmiException(Exception):
    pass


class IpmiException(PyghmiException):
    pass
'''

_PYGHMI_IPMI_INIT = ""

_PYGHMI_IPMI_COMMAND_TEMPLATE = '''\
"""Fake pyghmi.ipmi.command, generated by run_ipmi_mock.py. See ipmi_server.py's module docstring.

Backs plugins/module_utils/ipmi.py's `from pyghmi.ipmi import command as ipmi_command` /
`ipmi_command.Command(bmc=, userid=, password=, port=)` with ipmi_server.FakeIpmiBmc,
loaded from {mock_servers_dir!r} on this generated module's own sys.path insert below --
never from plugins/, and never shipped as part of the collection.
"""

import sys

sys.path.insert(0, {mock_servers_dir!r})

from ipmi_server import FakeIpmiBmc, command_factory  # noqa: E402 - see the sys.path insert immediately above

_fixture = FakeIpmiBmc(sync_path={state_path!r})
Command = command_factory(_fixture)
'''


def _write_shim_package(shim_dir: Path, *, mock_servers_dir: str, state_path: str) -> None:
    pyghmi_dir = shim_dir / "pyghmi"
    ipmi_dir = pyghmi_dir / "ipmi"
    ipmi_dir.mkdir(parents=True, exist_ok=True)
    (pyghmi_dir / "__init__.py").write_text(_PYGHMI_INIT, encoding="utf-8")
    (pyghmi_dir / "exceptions.py").write_text(textwrap.dedent(_PYGHMI_EXCEPTIONS), encoding="utf-8")
    (ipmi_dir / "__init__.py").write_text(_PYGHMI_IPMI_INIT, encoding="utf-8")
    (ipmi_dir / "command.py").write_text(
        _PYGHMI_IPMI_COMMAND_TEMPLATE.format(mock_servers_dir=mock_servers_dir, state_path=state_path),
        encoding="utf-8",
    )


def main() -> None:
    args = _parse_args()
    sys.path.insert(0, args.mock_servers_dir)
    from ipmi_server import DEFAULT_MC_INFO, FakeIpmiBmc  # local import: only resolvable after the sys.path insert above

    ready_file = Path(args.ready_file)
    shim_dir = Path(args.shim_dir) if args.shim_dir else ready_file.parent / "ipmi_mock_shim"
    state_path = Path(args.state_path) if args.state_path else ready_file.parent / "ipmi_mock_state.json"
    shim_dir.mkdir(parents=True, exist_ok=True)

    fixture = FakeIpmiBmc(
        username=args.username,
        password=args.password,
        sync_path=state_path,
        omit_uefimode_when_default=args.omit_uefimode_when_default,
    )
    fixture.state.powerstate = args.initial_power_state
    fixture.state.boot_device = args.initial_boot_device
    fixture.state.boot_persist = args.initial_boot_persist
    fixture.state.boot_uefi = args.initial_boot_uefi
    fixture.state.boot_ever_set = args.initial_boot_device != "default" or args.initial_boot_persist or args.initial_boot_uefi
    fixture.state.mc_info = args.mc_info if args.mc_info is not None else DEFAULT_MC_INFO
    fixture.faults.force_auth_failure = args.force_auth_failure
    fixture.faults.force_unreachable = args.force_unreachable
    fixture.faults.force_generic_exception = args.force_generic_exception
    fixture.faults.force_power_wait_timeout = args.force_power_wait_timeout
    fixture.save()

    _write_shim_package(shim_dir, mock_servers_dir=str(Path(args.mock_servers_dir).resolve()), state_path=str(state_path.resolve()))

    _write_json_atomic(
        args.ready_file,
        {"pid": os.getpid(), "shim_path": str(shim_dir.resolve()), "state_path": str(state_path.resolve())},
    )


if __name__ == "__main__":
    main()
