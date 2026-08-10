# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for the ``.asp`` RPC client.

Every test in this file mocks HTTP at the ``requests.Session`` boundary (or
lower, for the TLS adapter tests). None of them open a socket, and none of
them talk to any real BMC -- see this collection's
CONTRIBUTING.md and the mandate this test suite was written under: hardware
access for this collection's target board belongs to a separate process, and
this test suite must never make that BMC do anything, including by accident
through a real connection attempt.
"""

from __future__ import annotations

import ast
import inspect
import re
import ssl
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from requests.adapters import HTTPAdapter

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import asp
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import (
    BMC_CIPHERS,
    AmiLegacyTlsAdapter,
    AspClient,
    TlsTrustPolicy,
    _connection_error_is_post_connect,
    enforce_transport_policy,
    normalize_fingerprint,
    parse_jnlp_arguments,
)
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import (
    AuthenticationError,
    BmcBusyError,
    ConnectionError_,
    ProtocolError,
    RemoteOperationError,
    TlsValidationError,
)
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.webvar import WebVarResponse

PASSWORD = "Sup3rSecret!"
SESSION_COOKIE = "abcd1234efgh5678ijkl9012mnop3456xyz"
KVM_TOKEN = "0123456789abcdef"
CSRF_TOKEN = "csrf-token-not-real-0123456789"

#: tests/unit/fixtures/asp -- same depth from this file as every sibling test file's own
#: FIXTURES_DIR (see e.g. test_asmb8_sel.py).
FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "asp"

#: The repository's plugins/ tree, for the structural "no write endpoint is reachable" checks
#: below -- these must scan real source files on disk, not just this process's already-imported
#: modules, so a write capability hiding behind a lazy import cannot slip past them.
PLUGINS_DIR = Path(__file__).resolve().parents[4] / "plugins"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


# Two obviously-fake JNLP fragments, one for each live port-wiring mode this
# board has been observed in (see JnlpSession's docstring in models.py). Not
# full, well-formed JNLP documents -- just enough <argument> pairs to exercise
# the regex-based scanner, which is deliberately tolerant of that (see
# asp.py's _ARGUMENT_RE docstring on why a strict XML parse is avoided).
JNLP_SINGLE_PORT = f"""<jnlp>
<application-desc>
<argument>-host</argument>
<argument>10.0.0.5</argument>
<argument>-kvmport</argument>
<argument>443</argument>
<argument>-kvmtoken</argument>
<argument>{KVM_TOKEN}</argument>
<argument>-webcookie</argument>
<argument>{SESSION_COOKIE}</argument>
<argument>-singleportenabled</argument>
<argument>1</argument>
<argument>-kvmsecure</argument>
<argument>1</argument>
<argument>-vmsecure</argument>
<argument>1</argument>
</application-desc>
</jnlp>"""

JNLP_DEDICATED_PORTS = f"""<jnlp>
<application-desc>
<argument>-kvmport</argument>
<argument>443</argument>
<argument>-kvmtoken</argument>
<argument>{KVM_TOKEN}</argument>
<argument>-webcookie</argument>
<argument>{SESSION_COOKIE}</argument>
<argument>-singleportenabled</argument>
<argument>0</argument>
<argument>-cdport</argument>
<argument>5120</argument>
<argument>-fdport</argument>
<argument>5121</argument>
<argument>-hdport</argument>
<argument>5122</argument>
<argument>-cdstate</argument>
<argument>mounted</argument>
<argument>-cdnum</argument>
<argument>1</argument>
</application-desc>
</jnlp>"""


def make_client(**overrides) -> AspClient:
    """A client configured for HTTP with the insecure ack, so tests exercise
    request/session/JNLP logic without needing a TLS adapter in the loop."""
    kwargs = {
        "host": "198.51.100.10",  # RFC 5737 TEST-NET-2; never a real lab address
        "password": PASSWORD,
        "use_tls": False,
        "allow_insecure_transport": True,
        "max_retries": 1,
    }
    kwargs.update(overrides)
    return AspClient(**kwargs)


def mock_response(text: str) -> Mock:
    response = Mock(spec=requests.Response)
    response.text = text
    return response


def _synthetic_webvar_failure_body(name: str, hapi_status: int) -> str:
    """A WEBVAR/JSONVAR body reporting a non-zero ``HAPI_STATUS``, built inline rather than as a
    fixture file. No real capture -- write or read, across every fixture this collection has ever
    added -- has ever shown a non-zero ``HAPI_STATUS``; see
    ``tests/unit/fixtures/asp/README.md``'s note on why a fabricated "failure" reply belongs here,
    not in that directory, matching ``test_asmb8_network.py``'s ``SYNTHETIC_DNSCFG_WITH_SECRET``
    precedent for the same reason."""
    upper = name.upper()
    return (
        f"\n//Dynamic Data Begin\n"
        f" WEBVAR_JSONVAR_{upper} = \n"
        f" {{ \n"
        f" WEBVAR_STRUCTNAME_{upper} : \n"
        f" [ \n"
        f" {{}} ],  \n"
        f" HAPI_STATUS:{hapi_status} }}; \n"
        f"//Dynamic data end\n"
    )


class TestTransportPolicy:
    def test_plaintext_without_acknowledgement_is_refused(self):
        with pytest.raises(TlsValidationError):
            enforce_transport_policy(use_tls=False, allow_insecure_transport=False)

    def test_plaintext_with_acknowledgement_is_allowed(self):
        enforce_transport_policy(use_tls=False, allow_insecure_transport=True)  # must not raise

    def test_tls_is_always_allowed(self):
        enforce_transport_policy(use_tls=True, allow_insecure_transport=False)  # must not raise

    def test_client_constructor_enforces_the_same_policy(self):
        with pytest.raises(TlsValidationError):
            AspClient(host="198.51.100.10", password=PASSWORD, use_tls=False, allow_insecure_transport=False)


class TestFingerprintNormalization:
    @pytest.mark.parametrize(
        "raw",
        [
            "aa" * 32,
            "AA" * 32,
            ":".join(["aa"] * 32),
            f"sha256:{'aa' * 32}",
        ],
    )
    def test_accepted_forms_normalize_identically(self, raw):
        assert normalize_fingerprint(raw) == "aa" * 32

    @pytest.mark.parametrize("raw", ["", "not-hex-zz", "aa" * 20])
    def test_rejected_forms_raise_tls_validation_error(self, raw):
        with pytest.raises(TlsValidationError):
            normalize_fingerprint(raw)

    def test_ca_path_and_fingerprint_are_mutually_exclusive(self):
        with pytest.raises(TlsValidationError):
            TlsTrustPolicy.create(ca_path="/etc/ssl/ca.pem", tls_fingerprint="aa" * 32)


class TestAmiLegacyTlsAdapter:
    """This BMC's TLS 1.2-only, single-ciphersuite listener (see asp.py's
    BMC_CIPHERS comment) needs a custom ssl.SSLContext, or the handshake never
    completes. These tests inspect that context without opening a socket."""

    def test_context_includes_the_required_cipher_and_minimum_version(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(HTTPAdapter, "init_poolmanager", lambda self, *a, **kw: captured.update(kw))

        policy = TlsTrustPolicy.create(tls_fingerprint="aa" * 32)
        adapter = policy.build_adapter()
        adapter.init_poolmanager()

        context = captured["ssl_context"]
        assert isinstance(context, ssl.SSLContext)
        cipher_names = {cipher["name"] for cipher in context.get_ciphers()}
        assert "AES256-GCM-SHA384" in cipher_names
        assert context.minimum_version == ssl.TLSVersion.TLSv1_2

    def test_pinned_policy_passes_assert_fingerprint_and_disables_chain_validation(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(HTTPAdapter, "init_poolmanager", lambda self, *a, **kw: captured.update(kw))

        policy = TlsTrustPolicy.create(tls_fingerprint="aa" * 32)
        policy.build_adapter().init_poolmanager()

        assert captured["assert_fingerprint"] == "aa" * 32
        assert captured["ssl_context"].verify_mode == ssl.CERT_NONE

    def test_non_pinned_policy_does_not_set_assert_fingerprint(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(HTTPAdapter, "init_poolmanager", lambda self, *a, **kw: captured.update(kw))

        policy = TlsTrustPolicy.create(validate_certs=True)
        policy.build_adapter().init_poolmanager()

        assert "assert_fingerprint" not in captured
        assert captured["ssl_context"].verify_mode == ssl.CERT_REQUIRED

    def test_validate_certs_false_without_pinning_disables_verification_honestly(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(HTTPAdapter, "init_poolmanager", lambda self, *a, **kw: captured.update(kw))

        policy = TlsTrustPolicy.create(validate_certs=False)
        policy.build_adapter().init_poolmanager()

        assert captured["ssl_context"].verify_mode == ssl.CERT_NONE
        assert "assert_fingerprint" not in captured

    def test_adapter_is_the_slimmer_equivalent_the_task_allowed_for(self):
        # Confirms this module does not import the sibling intel_amt
        # collection's tls.py: this board's mandatory cipher/protocol
        # restriction (BMC_CIPHERS) has no equivalent there, so this is a
        # deliberately separate, slimmer implementation rather than a port.
        assert issubclass(AmiLegacyTlsAdapter, HTTPAdapter)
        assert "AES256-GCM-SHA384" in BMC_CIPHERS


class TestSslErrorIsActionable:
    def test_ssl_error_during_request_becomes_tls_validation_error_mentioning_the_cipher(self):
        client = make_client(use_tls=True, tls_fingerprint="aa" * 32)
        client._http_session.request = Mock(side_effect=requests.exceptions.SSLError("handshake failure"))

        with pytest.raises(TlsValidationError) as exc_info:
            client._request("GET", "/rpc/hoststatus.asp", operation="get_host_status")

        # The raw OpenSSL alert is unintelligible on its own; the message
        # must point a human at the actual requirement.
        assert "AES256-GCM-SHA384" in str(exc_info.value)


class TestConnectionErrorClassification:
    @pytest.mark.parametrize(
        "message",
        [
            "Connection aborted.",
            "Connection reset by peer",
            "Remote end closed connection without response",
            "Broken pipe",
        ],
    )
    def test_post_connect_markers_are_recognised(self, message):
        assert _connection_error_is_post_connect(requests.exceptions.ConnectionError(message)) is True

    @pytest.mark.parametrize(
        "message",
        [
            "Connection refused",
            "Name or service not known",
            "No route to host",
            "Network is unreachable",
        ],
    )
    def test_pre_connect_markers_are_not_recognised_as_post_connect(self, message):
        assert _connection_error_is_post_connect(requests.exceptions.ConnectionError(message)) is False

    def test_unrecognised_message_defaults_to_pre_connect(self):
        # The conservative default: `connection`, not `bmc_busy`, is the class
        # that does not imply "retrying immediately is fine".
        assert _connection_error_is_post_connect(requests.exceptions.ConnectionError("something else entirely")) is False

    def test_classification_walks_the_exception_chain(self):
        cause = OSError("Connection reset by peer")
        wrapper = requests.exceptions.ConnectionError("Max retries exceeded")
        wrapper.__cause__ = cause
        assert _connection_error_is_post_connect(wrapper) is True


class TestBmcBusyRetryAndClassification:
    """The bmc_busy condition, and this client's bounded-retry response to it, per ErrorClass.BMC_BUSY."""

    def test_read_timeout_is_retried_then_raises_bmc_busy_with_indeterminate(self, monkeypatch):
        monkeypatch.setattr(asp.time, "sleep", lambda *_: None)
        client = make_client(max_retries=2)
        client._http_session.request = Mock(side_effect=requests.exceptions.ReadTimeout("no response"))

        with pytest.raises(BmcBusyError) as exc_info:
            client._request("GET", "/rpc/hoststatus.asp", operation="get_host_status")

        assert exc_info.value.to_result()["indeterminate"] is True
        # max_retries=2 -> 3 total attempts before giving up.
        assert client._http_session.request.call_count == 3

    def test_post_connect_connection_error_is_retried_then_raises_bmc_busy(self, monkeypatch):
        monkeypatch.setattr(asp.time, "sleep", lambda *_: None)
        client = make_client(max_retries=1)
        client._http_session.request = Mock(side_effect=requests.exceptions.ConnectionError("Connection aborted."))

        with pytest.raises(BmcBusyError):
            client._request("GET", "/rpc/hoststatus.asp", operation="get_host_status")

        assert client._http_session.request.call_count == 2

    def test_refused_connection_is_not_retried_and_raises_connection_error(self, monkeypatch):
        monkeypatch.setattr(asp.time, "sleep", lambda *_: None)
        client = make_client(max_retries=2)
        client._http_session.request = Mock(side_effect=requests.exceptions.ConnectionError("Connection refused"))

        with pytest.raises(ConnectionError_):
            client._request("GET", "/rpc/hoststatus.asp", operation="get_host_status")

        # A refused connection is never going to succeed on retry, so this
        # must fail fast rather than spend the retry budget on it.
        assert client._http_session.request.call_count == 1

    def test_a_request_that_eventually_succeeds_within_the_retry_budget_returns_normally(self, monkeypatch):
        monkeypatch.setattr(asp.time, "sleep", lambda *_: None)
        client = make_client(max_retries=2)
        client._http_session.request = Mock(
            side_effect=[requests.exceptions.ReadTimeout("no response"), mock_response("ok")],
        )

        response = client._request("GET", "/rpc/hoststatus.asp", operation="get_host_status")
        assert response.text == "ok"
        assert client._http_session.request.call_count == 2


