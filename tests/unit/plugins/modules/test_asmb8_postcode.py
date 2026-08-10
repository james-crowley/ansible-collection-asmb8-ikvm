# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_postcode.

Every test here replaces `asmb8_postcode.build_asp_client` with a fake -- no
test constructs a real `AspClient`, so nothing here can reach a socket, let
alone any real BMC. The single-read tests drive the module against the real
`tests/unit/fixtures/asp/getpostcode.txt` capture, parsed by the real
`webvar.parse_webvar` -- only the HTTP transport is faked, not the parsing.
The multi-sample tests use directly-constructed `WebVarResponse` objects with
varying `CurrPostCode` values, since the fixture corpus has only ever
captured one snapshot of this endpoint and cannot demonstrate a code
changing over time; those tests are clearly scoped to the sampling loop's own
mechanics (bounding, dedup, ordering), not to fixture-driven parsing, which
the single-read tests already cover.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import ProtocolError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.webvar import WebVarResponse, parse_webvar
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_postcode

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "asp"

PASSWORD = "Sup3rSecret!"

BASE_ARGS = {
    "host": "10.0.0.5",
    "username": "admin",
    "password": PASSWORD,
}


def _fixture_text(name: str) -> str:
    return (FIXTURES_DIR / f"{name}.txt").read_text(encoding="utf-8")


def _set_module_args(args: dict) -> None:
    basic._ANSIBLE_ARGS = to_bytes(json.dumps({"ANSIBLE_MODULE_ARGS": args}))
    basic._ANSIBLE_PROFILE = "legacy"


class AnsibleExitJson(Exception):
    pass


class AnsibleFailJson(Exception):
    pass


def _exit_json(*_args, **kwargs):
    raise AnsibleExitJson(kwargs)


def _fail_json(*_args, **kwargs):
    raise AnsibleFailJson(kwargs)


@pytest.fixture(autouse=True)
def _patch_module_exit(monkeypatch):
    monkeypatch.setattr(basic.AnsibleModule, "exit_json", _exit_json)
    monkeypatch.setattr(basic.AnsibleModule, "fail_json", _fail_json)


def _run_ok(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleExitJson) as excinfo:
        asmb8_postcode.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_postcode.main()
    return excinfo.value.args[0]


def _fake_client_from_real_fixture(endpoint: str = "10.0.0.5:443") -> Mock:
    """A fake AspClient whose get_webvar() parses the real getpostcode.txt fixture."""
    client = Mock()
    client.endpoint = endpoint
    client.login.return_value = "session-cookie-not-real"
    client.get_webvar.side_effect = lambda name, operation=None: parse_webvar(_fixture_text(name), endpoint=endpoint, operation=operation)
    return client


def _fake_client_with_codes(codes: list[str], endpoint: str = "10.0.0.5:443") -> Mock:
    """A fake AspClient whose get_webvar() yields one synthetic response per call, in order.

    See this file's module docstring: used only for the sampling-loop tests, which need a
    changing value across calls that no single real capture can provide.
    """
    client = Mock()
    client.endpoint = endpoint
    client.login.return_value = "session-cookie-not-real"
    responses = [WebVarResponse(variable_name="GETPOSTCODE", struct_name="GETPOSTCODE", records=[{"CurrPostCode": code}], hapi_status=0) for code in codes]
    client.get_webvar.side_effect = responses
    return client


def _wire_fake_client(monkeypatch, fake_client) -> None:
    monkeypatch.setattr(asmb8_postcode, "build_asp_client", lambda params: fake_client)


