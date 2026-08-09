# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import pytest

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import (
    BOOT_DEVICES,
    POWER_STATES,
    RECEIPT_SCHEMA,
    JnlpSession,
    OperationReceipt,
    optional_bool_flag,
    optional_int,
    optional_str,
)

SECRET = "Sup3rSecret!"
KVM_TOKEN = "abcd1234efgh5678"


class TestSourcedValueTables:
    def test_power_states_match_ipmi_power_module_choices(self):
        # Sourced from community.general.ipmi_power's documented `state`
        # choices -- asmb8_power (not yet implemented) is planned to wrap that
        # module rather than reimplement IPMI power control. Not invented.
        assert POWER_STATES == ("on", "off", "shutdown", "reset", "boot")

    def test_boot_devices_match_ipmi_boot_module_choices(self):
        # Sourced from community.general.ipmi_boot's documented `bootdev`
        # choices, same reasoning as POWER_STATES.
        assert BOOT_DEVICES == ("network", "floppy", "hd", "safe", "optical", "setup", "default")


class TestValueCoercion:
    @pytest.mark.parametrize("value,expected", [("x", "x"), ("  padded  ", "padded"), (7, "7")])
    def test_optional_str_strips_and_stringifies(self, value, expected):
        assert optional_str(value) == expected

    @pytest.mark.parametrize("value", [None, "", "   ", {}, []])
    def test_optional_str_degrades_to_none(self, value):
        assert optional_str(value) is None

    @pytest.mark.parametrize("value,expected", [("1", 1), (" 42 ", 42), (0, 0), ("-3", -3)])
    def test_optional_int_parses_element_text(self, value, expected):
        assert optional_int(value) == expected

    @pytest.mark.parametrize("value", [None, "", "not-a-number", True, False])
    def test_optional_int_degrades_to_none_rather_than_raising(self, value):
        assert optional_int(value) is None

    @pytest.mark.parametrize("value,expected", [("1", True), ("0", False)])
    def test_optional_bool_flag_reads_jnlp_style_zero_one(self, value, expected):
        assert optional_bool_flag(value) is expected

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_optional_bool_flag_keeps_absent_distinct_from_false(self, value):
        # "The BMC did not report -singleportenabled at all" and "it reported
        # 0" are different findings -- collapsing them would have a caller act
        # on an invented value.
        assert optional_bool_flag(value) is None


class TestJnlpSessionPortMode:
    """Both live wiring modes observed on the target board must parse cleanly."""

    def test_single_port_mode_when_cdport_absent_and_flag_set(self):
        # The target board's shipped configuration, observed directly: no
        # -cdport/-fdport/-hdport at all, -singleportenabled 1.
        session = JnlpSession.from_arguments(
            {
                "kvmport": "443",
                "kvmtoken": KVM_TOKEN,
                "webcookie": SECRET,
                "singleportenabled": "1",
            }
        )
        assert session.port_mode == "single_port"
        assert session.single_port_enabled is True
        assert session.cd_port is None
        assert session.fd_port is None
        assert session.hd_port is None

    def test_dedicated_ports_mode_when_cdport_present(self):
        # The mode the board owner was, as of this writing, switching to:
        # dedicated per-device ports and singleportenabled 0.
        session = JnlpSession.from_arguments(
            {
                "kvmport": "443",
                "kvmtoken": KVM_TOKEN,
                "webcookie": SECRET,
                "singleportenabled": "0",
                "cdport": "5120",
                "fdport": "5121",
                "hdport": "5122",
            }
        )
        assert session.port_mode == "dedicated_ports"
        assert session.single_port_enabled is False
        assert session.cd_port == 5120
        assert session.fd_port == 5121
        assert session.hd_port == 5122

    def test_dedicated_ports_mode_wins_even_if_singleportenabled_disagrees(self):
        # Presence of an actual dedicated port is trusted over the flag: it is
        # what a caller is about to use, and a firmware inconsistency here
        # should not silently disappear behind the flag's word.
        session = JnlpSession.from_arguments({"cdport": "5120", "singleportenabled": "1"})
        assert session.port_mode == "dedicated_ports"

    def test_unknown_mode_when_neither_signal_is_present(self):
        session = JnlpSession.from_arguments({"kvmport": "443"})
        assert session.port_mode == "unknown"
        assert session.single_port_enabled is None

    @pytest.mark.parametrize(
        ("fetched_over", "kvmsecure", "vmsecure"),
        [
            ("http", "0", "0"),
            ("https", "1", "1"),
        ],
    )
    def test_secure_flags_follow_the_jnlp_fetch_scheme_not_a_fixed_setting(self, fetched_over, kvmsecure, vmsecure):
        # Observed directly against the target board, minutes apart, same
        # session: fetching the JNLP over plain HTTP returned kvmsecure=0/
        # vmsecure=0, and over HTTPS returned kvmsecure=1/vmsecure=1. Nothing
        # else about the board changed between the two fetches.
        session = JnlpSession.from_arguments({"kvmsecure": kvmsecure, "vmsecure": vmsecure})
        assert session.kvm_secure is (fetched_over == "https")
        assert session.vm_secure is (fetched_over == "https")

    def test_webcookie_is_captured_but_documented_as_the_same_secret_as_the_session_cookie(self):
        # Observed directly: -webcookie comes back byte-identical to the
        # SESSION_COOKIE issued at login. This is a documentation/usage
        # contract (see JnlpSession's docstring), not something this
        # classmethod itself can enforce structurally -- it just needs to
        # capture the value asp.py's caller received.
        session = JnlpSession.from_arguments({"webcookie": SECRET})
        assert session.web_cookie == SECRET

    def test_unnamed_arguments_are_captured_in_extra_without_dropping_named_ones(self):
        session = JnlpSession.from_arguments({"kvmport": "443", "somethingnew": "value"})
        assert session.extra == {"somethingnew": "value"}
        # A named argument must never leak into `extra` under a different key
        # -- that would be a second, unwatched place the same secret-shaped
        # value could hide.
        assert "kvmport" not in session.extra

    def test_cdstate_and_cdnum_are_parsed(self):
        session = JnlpSession.from_arguments({"cdstate": "mounted", "cdnum": "1"})
        assert session.cd_state == "mounted"
        assert session.cd_num == 1

    def test_an_entirely_empty_argument_set_yields_all_none_and_unknown_mode(self):
        session = JnlpSession.from_arguments({})
        assert session.kvm_token is None
        assert session.web_cookie is None
        assert session.port_mode == "unknown"
        assert session.extra == {}


