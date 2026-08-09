# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Deterministic mock of the ASMB8-iKVM BMC's legacy ``.asp`` RPC surface, for
integration testing.

There is exactly one real board in the world for this collection, and it is
currently unreachable -- see the top-level task this file was written under.
This mock is the only way to regression-test ``plugins/module_utils/asp.py``
(``AspClient``) end to end without it.

Standard library only: ``http.server`` for the HTTP listener,
``urllib.parse`` for form/query decoding. Test-side HTTP clients (the
collection's own ``requests``-based ``AspClient``) are fine; the *server*
itself gains no dependency the collection does not already ship.

Provenance and verification status
-----------------------------------

Every default behaviour below is one of:

``VERIFIED LIVE``
    Captured directly against the real board (see ``asp.py``'s own docstring
    citations: PR #40 for the HTTP-200-on-bad-credentials shape, and the
    maintainer's direct capture for the empty ``STOKEN`` / JNLP argument
    shapes). This mock reproduces these exactly.

``UNCONFIRMED``
    Plumbing exists because the endpoint path and the one input field name are
    real (``asp.py`` sends them), but the response *shape* has not been
    sourced from any capture. Marked at the point it is used -- see
    ``asp.py``'s own TODO on ``hoststatus.asp``/``hostctl.asp`` for why
    nothing here should be read as more than a placeholder.
"""

from __future__ import annotations

import http.server
import secrets
import socketserver
import string
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

#: Fixture defaults, not real credentials.
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "test-password-not-real"

#: VERIFIED LIVE: a successful SESSION_COOKIE is exactly 35 characters.
SESSION_COOKIE_LENGTH = 35
#: VERIFIED LIVE: -kvmtoken is a distinct value from the session cookie, 16 characters.
KVM_TOKEN_LENGTH = 16

#: VERIFIED LIVE marker prefix for a rejected login (see ``asp.py``'s
#: ``_FAILURE_LOGIN_PREFIX`` -- PR #40, nesvet/nojava-ipmi-kvm). This mock's
#: default failure marker string; a test may set a different one via
#: ``AspFaultConfig.force_login_failure_marker``.
DEFAULT_FAILURE_MARKER = "Failure_Login_Bad_Password"

_ALPHABET = string.ascii_lowercase + string.digits


def _random_string(length: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _position in range(length))


@dataclass
class AspState:
    """Mutable firmware-like state: what a ``Get``/``POST`` observes, and what
    a prior request changes for a later one to see."""

    username: str = DEFAULT_USERNAME
    password: str = DEFAULT_PASSWORD
    #: The cookie the most recent successful login issued, or ``None`` if
    #: nobody has logged in yet on this running mock. Deliberately a single
    #: slot, not a set: the real board caps concurrent web sessions low, and
    #: every one of this collection's clients (``AspClient``) is documented
    #: (see its own class docstring) to serialize its own requests -- so one
    #: outstanding session is the case this mock needs to model.
    session_cookie: str | None = None
    #: VERIFIED LIVE: -kvmtoken is a distinct 16-char value from the session
    #: cookie. Generated once per server start, like a real board's session
    #: allocator would mint on first use.
    kvm_token: str = field(default_factory=lambda: _random_string(KVM_TOKEN_LENGTH))
    #: Which of the two mutually exclusive port-wiring modes the JNLP reports
    #: (see ``models.JnlpSession``'s docstring: both are real, current
    #: configurations of the same board, not a hypothetical).
    single_port_enabled: bool = True
    kvm_port: int = 443
    cd_port: int = 5120
    fd_port: int = 5122
    hd_port: int = 5123
    #: VERIFIED LIVE: these follow the scheme THIS jviewer.jnlp fetch used,
    #: not a persistent board setting -- see ``allocate_media_session``'s
    #: docstring. This mock always answers according to whichever scheme the
    #: request actually arrived over, so this field is not read directly by
    #: the JNLP handler; kept here only as the default a test can override to
    #: prove the *scheme-follows-fetch* behaviour rather than a cached value.
    follow_request_scheme: bool = True
    #: Opt-in: include one extra ``<argument>`` pair whose value contains an
    #: unescaped ``&``, so the client's regex-scan-rather-than-XML-parse
    #: decision (see ``asp.py``'s ``_ARGUMENT_RE`` docstring) stays
    #: regression-tested against a document a strict XML parser would reject.
    jnlp_include_unescaped_ampersand: bool = False
    #: UNCONFIRMED shapes -- see module docstring. Plumbing only.
    host_status_text: str = "HOSTSTATUS: unconfirmed shape, placeholder text only"
    last_power_command: str | None = None


@dataclass
class AspFaultConfig:
    """Every fault-injection knob this mock understands.

    ``hang_before_response`` is the one that matters most: it is what a real
    saturated BMC worker pool does (complete the TCP handshake, then never
    serve the request at all -- see ``errors.ErrorClass.BMC_BUSY``), and it is
    the exact failure that locked the board's owner out of the real device.
    Persistent, unlike the login-failure fault below: it describes what this
    endpoint *is* for as long as a test wants that (matching how
    ``run_asp_mock.py`` uses it as a start-up flag for the whole process
    lifetime), not "the next request only". A test flips it back to
    ``False`` explicitly once it has what it needs.
    """

    hang_before_response: bool = False
    #: Upper bound on how long a hung handler thread actually blocks before
    #: giving up and closing with no response sent. A real saturated BMC has
    #: no such bound; this one exists purely so a test suite does not leak a
    #: thread forever (the listening server itself uses daemon threads, so
    #: process exit is never blocked on this either way).
    hang_seconds: float = 30.0

    #: One-shot: force the next create.asp login to answer this failure
    #: marker regardless of whether the supplied credentials were correct.
    force_login_failure_marker: str | None = None


def _create_asp_body(session_cookie: str) -> str:
    return f"{{'SESSION_COOKIE':'{session_cookie}'}}"


def _get_session_token_body() -> str:
    """VERIFIED LIVE: this endpoint answers HTTP 200 with an EMPTY STOKEN --
    it is never a usable token source, regardless of session state."""
    return "{'STOKEN':''}"


def _jnlp_body(state: AspState, *, secure: bool) -> str:
    """Build the JNLP document ``allocate_media_session`` fetches and parses.

    VERIFIED LIVE facts reproduced: ``-webcookie`` byte-identical to the
    session cookie; ``-kvmtoken`` a distinct value; single-port mode carries
    NO ``-cdport``/``-fdport``/``-hdport`` arguments at all; ``-kvmsecure``/
    ``-vmsecure`` follow the scheme THIS fetch used, not a cached setting.
    """
    kvm_secure = "1" if secure else "0"
    session_cookie = state.session_cookie or ""

    arguments: list[tuple[str, str]] = [
        ("-host", "127.0.0.1"),
        ("-kvmport", str(state.kvm_port)),
        ("-kvmtoken", state.kvm_token),
        ("-webcookie", session_cookie),
        ("-singleportenabled", "1" if state.single_port_enabled else "0"),
    ]
    if not state.single_port_enabled:
        arguments += [
            ("-cdport", str(state.cd_port)),
            ("-fdport", str(state.fd_port)),
            ("-hdport", str(state.hd_port)),
        ]
    arguments += [
        ("-kvmsecure", kvm_secure),
        ("-vmsecure", kvm_secure),
    ]
    if state.jnlp_include_unescaped_ampersand:
        # Deliberately NOT escaped: real JNLP documents from this class of
        # firmware routinely fail strict XML parsing for exactly this reason
        # (asp.py's _ARGUMENT_RE docstring). "a=1&b=2" is shaped like a query
        # string a firmware template forgot to entity-escape.
        arguments.append(("-note", "a=1&b=2"))

    argument_xml = "".join(f"<argument>{name}</argument>\n<argument>{value}</argument>\n" for name, value in arguments)
    return f"<jnlp>\n<application-desc>\n{argument_xml}</application-desc>\n</jnlp>\n"


class _AspRequestHandler(http.server.BaseHTTPRequestHandler):
    #: VERIFIED LIVE / task requirement: HTTP/1.0 semantics, no keep-alive.
    protocol_version = "HTTP/1.0"

    server: _AspHTTPServer  # type: ignore[assignment]

    def log_message(self, *_args: object) -> None:  # silence default stderr logging
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    def _maybe_hang(self) -> bool:
        """If armed, hang without ever writing a response, reproducing the
        real board's saturated-worker-pool failure. Returns True if it hung
        (caller must not write anything more). Persistent -- see
        AspFaultConfig.hang_before_response's docstring for why this does not
        reset itself the way the one-shot faults do."""
        faults = self.server.faults
        if not faults.hang_before_response:
            return False
        deadline = time.monotonic() + faults.hang_seconds
        while time.monotonic() < deadline and not self.server.stop_event.is_set():
            time.sleep(0.05)
        return True

    def _respond(self, status: int, body: str, content_type: str = "text/plain") -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        body = self._read_body()

        if path == "/rpc/WEBSES/create.asp":
            if self._maybe_hang():
                return
            fields = parse_qs(body.decode("utf-8", errors="replace"))
            username = fields.get("WEBVAR_USERNAME", [""])[0]
            password = fields.get("WEBVAR_PASSWORD", [""])[0]
            state = self.server.state
            faults = self.server.faults

            forced_marker = faults.force_login_failure_marker
            if forced_marker is not None:
                faults.force_login_failure_marker = None

            if forced_marker is None and username == state.username and password == state.password:
                # VERIFIED LIVE: 35-character session cookie on success.
                cookie = _random_string(SESSION_COOKIE_LENGTH)
                state.session_cookie = cookie
                self._respond(200, _create_asp_body(cookie))
            else:
                # CRITICAL, VERIFIED LIVE: HTTP 200, never 401, on bad
                # credentials -- PR #40 (nesvet/nojava-ipmi-kvm). This is the
                # single most important behaviour this mock implements: it is
                # the trap a status-code-only client falls into.
                marker = forced_marker or DEFAULT_FAILURE_MARKER
                self._respond(200, _create_asp_body(marker))
            return

        if path == "/rpc/hostctl.asp":
            if self._maybe_hang():
                return
            fields = parse_qs(body.decode("utf-8", errors="replace"))
            self.server.state.last_power_command = fields.get("WEBVAR_POWER_CMD", [None])[0]
            # UNCONFIRMED shape (see asp.py's own TODO on this endpoint):
            # nothing establishes what a real acknowledgement looks like, so
            # this plumbing-only body must not be cited as evidence of
            # firmware behaviour.
            self._respond(200, "OK")
            return

        self._respond(404, "not found")

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query)

        if path == "/rpc/getsessiontoken.asp":
            if self._maybe_hang():
                return
            self._respond(200, _get_session_token_body())
            return

        if path == "/Java/jviewer.jnlp":
            if self._maybe_hang():
                return
            # "-kvmsecure"/"-vmsecure" follow the scheme THIS request arrived
            # over -- this mock is plaintext-only (see class docstring: no TLS
            # listener here), so it always reports secure=False. A test that
            # wants to exercise the secure=True shape sets
            # AspState fields directly and calls _jnlp_body() itself (see
            # test_asp_server.py), rather than this handler inferring TLS from
            # a socket this mock never wraps.
            del query  # EXTRNIP/JNLPSTR are accepted but not validated -- asp.py does not require it either
            self._respond(200, _jnlp_body(self.server.state, secure=False), content_type="application/x-java-jnlp-file")
            return

        if path == "/rpc/hoststatus.asp":
            if self._maybe_hang():
                return
            self._respond(200, self.server.state.host_status_text)
            return

        self._respond(404, "not found")


class _AspHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    #: Handler threads must not block process/server shutdown, even one stuck
    #: in AspFaultConfig.hang_before_response's sleep loop.
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: AspState, faults: AspFaultConfig) -> None:
        super().__init__(address, _AspRequestHandler)
        self.state = state
        self.faults = faults
        self.stop_event = threading.Event()


class AspMockServer:
    """Threaded mock of the ``.asp`` RPC surface, playing the BMC's web server.

    Use as a context manager::

        with AspMockServer(username="admin", password="secret") as server:
            resp = requests.post(f"http://127.0.0.1:{server.port}/rpc/WEBSES/create.asp", data={...})

    Binds an ephemeral TCP port on 127.0.0.1 only.
    """

    def __init__(self, *, host: str = "127.0.0.1", username: str = DEFAULT_USERNAME, password: str = DEFAULT_PASSWORD) -> None:
        self.host = host
        self.state = AspState(username=username, password=password)
        self.faults = AspFaultConfig()
        self._httpd: _AspHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    def start(self) -> AspMockServer:
        self._httpd = _AspHTTPServer((self.host, 0), self.state, self.faults)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="asp-mock-serve", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.stop_event.set()
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._httpd = None
        self._thread = None

    def __enter__(self) -> AspMockServer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"
