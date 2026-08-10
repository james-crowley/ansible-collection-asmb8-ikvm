# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""A client for the ASMB8-iKVM BMC's legacy ``.asp`` RPC surface.

This is AMI MegaRAC firmware's older web-management API: plain HTTP(S) POSTs
and GETs against ``*.asp`` endpoints and a JNLP document, not a modern REST or
Redfish API. Nothing here is from a vendor specification -- AMI has not
published one for this surface -- so every protocol claim below cites where it
came from, per this collection's policy (README.md) of never asserting a
protocol fact it cannot source.

Sources cited throughout this file:

* "PR #40" = ``nesvet/nojava-ipmi-kvm`` pull request #40, which documents this
  BMC's HTTP-200-on-bad-credentials failure shape.
* "rd450x-console" = ``BadCoder1337/rd450x-console``, a third-party Go client
  for a related board and protocol (MIT-licensed, see licenses/MIT.txt),
  which documents that fetching the JNLP is what allocates the KVM/media
  session.
* "observed against the target board" / "live hardware" = a direct capture
  against this collection's target ASUS ASMB8-iKVM, reported by the
  maintainer who owns access to that hardware. This module's author (an
  automated contributor) made zero network requests of its own to that or any
  other BMC while writing this file -- see CONTRIBUTING.md and SECURITY.md for
  why that boundary is enforced deliberately.

