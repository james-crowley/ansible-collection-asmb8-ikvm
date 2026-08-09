# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for tests/hardware/redact-evidence.py.

Every fixture value here is deliberately, obviously fake: RFC 5737 TEST-NET-1
(``192.0.2.0/24``), RFC 3849 documentation IPv6 (``2001:db8::/32``), the RFC 7042
documentation MAC block, and ``.invalid`` domains reserved by RFC 2606. No real
lab/board value belongs in this file -- a fixture is committed, and a committed
real value is the exact leak the script under test exists to prevent.

Two properties matter equally: the script has to redact, and it has to leave
the diagnostic content alone. Evidence scrubbed into uselessness is evidence
nobody keeps, which is how a redaction step gets deleted a release later.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "hardware" / "redact-evidence.py"


def _load_module() -> Any:
    """Import the script by path.

    It is named with a hyphen and carries no shebang on purpose (ansible-test's
    `shebang` sanity test rejects a non-module shebang inside a collection), so
    it is not importable under its own name.
    """
    spec = importlib.util.spec_from_file_location("redact_evidence", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


redact_evidence = _load_module()


@pytest.fixture
def redactor() -> Any:
    return redact_evidence.Redactor()


# --- fixture data ------------------------------------------------------------

#: A structurally faithful stand-in for what these playbooks write, with every
#: redactable category present alongside the fields that must survive.
EVIDENCE: dict[str, Any] = {
    "ipmi": {
        "power_state": {"powerstate": "on"},
        "boot_device": {"bootdev": "optical", "persistent": False, "uefimode": False},
        "mc_info": "AMI MegaRAC",
    },
    "services": {
        "kvm": {"known": True, "on_demand": True, "enabled": True, "reachable": {"nonsecure": {"port": 7578, "reachable": False}, "secure": None}},
    },
    "operation": {
        "schema": "asmb8-ikvm-operation/v1",
        "action": "asmb8_media.attach",
        "endpoint": "192.0.2.10:5120",
        "changed": True,
        "error_class": None,
        "tls_peer_fingerprint": ":".join(["ab"] * 32),
        "observed": {
            "session_id": "9f5c2b1a-0000-0000-0000-000000000001",
            "image": "/home/jane/lab/asmb8-test.iso",
            "image_digest": "ab" * 32,
            "bytes_read": 4096,
            "sectors_served": 2,
        },
    },
    "network": {
        "ip_address": "192.0.2.10",
        "secondary": "2001:db8::35",
        "mac_address": "00:00:5e:00:53:01",
        "resolved_from": "board.example.invalid",
    },
    "host": "asmb8-fixture-1",
    "username": "admin",
    "diagnostic": "connect to 192.0.2.10 failed; see tests/hardware/README.md for the recovery path",
    "note": "session_state is a live read; see this playbook header.",
}

#: Values that carry the diagnostic weight of the evidence. Redacting any of
#: these would be a regression: they say what the firmware did, not who it is.
#:
#: Deliberately excludes "asmb8_media.attach" (operation.action -- exempt by
#: KEY inside redact_value, not a value that survives redact_text() with no
#: key context at all; see test_operation_action_is_never_redacted below,
#: which is the correct place to pin that) and "admin" (operation.username in
#: EVIDENCE below is intentionally redacted by key, exactly as
#: test_username_key_variants_are_redacted already covers).
PRESERVED_VALUES: tuple[Any, ...] = ("on", "optical", "AMI MegaRAC", "asmb8-ikvm-operation/v1")


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _all_strings(v)]
    if isinstance(value, list):
        return [s for item in value for s in _all_strings(item)]
    return [value] if isinstance(value, str) else []


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shape(item) for item in value]
    return type(value).__name__


# --- category coverage -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("192.0.2.10", "ipv4"),
        ("2001:db8::35", "ipv6"),
        ("2001:0db8:0000:0000:0000:0000:0000:0035", "ipv6"),
        ("fe80::1", "ipv6"),
        ("00:00:5e:00:53:01", "mac"),
        ("00-00-5e-00-53-01", "mac"),
        ("00:00:5E:00:53:01", "mac"),
        ("9f5c2b1a-0000-0000-0000-000000000001", "uuid"),
        ("9F5C2B1A-0000-0000-0000-000000000001", "uuid"),
        ("9f5c2b1a000000000000000000000001", "uuid"),
        (":".join(["ab"] * 32), "fingerprint"),
        ("ab" * 32, "digest"),
        ("mgmt.lab.example.invalid", "fqdn"),
        ("asmb8-fixture-1.example.invalid", "fqdn"),
    ],
)
def test_every_category_is_redacted(redactor: Any, text: str, category: str) -> None:
    result = redactor.redact_text(text)
    assert result == f"<redacted-{category}-1>", f"{text!r} was not redacted as {category}"
    assert redactor.distinct == {category: 1}


