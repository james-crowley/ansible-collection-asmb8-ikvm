# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""A thin, classified wrapper over ``pyghmi``'s synchronous IPMI calls.

Unlike ``asp.py``'s ``.asp``/JNLP surface, this collection does not reimplement
IPMI: RMCP+ session establishment, packet framing and retries are exactly the
kind of protocol machinery this collection's own README policy (never assert a
protocol fact without a source, never hand-roll something a maintained library
already does correctly) argues against duplicating. ``pyghmi`` is also what
``community.general.ipmi_power``/``ipmi_boot`` themselves call underneath --
delegating to it directly, rather than shelling out to those Ansible modules,
avoids adding a whole collection as a runtime dependency for two calls' worth
of functionality.

Every protocol fact this module encodes was sourced by reading ``pyghmi``'s
*installed source* directly (its docstrings and the ``pyghmi.ipmi.command``/
``pyghmi.ipmi.private.session`` implementations, inspected in a disposable
virtualenv with no BMC reachable from it) -- never from memory, and never by
making a single request against any real BMC. Specifically:

* ``pyghmi.ipmi.command.Command(bmc=..., userid=..., password=..., port=...)``
  is synchronous when constructed without an ``onlogon`` callback: it blocks
  until RMCP+ session establishment either succeeds or fails, and a failure of
  any kind -- unreachable host, DNS failure, wrong password, wrong Kg, no
  response at all -- surfaces uniformly as ``pyghmi.exceptions.IpmiException``
  (``pyghmi/ipmi/private/session.py``, ``Session.__init__``: ``self.login();
  ... if self.broken: raise exc.IpmiException(self.errormsg)``). Pyghmi does
  not give us a distinct exception *type* per failure mode the way this
  collection's own ``errors.py`` taxonomy wants; :func:`_classify_session_error`
  recovers an approximation of that distinction from ``errormsg`` text pyghmi
  itself assigns (``"Incorrect password provided"``, the literal string
  ``"timeout"`` on no response, etc.) -- the same style of best-effort,
  documented text classification ``asp.py``'s
  ``_connection_error_is_post_connect`` already uses for a similar gap in
  ``requests``/``urllib3``.
* ``Command.get_power()`` returns ``{'powerstate': 'on'}`` or
  ``{'powerstate': 'off'}`` -- confirmed both by reading
  ``Command._get_power_state`` (only those two strings are ever produced) and
  by the maintainer's live capture against the target hardware quoted in this
  collection's task brief.
* ``Command.set_power(state, wait=...)`` sends the raw IPMI chassis-control
  command immediately, then -- only when ``wait`` is truthy and the requested
  state is one it can actually confirm (``on``/``off``/``shutdown``/
  ``softoff``) -- polls ``get_power`` in a bounded loop. If that confirmation
  loop exhausts its budget without observing the target state, it raises
  ``IpmiException("System did not accomplish power state change")`` -- and
  critically, by that point the underlying chassis-control command was already
  accepted (no ``'error'`` key on the initial response; that path raises
  immediately and separately). This is exactly this collection's
  ``indeterminate`` case: the mutation may have taken effect even though
  *confirmation* of it did not, so :meth:`IpmiClient.set_power_state` matches
  this one message and re-raises as :class:`errors.TimeoutError_` with
  ``indeterminate=True`` rather than the ordinary
  :class:`errors.RemoteOperationError` every other pyghmi failure here becomes.
* ``Command.get_bootdev()`` returns a dict with ``bootdev``/``persistent`` keys
  always, and ``uefimode`` only on the branch that is not the bare
  ``'default'`` override -- read directly from its source; a caller that
  assumes ``uefimode`` is always present will hit a ``KeyError`` on some real
  responses.
* ``Command.set_bootdev(bootdev, persist=, uefiboot=)`` does not always raise
  on failure: an unrecognised ``bootdev`` string makes it *return*
  ``{'error': ...}`` rather than raise, while an IPMI-level rejection of either
  of its two underlying raw commands raises ``IpmiException`` as usual. Both
  shapes are handled in :meth:`IpmiClient.set_boot_device`.
* ``Command.get_mci()`` returns a bare ``str`` (or ``None``), never a dict --
  confirmed both by reading its source (it returns either
  ``self._oem.get_oem_identifier()`` or the result of a DCMI fetch helper,
  neither dict-shaped) and by the maintainer's live capture. Do not write a
  caller that treats this like ``get_power()``/``get_bootdev()``.