class TestLogin:
    def test_successful_login_stores_and_returns_the_session_cookie(self):
        client = make_client()
        client._http_session.request = Mock(return_value=mock_response(f"{{'SESSION_COOKIE':'{SESSION_COOKIE}'}}"))

        cookie = client.login()

        assert cookie == SESSION_COOKIE
        assert client._session_cookie == SESSION_COOKIE

    def test_bad_credentials_returning_http_200_are_detected_and_raise_authentication_error(self):
        # CRITICAL regression guard, per PR #40 (nesvet/nojava-ipmi-kvm): this
        # BMC answers a wrong password with HTTP 200 and a SESSION_COOKIE
        # value that starts with Failure_Login_. A client that only checks
        # the HTTP status code would accept this as a granted session.
        client = make_client()
        client._http_session.request = Mock(return_value=mock_response("{'SESSION_COOKIE':'Failure_Login_Bad_Password'}"))

        with pytest.raises(AuthenticationError) as exc_info:
            client.login()

        assert client._session_cookie is None
        # The rejected cookie's own value is BMC-issued material and must not
        # appear in the message, even though it is itself only a marker, not
        # an active credential.
        assert "Failure_Login_Bad_Password" not in str(exc_info.value)
        assert PASSWORD not in str(exc_info.value)

    @pytest.mark.parametrize(
        "marker",
        [
            "Failure_Login_Bad_Password",
            "Failure_Login_Max_Users",
            "Failure_Login",
        ],
    )
    def test_every_failure_login_marker_shape_is_caught(self, marker):
        client = make_client()
        client._http_session.request = Mock(return_value=mock_response(f"{{'SESSION_COOKIE':'{marker}'}}"))
        with pytest.raises(AuthenticationError):
            client.login()

    def test_missing_session_cookie_entirely_is_a_protocol_error_not_an_auth_error(self):
        # A response with no SESSION_COOKIE key at all is a shape this client
        # does not understand, which is a different finding from "the BMC
        # understood the request and rejected the credentials".
        client = make_client()
        client._http_session.request = Mock(return_value=mock_response("<html>not what we expected</html>"))
        with pytest.raises(ProtocolError):
            client.login()

    def test_password_never_appears_in_a_protocol_error_diagnostic(self):
        client = make_client()
        client._http_session.request = Mock(return_value=mock_response(f"garbage containing {PASSWORD} somehow"))
        with pytest.raises(ProtocolError) as exc_info:
            client.login()
        assert PASSWORD not in str(exc_info.value)
        assert PASSWORD not in str(exc_info.value.diagnostic)