def test_a_bare_hostname_is_redacted_by_key(redactor: Any) -> None:
    """A bare ASMB8 hostname matches no pattern, so the key is what identifies it."""
    assert redactor.redact_value({"host": "asmb8-fixture-1"}) == {"host": "<redacted-hostname-1>"}


@pytest.mark.parametrize("key", ["host", "hostname", "asmb8_host"])
def test_hostname_key_variants_are_redacted(redactor: Any, key: str) -> None:
    assert redactor.redact_value({key: "asmb8-fixture-1"}) == {key: "<redacted-hostname-1>"}


@pytest.mark.parametrize("key", ["username", "user", "asmb8_username", "client_username"])
def test_username_key_variants_are_redacted(redactor: Any, key: str) -> None:
    assert redactor.redact_value({key: "admin"}) == {key: "<redacted-username-1>"}


def test_a_bare_label_under_a_non_identifying_key_is_left_alone(redactor: Any) -> None:
    assert redactor.redact_value({"powerstate": "on"}) == {"powerstate": "on"}


# --- local filesystem paths, by key -------------------------------------------


@pytest.mark.parametrize("key", ["path", "image", "image_path", "iso_path", "output_path", "runtime_dir", "state_file"])
def test_path_keys_are_redacted(redactor: Any, key: str) -> None:
    assert redactor.redact_value({key: "/home/jane/lab/asmb8-test.iso"}) == {key: "<redacted-path-1>"}


def test_a_path_appearing_under_a_non_path_key_is_left_alone(redactor: Any) -> None:
    assert redactor.redact_value({"note": "/home/jane/lab/asmb8-test.iso"}) == {"note": "/home/jane/lab/asmb8-test.iso"}


def test_the_same_path_in_two_slots_is_recognisably_the_same_file(redactor: Any) -> None:
    result = redactor.redact_value({"image": "/home/jane/shared.iso", "output_path": "/home/jane/shared.iso"})
    assert result == {"image": "<redacted-path-1>", "output_path": "<redacted-path-1>"}


# --- exempt keys: action/note survive verbatim --------------------------------


@pytest.mark.parametrize(
    "action",
    ["asmb8_media.attach", "asmb8_media.detach", "asmb8_boot", "asmb8_power.reset", "asmb8_console.capture", "asmb8_redirection.report", "get_facts"],
)
def test_operation_action_is_never_redacted(redactor: Any, action: str) -> None:
    """Every action a module can write must survive verbatim.

    Parameterised over the real values rather than one example, because a
    dotted lowercase token is exactly what the fqdn pattern matches, and a
    single-value test would pass on `get_facts` alone while missing it.
    """
    result = redactor.redact_value({"action": action})
    assert result["action"] == action, f"{action} was redacted; the artifact no longer says what it recorded"


def test_action_exemption_does_not_leak_a_real_hostname_elsewhere(redactor: Any) -> None:
    """The positive control: exempting `action` must not disarm redaction generally."""
    result = redactor.redact_value({"action": "asmb8_media.attach", "host": "board1", "endpoint": "10.1.2.3:5120"})
    assert result["action"] == "asmb8_media.attach"
    assert result["host"] == "<redacted-hostname-1>"
    assert "10.1.2.3" not in str(result["endpoint"])


