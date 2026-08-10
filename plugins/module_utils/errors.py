# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Stable error classification and secret redaction for ASMB8-iKVM operations.

Every failure surfaced by this collection carries one of the classes in
:class:`ErrorClass`. Callers -- including any bare-metal-install automation
built on top of this collection -- branch on these strings, so they are part
of the public contract and must not be renamed or repurposed.

The other job of this module is redaction. Failures against this BMC are
diagnosed from raw HTTP bodies, ``.asp`` RPC responses, JNLP argument lists,
and header dumps, all of which routinely contain session cookies or the admin
password. Nothing in this collection should ever construct a user-visible
message without passing it through :func:`redact`.

This collection's error taxonomy is carried over from the sibling
``james_crowley.intel_amt`` collection (same author, same conventions) with
one addition specific to this hardware: :data:`ErrorClass.BMC_BUSY`. See its
docstring below for why this board needs a class Intel AMT never did.
"""

from __future__ import annotations

import re


class ErrorClass:
    """Stable, machine-readable failure classes.

    Deliberately a plain class of string constants rather than an enum: these
    values cross the boundary into Ansible module results as JSON strings, and
    an enum only adds conversion noise at every return site.
    """

    CONNECTION = "connection"
    TLS_VALIDATION = "tls_validation"
    AUTHENTICATION = "authentication"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    INVALID_STATE = "invalid_state"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"
    REMOTE_OPERATION = "remote_operation"
    IDENTITY_MISMATCH = "identity_mismatch"
    #: This BMC's AMI web server is HTTP/1.0, does not keep connections alive,
    #: caps out at 20 concurrent web sessions, and runs a separate worker pool
    #: per listener (the plain web UI, the .asp RPC surface, and the iUSB/KVM
    #: media listener are not the same pool). Two failure modes fall out of
    #: that, and neither is CONNECTION or TIMEOUT:
    #:
    #: 1. Under concurrent load, the BMC will accept and complete a TCP
    #:    handshake and then simply never serve the HTTP request on it -- no
    #:    RST, no FIN, no response, ever. The connection genuinely succeeded
    #:    (so this is not CONNECTION, which means the TCP/DNS layer itself
    #:    failed), and it is not an ordinary protocol-level TIMEOUT either:
    #:    an ordinary timeout implies a request in flight that might land
    #:    late, whereas this is a worker pool that is full and never picks the
    #:    request up at all. We observed this directly against the target
    #:    board (ASUS ASMB8-iKVM) under concurrent access, which is exactly
    #:    why this collection's clients serialize every request (see
    #:    ``asp.py``) rather than relying on retries to paper over it.
    #: 2. The virtual-media/KVM channel allows exactly ONE active session, and
    #:    the BMC has no server-side timeout to reclaim an abandoned one. "A
    #:    prior session already holds the single media slot and nothing is
    #:    going to evict it" is a distinct, retryable-after-manual-
    #:    intervention condition, and it is this same class: the BMC is not
    #:    unreachable and the request did not time out, it is simply busy in a
    #:    way this collection can name.
    BMC_BUSY = "bmc_busy"

    ALL = (
        CONNECTION,
        TLS_VALIDATION,
        AUTHENTICATION,
        UNSUPPORTED_CAPABILITY,
        INVALID_STATE,
        TIMEOUT,
        PROTOCOL,
        REMOTE_OPERATION,
        IDENTITY_MISMATCH,
        BMC_BUSY,
    )


#: Maximum length of any diagnostic excerpt embedded in an error. The .asp RPC
#: surface and JNLP documents can be large, and a full body never belongs in a
#: task result.
MAX_DIAGNOSTIC_BYTES = 2048

_REDACTED = "[REDACTED]"

#: AMI-specific token/cookie names observed on this BMC's web management
#: plane. None of these are generic English words that a "password|token|..."
#: pattern would already catch by itself:
#:
#: * ``SESSION_COOKIE`` -- the key name inside the Python-dict-shaped body
#:   ``/rpc/WEBSES/create.asp`` returns, e.g. ``'SESSION_COOKIE':'<value>'``.
#: * ``SessionCookie`` -- the same value, re-sent as a ``Cookie`` header:
#:   ``Cookie: SessionCookie=<value>``.
#: * ``STOKEN`` -- the key name ``/rpc/getsessiontoken.asp`` returns.
#: * ``kvmtoken`` / ``webcookie`` -- JNLP ``<argument>`` values that
#:   authorize the KVM/media viewer session (see ``asp.py``'s JNLP parser).
#: * ``WEBVAR_PASSWORD`` -- the login form field carrying the admin password.
#:   A generic ``\bpassword\b`` pattern will NOT match this: underscore is a
#:   word character, so there is no word boundary between ``WEBVAR_`` and
#:   ``PASSWORD`` and the substring is never isolated as its own word.
#: * ``CSRFTOKEN`` -- the anti-CSRF token ``asp.py``'s ``login()`` harvests
#:   from ``/rpc/WEBSES/create.asp``'s own response body (see that file's
#:   ``_CSRFTOKEN_RE``) and later replays as a request header on ``POST``
#:   reads (``post_webvar()``). Not credential material in the sense of
#:   granting access on its own, but it is BMC-issued session-bound material
#:   with no business appearing in a message, same as ``STOKEN``/``kvmtoken``.
_AMI_SECRET_NAMES = (
    "SESSION_COOKIE",
    "SessionCookie",
    "STOKEN",
    "kvmtoken",
    "webcookie",
    "WEBVAR_PASSWORD",
    "CSRFTOKEN",
)
_AMI_SECRET_NAME_ALTERNATION = "|".join(re.escape(name) for name in _AMI_SECRET_NAMES)

# Patterns are applied in order. Each must keep the surrounding structure intact
# so the redacted text is still useful for diagnosis -- we want to preserve
# "which field was present", while destroying its value.
_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Credential-bearing headers, quoted form first (e.g. inside a repr'd dict:
    # {'Authorization': 'Digest ...'}). Consuming only to the matching quote
    # keeps the surrounding structure readable.
    (
        re.compile(r"(?i)\b(authorization|proxy-authorization|set-cookie|cookie)(\W{0,3}\s*[:=]\s*)(['\"])(?:(?!\3).)*\3"),
        r"\1\2\3" + _REDACTED + r"\3",
    ),
    # Unquoted header form (e.g. a raw HTTP header line). The entire value is
    # taken to end of line: a Cookie/Authorization value has no non-secret
    # prefix worth preserving, and stopping early is how partial credentials
    # leak.
    (
        re.compile(r"(?i)^(\s*)(authorization|proxy-authorization|set-cookie|cookie)(\s*:\s*)[^\r\n]+", re.MULTILINE),
        r"\1\2\3" + _REDACTED,
    ),
    (
        re.compile(r"(?i)\b(authorization|proxy-authorization|set-cookie|cookie)(\s*:\s*)[^\r\n]+"),
        r"\1\2" + _REDACTED,
    ),
    # AMI-specific token/cookie key names, in either the quoted Python-dict
    # shape this BMC's .asp endpoints return (`'SESSION_COOKIE':'value'`) or a
    # bare key=value/key:value shape (a Cookie header body, a JNLP query
    # string, a log line). The optional leading/trailing quote group handles
    # both without two separate patterns.
    (
        re.compile(rf"(?i)(['\"]?)({_AMI_SECRET_NAME_ALTERNATION})\1(\s*[:=]\s*)(['\"]?)[^\s,;&}}'\"<]*\4"),
        r"\1\2\1\3\4" + _REDACTED + r"\4",
    ),
    # Generic secret-bearing keys in JSON, kwargs, or query strings. The optional
    # quote before the separator is what makes the quoted-key form work, e.g.
    # repr() output like {'password': 'x'} -- without it the pattern only matched
    # bare-key forms such as password=x.
    (
        re.compile(r"(?i)\b(password|passwd|pwd|secret|passphrase|api[_-]?key|token|apikey)(['\"]?\s*[:=]\s*)(['\"]?)[^\s,;&}'\"]*\3"),
        r"\1\2\3" + _REDACTED + r"\3",
    ),
    # XML elements whose names suggest secrets. AMI's own .asp/JNLP surface is
    # not XML-shaped, but this is cheap defence-in-depth against anything else
    # this collection ever has to parse (e.g. Redfish, if this board is ever
    # found to expose it -- see README.md's open question on that).
    (
        re.compile(r"(?is)<([a-z0-9_:.-]*(?:password|secret|passphrase|token|key)[a-z0-9_:.-]*)>.*?</\1>"),
        r"<\1>" + _REDACTED + r"</\1>",
    ),
    # userinfo embedded in a URL: scheme://user:pass@host
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@"),
        r"\1\2:" + _REDACTED + "@",
    ),
)


def redact(text: object, extra_secrets: object = None) -> str:
    """Return ``text`` with credential material removed and length bounded.

    Args:
        text: Any object; coerced with :func:`str`. ``None`` becomes ``""``.
        extra_secrets: Optional literal value, or iterable of values, to remove
            by exact substring match. Pass the actual password/session cookie/
            token here so that a value echoed verbatim by the BMC in a shape no
            pattern anticipates is still caught, rather than relying only on
            the patterns above.

    The literal replacement happens *first*, so a secret that happens to look
    like structure cannot survive by being reshaped by a later pattern.
    """
    if text is None:
        return ""

    result = text if isinstance(text, str) else str(text)

    if extra_secrets:
        secrets: list[str] = []
        if isinstance(extra_secrets, (str, bytes)):
            secrets = [extra_secrets if isinstance(extra_secrets, str) else extra_secrets.decode("utf-8", "replace")]
        else:
            try:
                for item in extra_secrets:
                    if item is None:
                        continue
                    secrets.append(item if isinstance(item, str) else str(item))
            except TypeError:
                secrets = [str(extra_secrets)]

        # Longest first: replacing a short secret that is a substring of a longer
        # one would otherwise leave fragments of the longer secret behind.
        for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
            result = result.replace(secret, _REDACTED)

    for pattern, replacement in _REDACTION_PATTERNS:
        result = pattern.sub(replacement, result)

    if len(result) > MAX_DIAGNOSTIC_BYTES:
        omitted = len(result) - MAX_DIAGNOSTIC_BYTES
        result = f"{result[:MAX_DIAGNOSTIC_BYTES]}... [truncated, {omitted} more characters]"

    return result


class IkvmError(Exception):
    """Base failure carrying everything a module needs for ``fail_json``.

    Modules should catch this, then hand :meth:`to_result` straight to
    ``fail_json`` rather than reformatting, so classification and redaction stay
    consistent across every module.
    """

    #: Subclasses override this. The base class is intentionally the vaguest
    #: class rather than something more specific that could be wrong.
    error_class = ErrorClass.PROTOCOL

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        operation: str | None = None,
        diagnostic: object = None,
        secrets: object = None,
        indeterminate: bool = False,
        return_value: int | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.operation = operation
        self.indeterminate = indeterminate
        self.return_value = return_value
        self.message = redact(message, secrets)
        self.diagnostic = redact(diagnostic, secrets) if diagnostic is not None else None
        super().__init__(self.message)

    def to_result(self) -> dict[str, object]:
        """Render as a dict suitable for ``AnsibleModule.fail_json(**result)``."""
        result: dict[str, object] = {
            "msg": self.message,
            "error_class": self.error_class,
        }
        if self.endpoint is not None:
            result["endpoint"] = self.endpoint
        if self.operation is not None:
            result["operation"] = self.operation
        if self.diagnostic:
            result["diagnostic"] = self.diagnostic
        if self.return_value is not None:
            result["return_value"] = self.return_value
        if self.indeterminate:
            # Signals to the caller that the mutation may have taken effect and
            # must be re-probed rather than retried.
            result["indeterminate"] = True
        return result

    def __repr__(self) -> str:
        return f"{type(self).__name__}(error_class={self.error_class!r}, message={self.message!r})"


class ConnectionError_(IkvmError):
    """TCP or DNS level failure: refused, unreachable, unresolvable, port closed.

    Named with a trailing underscore to avoid shadowing the builtin
    ``ConnectionError``, which callers may still want to catch separately.
    """

    error_class = ErrorClass.CONNECTION


class TlsValidationError(IkvmError):
    """Certificate chain or hostname verification failed, fingerprint mismatched,
    or plaintext transport was requested without explicit acknowledgement."""

    error_class = ErrorClass.TLS_VALIDATION


class AuthenticationError(IkvmError):
    """Credentials rejected.

    Covers both an HTTP-level auth failure and this BMC's own failure shape:
    HTTP 200 from ``/rpc/WEBSES/create.asp`` with a ``SESSION_COOKIE`` value
    that reads ``Failure_Login_*`` -- see ``asp.py``'s login logic, which is
    the only thing standing between a wrong password and a client that
    confidently proceeds with a session that was never granted.
    """

    error_class = ErrorClass.AUTHENTICATION


class UnsupportedCapabilityError(IkvmError):
    """Firmware does not implement the requested feature, or a required
    instance (such as a boot source) is absent or ambiguous."""

    error_class = ErrorClass.UNSUPPORTED_CAPABILITY


class InvalidStateError(IkvmError):
    """The operation is not legal from the endpoint's current state."""

    error_class = ErrorClass.INVALID_STATE