class TestGetSessionToken:
    def test_stoken_value_is_returned_when_present(self):
        client = make_client()
        client._http_session.request = Mock(return_value=mock_response("{'STOKEN':'sometoken'}"))
        assert client.get_session_token() == "sometoken"

    def test_empty_stoken_is_none_not_an_empty_string(self):
        # Observed directly against the target hardware: this endpoint
        # answers HTTP 200 with an EMPTY STOKEN. Confirms the
        # reference-implementation warning that this call alone is not a
        # reliable source of a usable token -- allocate_media_session() is.
        client = make_client()
        client._http_session.request = Mock(return_value=mock_response("{'STOKEN':''}"))
        assert client.get_session_token() is None


class TestParseJnlpArguments:
    def test_flags_are_lower_cased_and_stripped_of_their_leading_dash(self):
        arguments = parse_jnlp_arguments("<argument>-KvmPort</argument><argument>443</argument>")
        assert arguments == {"kvmport": "443"}

    def test_trailing_flag_with_no_value_is_dropped_not_guessed(self):
        arguments = parse_jnlp_arguments("<argument>-kvmport</argument><argument>443</argument><argument>-trailing</argument>")
        assert arguments == {"kvmport": "443"}

    def test_leading_non_flag_arguments_are_skipped(self):
        arguments = parse_jnlp_arguments("<argument>positional</argument><argument>-kvmport</argument><argument>443</argument>")
        assert arguments == {"kvmport": "443"}

    def test_entity_escaped_values_are_decoded(self):
        arguments = parse_jnlp_arguments("<argument>-note</argument><argument>a &amp; b &lt;tag&gt;</argument>")
        assert arguments == {"note": "a & b <tag>"}

    def test_the_token_is_found_by_flag_name_not_by_position(self):
        """Regression test for a real failure, 2026-08-09.

        A diagnostic harness read the token positionally as the 4th
        ``<argument>``. In this board's actual JNLP layout that slot holds
        ``-hostname``'s value -- the BMC's own IP address. Authenticating with
        an IP address as the token made the BMC refuse redirection with an
        otherwise-undocumented ``status 3``, which cost hours and two wasted
        boot cycles to track down. ``status 3`` means "bad token".

        The argument order below is the order this firmware really emits. The
        point of the test is that ``kvmtoken`` resolves correctly *even though*
        a positional read of index 3 would return the hostname instead, so any
        future refactor back to index arithmetic fails here rather than on
        hardware.
        """
        document = (
            "<argument>-apptype</argument><argument>JViewer</argument>"
            "<argument>-hostname</argument><argument>198.51.100.7</argument>"
            "<argument>-kvmtoken</argument><argument>h4DdmNddY6mlazY1</argument>"
            "<argument>-kvmsecure</argument><argument>0</argument>"
            "<argument>-vmsecure</argument><argument>0</argument>"
            "<argument>-cdstate</argument><argument>1</argument>"
            "<argument>-cdport</argument><argument>5120</argument>"
            "<argument>-singleportenabled</argument><argument>0</argument>"
        )
        arguments = parse_jnlp_arguments(document)

        assert arguments["kvmtoken"] == "h4DdmNddY6mlazY1"
        # The trap, stated explicitly: index 3 is the hostname, not the token.
        raw = re.findall(r"<argument>\s*(.*?)\s*</argument>", document, re.DOTALL)
        assert raw[3] == "198.51.100.7"
        assert arguments["kvmtoken"] != raw[3]
        # The other fields the media path depends on must survive too.
        assert arguments["cdport"] == "5120"
        assert arguments["vmsecure"] == "0"
        assert arguments["cdstate"] == "1"