* ``Command.reset_bmc()`` (``command.py:409-413``) issues Cold Reset (netfn
  ``0x06`` cmd ``0x02``) via ``self.raw_command(netfn=6, command=2,
  retry=False)`` and raises ``exc.IpmiException(response['error'])`` only if
  the response carries an ``'error'`` key; it returns nothing itself. Warm
  Reset (netfn ``0x06`` cmd ``0x03``) has no dedicated pyghmi wrapper at
  all -- confirmed by reading ``command.py`` end to end, the same way every
  other fact above was sourced -- so :meth:`IpmiClient.reset_bmc` issues it
  the identical way ``reset_bmc()`` issues Cold Reset internally: a bare
  ``raw_command(netfn=6, command=3, retry=False)`` plus the same
  ``'error' in response`` check. This is reproduction of an existing,
  already-standard pattern for the sibling command byte, not new protocol
  invention.

  **Live capture against the target board (ASUS ASMB8-iKVM), Cold Reset
  only** -- the maintainer issued ``c.raw_command(netfn=0x06, command=0x02)``
  directly and observed:

  - The response was ``{'netfn': 7, 'command': 2, 'code': 0, 'data':
    bytearray(b'')}`` -- completion code 0, i.e. no ``'error'`` key, exactly
    the success shape :meth:`IpmiClient.reset_bmc` checks for.
  - The host stayed powered on and completely unaffected throughout: a
    ``get_power()`` immediately before the reset read ``{'powerstate':
    'on'}``, and the host never rebooted. A BMC self-reset (either mode) is a
    management-controller-only operation, confirmed live, not an inference
    from the IPMI spec alone.
  - Recovery of the BMC afterwards was staged, not instantaneous or uniform
    across services: ICMP answered noticeably before the ``.asp`` web/HTTPS
    stack (port 443) did. No exact duration was captured for either stage,
    so this collection does not assert one; a caller that must know the BMC
    is back should poll the specific service it actually depends on (IPMI
    itself, via a fresh :class:`IpmiClient`/``asmb8_info``, or HTTPS) rather
    than ICMP, which recovers first and is not evidence the management stack
    is usable yet.
  - The on-demand iUSB media listener (port 5120) was closed immediately
    after the reset -- expected and normal, not a fault: that port is bound
    only after a JNLP fetch allocates a session (see ``asp.py``'s
    ``allocate_media_session``), and a reset drops every session, so nothing
    has allocated it yet.

  Warm Reset was not exercised live by that capture; nothing above should be
  read as observed for that mode. It is expected to share the same
  host-unaffected property (both modes are netfn ``0x06`` App-group
  management-controller self-resets, not chassis-power operations), but that
  expectation is not itself a live observation.