class TestOperationReceipt:
    def test_serializes_to_the_documented_schema(self):
        receipt = OperationReceipt(
            action="asmb8_power.set",
            endpoint="10.0.0.5:443",
            changed=True,
            previous={"state": "off"},
            desired={"state": "on"},
            observed={"state": "on"},
            error_class=None,
        )
        document = receipt.to_dict()
        assert set(document) == {
            "schema",
            "action",
            "endpoint",
            "changed",
            "previous",
            "desired",
            "observed",
            "error_class",
        }
        assert document["schema"] == RECEIPT_SCHEMA == "asmb8-ikvm-operation/v1"
        assert document["changed"] is True
        assert document["previous"] == {"state": "off"}
        assert document["desired"] == {"state": "on"}

    def test_plain_dict_and_none_payloads_pass_through(self):
        receipt = OperationReceipt(action="asmb8_info.gather", endpoint="10.0.0.5:443", changed=False, observed={"version": "1.14"})
        document = receipt.to_dict()
        assert document["observed"] == {"version": "1.14"}
        assert document["previous"] is None
        assert document["desired"] is None

    def test_no_labelled_secret_survives_serialization_even_if_smuggled_into_a_payload(self):
        # OperationReceipt has no credential-shaped field by construction, but
        # to_dict() also runs every string through redact() as a backstop in
        # case a caller passes through something it should not have.
        receipt = OperationReceipt(
            action="asmb8_boot.set",
            endpoint="10.0.0.5:443",
            changed=True,
            observed={"note": f"WEBVAR_PASSWORD={SECRET}"},
        )
        rendered = repr(receipt.to_dict())
        assert SECRET not in rendered
        assert "REDACTED" in rendered

    def test_extra_fields_are_merged_in(self):
        receipt = OperationReceipt(action="asmb8_media.attach", endpoint="10.0.0.5:443", changed=True, extra={"bytes_written": 512})
        assert receipt.to_dict()["bytes_written"] == 512

    def test_jnlp_session_is_not_a_documented_receipt_field(self):
        # Structural guard for the rule stated in JnlpSession's and
        # OperationReceipt's docstrings: nothing in the receipt schema is
        # named or shaped to carry a JnlpSession, so a caller cannot reach for
        # an "obvious" field and accidentally serialize a live token. This
        # does not stop a caller from passing one in as `observed` (redact()
        # cannot save an unlabelled bare token string -- see the docstring),
        # but it does mean doing so is visibly against the grain of the
        # schema rather than one of its intended slots.
        field_names = set(OperationReceipt.__dataclass_fields__)
        assert "jnlp_session" not in field_names
        assert "kvm_token" not in field_names