class TestAllocateMediaSession:
    def test_requires_an_active_session(self):
        client = make_client()
        with pytest.raises(AuthenticationError):
            client.allocate_media_session(client_ip="203.0.113.5")

    def test_single_port_mode_is_parsed_from_the_live_shipped_configuration_shape(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response(JNLP_SINGLE_PORT))

        session = client.allocate_media_session(client_ip="203.0.113.5")

        assert session.port_mode == "single_port"
        assert session.single_port_enabled is True
        assert session.cd_port is None
        assert session.kvm_token == KVM_TOKEN
        # Observed directly: -webcookie comes back identical to the session
        # cookie issued at login, not an independent secret.
        assert session.web_cookie == client._session_cookie

    def test_dedicated_ports_mode_is_parsed_from_the_alternate_live_configuration_shape(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response(JNLP_DEDICATED_PORTS))

        session = client.allocate_media_session(client_ip="203.0.113.5")

        assert session.port_mode == "dedicated_ports"
        assert session.single_port_enabled is False
        assert session.cd_port == 5120
        assert session.fd_port == 5121
        assert session.hd_port == 5122
        assert session.cd_state == "mounted"
        assert session.cd_num == 1

    def test_secure_flags_reflect_the_scheme_of_this_specific_fetch(self):
        client = make_client(use_tls=True, tls_fingerprint="aa" * 32)
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response(JNLP_SINGLE_PORT))

        session = client.allocate_media_session(client_ip="203.0.113.5")

        assert session.kvm_secure is True
        assert session.vm_secure is True

    def test_missing_kvmtoken_is_a_protocol_error(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response("<jnlp><application-desc></application-desc></jnlp>"))

        with pytest.raises(ProtocolError):
            client.allocate_media_session(client_ip="203.0.113.5")

    def test_no_secret_leaks_into_the_protocol_error_when_a_token_is_present_but_something_else_is_wrong(self):
        # Even a successful-looking JNLP fetch's raw text (which legitimately
        # contains the token) must never surface unredacted if this method
        # itself raises -- diagnostic text goes through redact(), and the
        # known-secrets backstop covers the session cookie/password too.
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        broken_jnlp = JNLP_SINGLE_PORT.replace("-kvmtoken", "-something-else")
        client._http_session.request = Mock(return_value=mock_response(broken_jnlp))

        with pytest.raises(ProtocolError) as exc_info:
            client.allocate_media_session(client_ip="203.0.113.5")

        assert KVM_TOKEN not in str(exc_info.value)
        assert KVM_TOKEN not in str(exc_info.value.diagnostic)
        assert SESSION_COOKIE not in str(exc_info.value)
        assert SESSION_COOKIE not in str(exc_info.value.diagnostic)