* ``Command.set_identify(on=True, duration=None, blink=False)``
  (``command.py:555-596``) is the standard IPMI Chassis Identify command
  (netfn ``0x00``, cmd ``0x04``). Read directly from the installed source, in
  a disposable virtualenv with no BMC reachable from it, never from memory:

  - It always calls ``self.oem_init()`` first, which resolves an OEM handler
    via ``pyghmi.ipmi.oem.lookup.get_oem_handler()`` keyed on the BMC's
    manufacturer ID. That lookup's ``oemmap`` (``pyghmi/ipmi/oem/lookup.py``)
    contains only three entries -- IBM x86/System X (20301), Lenovo x86
    (19046), and 7154 -- all routed to the same Lenovo handler module. American
    Megatrends (this board's manufacturer) is not a key in that map, so
    ``get_oem_handler()`` falls through to its own ``else`` branch and returns
    ``pyghmi.ipmi.oem.generic.OEMHandler`` instead.
  - ``set_identify()`` then calls ``self._oem.set_identify(on, duration,
    blink)`` inside a ``try``. ``generic.OEMHandler.set_identify()``
    (``oem/generic.py:398-404``) unconditionally
    ``raise exc.UnsupportedFunctionality()`` -- it has no real
    implementation, it exists only so a real OEM handler can override it.
    ``Command.set_identify()`` catches exactly that exception and falls
    through to the standard-command body below it; it does NOT propagate to
    this collection's caller on this board. (It also catches
    ``exc.BypassGenericBehavior`` separately and returns immediately if an
    OEM handler raises that instead -- not applicable here, since the generic
    handler never raises it.)
  - ``blink=True`` reaching the standard-command fallback always raises
    ``exc.IpmiException('Blink not supported with generic IPMI')`` --
    unconditionally, regardless of ``on``/``duration``. This module never
    passes ``blink=True`` for exactly that reason: exposing a ``blink`` option
    on this board would be offering a capability pyghmi itself cannot honour
    here, per this collection's own policy against inventing options a wrapped
    library cannot actually perform.
  - When ``duration`` is not ``None`` (an integer number of seconds, clamped
    by pyghmi itself to the range 0-255), the ``on`` argument is IGNORED
    entirely: a single-byte raw command (``raw_command(netfn=0, command=4,
    data=[duration])``) is sent, carrying only the Identify Interval byte with
    no Force-Identify-On byte. Per the IPMI 2.0 specification this byte's
    value governs identify on its own: ``0`` turns the LED off, any other
    value (1-255) turns it on for that many seconds. This is why
    :meth:`IpmiClient.set_identify` refuses ``duration=0`` combined with
    ``on=True`` at the module boundary -- sending it would silently turn the
    LED OFF despite the caller asking for "on".
  - When ``duration`` is ``None``, a two-byte command is sent instead
    (``data=[0, 1]`` for "on indefinitely", ``data=[0, 0]`` for "off"): byte 1
    (Identify Interval) is always ``0``, and byte 2 (Force Identify On) is
    ``1`` if ``on`` else ``0``. This is the only path that can request
    indefinite-on.
  - On success this method returns ``None`` -- a bare ``return`` on every
    successful branch, never a dict the way ``get_power()``/``get_bootdev()``
    do. A caller that expects a return payload to inspect will get nothing to
    inspect; there is also no standard IPMI command to read identify state
    back (Chassis Identify is fire-and-forget by the spec itself, not merely
    by this module's choice), which is why :meth:`IpmiClient.set_identify`
    likewise returns ``None`` and ``asmb8_identify`` cannot be idempotent.
  - Any raw-command rejection (a non-zero completion code, surfaced by
    ``raw_command()`` as a returned ``{'error': ...}`` dict) becomes
    ``exc.IpmiException(response['error'])``, raised directly by
    ``set_identify()`` itself -- the same "raises rather than returns an
    error dict" shape ``reset_bmc()`` has for its own failure path.

  Not exercised in a live capture against the target board (unlike Cold
  Reset above) -- this is sourced entirely from reading pyghmi 1.6.19's
  installed source directly, cited by file and line above, not from memory
  and not from any request made to a real BMC.
"""

from __future__ import annotations

from typing import Any

try:
    from pyghmi import exceptions as ipmi_exceptions
    from pyghmi.ipmi import command as ipmi_command

    HAS_PYGHMI = True
    PYGHMI_IMPORT_ERROR: str | None = None
except ImportError as _import_error:  # pragma: no cover - exercised by the import sanity test
    # Guarded the same way asp.py guards `requests`: importing this module on a
    # controller without `pyghmi` yields a clear, actionable failure from the
    # calling module via missing_required_lib(), rather than an ImportError
    # traceback with no remedy attached.
    ipmi_command = None  # type: ignore[assignment]
    ipmi_exceptions = None  # type: ignore[assignment]
    HAS_PYGHMI = False
    PYGHMI_IMPORT_ERROR = str(_import_error)

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import (
    AuthenticationError,
    ConnectionError_,
    RemoteOperationError,
    TimeoutError_,
    UnsupportedCapabilityError,
)

#: RMCP/IPMI-over-LAN's standard port. Deliberately a separate constant (and a
#: separate module option, wherever this client is used) from `asp.py`'s
#: `DEFAULT_PORT` -- that one is this BMC's HTTPS web-management port (443);
#: this one is unrelated UDP traffic to the same host.
DEFAULT_IPMI_PORT = 623

#: The exact message text `pyghmi.ipmi.command.Command.set_power`'s internal
#: confirmation loop raises when it exhausts its wait budget without observing
#: the target power state -- see this module's docstring for why that one
#: message, and only that one, means "the command was accepted; only the
#: confirmation timed out" rather than "the command itself failed".
_SET_POWER_WAIT_TIMEOUT_MESSAGE = "System did not accomplish power state change"

#: Substrings of a pyghmi session error message that indicate the BMC rejected
#: the *credentials* rather than being unreachable or slow to answer. Sourced
#: from reading `pyghmi.ipmi.private.session`'s literal error strings directly
#: (`"Incorrect password provided"`, `"Invalid RAKP4 integrity code (wrong
#: Kg?)"`, and the RAKP2 "unauthorized name" completion-code text surfaced
#: through `get_ipmi_error()`) -- not guessed. Matched case-insensitively and
#: as substrings because pyghmi appends context (e.g. a RAKP stage suffix) to
#: some of these rather than raising the bare string.
_AUTH_FAILURE_MARKERS = ("password", "rakp", "unauthorized", "wrong kg")

#: Substring pyghmi uses, verbatim, as the `errormsg` for a session that never
#: received a response at all (`Session._mark_broken` is fed
#: `{'error': 'timeout', ...}` from its own receive-wait logic). Distinguished
#: from an auth failure or a hard connection refusal because "no BMC ever
#: replied" is this collection's ordinary `errors.TimeoutError_`, not
#: `ConnectionError_` (the TCP/UDP send itself did not fail) and not
#: `BmcBusyError` (that class is specific to the `.asp` HTTP surface's
#: observed worker-pool saturation; nothing analogous has been observed or
#: sourced for this BMC's IPMI listener).
_TIMEOUT_MARKER = "timeout"

#: Standard IPMI App-group netfn (0x06) both self-reset commands live under.
#: Cold Reset is command 0x02 (pyghmi's own `Command.reset_bmc()` wraps this
#: exact byte pair -- `command.py:409-413`); Warm Reset is command 0x03, for
#: which pyghmi ships no wrapper at all. See this module's docstring for the
#: full citation, including the live capture confirming Cold Reset's
#: acceptance shape on the target board.
_RESET_NETFN = 0x06
_WARM_RESET_COMMAND = 0x03

#: The exact message text pyghmi's `Command.set_identify()` standard-command
#: fallback raises, unconditionally, when `blink=True` -- see this module's
#: docstring: `oem/generic.py`'s `OEMHandler.set_identify()` never implements
#: blink, and the standard IPMI command has no blink concept at all. This
#: client never passes `blink=True`, so this string is matched only
#: defensively (a future pyghmi release changing the fallback's own defaults
#: could still surface it) -- see `set_identify()` below.
_BLINK_UNSUPPORTED_MESSAGE = "Blink not supported with generic IPMI"


def _classify_session_error(message: str) -> type[AuthenticationError | TimeoutError_ | ConnectionError_]:
    """Best-effort classification of a pyghmi session-establishment failure message.

    See this module's docstring and the marker constants above for exactly
    what evidence this is based on. Falls back to :class:`errors.ConnectionError_`
    for anything unrecognised -- the same conservative default `asp.py`'s
    `_connection_error_is_post_connect` uses, and for the same reason: it is
    the classification that does NOT imply "retrying immediately is fine".
    """
    lowered = (message or "").lower()
    if any(marker in lowered for marker in _AUTH_FAILURE_MARKERS):
        return AuthenticationError
    if _TIMEOUT_MARKER in lowered:
        return TimeoutError_
    return ConnectionError_


class IpmiClient:
    """A single IPMI session against one BMC, opened eagerly at construction time.

    Mirrors `AspClient` in spirit -- one client instance, one connection,
    every call classified into this collection's `errors.IkvmError` taxonomy
    -- but is a much thinner wrapper: session establishment, retries and
    packet framing are pyghmi's job, not this collection's, per this module's
    docstring.
    """

    def __init__(self, *, host: str, port: int = DEFAULT_IPMI_PORT, username: str, password: str) -> None:
        if not HAS_PYGHMI:  # pragma: no cover - exercised by the import sanity test
            raise ImportError(f"The `pyghmi` library is required for IPMI operations. Import error was: {PYGHMI_IMPORT_ERROR}")

        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._endpoint = f"{host}:{port}"
        self._command = self._connect()

    @property
    def endpoint(self) -> str:
        """A stable, non-secret identifier for this connection, safe to embed in any error or receipt."""
        return self._endpoint

    def _known_secrets(self) -> list[str]:
        return [s for s in (self._password,) if s]

    def _connect(self) -> Any:
        try:
            # No `onlogon` callback: this is deliberately the synchronous,
            # blocking constructor form -- see this module's docstring on why
            # that is the form whose failures are uniformly one exception
            # type. `privlevel`/`kg` are left at pyghmi's own defaults: this
            # collection has no sourced reason yet to override either.
            return ipmi_command.Command(bmc=self._host, userid=self._username, password=self._password, port=self._port)
        except ipmi_exceptions.PyghmiException as exc:
            error_cls = _classify_session_error(str(exc))
            raise error_cls(
                f"Could not establish an IPMI session with {self._endpoint}: {exc}",
                endpoint=self._endpoint,
                operation="ipmi_connect",
                secrets=self._known_secrets(),
            ) from exc

    # --- power -------------------------------------------------------------

    def get_power_state(self) -> dict[str, Any]:
        """Return pyghmi's ``{'powerstate': 'on'|'off'}`` verbatim."""
        try:
            return dict(self._command.get_power())
        except ipmi_exceptions.PyghmiException as exc:
            raise RemoteOperationError(
                f"IPMI get-power-state failed: {exc}",
                endpoint=self._endpoint,
                operation="get_power",
                secrets=self._known_secrets(),
            ) from exc

    def set_power_state(self, state: str, *, wait: int | bool = False) -> dict[str, Any]:
        """Issue a power command and (if ``wait`` is truthy) block for pyghmi's own confirmation loop.

        See this module's docstring for exactly which failure this
        distinguishes as ``indeterminate``.
        """
        try:
            return dict(self._command.set_power(state, wait=wait))
        except ipmi_exceptions.PyghmiException as exc:
            message = str(exc)
            if _SET_POWER_WAIT_TIMEOUT_MESSAGE in message:
                raise TimeoutError_(
                    f"IPMI accepted the '{state}' power request, but confirmation of it timed out "
                    f"before observing the target state ({message}). The BMC may already have applied "
                    "the transition -- re-probe (e.g. asmb8_info or asmb8_power with a read) rather than "
                    "blindly retrying this request.",
                    endpoint=self._endpoint,
                    operation="set_power",
                    indeterminate=True,
                    secrets=self._known_secrets(),
                ) from exc
            raise RemoteOperationError(
                f"IPMI set-power-state to '{state}' failed: {message}",
                endpoint=self._endpoint,
                operation="set_power",
                secrets=self._known_secrets(),
            ) from exc

    # --- boot device ---------------------------------------------------------

    def get_boot_device(self) -> dict[str, Any]:
        """Return pyghmi's boot-device-override dict verbatim.

        See this module's docstring: ``uefimode`` is absent from the returned
        dict on the bare-``'default'``-override branch. Callers must not
        assume it is always present.
        """
        try:
            return dict(self._command.get_bootdev())
        except ipmi_exceptions.PyghmiException as exc:
            raise RemoteOperationError(
                f"IPMI get-boot-device failed: {exc}",
                endpoint=self._endpoint,
                operation="get_bootdev",
                secrets=self._known_secrets(),
            ) from exc

    def set_boot_device(self, device: str, *, persist: bool = False, uefiboot: bool = False) -> dict[str, Any]:
        """Set a (by contract, never persistent) one-time boot device override.

        Handles both of pyghmi's failure shapes for this call -- see this
        module's docstring: an unrecognised device name is *returned* as
        ``{'error': ...}`` rather than raised, while an IPMI-level rejection of
        either underlying raw command raises. Both become
        :class:`errors.RemoteOperationError`.
        """
        try:
            response = dict(self._command.set_bootdev(device, persist=persist, uefiboot=uefiboot))
        except ipmi_exceptions.PyghmiException as exc:
            raise RemoteOperationError(
                f"IPMI set-boot-device to '{device}' failed: {exc}",
                endpoint=self._endpoint,
                operation="set_bootdev",
                secrets=self._known_secrets(),
            ) from exc
        if "error" in response:
            raise RemoteOperationError(
                f"IPMI set-boot-device to '{device}' was rejected: {response['error']}",
                endpoint=self._endpoint,
                operation="set_bootdev",
                secrets=self._known_secrets(),
            )
        return response

    # --- management-controller info -----------------------------------------

    def get_mc_info(self) -> str | None:
        """Return pyghmi's management-controller identifier.

        Deliberately typed ``str | None``, not a dict: see this module's
        docstring on why ``get_mci()`` is not shaped like
        ``get_power()``/``get_bootdev()``.
        """
        try:
            identifier = self._command.get_mci()
        except ipmi_exceptions.PyghmiException as exc:
            raise RemoteOperationError(
                f"IPMI get-management-controller-info failed: {exc}",
                endpoint=self._endpoint,
                operation="get_mci",
                secrets=self._known_secrets(),
            ) from exc
        if identifier is None:
            return None
        return str(identifier)

    # --- self-reset -----------------------------------------------------------

    def reset_bmc(self, mode: str) -> dict[str, Any]:
        """Issue a BMC self-reset. ``mode`` is ``'cold'`` or ``'warm'``.

        Drops every active BMC session -- IPMI, the ``.asp`` web session, and
        any iUSB/KVM media session in flight -- see `asmb8_reset`'s own
        DOCUMENTATION for what that means operationally. This method never
        waits for the BMC to come back: there is nothing pyghmi (or the raw
        IPMI command) gives us to poll for after a self-reset, the same
        "fire and forget" reasoning `set_power_state` already documents for
        chassis-level ``reset``/``boot``.

        ``mode='cold'`` calls pyghmi's own ``Command.reset_bmc()`` directly --
        the call this module's docstring cites as VERIFIED LIVE against the
        target board. ``mode='warm'`` has no pyghmi wrapper, so it is issued
        the identical way ``reset_bmc()`` issues Cold Reset internally: a bare
        ``raw_command(netfn=0x06, command=0x03, retry=False)``, checked for
        the same ``'error'`` key. Both of pyghmi's two failure shapes for this
        call are handled the same way :meth:`set_boot_device` already handles
        them for ``set_bootdev()`` -- a raised exception, or a returned dict
        carrying an ``'error'`` key -- both become
        :class:`errors.RemoteOperationError`.
        """
        try:
            if mode == "cold":
                self._command.reset_bmc()
                response: dict[str, Any] = {}
            else:
                response = dict(self._command.raw_command(netfn=_RESET_NETFN, command=_WARM_RESET_COMMAND, retry=False) or {})
        except ipmi_exceptions.PyghmiException as exc:
            raise RemoteOperationError(
                f"IPMI {mode} reset failed: {exc}",
                endpoint=self._endpoint,
                operation="reset_bmc",
                secrets=self._known_secrets(),
            ) from exc
        if "error" in response:
            raise RemoteOperationError(
                f"IPMI {mode} reset was rejected: {response['error']}",
                endpoint=self._endpoint,
                operation="reset_bmc",
                secrets=self._known_secrets(),
            )
        return {"mode": mode, **response}

    # --- chassis identify -----------------------------------------------------

    def set_identify(self, *, on: bool, duration: int | None = None) -> None:
        """Issue the standard IPMI Chassis Identify command (netfn 0x00, cmd 0x04).

        Always resolves to pyghmi's standard-command fallback on this board --
        see this module's docstring for exactly why (American Megatrends is
        not a key in pyghmi's OEM ``oemmap``). ``blink`` is deliberately not a
        parameter of this method at all: pyghmi's own generic fallback cannot
        honour it (see the docstring), so this client never offers a caller
        the option to ask for it.

        Returns ``None`` on success -- pyghmi's real ``set_identify()``
        returns nothing itself (see this module's docstring), and there is no
        standard IPMI command to read identify state back at all, so this is
        not a gap this wrapper could paper over even if it wanted to.

        ``duration=0`` combined with ``on=True`` is refused by the calling
        module before this method is ever reached (see ``asmb8_identify``'s
        ``validate_duration()``) -- not re-validated here, since this class's
        job is to wrap pyghmi faithfully, not to re-implement a policy its
        caller already enforces.
        """
        try:
            self._command.set_identify(on, duration, False)
        except ipmi_exceptions.UnsupportedFunctionality as exc:
            # Defensive only: as of pyghmi 1.6.19, Command.set_identify()
            # always catches this itself and falls through to the standard
            # command (see this module's docstring) -- it never reaches this
            # far in practice on this board. Handled explicitly anyway in
            # case a future pyghmi release changes that internal catch.
            raise UnsupportedCapabilityError(
                f"IPMI set-identify is not supported by this endpoint: {exc}",
                endpoint=self._endpoint,
                operation="set_identify",
                secrets=self._known_secrets(),
            ) from exc
        except ipmi_exceptions.PyghmiException as exc:
            message = str(exc)
            if _BLINK_UNSUPPORTED_MESSAGE in message:
                raise UnsupportedCapabilityError(
                    f"IPMI set-identify failed: {message}",
                    endpoint=self._endpoint,
                    operation="set_identify",
                    secrets=self._known_secrets(),
                ) from exc
            raise RemoteOperationError(
                f"IPMI set-identify failed: {message}",
                endpoint=self._endpoint,
                operation="set_identify",
                secrets=self._known_secrets(),
            ) from exc
