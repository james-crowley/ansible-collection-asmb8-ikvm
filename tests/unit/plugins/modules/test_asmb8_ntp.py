# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for asmb8_ntp -- this collection's first module that writes BMC configuration.

Same discipline as `test_asmb8_network.py`: every end-to-end test wires a real `AspClient` to
canned HTTP responses, mocking only `requests.Session.request`. Nothing here opens a socket or
talks to any BMC, and nothing here makes a request to a real ASMB8 endpoint (no 172.20.x.x
address appears anywhere in this file).

`tests/unit/fixtures/asp/getntpcfg.txt` is the one real read capture this module is built from --
`SERVER_NAME1: pool.ntp.org`, `SERVER_NAME2: ' 192.0.2.10'` (leading space, deliberately preserved
throughout this file's assertions), `NTP_STATUS: 1`. Where a test needs a *different* NTP state
(to exercise a real change), it builds a synthetic getntpcfg.asp-shaped body inline -- see
`_synthetic_getntpcfg_body()` -- rather than adding a fabricated fixture file, matching
`test_asmb8_network.py`'s `SYNTHETIC_DNSCFG_WITH_SECRET` precedent.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import AspClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import AuthenticationError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_ntp

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "asp"

PASSWORD = "Sup3rSecret!"

BASE_ARGS = {
    "host": "198.51.100.10",  # RFC 5737 TEST-NET-2; never a real lab address
    "username": "admin",
    "password": PASSWORD,
}

#: The one real capture's own values -- see tests/unit/fixtures/asp/getntpcfg.txt and this
#: module's DOCUMENTATION for why SERVER_NAME2's leading space is not incidental.
REAL_SERVER1 = "pool.ntp.org"
REAL_SERVER2 = " 192.0.2.10"  # leading space, intact
REAL_NTP_STATUS = 1


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _synthetic_getntpcfg_body(server1: str | None, server2: str | None, ntp_status: int | None) -> str:
    """A getntpcfg.asp-shaped body for a state the real fixture does not carry.

    Built the same way `test_asmb8_network.py`'s `SYNTHETIC_DNSCFG_WITH_SECRET` is: a synthetic
    body honestly labelled as such, never presented as a second capture. ``ast.literal_eval``
    (via `webvar.py`'s parser) needs valid Python literals, so ``None`` is rendered as the field
    being entirely absent from the record rather than as a literal ``None`` -- matching how a
    real firmware omission would actually look on the wire.
    """
    fields = []
    if server1 is not None:
        fields.append(f"'SERVER_NAME1' : {server1!r}")
    if server2 is not None:
        fields.append(f"'SERVER_NAME2' : {server2!r}")
    if ntp_status is not None:
        fields.append(f"'NTP_STATUS' : {ntp_status}")
    record = ",".join(fields)
    return (
        "\n//Dynamic Data Begin\n"
        " WEBVAR_JSONVAR_GETNTPCFG = \n"
        " { \n"
        " WEBVAR_STRUCTNAME_GETNTPCFG : \n"
        f" [ \n"
        f" {{ {record} }},  {{}} ],  \n"
        " HAPI_STATUS:0 }; \n"
        "//Dynamic data end\n"
    )


def _synthetic_write_failure_body(hapi_status: int) -> str:
    """A setntpcfg.asp write reply reporting failure. No real capture has ever shown a non-zero
    HAPI_STATUS -- see tests/unit/fixtures/asp/README.md -- so this is inline and synthetic, not a
    fixture file."""
    return (
        "\n//Dynamic Data Begin\n"
        " WEBVAR_JSONVAR_SETNTPCFG = \n"
        " { \n"
        " WEBVAR_STRUCTNAME_SETNTPCFG : \n"
        " [ \n"
        " {} ],  \n"
        f" HAPI_STATUS:{hapi_status} }}; \n"
        "//Dynamic data end\n"
    )


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
        asmb8_ntp.main()
    return excinfo.value.args[0]


def _run_fail(args: dict) -> dict:
    _set_module_args(args)
    with pytest.raises(AnsibleFailJson) as excinfo:
        asmb8_ntp.main()
    return excinfo.value.args[0]


def mock_response(text: str) -> Mock:
    response = Mock(spec=requests.Response)
    response.text = text
    return response


def build_client(
    *,
    get_responses: list[str],
    write_response: str | None = None,
    login_ok: bool = True,
) -> tuple[AspClient, dict]:
    """A real `AspClient` wired to canned responses, plus a dict this test can inspect afterwards
    for exactly what `setntpcfg.asp` was asked to write (empty if it was never called).

    ``get_responses`` is read in order: the first `getntpcfg.asp` request gets
    ``get_responses[0]``, the second (the post-write re-read, when a write happens)
    ``get_responses[1]`` if given, or ``get_responses[0]`` again if only one was supplied -- a
    test that never writes only ever needs one.
    """
    captured_write: dict = {}
    get_calls = {"count": 0}

    def _request(method, url, **kwargs):
        if url.endswith("/rpc/WEBSES/create.asp"):
            if login_ok:
                return mock_response("{'SESSION_COOKIE':'test-session-cookie','CSRFTOKEN':'csrf-not-real'}")
            return mock_response("{'SESSION_COOKIE':'Failure_Login_Bad_Password'}")
        if url.endswith("/rpc/getntpcfg.asp"):
            index = min(get_calls["count"], len(get_responses) - 1)
            get_calls["count"] += 1
            return mock_response(get_responses[index])
        if url.endswith("/rpc/setntpcfg.asp"):
            captured_write["method"] = method
            captured_write["data"] = kwargs.get("data")
            captured_write["headers"] = kwargs.get("headers")
            return mock_response(write_response or _read_fixture("setntpcfg_write.txt"))
        raise AssertionError(f"unexpected request: {method} {url}")

    client = AspClient(host=BASE_ARGS["host"], password=PASSWORD, use_tls=False, allow_insecure_transport=True)
    client._http_session.request = Mock(side_effect=_request)
    return client, captured_write


REAL_GETNTPCFG_BODY = _read_fixture("getntpcfg.txt")


class TestArgumentSpec:
    def test_password_is_no_log(self):
        assert asmb8_ntp.argument_spec()["password"]["no_log"] is True

    def test_server_options_are_plain_strings_not_required(self):
        spec = asmb8_ntp.argument_spec()
        assert spec["server1"] == {"type": "str"}
        assert spec["server2"] == {"type": "str"}

    def test_enabled_is_a_plain_bool_not_required(self):
        assert asmb8_ntp.argument_spec()["enabled"] == {"type": "bool"}


class TestDecodeNtpState:
    def test_decodes_the_real_fixture(self):
        from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.webvar import parse_webvar

        record = parse_webvar(REAL_GETNTPCFG_BODY).records[0]
        state = asmb8_ntp.decode_ntp_state(record)
        assert state == {
            "server1": REAL_SERVER1,
            "server2": REAL_SERVER2,
            "enabled": True,
            "ntp_status_raw": REAL_NTP_STATUS,
        }

    def test_leading_space_on_server2_survives_decoding(self):
        from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.webvar import parse_webvar

        record = parse_webvar(REAL_GETNTPCFG_BODY).records[0]
        state = asmb8_ntp.decode_ntp_state(record)
        assert state["server2"].startswith(" ")
        assert state["server2"] == " 192.0.2.10"

    def test_ntp_status_zero_decodes_to_enabled_false(self):
        state = asmb8_ntp.decode_ntp_state({"SERVER_NAME1": "a", "SERVER_NAME2": "b", "NTP_STATUS": 0})
        assert state["enabled"] is False
        assert state["ntp_status_raw"] == 0

    def test_missing_ntp_status_decodes_to_enabled_none(self):
        state = asmb8_ntp.decode_ntp_state({"SERVER_NAME1": "a", "SERVER_NAME2": "b"})
        assert state["enabled"] is None
        assert state["ntp_status_raw"] is None


class TestPlan:
    CURRENT = {"server1": REAL_SERVER1, "server2": REAL_SERVER2, "enabled": True, "ntp_status_raw": 1}

    def test_no_options_given_is_never_a_change(self):
        params = {"server1": None, "server2": None, "enabled": None}
        desired, changed = asmb8_ntp.plan(self.CURRENT, params)
        assert changed is False
        assert desired == {"server1": REAL_SERVER1, "server2": REAL_SERVER2, "enabled": True}

    def test_server2_matching_with_the_same_leading_space_is_not_a_change(self):
        # The exact idempotence case DOCUMENTATION promises: a caller that supplies the same
        # leading space the BMC reported back must see no change, forever.
        params = {"server1": None, "server2": " 192.0.2.10", "enabled": None}
        _desired, changed = asmb8_ntp.plan(self.CURRENT, params)
        assert changed is False

    def test_server2_without_the_leading_space_is_a_change(self):
        # The trap this module's DOCUMENTATION warns about, demonstrated directly: omitting the
        # leading space the BMC actually stores is a real, byte-level difference.
        params = {"server1": None, "server2": "192.0.2.10", "enabled": None}
        desired, changed = asmb8_ntp.plan(self.CURRENT, params)
        assert changed is True
        assert desired["server2"] == "192.0.2.10"

    def test_server1_change_leaves_server2_and_enabled_untouched(self):
        params = {"server1": "time.example.org", "server2": None, "enabled": None}
        desired, changed = asmb8_ntp.plan(self.CURRENT, params)
        assert changed is True
        assert desired["server1"] == "time.example.org"
        assert desired["server2"] == REAL_SERVER2
        assert desired["enabled"] is True

    def test_enabled_change_leaves_servers_untouched(self):
        params = {"server1": None, "server2": None, "enabled": False}
        desired, changed = asmb8_ntp.plan(self.CURRENT, params)
        assert changed is True
        assert desired["enabled"] is False
        assert desired["server1"] == REAL_SERVER1
        assert desired["server2"] == REAL_SERVER2

    def test_enabled_matching_current_inferred_value_is_not_a_change(self):
        params = {"server1": None, "server2": None, "enabled": True}
        _desired, changed = asmb8_ntp.plan(self.CURRENT, params)
        assert changed is False


class TestBuildSetntpcfgData:
    CURRENT = {"server1": REAL_SERVER1, "server2": REAL_SERVER2, "enabled": True, "ntp_status_raw": 1}

    def test_old_ntpserver_name1_is_always_sent(self):
        desired = {"server1": REAL_SERVER1, "server2": REAL_SERVER2, "enabled": False}
        data = asmb8_ntp.build_setntpcfg_data(self.CURRENT, desired)
        assert data["OLD_NTPSERVER_NAME1"] == REAL_SERVER1

    def test_old_ntpserver_name1_reflects_the_pre_write_value_even_when_server1_is_changing(self):
        desired = {"server1": "time.example.org", "server2": REAL_SERVER2, "enabled": True}
        data = asmb8_ntp.build_setntpcfg_data(self.CURRENT, desired)
        assert data["OLD_NTPSERVER_NAME1"] == REAL_SERVER1  # the value before this write
        assert data["NEW_NTPSERVER_NAME1"] == "time.example.org"  # the value being written

    def test_no_old_ntpserver_name2_is_ever_invented(self):
        desired = {"server1": REAL_SERVER1, "server2": "203.0.113.5", "enabled": True}
        data = asmb8_ntp.build_setntpcfg_data(self.CURRENT, desired)
        assert "OLD_NTPSERVER_NAME2" not in data

    def test_new_ntpserver_name2_carries_the_leading_space_verbatim(self):
        desired = {"server1": REAL_SERVER1, "server2": REAL_SERVER2, "enabled": True}
        data = asmb8_ntp.build_setntpcfg_data(self.CURRENT, desired)
        assert data["NEW_NTPSERVER_NAME2"] == REAL_SERVER2

    def test_isntpenable_encodes_true_as_1(self):
        desired = {"server1": REAL_SERVER1, "server2": REAL_SERVER2, "enabled": True}
        data = asmb8_ntp.build_setntpcfg_data(self.CURRENT, desired)
        assert data["ISNTPENABLE"] == "1"

    def test_isntpenable_encodes_false_as_0(self):
        desired = {"server1": REAL_SERVER1, "server2": REAL_SERVER2, "enabled": False}
        data = asmb8_ntp.build_setntpcfg_data(self.CURRENT, desired)
        assert data["ISNTPENABLE"] == "0"


class TestIdempotence:
    def test_matching_desired_state_including_leading_space_is_a_noop(self, monkeypatch):
        client, captured_write = build_client(get_responses=[REAL_GETNTPCFG_BODY])
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS, server1=REAL_SERVER1, server2=REAL_SERVER2, enabled=True))

        assert result["changed"] is False
        assert captured_write == {}  # setntpcfg.asp was never requested

    def test_a_second_run_of_the_same_task_is_also_a_noop(self, monkeypatch):
        args = dict(BASE_ARGS, server1=REAL_SERVER1, server2=REAL_SERVER2, enabled=True)

        client1, _captured_write = build_client(get_responses=[REAL_GETNTPCFG_BODY])
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client1)
        first = _run_ok(args)

        client2, captured_write2 = build_client(get_responses=[REAL_GETNTPCFG_BODY])
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client2)
        second = _run_ok(args)

        assert first["changed"] is False
        assert second["changed"] is False
        assert captured_write2 == {}

    def test_omitting_the_leading_space_is_not_idempotent(self, monkeypatch):
        # Demonstrates the trap directly at the module level: supplying server2 without the
        # leading space the BMC actually stores reports changed=true, not a false idempotent match.
        after_write = _synthetic_getntpcfg_body("pool.ntp.org", "192.0.2.10", 1)
        client, captured_write = build_client(get_responses=[REAL_GETNTPCFG_BODY, after_write], write_response=_read_fixture("setntpcfg_write.txt"))
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS, server2="192.0.2.10"))

        assert result["changed"] is True
        assert captured_write["data"]["NEW_NTPSERVER_NAME2"] == "192.0.2.10"