class FakeClock:
    """A deterministic monotonic/sleep pair for testing sample_post_codes() without real delays."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_postcode.argument_spec()["password"]["no_log"] is True

    def test_sample_defaults_to_false(self):
        assert asmb8_postcode.argument_spec()["sample"]["default"] is False

    def test_poll_interval_seconds_defaults_to_5(self):
        assert asmb8_postcode.argument_spec()["poll_interval_seconds"]["default"] == 5

    def test_max_duration_seconds_defaults_to_60(self):
        assert asmb8_postcode.argument_spec()["max_duration_seconds"]["default"] == 60


class TestSingleReadAgainstRealFixture:
    def test_reads_the_real_getpostcode_fixture(self, monkeypatch):
        fake_client = _fake_client_from_real_fixture()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        # tests/unit/fixtures/asp/getpostcode.txt's one real record is {'CurrPostCode' : '00'}.
        assert result["post_code"] == "00"
        assert result["post_code_int"] == 0
        assert result["sample"] is None
        assert result["changed"] is False
        assert result["operation"]["changed"] is False
        assert result["operation"]["action"] == "asmb8_postcode.read"
        assert result["operation"]["endpoint"] == "10.0.0.5:443"

    def test_logs_in_before_reading(self, monkeypatch):
        fake_client = _fake_client_from_real_fixture()
        _wire_fake_client(monkeypatch, fake_client)
        _run_ok(dict(BASE_ARGS))
        fake_client.login.assert_called_once()

    def test_check_mode_behaves_identically_to_normal_mode(self, monkeypatch):
        fake_client = _fake_client_from_real_fixture()
        _wire_fake_client(monkeypatch, fake_client)
        normal = _run_ok(dict(BASE_ARGS))
        checked = _run_ok(dict(BASE_ARGS, _ansible_check_mode=True))
        assert normal["post_code"] == checked["post_code"]
        assert normal["post_code_int"] == checked["post_code_int"]

    def test_no_credential_leakage(self, monkeypatch):
        fake_client = _fake_client_from_real_fixture()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS))
        assert PASSWORD not in json.dumps(result)


class TestParsePostCodeHex:
    def test_parses_the_real_fixture_value(self):
        assert asmb8_postcode.parse_post_code_hex("00") == 0

    def test_parses_a_non_trivial_hex_value(self):
        assert asmb8_postcode.parse_post_code_hex("AE") == 0xAE

    def test_returns_none_for_non_hex_text(self):
        assert asmb8_postcode.parse_post_code_hex("not-hex") is None

    def test_returns_none_for_none(self):
        assert asmb8_postcode.parse_post_code_hex(None) is None


class TestReadPostCodeErrorHandling:
    def test_raises_protocol_error_when_no_records(self):
        client = Mock()
        client.endpoint = "10.0.0.5:443"
        client.get_webvar.return_value = WebVarResponse(variable_name="GETPOSTCODE", struct_name="GETPOSTCODE", records=[], hapi_status=0)
        with pytest.raises(ProtocolError):
            asmb8_postcode.read_post_code(client)

    def test_raises_protocol_error_when_field_missing(self):
        client = Mock()
        client.endpoint = "10.0.0.5:443"
        client.get_webvar.return_value = WebVarResponse(variable_name="GETPOSTCODE", struct_name="GETPOSTCODE", records=[{"SomethingElse": 1}], hapi_status=0)
        with pytest.raises(ProtocolError):
            asmb8_postcode.read_post_code(client)


class TestSamplePostCodesDirectly:
    def test_bounded_sampling_produces_the_expected_number_of_polls(self):
        clock = FakeClock()
        client = _fake_client_with_codes(["00", "01", "01", "02"])
        observations = asmb8_postcode.sample_post_codes(
            client,
            poll_interval_seconds=5,
            max_duration_seconds=17,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            wall_clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
        )
        # Poll bound: 17 // 5 -> t=0,5,10,15 land within bound; t=20 would not, so 4 polls.
        assert [o["post_code"] for o in observations] == ["00", "01", "01", "02"]
        assert [o["elapsed_seconds"] for o in observations] == [0.0, 5.0, 10.0, 15.0]
        assert clock.sleep_calls == [5, 5, 5]

    def test_always_polls_at_least_once_even_if_duration_is_tiny(self):
        clock = FakeClock()
        client = _fake_client_with_codes(["00"])
        observations = asmb8_postcode.sample_post_codes(
            client,
            poll_interval_seconds=60,
            max_duration_seconds=1,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            wall_clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
        )
        assert len(observations) == 1
        assert clock.sleep_calls == []

    def test_post_code_int_is_populated_per_observation(self):
        clock = FakeClock()
        client = _fake_client_with_codes(["AE"])
        observations = asmb8_postcode.sample_post_codes(
            client,
            poll_interval_seconds=60,
            max_duration_seconds=1,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            wall_clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
        )
        assert observations[0]["post_code_int"] == 0xAE

    def test_timestamp_uses_the_injected_wall_clock(self):
        clock = FakeClock()
        client = _fake_client_with_codes(["00"])
        fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        observations = asmb8_postcode.sample_post_codes(
            client,
            poll_interval_seconds=60,
            max_duration_seconds=1,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            wall_clock=lambda: fixed,
        )
        assert observations[0]["timestamp"] == fixed.isoformat()


class TestDistinctPostCodesInOrder:
    def test_deduplicates_while_preserving_first_seen_order(self):
        observations = [{"post_code": code} for code in ["00", "01", "01", "02", "00"]]
        assert asmb8_postcode.distinct_post_codes_in_order(observations) == ["00", "01", "02"]

    def test_empty_input_yields_empty_output(self):
        assert asmb8_postcode.distinct_post_codes_in_order([]) == []


class TestValidateSamplingBounds:
    def test_valid_bounds_return_none(self):
        assert asmb8_postcode.validate_sampling_bounds(poll_interval_seconds=5, max_duration_seconds=60) is None

    def test_poll_interval_too_low_is_rejected(self):
        assert asmb8_postcode.validate_sampling_bounds(poll_interval_seconds=1, max_duration_seconds=60) is not None

    def test_poll_interval_too_high_is_rejected(self):
        assert asmb8_postcode.validate_sampling_bounds(poll_interval_seconds=301, max_duration_seconds=60) is not None

    def test_max_duration_too_low_is_rejected(self):
        assert asmb8_postcode.validate_sampling_bounds(poll_interval_seconds=5, max_duration_seconds=0) is not None

    def test_max_duration_too_high_is_rejected(self):
        assert asmb8_postcode.validate_sampling_bounds(poll_interval_seconds=5, max_duration_seconds=901) is not None


class TestSamplingEndToEnd:
    def test_sample_true_populates_the_sample_field(self, monkeypatch):
        # poll_interval_seconds at its minimum bound (2) with max_duration_seconds at its minimum
        # bound (1) yields exactly one poll and zero real sleeps -- see
        # TestSamplePostCodesDirectly.test_always_polls_at_least_once_even_if_duration_is_tiny.
        # This keeps this end-to-end test of main()'s own wiring fast without needing to inject
        # fakes into main()'s internals.
        fake_client = _fake_client_from_real_fixture()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, sample=True, poll_interval_seconds=2, max_duration_seconds=1))
        assert result["sample"] is not None
        assert result["sample"]["sample_count"] == 1
        assert result["sample"]["poll_interval_seconds"] == 2
        assert result["sample"]["max_duration_seconds"] == 1
        assert result["sample"]["distinct_post_codes"] == ["00"]
        assert result["post_code"] == "00"

    def test_out_of_range_poll_interval_fails_when_sampling(self, monkeypatch):
        fake_client = _fake_client_from_real_fixture()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS, sample=True, poll_interval_seconds=1))
        assert "poll_interval_seconds" in result["msg"]
        fake_client.login.assert_not_called()

    def test_out_of_range_max_duration_fails_when_sampling(self, monkeypatch):
        fake_client = _fake_client_from_real_fixture()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS, sample=True, max_duration_seconds=1000))
        assert "max_duration_seconds" in result["msg"]

    def test_out_of_range_values_are_ignored_when_not_sampling(self, monkeypatch):
        # sample=false: poll_interval_seconds/max_duration_seconds are documented as ignored, so an
        # out-of-range value on either must not fail a plain single read.
        fake_client = _fake_client_from_real_fixture()
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_ok(dict(BASE_ARGS, sample=False, poll_interval_seconds=1, max_duration_seconds=1000))
        assert result["post_code"] == "00"


class TestErrorHandling:
    def test_missing_requests_dependency_is_fatal(self, monkeypatch):
        monkeypatch.setattr(asmb8_postcode, "HAS_REQUESTS", False)
        monkeypatch.setattr(asmb8_postcode, "REQUESTS_IMPORT_ERROR", "No module named 'requests'")
        result = _run_fail(dict(BASE_ARGS))
        assert "requests" in result["msg"]

    def test_read_failure_fails_the_whole_module(self, monkeypatch):
        fake_client = Mock()
        fake_client.endpoint = "10.0.0.5:443"
        fake_client.login.return_value = "cookie"
        fake_client.get_webvar.side_effect = ProtocolError("getpostcode.asp returned no records", endpoint="10.0.0.5:443", operation="asmb8_postcode.read")
        _wire_fake_client(monkeypatch, fake_client)
        result = _run_fail(dict(BASE_ARGS))
        assert result["error_class"] == "protocol"
