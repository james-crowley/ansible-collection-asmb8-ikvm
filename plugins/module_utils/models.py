# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Typed result objects and the operation receipt for ASMB8-iKVM modules.

Mirrors the sibling ``james_crowley.intel_amt`` collection's ``models.py`` in
shape and in its central design rule: **the receipt never carries
credentials.** None of these dataclasses has a field shaped like a secret, and
:meth:`OperationReceipt.to_dict` also runs every string value through
``errors.redact`` as a defence-in-depth backstop, in case a caller ever stuffs
something unexpected into ``previous``/``desired``/``observed``.

:class:`JnlpSession` is the one dataclass here that is an exception to "no
secret-shaped field" -- see its docstring for why that is unavoidable and what
the rule is instead.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import redact

#: ``community.general.ipmi_power``'s ``state`` choices, exactly as documented
#: (https://docs.ansible.com/ansible/latest/collections/community/general/ipmi_power_module.html).
#: Sourced from that module's own documentation rather than invented, per this
#: collection's policy of never guessing at a protocol constant it has not
#: seen. ``asmb8_power`` (not yet implemented) wraps this module rather than
#: reimplementing IPMI power control -- see README.md "Why this exists".
POWER_STATES = ("on", "off", "shutdown", "reset", "boot")

#: ``community.general.ipmi_boot``'s ``bootdev`` choices, exactly as documented
#: (https://docs.ansible.com/ansible/latest/collections/community/general/ipmi_boot_module.html).
#: Same sourcing policy as :data:`POWER_STATES`. ``default`` here means "clear
#: any standing IPMI boot-device override", not "the board's power-on default
#: device" -- that is how the underlying module documents it, and repeating an
#: ambiguous name is safer than inventing a clearer one that no longer matches
#: what the wrapped module actually does.
BOOT_DEVICES = ("network", "floppy", "hd", "safe", "optical", "setup", "default")


def optional_str(value: Any) -> str | None:
    """Coerce a value to a non-empty ``str``, or ``None``.

    Ported from the sibling collection's ``models.py`` helper of the same
    name: BMC responses (``.asp`` bodies, JNLP ``<argument>`` text) arrive as
    strings or are trivially stringified, and "present but blank" should read
    as "not reported" rather than as an empty string a caller has to check for
    separately.
    """
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def optional_int(value: Any) -> int | None:
    """Coerce a value to ``int``, or ``None`` if it is absent/not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def optional_bool_flag(value: Any) -> bool | None:
    """Interpret a JNLP-style ``0``/``1`` flag string as a boolean, or ``None`` if absent.

    JNLP arguments such as ``-singleportenabled`` and ``-kvmsecure`` arrive as
    the literal text ``"0"`` or ``"1"``, never a Python bool -- this is the
    boundary that turns that wire text into a real boolean while keeping
    "the BMC did not report this argument at all" distinct from "reported as
    off", for the same reason the sibling collection's ``optional_bool``
    keeps that distinction for WS-Man properties.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text != "0"