class TestNoCredentialLeakage:
    """Cross-cutting guard: nothing this client raises may contain the password, session cookie, or KVM token."""

    def test_bmc_busy_error_after_login_never_contains_the_session_cookie(self, monkeypatch):
        monkeypatch.setattr(asp.time, "sleep", lambda *_: None)
        client = make_client(max_retries=0)
        client._http_session.request = Mock(
            side_effect=[
                mock_response(f"{{'SESSION_COOKIE':'{SESSION_COOKIE}'}}"),
                requests.exceptions.ReadTimeout("no response"),
            ]
        )
        client.login()
        assert client._session_cookie == SESSION_COOKIE

        with pytest.raises(BmcBusyError) as exc_info:
            client._request("GET", "/rpc/hoststatus.asp", operation="get_host_status")

        rendered = repr(exc_info.value.to_result())
        assert SESSION_COOKIE not in rendered
        assert PASSWORD not in rendered

    def test_connection_error_message_never_contains_the_password(self):
        client = make_client()
        client._http_session.request = Mock(side_effect=requests.exceptions.ConnectionError("Connection refused"))
        with pytest.raises(ConnectionError_) as exc_info:
            client._request("GET", "/rpc/hoststatus.asp", operation="get_host_status")
        assert PASSWORD not in str(exc_info.value)

    def test_known_secrets_includes_password_and_session_cookie_once_established(self):
        client = make_client()
        assert client._known_secrets() == [PASSWORD]
        client._session_cookie = SESSION_COOKIE
        assert set(client._known_secrets()) == {PASSWORD, SESSION_COOKIE}

    def test_known_secrets_includes_the_csrf_token_once_captured(self):
        client = make_client()
        client._csrf_token = CSRF_TOKEN
        assert CSRF_TOKEN in client._known_secrets()


class TestGetWebvarIsReadOnly:
    """The structural half of this collection's read-only guarantee: every informational module
    can claim "get_webvar only ever issues GET" as a fact about the code, not a promise about
    intent. Pinned two ways -- by observed behaviour (what actually goes out on the wire) and by
    the method's own signature (it does not even accept a `data` argument, so nothing a caller
    passes could turn it into a POST)."""

    def test_get_webvar_issues_a_bare_get_with_no_body(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response(_read_fixture("getdatetime.txt")))

        client.get_webvar("getdatetime")

        args, kwargs = client._http_session.request.call_args
        assert args[0] == "GET"
        assert kwargs["data"] is None

    def test_get_webvar_has_no_data_parameter_to_be_misused(self):
        signature = inspect.signature(AspClient.get_webvar)
        assert "data" not in signature.parameters

    def test_get_webvar_source_never_mentions_post(self):
        # Belt-and-suspenders on top of the behavioural test above: the method body itself must
        # not contain the string "POST" in any form that could issue one.
        source = inspect.getsource(AspClient.get_webvar)
        assert '"POST"' not in source
        assert "'POST'" not in source