Everything in this client is written to be called by exactly one caller at a
time and to serialize its own requests (see :class:`AspClient`'s docstring):
this BMC's web server has a small, per-listener worker pool, and concurrent
load against it has been observed, against the target hardware, to exhaust
that pool -- the BMC completes the TCP handshake and then never serves the
request, locking out even the BMC's own web UI for several minutes. Retrying
or parallelizing around that condition is exactly the load pattern that causes
it.
"""

from __future__ import annotations

import re
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.ssl_ import create_urllib3_context

    HAS_REQUESTS = True
    REQUESTS_IMPORT_ERROR: str | None = None
except ImportError as _import_error:  # pragma: no cover - exercised by the import sanity test
    # Guarded so that importing this module on a controller without `requests`
    # yields a clear "requests is required" failure from the calling module via
    # missing_required_lib(), rather than an ImportError traceback.
    requests = None  # type: ignore[assignment]
    HTTPAdapter = object  # type: ignore[assignment,misc]
    create_urllib3_context = None  # type: ignore[assignment]
    HAS_REQUESTS = False
    REQUESTS_IMPORT_ERROR = str(_import_error)

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import (
    AuthenticationError,
    BmcBusyError,
    ConnectionError_,
    ProtocolError,
    TimeoutError_,
    TlsValidationError,
)
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import JnlpSession

#: This board's web management port, per the connection doc fragment
#: (plugins/doc_fragments/connection.py) -- observed default on the target
#: hardware for both the plaintext and TLS listeners; unlike Intel AMT there is
#: no separate well-known plaintext port to fall back to here.
DEFAULT_PORT = 443

# --- TLS: this BMC will not talk to a default Python TLS client -----------
#
# Observed directly against the target hardware: the BMC's TLS listener offers
# TLS 1.2 only (1.0, 1.1 and 1.3 are all refused at the handshake) and exactly
# one ciphersuite, AES256-GCM-SHA384. That ciphersuite is static-RSA key
# exchange -- there is no ECDHE/DHE component, so the connection has no
# forward secrecy. Modern OpenSSL/Python builds deliberately exclude
# non-forward-secret ciphersuites from their default cipher list, so a plain
# `requests.get(...)` against this BMC fails with
# `ssl.SSLError: [SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] ssl/tls alert handshake failure`
# every time, regardless of any of this collection's own trust-policy options.
# `curl` is more permissive by default and will appear to work against the
# same endpoint -- that is not evidence this collection has a bug.
#
# This is a single named constant, not inlined into the adapter below, on
# purpose: it is the kind of thing a future "cleanup" would delete as dead
# weight without the comment explaining it is load-bearing.
BMC_CIPHERS = "AES256-GCM-SHA384:AES128-GCM-SHA256:AES256-SHA256:AES128-SHA256:AES256-SHA:AES128-SHA"

#: A SHA-256 digest is exactly 32 bytes, i.e. 64 hex characters.
_FINGERPRINT_HEX_LENGTH = 64
_HEX_RE = re.compile(r"^[0-9a-f]+$")


def normalize_fingerprint(raw: str) -> str:
    """Normalize a caller-supplied SHA-256 fingerprint to bare lowercase hex.

    Ported from the sibling ``james_crowley.intel_amt`` collection's
    ``tls.py``: accepts colon-separated or bare hex, any case, and an optional
    ``sha256:`` prefix, because that is the range of formats a human is likely
    to copy out of a browser or ``openssl x509 -fingerprint``.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise TlsValidationError("tls_fingerprint must be a non-empty string containing a SHA-256 hex digest")

    candidate = raw.strip().lower()
    if candidate.startswith("sha256:"):
        candidate = candidate[len("sha256:") :]
    candidate = candidate.replace(":", "").replace(" ", "")

    if len(candidate) != _FINGERPRINT_HEX_LENGTH or not _HEX_RE.match(candidate):
        raise TlsValidationError(
            "tls_fingerprint must normalize to exactly 32 bytes of hex (a SHA-256 digest); "
            f"got {len(candidate)} hex character(s) after stripping colons/whitespace and any 'sha256:' prefix"
        )
    return candidate


def enforce_transport_policy(*, use_tls: bool, allow_insecure_transport: bool) -> None:
    """Refuse plaintext HTTP to the BMC unless the caller explicitly opted in.

    Ported from the sibling collection's ``tls.py``. ``use_tls=False`` is a
    legitimate configuration for this board (see README.md's note that it is
    normally run in a plaintext, single-port mode), but it must never be the
    accidental result of a default or a typo: the session cookie and the
    KVM/media token both cross the network in plaintext when this is off.
    """
    if use_tls or allow_insecure_transport:
        return
    raise TlsValidationError(
        "use_tls=false requires allow_insecure_transport=true. Without TLS, the BMC session "
        "cookie and KVM/media token cross the network in a form an on-path attacker can "
        "recover. Set allow_insecure_transport=true only if this endpoint is reachable solely "
        "over an isolated management VLAN and cannot be upgraded to TLS."
    )


@dataclass(frozen=True, slots=True)
class TlsTrustPolicy:
    """A resolved, validated trust decision for one connection to this BMC.

    Construct via :meth:`create`, not the constructor directly, so the
    mutual-exclusion check and fingerprint normalisation always run. Mirrors
    the sibling collection's ``tls.TlsTrustPolicy`` in shape; the reason this
    collection needs its own copy rather than importing that one is
    :data:`BMC_CIPHERS` below -- the trust *decision* (pin vs. chain vs.
    insecure) is the same shape, but enforcing it requires a custom
    ``ssl.SSLContext`` this board's cipher restriction makes mandatory, not
    optional the way it is for Intel AMT.
    """

    validate_certs: bool = True
    ca_path: str | None = None
    fingerprint: str | None = None  # normalized lowercase hex, no separators

    @classmethod
    def create(
        cls,
        *,
        validate_certs: bool = True,
        ca_path: str | None = None,
        tls_fingerprint: str | None = None,
    ) -> TlsTrustPolicy:
        if ca_path and tls_fingerprint:
            raise TlsValidationError(
                "ca_path and tls_fingerprint select mutually exclusive TLS trust modes; set only one. "
                "tls_fingerprint is the recommended mode for this board: its factory certificate is "
                "self-signed and, as of this writing, already past its own expiry date, so chain "
                "validation via ca_path cannot succeed against it."
            )
        fingerprint = normalize_fingerprint(tls_fingerprint) if tls_fingerprint else None
        return cls(validate_certs=validate_certs, ca_path=ca_path, fingerprint=fingerprint)

    @property
    def pinned(self) -> bool:
        return self.fingerprint is not None

    def build_ssl_context(self) -> ssl.SSLContext:
        """Build the ``ssl.SSLContext`` this policy requires, with this board's mandatory cipher restriction applied.

        The cipher/protocol restriction below is NOT part of the trust
        decision and is applied identically regardless of ``validate_certs``/
        ``ca_path``/``fingerprint`` -- it is a compatibility requirement (this
        BMC simply will not complete a handshake without it), not a security
        posture choice. What DOES vary by trust mode is ``verify_mode`` and
        ``check_hostname``:

        * Fingerprint-pinned: verification is disabled here because the pin
          itself is the trust decision, enforced separately by urllib3's
          ``assert_fingerprint`` pool option during the handshake (see
          :class:`AmiLegacyTlsAdapter`). This is not "silent insecure trust":
          a connection whose leaf certificate does not match the pinned
          fingerprint is still aborted -- by a different mechanism than chain
          validation, not by no mechanism at all.
        * ``validate_certs=False`` and not pinned: verification is disabled
          because the caller explicitly asked for that, honestly reflected
          rather than silently kept alive underneath a flag that claims
          otherwise.
        * Otherwise: ordinary chain and hostname verification, using
          ``ca_path`` if given or the platform's default trust store
          otherwise. Kept as a real, working mode for the case where this
          board's factory certificate has been replaced with one issued by an
          actual CA -- not the expected posture for the target hardware today
          (see the connection doc fragment's note on the factory certificate's
          validity window), but not removed either, since "the BMC has since
          been fixed" is exactly the situation this mode exists for.
        """
        context = create_urllib3_context(ciphers=BMC_CIPHERS)
        context.minimum_version = ssl.TLSVersion.TLSv1_2

        if self.pinned or not self.validate_certs:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context

        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        if self.ca_path:
            context.load_verify_locations(self.ca_path)
        return context

    def build_adapter(self) -> AmiLegacyTlsAdapter:
        return AmiLegacyTlsAdapter(self)


class AmiLegacyTlsAdapter(HTTPAdapter):
    """A ``requests`` HTTPAdapter for this BMC's TLS 1.2/static-RSA-only listener.

    Beyond restoring the required cipher (see :data:`BMC_CIPHERS`), this
    enforces fingerprint pinning the same way the sibling collection's
    ``tls.FingerprintPinningAdapter`` does: via urllib3's
    ``assert_fingerprint`` pool option, checked *during* the handshake so a
    mismatch aborts the connection before any request data is sent. Comparing
    a peer certificate after the fact would authenticate nothing, because the
    attacker's certificate would already have completed the handshake by the
    time such a comparison ran.
    """

    def __init__(self, policy: TlsTrustPolicy, *args: Any, **kwargs: Any) -> None:
        self._policy = policy
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["ssl_context"] = self._policy.build_ssl_context()
        if self._policy.pinned:
            kwargs["assert_fingerprint"] = self._policy.fingerprint
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy: str, **kwargs: Any) -> Any:
        kwargs["ssl_context"] = self._policy.build_ssl_context()
        if self._policy.pinned:
            kwargs["assert_fingerprint"] = self._policy.fingerprint
        return super().proxy_manager_for(proxy, **kwargs)

    def cert_verify(self, conn: Any, url: str, verify: Any, cert: Any) -> None:
        """Keep Requests from replacing this adapter's resolved trust mode.

        ``requests.Session.request()`` defaults ``verify`` to ``True`` and
        :class:`requests.adapters.HTTPAdapter` applies that value after the
        pool's custom ``ssl_context`` has been created.  Its default
        ``cert_verify()`` therefore writes ``CERT_REQUIRED`` onto the
        connection even when :class:`TlsTrustPolicy` deliberately selected
        ``CERT_NONE`` so urllib3 could enforce a reviewed leaf fingerprint,
        or when the caller explicitly selected ``validate_certs=False``.

        Resolve that late Requests setting from the policy as well.  Ordinary
        CA validation continues through the parent implementation unchanged;
        a configured ``ca_path`` is passed as the effective CA bundle.  The
        pin-only and explicit-no-validation modes pass ``False`` so the
        connection agrees with their SSL context.  Fingerprint enforcement
        itself remains urllib3's ``assert_fingerprint`` handshake check.
        """
        if self._policy.pinned or not self._policy.validate_certs:
            effective_verify: Any = False
        elif self._policy.ca_path:
            effective_verify = self._policy.ca_path
        else:
            effective_verify = verify
        super().cert_verify(conn, url, effective_verify, cert)