class TestRealWrite:
    def test_write_happens_when_a_value_differs(self, monkeypatch):
        after_write = _synthetic_getntpcfg_body("pool.ntp.org", "203.0.113.5", 1)
        client, captured_write = build_client(get_responses=[REAL_GETNTPCFG_BODY, after_write])
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS, server2="203.0.113.5"))

        assert result["changed"] is True
        assert captured_write["method"] == "POST"
        assert captured_write["data"]["NEW_NTPSERVER_NAME2"] == "203.0.113.5"
        assert result["observed"]["server2"] == "203.0.113.5"

    def test_unrelated_fields_are_echoed_back_unchanged_on_a_write(self, monkeypatch):
        # Matches the one real capture's own shape: a save action that only toggled ISNTPENABLE
        # still resubmitted both server fields unchanged -- this module does the same for the
        # reverse case (a server change resubmitting the unchanged enable state).
        after_write = _synthetic_getntpcfg_body("pool.ntp.org", "203.0.113.5", 1)
        client, captured_write = build_client(get_responses=[REAL_GETNTPCFG_BODY, after_write])
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)

        _run_ok(dict(BASE_ARGS, server2="203.0.113.5"))

        assert captured_write["data"]["NEW_NTPSERVER_NAME1"] == REAL_SERVER1
        assert captured_write["data"]["OLD_NTPSERVER_NAME1"] == REAL_SERVER1
        assert captured_write["data"]["ISNTPENABLE"] == "1"  # current enabled=True, carried forward

    def test_old_ntpserver_name1_is_sent_on_a_real_write(self, monkeypatch):
        after_write = _synthetic_getntpcfg_body("pool.ntp.org", "203.0.113.5", 1)
        client, captured_write = build_client(get_responses=[REAL_GETNTPCFG_BODY, after_write])
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)

        _run_ok(dict(BASE_ARGS, server2="203.0.113.5"))

        assert captured_write["data"]["OLD_NTPSERVER_NAME1"] == REAL_SERVER1

    def test_no_old_ntpserver_name2_is_sent_on_a_real_write(self, monkeypatch):
        after_write = _synthetic_getntpcfg_body("pool.ntp.org", "203.0.113.5", 1)
        client, captured_write = build_client(get_responses=[REAL_GETNTPCFG_BODY, after_write])
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)

        _run_ok(dict(BASE_ARGS, server2="203.0.113.5"))

        assert "OLD_NTPSERVER_NAME2" not in captured_write["data"]

    def test_prior_state_is_returned_alongside_the_new_state(self, monkeypatch):
        after_write = _synthetic_getntpcfg_body("pool.ntp.org", "203.0.113.5", 1)
        client, _captured_write = build_client(get_responses=[REAL_GETNTPCFG_BODY, after_write])
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS, server2="203.0.113.5"))

        assert result["previous_state"] == {
            "server1": REAL_SERVER1,
            "server2": REAL_SERVER2,
            "enabled": True,
            "ntp_status_raw": 1,
        }
        assert result["desired_state"] == {"server1": REAL_SERVER1, "server2": "203.0.113.5", "enabled": True}
        assert result["observed"]["server2"] == "203.0.113.5"

    def test_operation_receipt_carries_previous_desired_and_observed(self, monkeypatch):
        after_write = _synthetic_getntpcfg_body("pool.ntp.org", "203.0.113.5", 1)
        client, _captured_write = build_client(get_responses=[REAL_GETNTPCFG_BODY, after_write])
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS, server2="203.0.113.5"))

        operation = result["operation"]
        assert operation["schema"] == "asmb8-ikvm-operation/v1"
        assert operation["changed"] is True
        assert operation["previous"]["server2"] == REAL_SERVER2
        assert operation["desired"]["server2"] == "203.0.113.5"
        assert operation["observed"]["server2"] == "203.0.113.5"