@dataclass(frozen=True, slots=True)
class JnlpSession:
    """Parsed ``<argument>`` pairs from a ``/Java/jviewer.jnlp`` fetch.

    Fetching this document is not a read -- see ``asp.py``'s
    ``allocate_media_session``, which is named to make that side effect
    explicit: on this BMC, fetching the JNLP is what allocates the KVM/media
    session server-side (verified against rd450x-console, a reference client
    for a related board and protocol). A bare ``getsessiontoken.asp`` call
    does not substitute for it -- observed directly against the target
    hardware, that endpoint returns an empty ``STOKEN``.

    **Port mode is structural, not a single field.** Whether ``cd_port`` (etc.)
    is present at all tells you which of two mutually exclusive wiring modes
    the board is in:

    * Absent, with ``single_port_enabled=True``: virtual media multiplexes
      over the same port as the web/KVM session (observed as the target
      board's shipped configuration).
    * Present: each virtual device gets its own dedicated TCP port
      (``single_port_enabled`` is then ``False``). The board owner was, as of
      this writing, in the process of switching to this mode -- both shapes
      are real, current configurations of the *same* board, not a
      hypothetical to plan for later.

    ``port_mode`` is the derived, unambiguous answer a caller should branch
    on; the individual port fields are kept alongside it because "which port"
    is still needed once the mode is known.

    **This dataclass carries two fields that ARE secrets** (``kvm_token``,
    ``web_cookie``), unlike every other dataclass in this module. That is
    unavoidable: the KVM/media protocol needs the actual token value to open
    a session, so there is no way to model "the JNLP allocated a session"
    without holding the credential that session grants. The rule this
    collection substitutes for "never a secret-shaped field" is instead:
    **never pass a `JnlpSession` (or any of its fields) into `OperationReceipt`
    or any Ansible return value.** `redact()`'s generic patterns cannot save
    you here -- they redact a *labelled* value (``kvmtoken=...``), and a bare
    token string with no surrounding key is indistinguishable from any other
    string once it is out of this object. Treat an instance of this class the
    way you would treat the password itself: something plumbed between
    ``asp.py`` and the module that requested it, and mark it ``no_log`` at
    every boundary that isn't Python.

    Observed directly against the target hardware: ``web_cookie`` comes back
    byte-identical to the session cookie ``create.asp`` issued earlier in the
    same login. It is not an independent secret the JNLP mints -- it is the
    same cookie, handed back for the KVM/media client's convenience. Callers
    should not treat ``web_cookie`` as something that needs its own storage or
    rotation apart from the session cookie it duplicates.
    """

    kvm_port: int | None
    kvm_token: str | None
    web_cookie: str | None
    single_port_enabled: bool | None
    cd_port: int | None
    fd_port: int | None
    hd_port: int | None
    cd_state: str | None
    cd_num: int | None
    #: Whether THIS session is TLS-secured. Follows the scheme the JNLP itself
    #: was fetched over (observed directly: the same board returned
    #: ``-kvmsecure 0 -vmsecure 0`` when the JNLP was fetched over plain HTTP,
    #: and ``-kvmsecure 1 -vmsecure 1`` minutes later over HTTPS, with nothing
    #: else changed). It is a property of *this* session, not a standing board
    #: setting -- do not cache it across sessions fetched over a different
    #: scheme.
    kvm_secure: bool | None
    vm_secure: bool | None
    port_mode: str
    #: Any other ``-argument value`` pair this parser did not name explicitly
    #: above, keyed by argument name with the leading dash stripped. Kept so a
    #: JNLP field this collection has not yet modelled is still visible for
    #: diagnosis rather than silently dropped -- the same reasoning as the
    #: sibling collection's "unknown(<raw>)" enum handling, applied to a field
    #: set instead of a value table. May itself contain secret-shaped values
    #: (the JNLP is one dictionary of arguments; this collection does not
    #: enumerate every one a firmware revision might add) -- treat this field
    #: with the same no-receipt, no-log discipline as ``kvm_token``.
    extra: dict[str, str] = field(default_factory=dict)

    #: The known-secret argument names, excluded from `extra` so a firmware
    #: revision's argument order or spelling can never cause a token to leak
    #: into the "everything else" bucket where nothing is watching for it.
    _NAMED_ARGUMENTS = frozenset(
        {
            "kvmport",
            "kvmtoken",
            "webcookie",
            "singleportenabled",
            "cdport",
            "fdport",
            "hdport",
            "cdstate",
            "cdnum",
            "kvmsecure",
            "vmsecure",
        }
    )

    @classmethod
    def from_arguments(cls, arguments: dict[str, str]) -> JnlpSession:
        """Build from a flat ``{argument_name_without_dash: value}`` mapping.

        ``arguments`` is what ``asp.py``'s JNLP text parser produces: JNLP
        ``<argument>`` elements alternate name/value
        (``-kvmport``, ``5900``, ``-kvmtoken``, ``<value>``, ...), and the
        parser's job ends at turning that alternating list into this mapping
        with the leading ``-`` stripped and the name lower-cased. This
        classmethod's job is purely the semantic step: deciding port mode and
        typing each field.
        """
        cd_port = optional_int(arguments.get("cdport"))
        fd_port = optional_int(arguments.get("fdport"))
        hd_port = optional_int(arguments.get("hdport"))
        single_port_enabled = optional_bool_flag(arguments.get("singleportenabled"))

        # Presence of ANY dedicated device port is what decides the mode, not
        # single_port_enabled alone -- the two are expected to agree, but if a
        # firmware revision ever reports them inconsistently, believing the
        # actual port fields over a flag that describes them is the safer
        # failure: it is what a caller is about to *use*.
        has_dedicated_ports = any(p is not None for p in (cd_port, fd_port, hd_port))
        if has_dedicated_ports:
            port_mode = "dedicated_ports"
        elif single_port_enabled:
            port_mode = "single_port"
        else:
            # Neither a dedicated port nor an explicit single-port flag was
            # seen. Rather than guess, this is surfaced as its own state: an
            # unrecognised board configuration is a finding, not a default.
            port_mode = "unknown"

        extra = {name: value for name, value in arguments.items() if name not in cls._NAMED_ARGUMENTS}

        return cls(
            kvm_port=optional_int(arguments.get("kvmport")),
            kvm_token=optional_str(arguments.get("kvmtoken")),
            web_cookie=optional_str(arguments.get("webcookie")),
            single_port_enabled=single_port_enabled,
            cd_port=cd_port,
            fd_port=fd_port,
            hd_port=hd_port,
            cd_state=optional_str(arguments.get("cdstate")),
            cd_num=optional_int(arguments.get("cdnum")),
            kvm_secure=optional_bool_flag(arguments.get("kvmsecure")),
            vm_secure=optional_bool_flag(arguments.get("vmsecure")),
            port_mode=port_mode,
            extra=extra,
        )