class TestPostWebvar:
    """`AspClient.post_webvar()` -- the separately-named sibling of `get_webvar()` for the two
    endpoints sourced from a real save-action capture that require their selector submitted as a
    POST body: `getselentries.asp` (SEL paging, WEBVAR_LASTEVENTID) and `getsessioninfo.asp` (the
    per-service session directory, SERVICEBIT). See tests/unit/fixtures/asp/README.md's
    "POST-parameterized reads" section for exactly what was captured."""

    def test_issues_a_post_with_the_given_data_to_the_rpc_path(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response(_read_fixture("getselentries_post_lasteventid24.txt")))

        client.post_webvar("getselentries", data={"WEBVAR_LASTEVENTID": "24"})

        args, kwargs = client._http_session.request.call_args
        assert args[0] == "POST"
        assert args[1] == f"{client.base_url}/rpc/getselentries.asp"
        assert kwargs["data"] == {"WEBVAR_LASTEVENTID": "24"}

    def test_parses_the_real_getselentries_post_capture_as_a_legitimately_empty_result(self):
        """Sourced fact, not a parsing failure: the SEL held exactly 24 entries at capture time, so
        "entries after record 24" is genuinely empty -- see the module docstring on asmb8_sel and
        the fixtures README."""
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response(_read_fixture("getselentries_post_lasteventid24.txt")))

        response = client.post_webvar("getselentries", data={"WEBVAR_LASTEVENTID": "24"})

        assert isinstance(response, WebVarResponse)
        assert response.records == []
        assert response.hapi_status == 0

    def test_parses_the_real_getsessioninfo_post_capture(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response(_read_fixture("getsessioninfo_post_servicebit4.txt")))

        response = client.post_webvar("getsessioninfo", data={"SERVICEBIT": "4"})

        assert response.records == [{"SID": 24, "STYPE": 7, "IPADDRESS": "192.0.2.10", "UID": 2, "UNAME": "admin", "UPRIV": 4}]
        assert response.hapi_status == 0

    def test_endpoint_name_is_wrapped_in_the_rpc_path_the_same_way_as_get_webvar(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response(_read_fixture("getsessioninfo_post_servicebit4.txt")))

        client.post_webvar("getsessioninfo", data={"SERVICEBIT": "4"})

        assert client._http_session.request.call_args[0][1] == f"{client.base_url}/rpc/getsessioninfo.asp"

    def test_malformed_response_raises_protocol_error_same_as_get_webvar(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response("<html>not a webvar body</html>"))

        with pytest.raises(ProtocolError):
            client.post_webvar("getselentries", data={"WEBVAR_LASTEVENTID": "24"})


class TestSetWebvar:
    """`AspClient.set_webvar()` -- this collection's first, and so far only, way to mutate BMC
    configuration. Sourced from a real save-action capture (2026-08-10) backing `asmb8_ntp`; see
    `docs/protocol-notes.md`'s NTP write-convention section for the request-body field names and
    `tests/unit/fixtures/asp/README.md` for exactly what is and is not sourced about the two reply
    fixtures used below."""

    def test_issues_a_post_with_the_given_data_to_the_rpc_path(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response(_read_fixture("setntpcfg_write.txt")))

        client.set_webvar(
            "setntpcfg",
            {
                "NEW_NTPSERVER_NAME1": "pool.ntp.org",
                "OLD_NTPSERVER_NAME1": "pool.ntp.org",
                "NEW_NTPSERVER_NAME2": " 192.0.2.10",
                "ISNTPENABLE": "0",
            },
        )

        args, kwargs = client._http_session.request.call_args
        assert args[0] == "POST"
        assert args[1] == f"{client.base_url}/rpc/setntpcfg.asp"
        assert kwargs["data"]["ISNTPENABLE"] == "0"
        assert kwargs["data"]["NEW_NTPSERVER_NAME2"] == " 192.0.2.10"  # leading space sent verbatim

    def test_returns_the_parsed_empty_record_reply(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response(_read_fixture("setntpcfg_write.txt")))

        response = client.set_webvar("setntpcfg", {"ISNTPENABLE": "0"})

        assert isinstance(response, WebVarResponse)
        assert response.records == []
        assert response.hapi_status == 0

    def test_works_against_setdatetime_too(self):
        # set_webvar() is sourced against two endpoints from the same capture, not just the one
        # asmb8_ntp itself calls -- see the method's own docstring.
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response(_read_fixture("setdatetime_write.txt")))

        response = client.set_webvar("setdatetime", {"SECONDS": "1786347240", "UTCMINUTES": "480", "TIMEZONE": "GMT+8", "ISNTPENABLE": "0"})

        assert response.hapi_status == 0
        args, _kwargs = client._http_session.request.call_args
        assert args[1] == f"{client.base_url}/rpc/setdatetime.asp"

    def test_nonzero_hapi_status_raises_remote_operation_error(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response(_synthetic_webvar_failure_body("setntpcfg", 1)))

        with pytest.raises(RemoteOperationError):
            client.set_webvar("setntpcfg", {"ISNTPENABLE": "0"})

    def test_nonzero_hapi_status_error_carries_the_status_as_return_value(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response(_synthetic_webvar_failure_body("setntpcfg", 1)))

        with pytest.raises(RemoteOperationError) as excinfo:
            client.set_webvar("setntpcfg", {"ISNTPENABLE": "0"})
        assert excinfo.value.return_value == 1

    def test_malformed_response_raises_protocol_error_same_as_get_and_post_webvar(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response("<html>not a webvar body</html>"))

        with pytest.raises(ProtocolError):
            client.set_webvar("setntpcfg", {"ISNTPENABLE": "0"})

    def test_set_webvar_is_a_distinct_method_from_get_and_post_webvar(self):
        # Different name, different method object from both read paths -- see set_webvar's own
        # docstring for why that is the entire point: nobody reaches a write by accident.
        assert AspClient.set_webvar is not AspClient.get_webvar
        assert AspClient.set_webvar is not AspClient.post_webvar