def test_no_hardware_note_is_templated() -> None:
    """No `note` in tests/hardware/*.yml may contain a Jinja expression.

    Deliberately checks the source playbooks rather than the redactor: the
    redactor cannot tell an authored note from an interpolated one, so the
    invariant has to be enforced where notes are written. This is the
    condition the `note` exemption in _EXEMPT_KEYS depends on.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / "hardware"
    playbooks = sorted(root.glob("*.yml"))
    assert playbooks, f"found no hardware playbooks under {root}; this test would pass vacuously"

    offenders = []
    for pb in playbooks:
        text = pb.read_text(encoding="utf-8")
        for m in re.finditer(r"'note':\s*(.{0,600}?)(?:\}|\n\s{0,8}[a-z_]+:)", text, re.S):
            if "{{" in m.group(1):
                offenders.append(f"{pb.name}: {m.group(1)[:80]!r}")

    assert not offenders, (
        "a hardware playbook note now interpolates a value, but `note` is in "
        "_EXEMPT_KEYS and is published verbatim. Either remove the interpolation or "
        "remove the exemption:\n  " + "\n  ".join(offenders)
    )


def test_keys_are_never_rewritten(redactor: Any) -> None:
    result = redactor.redact_value({"192.0.2.10": {"00:00:5e:00:53:01": 1}})
    assert list(result) == ["192.0.2.10"]
    assert list(result["192.0.2.10"]) == ["00:00:5e:00:53:01"]


# --- stable pseudonyms ---------------------------------------------------------


def test_the_same_value_always_gets_the_same_token(redactor: Any) -> None:
    first = redactor.redact_text("192.0.2.10")
    second = redactor.redact_text("192.0.2.10")
    assert first == second == "<redacted-ipv4-1>"
    assert redactor.distinct["ipv4"] == 1
    assert redactor.occurrences["ipv4"] == 2


def test_different_values_get_different_tokens(redactor: Any) -> None:
    assert redactor.redact_text("192.0.2.10") == "<redacted-ipv4-1>"
    assert redactor.redact_text("192.0.2.11") == "<redacted-ipv4-2>"
    assert redactor.distinct["ipv4"] == 2


def test_tokens_are_assigned_in_first_seen_order_not_derived_from_the_value(redactor: Any) -> None:
    """A hash would be a reversible oracle -- an IPv4 on a known /24 is 254 guesses."""
    reversed_redactor = redact_evidence.Redactor()
    assert redactor.redact_text("192.0.2.10 then 192.0.2.11") == "<redacted-ipv4-1> then <redacted-ipv4-2>"
    assert reversed_redactor.redact_text("192.0.2.11 then 192.0.2.10") == "<redacted-ipv4-1> then <redacted-ipv4-2>"


def test_correlation_survives_across_a_whole_structure(redactor: Any) -> None:
    """One board's address in three places is still recognisably one board."""
    result = redactor.redact_value(
        {
            "network": {"ip_address": "192.0.2.10"},
            "operation": {"endpoint": "192.0.2.10:5120"},
            "invocation": {"module_args": {"host": "192.0.2.10"}},
        }
    )
    assert result["network"]["ip_address"] == "<redacted-ipv4-1>"
    assert result["operation"]["endpoint"] == "<redacted-ipv4-1>:5120"
    assert result["invocation"]["module_args"]["host"] == "<redacted-ipv4-1>"
    assert redactor.distinct["ipv4"] == 1


def test_the_same_value_under_two_categories_does_not_collide(redactor: Any) -> None:
    result = redactor.redact_value({"host": "192.0.2.10", "hostname": "asmb8-fixture-1"})
    assert result == {"host": "<redacted-ipv4-1>", "hostname": "<redacted-hostname-1>"}


# --- embedded values -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("connect to 192.0.2.10 failed", "connect to <redacted-ipv4-1> failed"),
        ("endpoint 192.0.2.10:5120 timed out", "endpoint <redacted-ipv4-1>:5120 timed out"),
        ("[192.0.2.10]", "[<redacted-ipv4-1>]"),
        ('{"peer": "192.0.2.10"}', '{"peer": "<redacted-ipv4-1>"}'),
        ("port 0 mac 00:00:5e:00:53:01 up", "port 0 mac <redacted-mac-1> up"),
        ("resolved board.example.invalid to an address", "resolved <redacted-fqdn-1> to an address"),
    ],
)
def test_a_value_embedded_mid_string_is_caught(redactor: Any, text: str, expected: str) -> None:
    assert redactor.redact_text(text) == expected


# --- preservation ---------------------------------------------------------------


@pytest.mark.parametrize("value", PRESERVED_VALUES)
def test_diagnostic_values_are_left_exactly_as_they_are(redactor: Any, value: str) -> None:
    assert redactor.redact_text(value) == value
    assert redactor.distinct == {}


@pytest.mark.parametrize(
    "value",
    [
        "see tests/hardware/README.md for the recovery path",
        "python3 tests/hardware/redact-evidence.py",
        "wired into .circleci/config.yml",
        "http://www.w3.org/2003/05/soap-envelope",
    ],
)
def test_filenames_and_standards_domains_are_not_redacted(redactor: Any, value: str) -> None:
    assert redactor.redact_text(value) == value


def test_numeric_and_boolean_leaves_are_returned_unchanged(redactor: Any) -> None:
    payload = {"bytes_read": 4096, "changed": True, "session_id": None, "error_class": None, "sectors_served": 2}
    assert redactor.redact_value(payload) == payload


def test_a_full_evidence_document_keeps_every_preserved_value(redactor: Any) -> None:
    result = redactor.redact_value(EVIDENCE)
    strings = _all_strings(result)
    for value in PRESERVED_VALUES:
        assert value in strings, f"{value!r} should have survived redaction"
    assert result["ipmi"]["power_state"] == EVIDENCE["ipmi"]["power_state"]
    assert result["ipmi"]["boot_device"] == EVIDENCE["ipmi"]["boot_device"]
    assert result["services"]["kvm"]["reachable"]["nonsecure"]["port"] == 7578


