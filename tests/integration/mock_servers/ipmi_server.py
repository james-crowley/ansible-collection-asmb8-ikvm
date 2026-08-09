# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""A ``pyghmi.ipmi.command.Command`` TEST DOUBLE, used to fill the one gap this
collection's mock fixtures had left: there was no fixture at all standing in
for the IPMI/RMCP+ plane ``asmb8_info``/``asmb8_power``/``asmb8_boot`` speak
through ``plugins/module_utils/ipmi.py``.

READ THIS BEFORE REUSING ANYTHING HERE AS EVIDENCE OF WIRE BEHAVIOUR
----------------------------------------------------------------------

Unlike this directory's other two fixtures (``asp_server.py``, an HTTP
listener; ``iusb_server.py``, a raw-TCP listener), :class:`FakeIpmiBmc` in
this file binds NO socket, speaks NO RMCP+/UDP-623 framing, and performs NO
RAKP authentication cryptography. It is a same-process substitute for
``pyghmi.ipmi.command.Command`` itself -- the object
``plugins/module_utils/ipmi.py``'s ``IpmiClient._connect()`` constructs and
calls methods on -- not a responder pyghmi's own session/RAKP code talks to
over a wire. Every method here (``get_power()``/``set_power()``/
``get_bootdev()``/``set_bootdev()``/``get_mci()``) exists purely to return the
shapes ``ipmi.py``'s docstring documents pyghmi's real methods returning (see
that file's citations of pyghmi's *installed source*), so that
:class:`IpmiClient` -- the collection's own code -- gets to run for real
against something, while the RMCP+ wire itself does not.

Why not a real RMCP+ responder, the way ``asp_server.py``/``iusb_server.py``
are real HTTP/TCP responders for their protocols
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This was genuinely attempted first, not skipped for convenience, and using
pyghmi's OWN server-side sample (``pyghmi.ipmi.bmc.Bmc`` /
``pyghmi.ipmi.private.serversession``) rather than hand-rolling RAKP --
exactly the kind of protocol machinery this collection's README policy (never
hand-roll something a maintained library already does correctly) argues
against duplicating. That investigation found two independent, reproducible
problems in running pyghmi's client (``pyghmi.ipmi.command.Command``) and its
own server sample in the same process tree on this project's supported dev/CI
platforms:

1. ``pyghmi.ipmi.private.session`` bootstraps a process-wide self-wake socket
   the first time ANY session (client or server) is created, and records that
   first socket's address family as a module-level ``myself``/``iosockets[0]``
   pair used for every later session regardless of family. Constructing the
   server (``Bmc(..., address='127.0.0.1')``, i.e. AF_INET, to satisfy this
   task's "bind everything to 127.0.0.1" rule) before any client session
   exists left that bootstrap in a self-inconsistent state
   (``socket.gaierror: nodename nor servname provided, or not known`` from
   ``_io_wait``'s internal wake-up ``sendto``) -- reproduced directly, not
   assumed.
2. Forcing IPv4-only globals to route around problem 1 (both in-process and
   across two genuinely separate OS processes, sidestepping the shared-globals
   theory entirely) still left the client's own ``Command()`` constructor
   failing after its own internal retries, with ``pyghmi.exceptions.
   IpmiException`` carrying no message at all (``errormsg=None``) -- i.e. the
   server-sample code, by its own module docstring "a quick sample of how to
   write something that acts like a bmc", does not reliably complete RAKP
   against pyghmi's own client in this environment for a reason this
   investigation did not chase further once two independent failure modes had
   already appeared.

Both are exactly the "easy to get subtly wrong" territory this task's own
brief warned about for a hand-rolled responder -- except here it surfaced from
reusing pyghmi's OWN maintained (if self-admittedly "quick sample") code, which
is a stronger signal to stop than if it were this collection's own bug. Per
this task's explicit instruction to fall back rather than ship something
half-working, this file is the fallback: a test double, named as one, with
every behaviour it reproduces cited back to ``ipmi.py``'s own sourced
docstring rather than re-derived here.

How this double is actually used for something more than a monkeypatch target
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Two consumers, both in this collection's own test tree, never in
``plugins/``:

* Unit tests monkeypatch ``ipmi.ipmi_command.Command`` directly with
  :func:`command_factory`'s return value (the same seam
  ``tests/unit/plugins/module_utils/test_ipmi.py`` already patches with a bare
  ``Mock`` -- this is a stateful, fault-injecting upgrade of that same seam,
  not a new one), so the REAL ``IpmiClient`` class runs against this double
  end to end.
* ``run_ipmi_mock.py`` (this directory) writes a tiny, genuinely importable
  fake ``pyghmi`` package to a scratch directory, backed by this file's
  :class:`FakeIpmiBmc`, and hands that directory's path to a caller to put
  first on ``PYTHONPATH``. Verified directly: ``ansible``'s AnsiballZ module
  wrapper honours a ``PYTHONPATH`` set via a task's ``environment:`` (or
  inherited from ``ansible-playbook``'s own process environment) for local
  module execution, so a REAL, unmodified ``asmb8_power``/``asmb8_boot``/
  ``asmb8_info`` module, run by a real ``ansible-playbook`` process, imports
  THIS double instead of real ``pyghmi`` for the duration of that run -- genuine
  module-level integration coverage with no wire protocol anywhere in the
  loop, and no line in ``plugins/`` or ``roles/`` touched to arrange it. See
  ``run_ipmi_mock.py``'s own docstring for the generated package's shape.

Cross-process state
~~~~~~~~~~~~~~~~~~~~

A single ``ansible-playbook`` run of a realistic lifecycle issues
``asmb8_boot``, then ``asmb8_power``, then ``asmb8_info`` as three SEPARATE
module invocations -- three separate OS processes under AnsiballZ, even for
``delegate_to: localhost``. In-memory state on a Python object cannot survive
that. :class:`FakeIpmiBmc` therefore supports an optional ``sync_path``: when
set, every call reloads current state from that JSON file first and persists
any mutation back to it, mirroring this collection's own
``media_session.py``'s one-JSON-file-per-session pattern for exactly the same
cross-process reason. Unit tests that never leave one process may simply omit
``sync_path`` and get plain in-memory behaviour.

Provenance of the specific behaviours modelled below
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Every one of these is cited, in ``plugins/module_utils/ipmi.py``'s own
docstring, back to pyghmi's installed source or the maintainer's live capture
against the target board -- restated here only to the extent needed to model
it, never re-derived independently:

* ``get_power()`` -> ``{'powerstate': 'on'|'off'}``.
* ``get_bootdev()`` -> a dict with ``bootdev``/``persistent`` always, and
  ``uefimode`` present on this board's own observed default state too (VERIFIED
  LIVE per this task's brief: ``{'bootdev': 'default', 'persistent': False,
  'uefimode': False}``) -- NOT the "no key at all" shape some other pyghmi
  BMCs report for an untouched override, which ``omit_uefimode_when_default``
  exists to model separately, opt-in, as a defensive-code-path fixture rather
  than something ever observed on THIS board.
* ``set_bootdev(device, persist=False, uefiboot=...)`` establishes a one-time
  override; the single most important behaviour this file pins is that an
  actual power ON or RESET transition consumes a non-persistent override back
  to ``'default'`` -- modelled in :meth:`FakeIpmiBmc.consume_one_time_boot`,
  called from :meth:`FakeIpmiCommand.set_power`'s own transition handling, the
  same place pyghmi's real client would trigger the real chassis-control
  command that a real BMC would react to the same way.