# --- .asp RPC surface -------------------------------------------------------

#: Marker prefix this BMC uses in the SESSION_COOKIE value of a FAILED login.
#:
#: CRITICAL, verified via PR #40 (nesvet/nojava-ipmi-kvm): on bad credentials
#: this BMC's `/rpc/WEBSES/create.asp` still answers HTTP 200 -- there is no
#: 401, no non-2xx status of any kind -- with a body that looks exactly like a
#: successful login except that the SESSION_COOKIE value itself is one of
#: these Failure_Login_* markers. A caller that checks only the HTTP status
#: code will happily treat this as a granted session and proceed to use a
#: cookie that the BMC will reject on every subsequent request, which surfaces
#: later and far more confusingly as an unrelated-looking authorization
#: failure. DO NOT "simplify" login() to drop this check because "the status
#: code is already 200" -- that is precisely the bug PR #40 documents.
_FAILURE_LOGIN_PREFIX = "Failure_Login"

_SESSION_COOKIE_RE = re.compile(r"'SESSION_COOKIE'\s*:\s*'([^']*)'")
_STOKEN_RE = re.compile(r"'STOKEN'\s*:\s*'([^']*)'")

#: JNLP `<argument>value</argument>` elements alternate name/value: a `-flag`
#: form (e.g. `-kvmport`) is immediately followed by a sibling `<argument>`
#: holding that flag's value. This regex-based scan (rather than a full JNLP/
#: XML parse of the surrounding document) is deliberate: real JNLP documents
#: from this class of firmware routinely fail strict XML parsing (unescaped
#: `&` in query-string-shaped argument values is the recurring cause), and a
#: parser that raises on that is useless against the one document this module
#: exists to read. Scanning for the argument elements directly is robust to
#: that, at the cost of assuming (correctly, per every sample seen) that
#: arguments are simple non-nested elements.
_ARGUMENT_RE = re.compile(r"<argument>\s*(.*?)\s*</argument>", re.DOTALL)