def test_a_full_evidence_document_leaves_no_identifying_value_behind(redactor: Any) -> None:
    serialised = json.dumps(redactor.redact_value(EVIDENCE))
    for leaked in (
        "192.0.2.10",
        "2001:db8::35",
        "00:00:5e:00:53:01",
        "9f5c2b1a-0000-0000-0000-000000000001",
        "asmb8-fixture-1",
        "/home/jane/lab/asmb8-test.iso",
        "admin",
        ":".join(["ab"] * 32),
    ):
        assert leaked not in serialised, f"{leaked!r} survived redaction"


# --- structure -------------------------------------------------------------------


def test_the_json_structure_is_identical_before_and_after(redactor: Any) -> None:
    result = redactor.redact_value(EVIDENCE)
    assert _shape(result) == _shape(EVIDENCE)


def test_nested_lists_of_dicts_are_walked(redactor: Any) -> None:
    result = redactor.redact_value({"targets": [{"ip": "192.0.2.10"}, {"ip": "192.0.2.11"}], "peers": [["192.0.2.10"]]})
    assert result["targets"] == [{"ip": "<redacted-ipv4-1>"}, {"ip": "<redacted-ipv4-2>"}]
    assert result["peers"] == [["<redacted-ipv4-1>"]]


# --- file handling ----------------------------------------------------------------


def _write_evidence(directory: Path, name: str = "asmb8-board-observe.json") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(EVIDENCE, indent=4) + "\n", encoding="utf-8")
    return path


def test_redact_file_rewrites_in_place(tmp_path: Path, redactor: Any) -> None:
    path = _write_evidence(tmp_path / "output")
    assert redact_evidence.redact_file(path, redactor) == "json"
    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert _shape(rewritten) == _shape(EVIDENCE)
    assert rewritten["network"]["ip_address"] == "<redacted-ipv4-1>"


def test_redacting_twice_is_a_no_op(tmp_path: Path) -> None:
    output = tmp_path / "output"
    path = _write_evidence(output)

    assert redact_evidence.main(["redact-evidence.py", str(output)]) == 0
    after_first = path.read_text(encoding="utf-8")

    second = redact_evidence.Redactor()
    redact_evidence.redact_file(path, second)
    assert path.read_text(encoding="utf-8") == after_first
    assert second.distinct == {}


def test_main_walks_subdirectories_and_shares_tokens_across_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "output"
    first = _write_evidence(output, "asmb8-board-observe.json")
    second = _write_evidence(output / "nested", "asmb8-board-media_attach.json")

    assert redact_evidence.main(["redact-evidence.py", str(output)]) == 0

    first_doc = json.loads(first.read_text(encoding="utf-8"))
    second_doc = json.loads(second.read_text(encoding="utf-8"))
    assert first_doc["network"]["ip_address"] == second_doc["network"]["ip_address"]

    out = capsys.readouterr().out
    assert "2 JSON file(s) rewritten" in out


def test_main_prints_a_per_category_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "output"
    _write_evidence(output)

    assert redact_evidence.main(["redact-evidence.py", str(output)]) == 0

    out = capsys.readouterr().out
    assert "1 JSON file(s) rewritten" in out
    for category in ("ipv4", "ipv6", "mac", "uuid", "fingerprint", "digest", "fqdn", "hostname", "username", "path"):
        assert f"  {category}: " in out, f"summary is missing a {category} line"


def test_main_succeeds_when_there_is_no_evidence_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The CI step runs with `when: always`, including after a job that wrote nothing."""
    assert redact_evidence.main(["redact-evidence.py", str(tmp_path / "absent")]) == 0
    assert "nothing to redact" in capsys.readouterr().out


def test_unparseable_json_is_redacted_as_raw_text(tmp_path: Path, redactor: Any) -> None:
    """A file truncated mid-write still has to be safe to publish."""
    output = tmp_path / "output"
    output.mkdir()
    path = output / "truncated.json"
    path.write_text('{"network": {"ip_address": "192.0.2.10", "mac_address": "00:00:5e:00:5', encoding="utf-8")

    assert redact_evidence.redact_file(path, redactor) == "text"
    content = path.read_text(encoding="utf-8")
    assert "192.0.2.10" not in content
    assert "<redacted-ipv4-1>" in content


def test_non_json_files_are_reported_rather_than_silently_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "output"
    _write_evidence(output)
    (output / "console.log").write_text("192.0.2.10\n", encoding="utf-8")

    assert redact_evidence.main(["redact-evidence.py", str(output)]) == 0

    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "console.log" in out