* ``get_mci()`` -> a bare ``str`` (or ``None``), never a dict.
* ``set_power(state, wait=...)``'s confirmation loop raises
  ``pyghmi.exceptions.IpmiException("System did not accomplish power state
  change")`` -- the exact, verbatim string ``ipmi.py`` pattern-matches to
  produce ``indeterminate=True`` -- when confirmation never observes the
  target state. :attr:`IpmiFaultConfig.force_power_wait_timeout` reproduces
  that outcome directly (the underlying transition still applies -- see the
  docstring on ``ipmi.py`` for why that ordering matters) rather than
  simulating a slow poll loop.
* A session-establishment failure surfaces uniformly as
  ``pyghmi.exceptions.IpmiException``, classified by ``ipmi.py``'s own
  ``_classify_session_error`` from message text pyghmi itself assigns
  (``"Incorrect password provided"`` for bad credentials; the literal
  substring ``"timeout"`` for no response at all). :func:`connect` raises with
  exactly those two strings for the two corresponding faults, and a bare
  ``pyghmi.exceptions.IpmiException`` with a caller-chosen message for
  anything that should classify as neither (this collection's
  ``error_class=remote_operation``/``connection`` catch-all).
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    # The normal case: this module is used directly (unit tests monkeypatching
    # `ipmi.ipmi_command.Command`, or a real ansible-test integration process
    # where pyghmi is genuinely installed alongside the fake `pyghmi` package
    # this file's own `command_factory()`/`connect()` back). Reusing the real
    # `pyghmi.exceptions` classes here means whatever `plugins/module_utils/
    # ipmi.py` catches (`except ipmi_exceptions.PyghmiException`) is the exact
    # same class object this double raises.
    from pyghmi import exceptions as ipmi_exceptions
except ImportError:
    # `run_ipmi_mock.py` imports this module to GENERATE the fake `pyghmi`
    # package -- before any such package exists on `sys.path` -- specifically
    # so that generation step itself does not require the real `pyghmi`
    # runtime dependency to be installed (the whole point of a double is to
    # let this collection's IPMI modules be exercised without it). At actual
    # module-EXECUTION time (inside the generated shim), `pyghmi` is already
    # resolved to that shim package by the time this file is imported (see
    # the generated `pyghmi/ipmi/command.py`'s own `sys.path` insert), so the
    # `try` branch above succeeds there even when the real library is absent
    # -- this `except` branch is reached only during generation, and its
    # classes are never actually raised at that point.
    class _FallbackPyghmiExceptions:
        class PyghmiException(Exception):
            pass

        class IpmiException(PyghmiException):
            pass

    ipmi_exceptions = _FallbackPyghmiExceptions

#: Fixture defaults, not real credentials.
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "test-password-not-real"

#: This fixture's own made-up management-controller identifier string --
#: get_mci()'s return shape (a bare string) is VERIFIED LIVE per ipmi.py's
#: docstring; this specific text is this mock's own choice, not a captured
#: value.
DEFAULT_MC_INFO = "asmb8-ikvm-mock-mc-id"

#: VERIFIED, per ipmi.py's docstring citing pyghmi's own source directly: the
#: exact message pyghmi's `Command.set_power(wait=...)` raises when its
#: confirmation loop exhausts its budget without observing the target power
#: state.
SET_POWER_WAIT_TIMEOUT_MESSAGE = "System did not accomplish power state change"

#: VERIFIED, per ipmi.py's docstring: the literal marker text pyghmi's own
#: session code assigns to a login rejected on credentials, and the one
#: ipmi.py's `_classify_session_error` looks for.
AUTH_FAILURE_MESSAGE = "Incorrect password provided"

#: VERIFIED, per ipmi.py's docstring: the literal substring pyghmi's own
#: session code assigns to a session that timed out with no response at all.
UNREACHABLE_MESSAGE = "timeout"

#: The seven `asmb8_boot` device choices (module_utils/models.py's
#: BOOT_DEVICES, itself sourced from community.general.ipmi_boot's
#: documentation -- not invented here).
KNOWN_BOOT_DEVICES = frozenset({"network", "floppy", "hd", "safe", "optical", "setup", "default"})

#: Aliases pyghmi's own `boot_devices` dict accepts for the same underlying
#: selector (read directly from pyghmi's installed source) -- reproduced here
#: so a test can exercise the exact "'cd' is accepted, and reads back as
#: 'optical'" example this task's brief itself calls out.
BOOT_DEVICE_ALIASES = {
    "cd": "optical",
    "cdrom": "optical",
    "dvd": "optical",
    "pxe": "network",
    "net": "network",
    "http": "network",
    "usb": "floppy",
    "bios": "setup",
    "f1": "setup",
}

#: Power states this fixture's `set_power()` recognises -- pyghmi's own
#: `power_states` vocabulary (read directly from its installed source), minus
#: the raw wire-level integer values this double has no use for.
KNOWN_POWER_STATES = frozenset({"on", "off", "reset", "shutdown", "softoff", "boot"})

#: `state` values pyghmi's own confirmation loop actually polls for -- see
#: ipmi.py's docstring: `reset`/`boot`-resolved-to-`reset` are fire-and-forget
#: regardless of `wait`.
_CONFIRMABLE_STATES = frozenset({"on", "off", "shutdown", "softoff"})


@dataclass
class IpmiBmcState:
    """Mutable firmware-like state: what a get_* call observes, and what a prior set_* call changed."""

    username: str = DEFAULT_USERNAME
    password: str = DEFAULT_PASSWORD
    powerstate: str = "off"
    boot_device: str = "default"
    boot_persist: bool = False
    boot_uefi: bool = False
    #: Whether `set_bootdev()` has ever been called on this fixture. Only
    #: relevant when `omit_uefimode_when_default` is enabled -- see that
    #: field's docstring on `FakeIpmiBmc`.
    boot_ever_set: bool = False
    mc_info: str | None = DEFAULT_MC_INFO


@dataclass
class IpmiFaultConfig:
    """Every fault-injection knob this double understands.

    All persistent (not one-shot), matching ``run_asp_mock.py``'s own
    documented reasoning for why its mocks offer no fine-grained per-call
    control channel across process boundaries: a genuinely separate future
    process (the shim case) cannot be sent a "the next call only" instruction
    after it has already started, so every fault here is a start-up-time,
    stays-in-effect property, exactly like ``AspFaultConfig.
    hang_before_response``. A test using this double in a single process can
    still flip a field back and forth between calls if it wants one-shot
    behaviour -- nothing here prevents that -- but nothing here provides it
    automatically either.
    """

    #: `connect()` always raises with :data:`AUTH_FAILURE_MESSAGE`, regardless
    #: of whether the supplied credentials were actually correct. A genuine
    #: credential mismatch (see `connect()`) raises the same way with no flag
    #: needed -- this exists for a test that wants the failure despite
    #: otherwise-correct credentials.
    force_auth_failure: bool = False
    #: `connect()` always raises with :data:`UNREACHABLE_MESSAGE`, standing in
    #: for a BMC that never answers at all.
    force_unreachable: bool = False
    #: `connect()` always raises `pyghmi.exceptions.IpmiException(message)`
    #: with this text, when neither of the two flags above is set -- the
    #: generic, neither-auth-nor-timeout-shaped `IpmiException` this task's
    #: brief calls out separately from the other two.
    force_generic_exception: str | None = None
    #: Any confirmable `set_power(..., wait=...)` call raises
    #: :data:`SET_POWER_WAIT_TIMEOUT_MESSAGE` -- see this module's docstring:
    #: the underlying transition still applies before this is raised, exactly
    #: as ipmi.py's own docstring describes the real failure.
    force_power_wait_timeout: bool = False


class FakeIpmiBmc:
    """The stateful fixture a test configures and asserts against.

    Not a network listener -- see this module's docstring. ``sync_path``, when
    given, makes :attr:`state`/:attr:`faults` persist to (and reload from) a
    JSON file, for the cross-process shim use case; omit it for plain
    in-memory use within a single test.
    """

    def __init__(
        self,
        *,
        username: str = DEFAULT_USERNAME,
        password: str = DEFAULT_PASSWORD,
        sync_path: str | os.PathLike[str] | None = None,
        omit_uefimode_when_default: bool = False,
    ) -> None:
        self.state = IpmiBmcState(username=username, password=password)
        self.faults = IpmiFaultConfig()
        #: Opt-in, NOT observed on the target board -- see this module's
        #: docstring's provenance section. Off by default so this fixture's
        #: default shape matches the VERIFIED LIVE default observation
        #: exactly.
        self.omit_uefimode_when_default = omit_uefimode_when_default
        self.sync_path = Path(sync_path) if sync_path is not None else None
        if self.sync_path is not None and not self.sync_path.exists():
            # First writer wins -- a later construction in another process
            # (the shim case) must never reset state an earlier one already
            # persisted. See this module's docstring's "Cross-process state".
            self._save()

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        """Reload `state`/`faults` from `sync_path`, if set and the file exists. No-op otherwise."""
        if self.sync_path is None or not self.sync_path.exists():
            return
        payload = json.loads(self.sync_path.read_text(encoding="utf-8"))
        for field_name, value in payload.get("state", {}).items():
            setattr(self.state, field_name, value)
        for field_name, value in payload.get("faults", {}).items():
            setattr(self.faults, field_name, value)
        self.omit_uefimode_when_default = payload.get("omit_uefimode_when_default", self.omit_uefimode_when_default)

    def _save(self) -> None:
        if self.sync_path is None:
            return
        payload = {
            "state": dataclasses.asdict(self.state),
            "faults": dataclasses.asdict(self.faults),
            "omit_uefimode_when_default": self.omit_uefimode_when_default,
        }
        tmp = self.sync_path.with_name(f"{self.sync_path.name}.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self.sync_path)

    # -- one-time boot semantics --------------------------------------------

    def consume_one_time_boot(self) -> None:
        """Revert a non-persistent boot override to 'default'.

        The single most important behaviour this fixture pins -- see this
        module's docstring. A no-op when the current override is already
        'default', or was armed with `persist=True` (out of scope for this
        collection's own `asmb8_boot`, which refuses `persistent=True`
        outright before ever reaching this double -- but pyghmi's own API
        allows it, and this fixture stays honest about that boundary too).
        """
        if self.state.boot_device != "default" and not self.state.boot_persist:
            self.state.boot_device = "default"
            self.state.boot_persist = False
            self.state.boot_uefi = False

    def save(self) -> None:
        """Persist current state -- exposed for a test that mutates `state`/`faults` directly."""
        self._save()


def connect(fixture: FakeIpmiBmc, *, bmc: str, userid: str, password: str, port: int = 623, **_ignored: Any) -> FakeIpmiCommand:
    """Stand-in for `pyghmi.ipmi.command.Command(bmc=, userid=, password=, port=)`.

    Raises exactly the way `IpmiClient._connect()`'s docstring says a real
    session failure does: uniformly as `pyghmi.exceptions.IpmiException`,
    classified by message text alone. See :class:`IpmiFaultConfig` for what
    each fault produces.
    """
    fixture.load()
    if fixture.faults.force_unreachable:
        raise ipmi_exceptions.IpmiException(UNREACHABLE_MESSAGE)
    if fixture.faults.force_auth_failure or userid != fixture.state.username or password != fixture.state.password:
        raise ipmi_exceptions.IpmiException(AUTH_FAILURE_MESSAGE)
    if fixture.faults.force_generic_exception:
        raise ipmi_exceptions.IpmiException(fixture.faults.force_generic_exception)
    return FakeIpmiCommand(fixture, endpoint=f"{bmc}:{port}")


def command_factory(fixture: FakeIpmiBmc):
    """Return a callable matching `pyghmi.ipmi.command.Command`'s constructor signature.

    For `monkeypatch.setattr(ipmi.ipmi_command, "Command", command_factory(fixture))`
    -- the same seam `tests/unit/plugins/module_utils/test_ipmi.py` already
    patches with a bare `Mock`.
    """

    def _factory(*, bmc: str, userid: str, password: str, port: int = 623, **kwargs: Any) -> FakeIpmiCommand:
        return connect(fixture, bmc=bmc, userid=userid, password=password, port=port, **kwargs)

    return _factory


class FakeIpmiCommand:
    """Stands in for one `pyghmi.ipmi.command.Command` instance -- i.e. one open IPMI session."""

    def __init__(self, fixture: FakeIpmiBmc, *, endpoint: str) -> None:
        self._fixture = fixture
        #: Not part of the real pyghmi API (IpmiClient never reads this
        #: attribute back) -- kept only because it is harmless and useful for
        #: a test to inspect.
        self.endpoint = endpoint

    # -- power ---------------------------------------------------------------

    def get_power(self) -> dict[str, str]:
        self._fixture.load()
        return {"powerstate": self._fixture.state.powerstate}

    def set_power(self, powerstate: str, wait: bool | int = False) -> dict[str, Any]:
        self._fixture.load()
        if powerstate not in KNOWN_POWER_STATES:
            raise ipmi_exceptions.IpmiException(f"Unknown power state {powerstate} requested")

        old = self._fixture.state.powerstate
        new = powerstate
        if old == new:
            # Convergent, matching pyghmi's own real set_power(): no state
            # change, no transition applied, nothing to confirm.
            return {"powerstate": old}
        if new == "boot":
            new = "on" if old == "off" else "reset"

        self._apply_transition(new)
        self._fixture._save()

        if wait and new in _CONFIRMABLE_STATES:
            if self._fixture.faults.force_power_wait_timeout:
                # The transition above already applied -- see this module's
                # docstring and ipmi.py's own: confirmation failing is not the
                # same as the command being rejected.
                raise ipmi_exceptions.IpmiException(SET_POWER_WAIT_TIMEOUT_MESSAGE)
            target = "off" if new in ("shutdown", "softoff") else new
            return {"powerstate": target}
        return {"pendingpowerstate": new}

    def _apply_transition(self, new: str) -> None:
        if new == "on":
            self._fixture.state.powerstate = "on"
            self._fixture.consume_one_time_boot()
        elif new == "reset":
            # A reset does not change the reported power state (the board
            # never appeared 'off' to a caller) -- it is the transition
            # itself, not a resulting state, that consumes a one-time boot
            # override.
            self._fixture.consume_one_time_boot()
        elif new in ("off", "shutdown", "softoff"):
            self._fixture.state.powerstate = "off"

    # -- boot device ---------------------------------------------------------

    def get_bootdev(self) -> dict[str, Any]:
        self._fixture.load()
        state = self._fixture.state
        if state.boot_device == "default" and self._fixture.omit_uefimode_when_default and not state.boot_ever_set:
            # Opt-in, not observed on this board -- see this module's
            # docstring's provenance section.
            return {"bootdev": "default", "persistent": True}
        return {"bootdev": state.boot_device, "persistent": state.boot_persist, "uefimode": state.boot_uefi}

    def set_bootdev(self, bootdev: str, persist: bool = False, uefiboot: bool = False) -> dict[str, Any]:
        self._fixture.load()
        device = BOOT_DEVICE_ALIASES.get(bootdev, bootdev)
        if device not in KNOWN_BOOT_DEVICES:
            # pyghmi's own set_bootdev() *returns* this shape rather than
            # raising for an unrecognised device -- see ipmi.py's docstring.
            return {"error": f"Unknown bootdevice {bootdev} requested"}
        state = self._fixture.state
        state.boot_device = device
        state.boot_persist = bool(persist)
        state.boot_uefi = bool(uefiboot)
        state.boot_ever_set = True
        self._fixture._save()
        return {"bootdev": device}

    # -- management-controller info -------------------------------------------

    def get_mci(self) -> str | None:
        self._fixture.load()
        return self._fixture.state.mc_info


__all__ = [
    "AUTH_FAILURE_MESSAGE",
    "BOOT_DEVICE_ALIASES",
    "DEFAULT_MC_INFO",
    "DEFAULT_PASSWORD",
    "DEFAULT_USERNAME",
    "KNOWN_BOOT_DEVICES",
    "KNOWN_POWER_STATES",
    "SET_POWER_WAIT_TIMEOUT_MESSAGE",
    "UNREACHABLE_MESSAGE",
    "FakeIpmiBmc",
    "FakeIpmiCommand",
    "IpmiBmcState",
    "IpmiFaultConfig",
    "command_factory",
    "connect",
]