def _looks_like_failed_login(session_cookie: str) -> bool:
    return session_cookie.startswith(_FAILURE_LOGIN_PREFIX)


def parse_jnlp_arguments(document: str) -> dict[str, str]:
    """Extract ``-flag``/value pairs from a JNLP document's ``<argument>`` elements.

    Returns a flat mapping keyed by argument name with the leading ``-``
    stripped and lower-cased (``-kvmToken`` -> ``kvmtoken``), so lookups don't
    depend on a firmware revision's exact casing. A flag with no following
    value (the list ends on a flag, or two flags appear back to back) is
    dropped rather than guessed at -- silently pairing it with the wrong
    neighbour would be worse than losing one field.
    """
    # xml.etree.ElementTree has no public entity-unescape helper; decode the
    # small, fixed set JNLP documents actually use by hand instead of pulling
    # a full XML parser over text this module already knows is not strict XML
    # (see _ARGUMENT_RE's docstring note on why).
    values = [_unescape_entities(m.group(1)) for m in _ARGUMENT_RE.finditer(document)]

    arguments: dict[str, str] = {}
    index = 0
    while index < len(values) - 1:
        name = values[index]
        if name.startswith("-"):
            arguments[name[1:].lower()] = values[index + 1]
            index += 2
        else:
            # Not a flag (JViewer's own argument list also carries positional,
            # non-flag arguments before the -flag ones start) -- skip forward
            # one at a time rather than assuming every entry pairs.
            index += 1
    return arguments


def _unescape_entities(text: str) -> str:
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")


class _TransientBusy(Exception):
    """Internal marker: this attempt hit the busy condition and may be retried.

    Never escapes :meth:`AspClient._send_with_retry`. Kept as an exception
    (rather than, say, a sentinel return value) so :meth:`AspClient._send_once`
    can raise it from deep inside an ``except`` block via ``raise ... from
    exc`` and have the original exception preserved as ``__cause__`` for
    :class:`errors.BmcBusyError`'s eventual chaining, while still using
    Python's normal control flow rather than threading a classification
    value back up through every caller.
    """


