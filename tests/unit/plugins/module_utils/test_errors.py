# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import pytest

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import (
    ERROR_CLASS_TO_EXCEPTION,
    MAX_DIAGNOSTIC_BYTES,
    AuthenticationError,
    BmcBusyError,
    ErrorClass,
    IdentityMismatchError,
    IkvmError,
    RemoteOperationError,
    TimeoutError_,
    TlsValidationError,
    redact,
)

SECRET = "Sup3rSecret!"


class TestErrorClasses:
    def test_all_classes_are_unique(self):
        assert len(ErrorClass.ALL) == len(set(ErrorClass.ALL))

    def test_every_class_maps_to_an_exception(self):
        assert set(ERROR_CLASS_TO_EXCEPTION) == set(ErrorClass.ALL)

    @pytest.mark.parametrize("error_class", ErrorClass.ALL)
    def test_mapped_exception_reports_its_own_class(self, error_class):
        # Guards against a copy-paste error where two exception types claim the
        # same class string, which would silently break caller branching.
        assert ERROR_CLASS_TO_EXCEPTION[error_class].error_class == error_class

    def test_bmc_busy_is_the_addition_specific_to_this_hardware(self):
        # This is the one class the sibling james_crowley.intel_amt collection
        # does not have. Guard its presence and mapping explicitly so a future
        # refactor cannot quietly drop it while the parametrized test above
        # still passes for every class that remains.
        assert ErrorClass.BMC_BUSY == "bmc_busy"
        assert ERROR_CLASS_TO_EXCEPTION[ErrorClass.BMC_BUSY] is BmcBusyError


class TestRedaction:
    @pytest.mark.parametrize(
        "leaky",
        [
            'Authorization: Digest username="admin", response="deadbeef", nonce="abc"',
            "Authorization: Basic YWRtaW46U3VwM3JTZWNyZXQh",
            "headers={'Authorization': 'Digest response=deadbeef'}",
            "Set-Cookie: SESSIONID=abc123; Path=/",
        ],
    )
    def test_credential_headers_are_fully_removed(self, leaky):
        out = redact(leaky)
        for fragment in ("deadbeef", "YWRtaW46", "abc123", "Digest username"):
            assert fragment not in out
        assert "[REDACTED]" in out

    @pytest.mark.parametrize(
        ("leaky", "secret_fragment"),
        [
            # The exact Python-dict-repr shape /rpc/WEBSES/create.asp returns.
            ("{'SESSION_COOKIE':'abcd1234efgh5678'}", "abcd1234efgh5678"),
            # The Cookie header this client re-sends the same value as.
            ("Cookie: SessionCookie=abcd1234efgh5678", "abcd1234efgh5678"),
            # getsessiontoken.asp's response shape.
            ("{'STOKEN':'zzzzyyyyxxxx'}", "zzzzyyyyxxxx"),
            # JNLP <argument> values, as they would appear in a flattened dump
            # or a query string built from them.
            ("kvmtoken=deadbeef00112233", "deadbeef00112233"),
            ("webcookie=abcd1234efgh5678", "abcd1234efgh5678"),
            # WEBVAR_PASSWORD is the login form field name. A generic
            # \bpassword\b pattern would NOT catch this: underscore is a word
            # character, so "WEBVAR_PASSWORD" has no internal word boundary.
            ("WEBVAR_PASSWORD=Sup3rSecret!", "Sup3rSecret!"),
        ],
    )
    def test_ami_specific_secret_names_are_redacted(self, leaky, secret_fragment):
        out = redact(leaky)
        assert secret_fragment not in out
        assert "[REDACTED]" in out

    def test_webvar_password_field_name_survives_its_own_redaction(self):
        # The key name is diagnostic ("which field was rejected"); only the
        # value must go.
        out = redact("WEBVAR_PASSWORD=Sup3rSecret!")
        assert "WEBVAR_PASSWORD" in out
        assert "Sup3rSecret!" not in out

    def test_password_xml_element_value_removed_but_tag_kept(self):
        out = redact(f"<AdminPassword>{SECRET}</AdminPassword><Name>keep</Name>")
        assert SECRET not in out
        # The tag name must survive: knowing *which* field was rejected is the
        # whole diagnostic value.
        assert "AdminPassword" in out
        assert "keep" in out

    def test_url_userinfo_password_removed_but_host_kept(self):
        out = redact(f"https://admin:{SECRET}@10.0.0.5:443/rpc/WEBSES/create.asp")
        assert SECRET not in out
        assert "10.0.0.5:443" in out

    def test_dict_style_password_key(self):
        out = redact(f"{{'password': '{SECRET}', 'host': '10.0.0.5'}}")
        assert SECRET not in out
        assert "10.0.0.5" in out

    def test_literal_secret_removed_even_in_unrecognised_shape(self):
        # The BMC sometimes echoes values in shapes no pattern anticipates, so
        # the literal secret must also be scrubbed by exact match.
        out = redact(f"unexpected|{SECRET}|shape", SECRET)
        assert SECRET not in out

    def test_overlapping_secrets_leave_no_fragments(self):
        # Replacing the shorter secret first would leave "extra" behind.
        out = redact("token=abc123extra", ["abc123", "abc123extra"])
        assert "abc123" not in out
        assert "extra" not in out

    def test_output_is_bounded(self):
        out = redact("A" * (MAX_DIAGNOSTIC_BYTES * 3))
        assert len(out) < MAX_DIAGNOSTIC_BYTES + 100
        assert "truncated" in out

    @pytest.mark.parametrize("value,expected", [(None, ""), (12345, "12345")])
    def test_non_string_inputs(self, value, expected):
        assert redact(value) == expected

    def test_bytes_secret_accepted(self):
        assert SECRET not in redact(f"x {SECRET} y", SECRET.encode())


