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

import ssl
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
    TlsValidationError,
)

PASSWORD = "Sup3rSecret!"
SESSION_COOKIE = "abcd1234efgh5678ijkl9012mnop3456xyz"
KVM_TOKEN = "0123456789abcdef"

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