class AspClient:
    """Serialized HTTP client for one BMC's ``.asp`` RPC surface and JNLP-mediated media session.

    Every request this client makes to the BMC goes through :meth:`_request`,
    which holds an instance-level lock for the duration of the call. This is
    not merely "safe for concurrent use" -- it actively refuses to let two
    calls overlap on the wire, because concurrent load against this board's
    web server has been observed, against the target hardware, to exhaust its
    worker pool (a separate pool per listener: port 80 and port 443 were
    observed failing independently of each other) and lock out its own web UI
    for several minutes. The lock protects one client instance; it does
    nothing for two separate client instances (or two separate Ansible tasks)
    hitting the same BMC at once -- see the connection doc fragment's note on
    that limit.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int = DEFAULT_PORT,
        username: str = "admin",
        password: str,
        use_tls: bool = True,
        validate_certs: bool = True,
        ca_path: str | None = None,
        tls_fingerprint: str | None = None,
        allow_insecure_transport: bool = False,
        timeout: int = 30,
        connect_timeout: int = 10,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        if not HAS_REQUESTS:  # pragma: no cover - exercised by the import sanity test
            raise ImportError(f"The `requests` library is required by this module. Import error was: {REQUESTS_IMPORT_ERROR}")

        enforce_transport_policy(use_tls=use_tls, allow_insecure_transport=allow_insecure_transport)

        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        # Bounded on purpose: unbounded retries against a BMC whose failure
        # mode IS "too much load" would make this client part of the problem
        # it exists to avoid. Two retries (three attempts total) is enough to
        # ride out a transient worker-pool stall without turning into a retry
        # storm of its own.
        self._max_retries = max(0, max_retries)
        self._retry_backoff_seconds = retry_backoff_seconds

        self._policy = TlsTrustPolicy.create(validate_certs=validate_certs, ca_path=ca_path, tls_fingerprint=tls_fingerprint)
        self._lock = threading.Lock()
        self._session_cookie: str | None = None

        self._http_session = requests.Session()
        if use_tls:
            adapter = self._policy.build_adapter()
            self._http_session.mount("https://", adapter)
        # No adapter is mounted for "http://": this board's plaintext listener
        # has not been observed to share the TLS listener's cipher
        # restriction (there is no TLS handshake to restrict), so the default
        # adapter is correct there.

    @property
    def base_url(self) -> str:
        scheme = "https" if self._use_tls else "http"
        return f"{scheme}://{self._host}:{self._port}"

    @property
    def endpoint(self) -> str:
        """A stable, non-secret identifier for this connection, safe to embed in any error or receipt."""
        return f"{self._host}:{self._port}"

    def _known_secrets(self) -> list[str]:
        """Every literal secret this client currently holds, for :func:`errors.redact`'s extra-secrets pass.

        Passed into every exception this client raises, as a backstop beyond
        the named-key patterns in ``errors.py``: if the BMC ever echoes a
        secret back in a shape no pattern anticipates, matching it by exact
        value still catches it.
        """
        secrets = [self._password]
        if self._session_cookie:
            secrets.append(self._session_cookie)
        return [s for s in secrets if s]

    # --- low-level request plumbing ----------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self._session_cookie:
            headers["Cookie"] = f"SessionCookie={self._session_cookie}"
        return headers

    def _send_once(
        self, method: str, url: str, *, operation: str, data: dict[str, str] | None = None, params: dict[str, str] | None = None
    ) -> requests.Response:
        """Issue exactly one HTTP request, holding the serialization lock only for this single attempt.

        Raises the final, typed exception directly for every failure that is
        NOT retryable (:class:`errors.TimeoutError_` for a pre-connect
        timeout, :class:`errors.TlsValidationError` for a handshake failure,
        :class:`errors.ConnectionError_` for a refused/unreachable
        connection). For the one shape this module treats as retryable --
        a request that was sent over a completed TCP connection and then
        produced no response at all -- it raises the internal
        :class:`_TransientBusy` marker instead, which :meth:`_send_with_retry`
        is responsible for turning into a bounded number of further attempts
        and, eventually, a real :class:`errors.BmcBusyError`.

        Deliberately not itself retrying and not itself sleeping: this method
        holds ``self._lock``, and sleeping while holding it would serialize a
        retry backoff against everything else this client might want to do,
        for no benefit -- the lock's job is to keep two requests off the wire
        at once, not to pace retries.
        """
        with self._lock:
            try:
                return self._http_session.request(
                    method,
                    url,
                    data=data,
                    params=params,
                    headers=self._headers(),
                    timeout=(self._connect_timeout, self._timeout),
                )
            except requests.exceptions.ConnectTimeout as exc:
                # Nothing was ever sent: the TCP/TLS handshake itself never
                # completed. A safe, ordinary timeout -- not the busy
                # condition, which requires a completed connection.
                raise TimeoutError_(
                    f"Timed out connecting to {url}",
                    endpoint=self.endpoint,
                    operation=operation,
                    secrets=self._known_secrets(),
                ) from exc
            except requests.exceptions.SSLError as exc:
                raise TlsValidationError(
                    f"TLS handshake with {self.endpoint} failed: {exc}. This BMC's TLS listener has been "
                    f"observed to accept TLS 1.2 only with the single ciphersuite {BMC_CIPHERS.split(':', maxsplit=1)[0]} "
                    "(static RSA, no forward secrecy) -- if this is a cipher/protocol mismatch rather than a "
                    "certificate problem, check that nothing overrode this client's TLS context.",
                    endpoint=self.endpoint,
                    operation=operation,
                    secrets=self._known_secrets(),
                ) from exc
            except requests.exceptions.ReadTimeout as exc:
                # The request was already sent: the socket connected, HTTP
                # data went out, and nothing ever came back before our own
                # timeout fired. Per ErrorClass.BMC_BUSY, observed directly
                # against the target hardware: this is this board's
                # saturated-worker-pool shape, not an ordinary timeout.
                raise _TransientBusy from exc
            except requests.exceptions.ConnectionError as exc:
                if _connection_error_is_post_connect(exc):
                    raise _TransientBusy from exc
                raise ConnectionError_(
                    f"Could not connect to {url}: {exc}",
                    endpoint=self.endpoint,
                    operation=operation,
                    secrets=self._known_secrets(),
                ) from exc

    def _send_with_retry(
        self, method: str, url: str, *, operation: str | None = None, data: dict[str, str] | None = None, params: dict[str, str] | None = None
    ) -> requests.Response:
        """Wrap :meth:`_send_once` with this client's bounded retry policy for the busy condition only.

        See :meth:`AspClient.__init__`'s note on ``max_retries``: this exists
        to ride out a transient worker-pool stall, not to paper over a BMC
        that is genuinely unreachable or rejecting the request outright --
        those are raised straight through by ``_send_once`` and never reach
        this loop at all.
        """
        operation = operation or method
        attempt = 0
        while True:
            try:
                return self._send_once(method, url, operation=operation, data=data, params=params)
            except _TransientBusy as marker:
                attempt += 1
                if attempt > self._max_retries:
                    raise BmcBusyError(
                        f"{self.endpoint} accepted the connection but never served {operation} ({method} {url}) after "
                        f"{attempt} attempt(s). This BMC's web server has a small per-listener worker pool "
                        "that saturates under concurrent load; wait and retry later rather than immediately, "
                        "and confirm nothing else is hitting this BMC concurrently.",
                        endpoint=self.endpoint,
                        operation=operation,
                        # A busy-pool failure happens after the request was
                        # already sent, so -- unlike a refused connection --
                        # the BMC may have partially processed it. Only
                        # meaningful for a mutating call; harmless for a read.
                        indeterminate=True,
                        secrets=self._known_secrets(),
                    ) from marker.__cause__
                # No lock is held here: a retry backoff should not block out
                # anything else this process might want to do with the client
                # while it waits.
                time.sleep(self._retry_backoff_seconds * attempt)

    def _request(
        self, method: str, path: str, *, operation: str | None = None, data: dict[str, str] | None = None, params: dict[str, str] | None = None
    ) -> requests.Response:
        """Issue one request against this client's own ``base_url``, serialized with bounded retries."""
        return self._send_with_retry(method, f"{self.base_url}{path}", operation=operation or path, data=data, params=params)

    # --- session lifecycle --------------------------------------------------

    def login(self) -> str:
        """Authenticate and store the session cookie for subsequent requests.

        See ``_FAILURE_LOGIN_PREFIX``'s docstring: this checks the
        SESSION_COOKIE value's content, not just the HTTP status, because
        this BMC answers a bad password with HTTP 200 (PR #40).
        """
        response = self._request(
            "POST",
            "/rpc/WEBSES/create.asp",
            operation="login",
            data={"WEBVAR_USERNAME": self._username, "WEBVAR_PASSWORD": self._password},
        )
        match = _SESSION_COOKIE_RE.search(response.text)
        if not match:
            raise ProtocolError(
                "create.asp response did not contain a SESSION_COOKIE value",
                endpoint=self.endpoint,
                operation="login",
                diagnostic=response.text,
                secrets=self._known_secrets(),
            )

        session_cookie = match.group(1)
        if not session_cookie or _looks_like_failed_login(session_cookie):
            # Do not store this value and do not echo it: even a rejected
            # session's cookie value is BMC-issued material that has no
            # business appearing in a message, and its being a *failure*
            # marker rather than an active credential does not change that.
            raise AuthenticationError(
                "The BMC rejected the supplied credentials (create.asp returned a Failure_Login "
                "SESSION_COOKIE). See PR #40 (nesvet/nojava-ipmi-kvm) for why this is HTTP 200, not 401.",
                endpoint=self.endpoint,
                operation="login",
                secrets=[self._password],
            )

        self._session_cookie = session_cookie
        return session_cookie

    def get_session_token(self) -> str | None:
        """Fetch ``STOKEN`` from ``getsessiontoken.asp``.

        Do not use this as your source of a usable KVM/media token. Observed
        directly against the target hardware: this endpoint answers HTTP 200
        but returns an EMPTY ``STOKEN`` -- confirming the reference-
        implementation warning that this call alone does not yield anything
        usable. It is kept here because the endpoint is real and some future
        firmware or board revision may behave differently, but
        :meth:`allocate_media_session` -- not this method -- is this
        client's supported way to obtain a working token.
        """
        response = self._request("GET", "/rpc/getsessiontoken.asp", operation="get_session_token")
        match = _STOKEN_RE.search(response.text)
        return match.group(1) if match and match.group(1) else None

    def allocate_media_session(self, *, client_ip: str, secure: bool | None = None) -> JnlpSession:
        """Fetch the JNLP document, which allocates the KVM/media session server-side, and parse it.

        ``secure`` selects the scheme this specific fetch uses, independent of
        how the session itself was authenticated. Defaults to this client's
        own ``use_tls`` setting, but is exposed explicitly rather than only
        implied by it: observed directly against the target hardware, the
        scheme used for THIS request determines ``-kvmsecure``/``-vmsecure``
        in the response (0 over plain HTTP, 1 over HTTPS, on the same board
        minutes apart) -- it is a property of this fetch, not a persistent
        board setting, and a caller reasoning about the resulting session's
        security should not have to infer that from a constructor argument
        set who knows how long ago.
        """
        if self._session_cookie is None:
            raise AuthenticationError(
                "allocate_media_session() requires an active session; call login() first",
                endpoint=self.endpoint,
                operation="allocate_media_session",
            )

        use_tls_for_fetch = self._use_tls if secure is None else secure
        scheme = "https" if use_tls_for_fetch else "http"
        # Deliberately not reusing self._request()/self.base_url when `secure`
        # overrides the client's own scheme: doing so over a different scheme
        # than the client was built for would need a differently-configured
        # TLS adapter (or none), which this method does not attempt to stand
        # up on the fly. Overriding `secure` away from the client's own
        # `use_tls` is therefore only meaningful when the caller's own
        # `use_tls` already matches what they are asking for here; mismatched
        # use is a caller error, not something this method silently repairs.
        url = f"{scheme}://{self._host}:{self._port}/Java/jviewer.jnlp"
        response = self._issue_media_request(url, params={"EXTRNIP": client_ip, "JNLPSTR": "JViewer"})

        arguments = parse_jnlp_arguments(response.text)
        session = JnlpSession.from_arguments(arguments)

        if session.kvm_token is None:
            # Deliberately NOT `diagnostic=response.text` here, unlike this
            # client's other ProtocolErrors. The JNLP body legitimately
            # contains the real KVM token as one of its <argument> values --
            # that is the whole point of fetching it -- so if parsing ever
            # misattributes that value to an unrecognised flag name (which is
            # exactly the failure this branch handles: "no argument we
            # recognise as -kvmtoken was found"), the token sits in the raw
            # text under a label none of errors.redact()'s patterns match. A
            # bare secret string with no recognisable key next to it cannot
            # be redacted by pattern -- see JnlpSession's docstring in
            # models.py. The argument NAMES this parser did recognise are not
            # secret-shaped and are enough to diagnose a firmware/parsing
            # mismatch without that risk.
            raise ProtocolError(
                "jviewer.jnlp response did not contain a usable -kvmtoken argument",
                endpoint=self.endpoint,
                operation="allocate_media_session",
                diagnostic=f"argument names seen: {sorted(arguments)}",
                secrets=self._known_secrets(),
            )
        return session

    def _issue_media_request(self, url: str, *, params: dict[str, str]) -> requests.Response:
        # Shares _send_with_retry()'s lock, retry-bounding, and busy/timeout/
        # connection classification with _request() -- the only reason this
        # is not simply a call to _request() is that the URL here may use a
        # different scheme than self.base_url; see allocate_media_session()'s
        # docstring for why that is a deliberate, narrow exception.
        return self._send_with_retry("GET", url, operation="allocate_media_session", params=params)

    # --- power/boot RPCs -----------------------------------------------
    #
    # TODO: hoststatus.asp's and hostctl.asp's exact response/request field
    # shapes (beyond the endpoint paths and the WEBVAR_POWER_CMD field name
    # themselves) have not been sourced from any capture or reference client
    # yet -- unlike login/getsessiontoken/jviewer.jnlp above, nothing below
    # this point should be treated as verified. These two methods exist only
    # to give asmb8_power/asmb8_info a plumbing point to call once that
    # evidence exists; they deliberately do not attempt to parse or validate
    # a response shape this collection has not seen.

    def get_host_status(self) -> str:
        """Fetch the raw ``hoststatus.asp`` response body.

        Returns the raw text rather than a parsed structure: the response
        field names/values are not yet sourced from any capture. Treat the
        return value as opaque diagnostic text, not a stable API, until this
        is revisited with real evidence.
        """
        return self._request("GET", "/rpc/hoststatus.asp", operation="get_host_status").text

    def set_power(self, command: str) -> str:
        """POST a power command to ``hostctl.asp``.

        ``command`` is passed through verbatim as ``WEBVAR_POWER_CMD`` --
        this client does not validate it against a set of known values,
        because those values have not been sourced yet (see the TODO above).
        Callers should treat any specific command string as provisional until
        it is confirmed against real hardware.
        """
        return self._request("POST", "/rpc/hostctl.asp", operation="set_power", data={"WEBVAR_POWER_CMD": command}).text