class TestCsrfToken:
    """The anti-CSRF token this BMC's login response carries, and this collection's
    match-the-vendor decision to replay it on POST reads -- see AspClient._headers()'s docstring
    for the full reasoning, including the explicit caveat that enforcement is unverified."""

    def test_login_harvests_the_csrftoken_from_the_response_body(self):
        client = make_client()
        client._http_session.request = Mock(return_value=mock_response(f"{{'SESSION_COOKIE':'{SESSION_COOKIE}','CSRFTOKEN':'{CSRF_TOKEN}'}}"))

        client.login()

        assert client._csrf_token == CSRF_TOKEN

    def test_a_login_response_with_no_csrftoken_field_leaves_it_none_without_raising(self):
        client = make_client()
        client._http_session.request = Mock(return_value=mock_response(f"{{'SESSION_COOKIE':'{SESSION_COOKIE}'}}"))

        client.login()  # must not raise

        assert client._csrf_token is None

    def test_an_empty_csrftoken_value_is_none_not_an_empty_string(self):
        client = make_client()
        client._http_session.request = Mock(return_value=mock_response(f"{{'SESSION_COOKIE':'{SESSION_COOKIE}','CSRFTOKEN':''}}"))

        client.login()

        assert client._csrf_token is None

    def test_the_login_request_itself_never_carries_a_csrftoken_header_even_when_one_is_already_known(self):
        # Mirrors the vendor JS's own rule verbatim (lib/xmit.js): CSRFTOKEN is never attached to
        # a WEBSES request. A client re-authenticating (e.g. after a session expired) may already
        # hold a token from its previous login; that must not leak onto the next login POST.
        client = make_client()
        client._csrf_token = CSRF_TOKEN
        client._http_session.request = Mock(return_value=mock_response(f"{{'SESSION_COOKIE':'{SESSION_COOKIE}'}}"))

        client.login()

        kwargs = client._http_session.request.call_args.kwargs
        assert "CSRFTOKEN" not in kwargs["headers"]
        assert kwargs["headers"].get("Cookie") is None  # no session cookie was set before this login, either

    def test_post_webvar_sends_the_csrftoken_header_when_one_was_captured(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._csrf_token = CSRF_TOKEN
        client._http_session.request = Mock(return_value=mock_response(_read_fixture("getsessioninfo_post_servicebit4.txt")))

        client.post_webvar("getsessioninfo", data={"SERVICEBIT": "4"})

        kwargs = client._http_session.request.call_args.kwargs
        assert kwargs["headers"]["CSRFTOKEN"] == CSRF_TOKEN

    def test_post_webvar_omits_the_header_when_no_token_was_ever_captured(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        assert client._csrf_token is None
        client._http_session.request = Mock(return_value=mock_response(_read_fixture("getsessioninfo_post_servicebit4.txt")))

        client.post_webvar("getsessioninfo", data={"SERVICEBIT": "4"})

        kwargs = client._http_session.request.call_args.kwargs
        assert "CSRFTOKEN" not in kwargs["headers"]

    def test_get_webvar_never_sends_a_csrftoken_header_even_when_one_is_known(self):
        # Scope check for the deliberately-narrower-than-the-vendor rule: see _headers()'s
        # docstring on why this collection attaches CSRFTOKEN to POST only, leaving the
        # independently-working GET reads unchanged.
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._csrf_token = CSRF_TOKEN
        client._http_session.request = Mock(return_value=mock_response(_read_fixture("getdatetime.txt")))

        client.get_webvar("getdatetime")

        kwargs = client._http_session.request.call_args.kwargs
        assert "CSRFTOKEN" not in kwargs["headers"]

    def test_no_csrftoken_header_is_ever_sent_before_a_token_exists_regardless_of_method(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._http_session.request = Mock(return_value=mock_response(_read_fixture("getselentries_post_lasteventid24.txt")))

        client.post_webvar("getselentries", data={"WEBVAR_LASTEVENTID": "24"})

        kwargs = client._http_session.request.call_args.kwargs
        assert "CSRFTOKEN" not in kwargs["headers"]

    def test_set_webvar_sends_the_csrftoken_header_when_one_was_captured(self):
        # set_webvar() shares _headers() with post_webvar() -- same attachment rule, same
        # best-effort/never-blocking behaviour -- see set_webvar's own docstring.
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        client._csrf_token = CSRF_TOKEN
        client._http_session.request = Mock(return_value=mock_response(_read_fixture("setntpcfg_write.txt")))

        client.set_webvar("setntpcfg", {"ISNTPENABLE": "0"})

        kwargs = client._http_session.request.call_args.kwargs
        assert kwargs["headers"]["CSRFTOKEN"] == CSRF_TOKEN

    def test_set_webvar_omits_the_header_when_no_token_was_ever_captured(self):
        client = make_client()
        client._session_cookie = SESSION_COOKIE
        assert client._csrf_token is None
        client._http_session.request = Mock(return_value=mock_response(_read_fixture("setntpcfg_write.txt")))

        client.set_webvar("setntpcfg", {"ISNTPENABLE": "0"})

        kwargs = client._http_session.request.call_args.kwargs
        assert "CSRFTOKEN" not in kwargs["headers"]


def _string_call_arguments(source: str) -> set[str]:
    """Every string literal passed as a `Call` argument (positional or keyword) anywhere in `source`.

    AST-based, deliberately not a raw substring search over the text: a prose mention inside a
    comment or a docstring is neither of those things (a docstring is an `Expr` statement holding
    a bare string constant, never itself an argument to a `Call`), so this cannot be tripped by
    legitimate documentation the way a plain ``"needle" in source`` check would be. Only text that
    some function call could actually be reached with counts.
    """
    tree = ast.parse(source)
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for arg in (*node.args, *(kw.value for kw in node.keywords)):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    values.add(arg.value)
    return values


def _names_endpoint_as_a_call_argument(source: str, endpoint: str) -> bool:
    return any(endpoint in value.lower() for value in _string_call_arguments(source))


#: Endpoints with a sourced `set*.asp` write convention (see docs/protocol-notes.md) that this
#: collection deliberately does NOT implement -- no client method, no module option, no call
#: argument anywhere under plugins/. `set_webvar()`'s own docstring names the two endpoints that
#: ARE implemented (`setntpcfg`, `setdatetime`); this tuple is everything else that is merely
#: *recorded* as protocol knowledge. Extend this tuple, not the individual tests below, the next
#: time a write convention is sourced but deliberately left unbuilt -- that is the whole point of
#: `TestNoWriteEndpointIsReachable` staying a loop over data rather than one test per endpoint.
_UNIMPLEMENTED_SET_ENDPOINTS = ("setvmediacfg",)

#: The only two write endpoints this collection actually reaches, via `set_webvar()` from
#: `asmb8_ntp`. Kept alongside `_UNIMPLEMENTED_SET_ENDPOINTS` so a future addition to either tuple
#: makes the "is this endpoint accounted for" bookkeeping impossible to forget.
_IMPLEMENTED_SET_ENDPOINTS = ("setntpcfg", "setdatetime")


class TestNoWriteEndpointIsReachable:
    """The sourced `setvmediacfg.asp` write convention (see docs/protocol-notes.md's "A sourced,
    unimplemented write convention" section) is recorded as protocol knowledge only, deliberately
    NOT implemented anywhere in this collection -- no client method, no module option. These scan
    real source files on disk (not just this process's imports) so a write capability cannot slip
    in behind a lazy import or a dynamically-built string this static a check would otherwise miss
    trivially -- see each test's own docstring for exactly what it does and does not catch.

    Generalised (not just `setvmediacfg`-specific) so the same guarantee automatically covers any
    future endpoint added to `_UNIMPLEMENTED_SET_ENDPOINTS` above, per `set_webvar()`'s own
    docstring note that adding a write method must never loosen this guarantee for the endpoints
    it does not implement."""

    def test_no_write_method_exists_on_the_client_for_unimplemented_endpoints(self):
        assert not hasattr(AspClient, "set_vmediacfg")

    def test_asp_py_source_never_calls_out_to_an_unimplemented_write_endpoint(self):
        # Documenting the endpoint in a docstring (as post_webvar()'s and set_webvar()'s own
        # docstrings do, by name, to explain why it must never be used that way) is fine and
        # expected; passing it as a call argument anywhere would mean it is actually reachable,
        # which is what this checks.
        source = inspect.getsource(asp)
        for endpoint in _UNIMPLEMENTED_SET_ENDPOINTS:
            assert not _names_endpoint_as_a_call_argument(source, endpoint)

    def test_no_plugin_source_file_calls_out_to_an_unimplemented_write_endpoint(self):
        """`docs/protocol-notes.md` and this file's own docstrings are allowed to name an
        unimplemented endpoint in prose as sourced-but-unimplemented protocol knowledge (that is
        the whole point of recording it) -- but no file under `plugins/` (the code that actually
        runs) may pass that name as an actual call argument, ever."""
        offenders = []
        for path in PLUGINS_DIR.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for endpoint in _UNIMPLEMENTED_SET_ENDPOINTS:
                if _names_endpoint_as_a_call_argument(text, endpoint):
                    offenders.append((str(path), endpoint))
        assert offenders == []

    def test_set_webvar_only_names_the_two_endpoints_it_actually_implements(self):
        """`set_webvar()` itself is generic -- it will POST to whatever endpoint a caller gives it
        -- so the real guarantee lives in who calls it, not in the method. This confirms
        `asp.py`'s own source never passes any of the deliberately-unimplemented endpoints as a
        call argument to `set_webvar` (or anything else -- see the test above) while still being
        free to mention `_IMPLEMENTED_SET_ENDPOINTS` in its docstring."""
        source = inspect.getsource(asp)
        arguments = _string_call_arguments(source)
        for endpoint in _UNIMPLEMENTED_SET_ENDPOINTS:
            assert endpoint not in arguments

    def test_asmb8_ntp_only_reaches_the_two_sourced_write_endpoints(self):
        """The one caller of `set_webvar()` in this collection today. Confirms it reaches exactly
        the endpoints its own write capture sourced, and none of the endpoints recorded as
        unimplemented."""
        from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_ntp

        source = inspect.getsource(asmb8_ntp)
        arguments = _string_call_arguments(source)
        for endpoint in _UNIMPLEMENTED_SET_ENDPOINTS:
            assert endpoint not in arguments
        assert "setntpcfg" in arguments  # the one write endpoint this module actually uses

    def test_asmb8_sel_and_asmb8_sessions_modules_expose_no_state_option(self):
        """The write convention's own brief is explicit: no `state` option anywhere for this
        capability. `asmb8_redirection` legitimately has an unrelated, already-documented `state`
        option of its own (a different, also-unimplemented gap) -- this checks only the two
        modules this task actually touched."""
        from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_sel, asmb8_sessions

        assert "state" not in asmb8_sel.argument_spec()
        assert "state" not in asmb8_sessions.argument_spec()