#: The receipt schema identifier. Part of the public contract: callers key
#: off this string to know how to interpret the rest of the document.
RECEIPT_SCHEMA = "asmb8-ikvm-operation/v1"


@dataclass(frozen=True, slots=True)
class OperationReceipt:
    """The ``asmb8-ikvm-operation/v1`` receipt returned by every mutating module.

    ``previous``/``desired``/``observed`` accept any typed dataclass, a plain
    dict, or ``None`` -- :meth:`to_dict` normalizes whichever was given into
    plain JSON-safe structures.

    Never pass a :class:`JnlpSession` (or a raw token/cookie string) as
    ``previous``/``desired``/``observed``/``extra``. See that class's
    docstring for why ``redact()``'s backstop cannot be relied on to catch an
    unlabelled secret string, which is exactly the shape those fields are.
    """

    action: str
    endpoint: str
    changed: bool
    previous: Any = None
    desired: Any = None
    observed: Any = None
    error_class: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Render to exactly the ``asmb8-ikvm-operation/v1`` schema.

        Every string value is passed through :func:`errors.redact` as a
        last-resort backstop -- the structural guarantee is that none of
        these dataclasses have a credential-shaped field, but this catches
        the case where a caller passes through data it should not have.
        """
        document: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "action": self.action,
            "endpoint": self.endpoint,
            "changed": self.changed,
            "previous": _to_serializable(self.previous),
            "desired": _to_serializable(self.desired),
            "observed": _to_serializable(self.observed),
            "error_class": self.error_class,
        }
        if self.extra:
            document.update({k: _to_serializable(v) for k, v in self.extra.items()})
        return _redact_strings(document)


def _to_serializable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return value


def _redact_strings(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redact_strings(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_strings(item) for item in value]
    return value