def _connection_error_is_post_connect(exc: BaseException) -> bool:
    """Best-effort classification of a ``requests.exceptions.ConnectionError`` as pre- or post-connect.

    ``requests``/``urllib3`` do not expose a clean, version-stable type for
    this distinction -- both shapes surface as the same exception class with
    different chained causes and message text. This inspects the exception's
    string representation (including its chain) for the vocabulary Python's
    own ``http.client``/``urllib3`` use for each case:

    * Pre-connect (refused/unreachable/unresolvable): "connection refused",
      "no route to host", "name or service not known", "network is
      unreachable" -- the TCP layer itself never established a connection.
    * Post-connect (this BMC's busy shape, per ErrorClass.BMC_BUSY): "connection
      aborted", "connection reset", "remote end closed connection without
      response" -- a connection existed and then produced nothing usable.

    Defaults to treating an unrecognised message as pre-connect (the more
    conservative classification: ``connection``, not ``bmc_busy``, is the
    class that does NOT imply "retrying immediately is fine"). This function
    makes no network calls; it only inspects an already-raised exception.
    """
    text_parts = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text_parts.append(str(current))
        current = current.__cause__ or current.__context__
    text = " ".join(text_parts).lower()

    post_connect_markers = (
        "connection aborted",
        "connection reset",
        "remote end closed connection",
        "broken pipe",
    )
    if any(marker in text for marker in post_connect_markers):
        return True
    return False