class TestCheckMode:
    def test_check_mode_never_writes(self, monkeypatch):
        client, captured_write = build_client(get_responses=[REAL_GETNTPCFG_BODY])
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS, server2="203.0.113.5", _ansible_check_mode=True))

        assert result["changed"] is True  # correctly predicts a change would happen
        assert captured_write == {}  # but setntpcfg.asp was never called

    def test_check_mode_still_reads_the_current_state(self, monkeypatch):
        client, _captured_write = build_client(get_responses=[REAL_GETNTPCFG_BODY])
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS, server2="203.0.113.5", _ansible_check_mode=True))

        assert result["previous_state"]["server2"] == REAL_SERVER2
        assert result["desired_state"]["server2"] == "203.0.113.5"

    def test_check_mode_on_an_already_converged_state_reports_no_change(self, monkeypatch):
        client, captured_write = build_client(get_responses=[REAL_GETNTPCFG_BODY])
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS, server1=REAL_SERVER1, server2=REAL_SERVER2, enabled=True, _ansible_check_mode=True))

        assert result["changed"] is False
        assert captured_write == {}

    def test_check_mode_observed_equals_previous_state(self, monkeypatch):
        client, _captured_write = build_client(get_responses=[REAL_GETNTPCFG_BODY])
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)

        result = _run_ok(dict(BASE_ARGS, server2="203.0.113.5", _ansible_check_mode=True))

        assert result["observed"] == result["previous_state"]