class TestIkvmError:
    def test_message_and_diagnostic_are_redacted(self):
        err = IkvmError(
            f"failed with password={SECRET}",
            diagnostic=f"<Password>{SECRET}</Password>",
            secrets=SECRET,
        )
        assert SECRET not in err.message
        assert SECRET not in str(err)
        assert SECRET not in (err.diagnostic or "")
        # And nothing leaks through the rendered result either.
        assert SECRET not in repr(err.to_result())

    def test_result_shape(self):
        err = RemoteOperationError(
            "hostctl.asp rejected the power command",
            endpoint="10.0.0.5:443",
            operation="set_power",
            return_value=2,
        )
        result = err.to_result()
        assert result["error_class"] == ErrorClass.REMOTE_OPERATION
        assert result["endpoint"] == "10.0.0.5:443"
        assert result["operation"] == "set_power"
        assert result["return_value"] == 2
        assert "indeterminate" not in result

    def test_indeterminate_is_surfaced(self):
        # A timeout after the mutation was transmitted must be distinguishable,
        # or a caller could retry a destructive operation that already applied.
        err = TimeoutError_("timed out after send", indeterminate=True)
        assert err.to_result()["indeterminate"] is True

    def test_bmc_busy_result_carries_indeterminate(self):
        # BmcBusyError's "TCP connected, no response" case happens after the
        # request was already on the wire, so a caller must treat it the same
        # way as a post-send timeout: re-probe, do not blindly retry.
        err = BmcBusyError("BMC accepted the connection but never responded", indeterminate=True)
        result = err.to_result()
        assert result["error_class"] == ErrorClass.BMC_BUSY
        assert result["indeterminate"] is True

    def test_absent_optional_fields_are_omitted(self):
        result = IkvmError("bare").to_result()
        assert set(result) == {"msg", "error_class"}

    @pytest.mark.parametrize(
        "exc,expected",
        [
            (TlsValidationError, ErrorClass.TLS_VALIDATION),
            (AuthenticationError, ErrorClass.AUTHENTICATION),
            (IdentityMismatchError, ErrorClass.IDENTITY_MISMATCH),
            (BmcBusyError, ErrorClass.BMC_BUSY),
        ],
    )
    def test_subclass_classes(self, exc, expected):
        assert exc("x").to_result()["error_class"] == expected

    def test_is_catchable_as_ikvm_error(self):
        with pytest.raises(IkvmError):
            raise TlsValidationError("fingerprint mismatch")