class TimeoutError_(IkvmError):
    """Operation timed out before a TCP connection to the BMC was even made.

    ``indeterminate`` distinguishes the two cases that matter operationally: a
    timeout *before* the request was transmitted is a safe, retryable failure,
    while a timeout *after* transmission means the mutation may have been
    applied. Only the caller can decide what to do about the latter, and it
    must re-probe rather than retry.

    This class is deliberately narrower here than in some sibling collections:
    "connected, then no response ever arrived" is this BMC's ``bmc_busy``
    condition (see :data:`ErrorClass.BMC_BUSY`), not an ordinary timeout --
    the two point a caller at different remedies (retry later vs. this BMC's
    worker pool is saturated, back off harder).
    """

    error_class = ErrorClass.TIMEOUT


class ProtocolError(IkvmError):
    """Malformed or unparseable response: an ``.asp`` body or JNLP document
    that does not match the shape this collection knows how to read."""

    error_class = ErrorClass.PROTOCOL


class RemoteOperationError(IkvmError):
    """The request was well-formed and accepted, but the BMC reported the
    operation itself failed (a non-zero/error result from the RPC surface)."""

    error_class = ErrorClass.REMOTE_OPERATION


class IdentityMismatchError(IkvmError):
    """Observed endpoint evidence disagrees with the reviewed inventory binding.

    Raised before any mutation. Guards against power-cycling or attaching
    media to the wrong machine when inventory and reality have drifted apart.
    """

    error_class = ErrorClass.IDENTITY_MISMATCH