class TestErrorHandling:
    def test_nonzero_hapi_status_on_write_raises_remote_operation_error(self, monkeypatch):
        client, _captured_write = build_client(
            get_responses=[REAL_GETNTPCFG_BODY],
            write_response=_synthetic_write_failure_body(1),
        )
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)

        result = _run_fail(dict(BASE_ARGS, server2="203.0.113.5"))

        assert result["error_class"] == "remote_operation"

    def test_login_failure_fails_the_whole_module(self, monkeypatch):
        client, _captured_write = build_client(get_responses=[REAL_GETNTPCFG_BODY], login_ok=False)
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)

        result = _run_fail(dict(BASE_ARGS, server2="203.0.113.5"))

        assert result["error_class"] == "authentication"

    def test_missing_requests_dependency_is_fatal(self, monkeypatch):
        monkeypatch.setattr(asmb8_ntp, "HAS_REQUESTS", False)
        monkeypatch.setattr(asmb8_ntp, "REQUESTS_IMPORT_ERROR", "No module named 'requests'")
        result = _run_fail(dict(BASE_ARGS))
        assert "requests" in result["msg"]

    def test_missing_ntp_status_with_an_unrelated_change_refuses_to_guess(self, monkeypatch):
        # current["enabled"] is None (NTP_STATUS absent) and `enabled` was never given, so this
        # module must refuse to invent an ISNTPENABLE value rather than risk clobbering it as a
        # side effect of an unrelated server change -- see main()'s guard and DOCUMENTATION.
        ambiguous = _synthetic_getntpcfg_body("pool.ntp.org", " 192.0.2.10", None)
        client, captured_write = build_client(get_responses=[ambiguous])
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)

        result = _run_fail(dict(BASE_ARGS, server2="203.0.113.5"))

        assert result["error_class"] == "protocol"
        assert captured_write == {}


class TestNoCredentialLeakage:
    def test_password_never_appears_in_a_failure_result(self, monkeypatch):
        def _raise(_params):
            raise AuthenticationError(f"rejected password={PASSWORD}", endpoint="198.51.100.10:443", operation="login", secrets=PASSWORD)

        monkeypatch.setattr(asmb8_ntp, "build_asp_client", _raise)
        result = _run_fail(dict(BASE_ARGS, server2="203.0.113.5"))
        assert PASSWORD not in json.dumps(result)
        assert "[REDACTED]" in result["msg"]

    def test_password_never_appears_in_a_successful_result(self, monkeypatch):
        client, _captured_write = build_client(get_responses=[REAL_GETNTPCFG_BODY])
        monkeypatch.setattr(asmb8_ntp, "build_asp_client", lambda params: client)
        result = _run_ok(dict(BASE_ARGS, server1=REAL_SERVER1, server2=REAL_SERVER2, enabled=True))
        assert PASSWORD not in json.dumps(result)