class BmcBusyError(IkvmError):
    """The BMC accepted the TCP connection but never served the request, or
    the single-slot media session is already held by someone else.

    See :data:`ErrorClass.BMC_BUSY` for the hardware evidence behind this
    class. Both of its causes are retryable, but neither should be retried
    tightly or concurrently: retrying while the BMC is already saturated is
    exactly the load pattern that produces this failure in the first place,
    which is why ``asp.py`` serializes every request instead of leaning on
    concurrency-hiding retries.
    """

    error_class = ErrorClass.BMC_BUSY


#: Mapping from class string back to exception type, for reconstructing an error
#: from a serialized receipt.
ERROR_CLASS_TO_EXCEPTION: dict[str, type[IkvmError]] = {
    ErrorClass.CONNECTION: ConnectionError_,
    ErrorClass.TLS_VALIDATION: TlsValidationError,
    ErrorClass.AUTHENTICATION: AuthenticationError,
    ErrorClass.UNSUPPORTED_CAPABILITY: UnsupportedCapabilityError,
    ErrorClass.INVALID_STATE: InvalidStateError,
    ErrorClass.TIMEOUT: TimeoutError_,
    ErrorClass.PROTOCOL: ProtocolError,
    ErrorClass.REMOTE_OPERATION: RemoteOperationError,
    ErrorClass.IDENTITY_MISMATCH: IdentityMismatchError,
    ErrorClass.BMC_BUSY: BmcBusyError,
}
