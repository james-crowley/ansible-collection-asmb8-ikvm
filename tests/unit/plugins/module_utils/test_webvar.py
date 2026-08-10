# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for the ``.asp`` WEBVAR/JSONVAR parser.

The corpus-wide test below is the point of this file: every one of the 54
real, redacted response bodies in ``tests/unit/fixtures/asp/`` (see that
directory's own ``README.md`` for capture provenance) is parsed and checked,
so a claim like "this parser handles every endpoint shape this board's web
UI actually returned" is sourced to real bytes rather than to a hand-picked
sample that happens to be convenient.

``create.txt`` is deliberately excluded from that corpus-wide "parses
successfully" check and given its own test instead: its own redaction pass
corrupted the ``SESSION_COOKIE`` field (see ``webvar.py``'s module docstring
and ``tests/unit/fixtures/asp/README.md``), leaving genuinely invalid
syntax. That makes it this suite's one real-world malformed-input fixture,
used alongside the synthetic ones below.

None of these tests open a socket or talk to any BMC -- every fixture is a
static file already on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import ProtocolError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.webvar import (
    WebVarResponse,
    parse_webvar,
)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "asp"

#: The one fixture known to be naturally malformed -- see module docstring.
_MALFORMED_FIXTURE_NAME = "create.txt"

ALL_FIXTURES = sorted(FIXTURES_DIR.glob("*.txt"))
WELL_FORMED_FIXTURES = [path for path in ALL_FIXTURES if path.name != _MALFORMED_FIXTURE_NAME]

#: Sanity check on the corpus itself, not on the parser: if this ever fails,
#: the fixture directory has drifted (files added/removed) without the count
#: below -- and the docstrings throughout webvar.py that cite "54" and "49
#: of 54"/"5 of 54" -- being revisited to match.
_EXPECTED_FIXTURE_COUNT = 54


def _read(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_corpus_has_the_expected_fixture_count():
    assert len(ALL_FIXTURES) == _EXPECTED_FIXTURE_COUNT
    assert len(WELL_FORMED_FIXTURES) == _EXPECTED_FIXTURE_COUNT - 1


class TestCorpusWide:
    """Every real, captured response in the corpus, parsed and checked.

    This is deliberately a thin, structural assertion (not per-field
    checks -- those belong in the focused tests below, each tied to a
    specific fixture and a specific claim). The point here is coverage: 53
    real bodies, all shapes this board's web UI is known to produce, all
    parsing without error.
    """

    @pytest.mark.parametrize("fixture_path", WELL_FORMED_FIXTURES, ids=lambda p: p.name)
    def test_every_well_formed_fixture_parses(self, fixture_path: Path):
        result = parse_webvar(fixture_path.read_text(encoding="utf-8"))

        assert isinstance(result, WebVarResponse)
        assert result.variable_name
        assert result.struct_name
        assert isinstance(result.records, list)
        assert all(isinstance(record, dict) for record in result.records)
        assert isinstance(result.hapi_status, int)

    @pytest.mark.parametrize("fixture_path", WELL_FORMED_FIXTURES, ids=lambda p: p.name)
    def test_variable_name_matches_struct_name(self, fixture_path: Path):
        """Observed identical in all 54 corpus samples -- see WebVarResponse's
        docstring on why this parser records both fields but does not enforce
        the equality itself. This test documents the observation without
        baking it into the parser as a rule a future, differently-behaved
        firmware revision would then be unable to report honestly."""
        result = parse_webvar(fixture_path.read_text(encoding="utf-8"))
        assert result.variable_name == result.struct_name

    def test_the_one_malformed_fixture_raises_protocol_error(self):
        """``create.txt``'s own redaction pass corrupted its SESSION_COOKIE
        field into invalid syntax -- see module docstring. This is the
        corpus's one real (if accidental) example of malformed input; the
        parser must raise, not return a half-parsed result."""
        with pytest.raises(ProtocolError):
            parse_webvar(_read(_MALFORMED_FIXTURE_NAME))


class TestSentinelHandling:
    def test_trailing_sentinel_is_dropped(self):
        """getdatetime.txt's array is `[ {...one record...}, {} ]` -- the
        trailing `{}` must not appear in the parsed records."""
        result = parse_webvar(_read("getdatetime.txt"))
        assert len(result.records) == 1
        assert result.records[0]["TIMEZONE"] == "GMT+8"
        assert {} not in result.records

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "getfruinfo.txt",
            "getfwalliprule.txt",
            "getfwallportrule.txt",
            "getselentries.txt",
            "getvideoinfo.txt",
        ],
    )
    def test_sentinel_only_array_yields_a_genuinely_empty_record_list(self, fixture_name):
        """These 5 fixtures' entire array is `[ {} ]` -- the sentinel alone,
        with no real record before it. After sentinel removal, `records`
        must be an honest empty list, not a list containing one meaningless
        `{}`."""
        result = parse_webvar(_read(fixture_name))
        assert result.records == []


class TestTypesPreserved:
    def test_numeric_and_string_values_keep_their_python_types(self):
        result = parse_webvar(_read("getdatetime.txt"))
        record = result.records[0]
        assert record["SECONDS"] == 1786409408
        assert isinstance(record["SECONDS"], int)
        assert record["TIMEZONE"] == "GMT+8"
        assert isinstance(record["TIMEZONE"], str)

    def test_firmware_revision_is_read_as_a_plain_integer_not_bcd_decoded(self):
        """getfwinfo.txt reports FirmwareRevision2=20 -- 20 decimal is 0x14,
        which is how this board encodes "1.14" (BCD). This parser is
        generic and must NOT special-case that field: it reads 20 as the
        plain integer 20, and it is docs/protocol-notes.md's job, not this
        parser's, to say what that value means for THIS field. This test
        pins the parser's side of that division of labour."""
        result = parse_webvar(_read("getfwinfo.txt"))
        record = result.records[0]
        assert record["FirmwareRevision1"] == 1
        assert record["FirmwareRevision2"] == 20
        assert record["CompletionCode"] == 0

    def test_service_timeouts_are_read_as_plain_integers(self):
        """getallservicescfg.txt reports SERVICE_TIMEOUT=4294967295
        (0xFFFFFFFF) for cd-media/fd-media/hd-media, and 1800/600 for
        web/kvm/ssh -- see docs/protocol-notes.md for what that means
        operationally. This test only pins that the parser hands back the
        exact integers the BMC reported, with no reinterpretation."""
        result = parse_webvar(_read("getallservicescfg.txt"))
        timeouts = {record["SERVICENAME"]: record["SERVICE_TIMEOUT"] for record in result.records}
        assert timeouts["web"] == 1800
        assert timeouts["kvm"] == 1800
        assert timeouts["cd-media"] == 4294967295
        assert timeouts["fd-media"] == 4294967295
        assert timeouts["hd-media"] == 4294967295
        assert timeouts["ssh"] == 600


class TestQuoteHandling:
    def test_brackets_inside_a_quoted_string_do_not_break_the_array_scan(self):
        """getauditlog.txt's AUDIT_LOG value contains a literal '[2615
        INFO]' -- real corpus evidence that the array-closing scan must be
        quote-aware, not a naive bracket-depth counter. See
        webvar.py's _scan_matching_bracket docstring."""
        result = parse_webvar(_read("getauditlog.txt"))
        assert len(result.records) == 1
        assert "[2615 INFO]" in result.records[0]["AUDIT_LOG"]

    def test_escaped_quote_inside_string_value_is_preserved(self):
        """No fixture in this corpus contains a value with an embedded
        single quote -- checked directly, zero occurrences. This is
        therefore a SYNTHETIC body, not a captured one, proving the parser
        does not corrupt (or fail on) a backslash-escaped quote if this
        board's firmware ever renders one."""
        body = (
            "//Dynamic Data Begin\n"
            " WEBVAR_JSONVAR_SYNTHETIC = \n"
            " { \n"
            " WEBVAR_STRUCTNAME_SYNTHETIC : \n"
            " [ \n"
            " { 'NOTE' : 'it\\'s a test' },  {} ],  \n"
            " HAPI_STATUS:0 }; \n"
            "//Dynamic data end\n"
        )
        result = parse_webvar(body)
        assert result.records == [{"NOTE": "it's a test"}]


class TestBannerTolerance:
    def test_a_leading_comment_banner_is_ignored(self):
        """No fixture in this corpus is actually preceded by the AMI
        copyright banner this parser was originally briefed to expect --
        see webvar.py's module docstring for that correction. This test
        prepends a SYNTHETIC banner (not captured) to a real fixture body
        and checks the parse result is identical either way, since the
        parser locates WEBVAR_JSONVAR_<NAME> with re.search rather than
        assuming it starts at offset 0."""
        synthetic_banner = (
            "//;--------------------------------------------------------\n"
            "//; American Megatrends Inc.\n"
            "//; Copyright (c) 1985-2018, American Megatrends Inc.\n"
            "//; All Rights Reserved.\n"
            "//;\n"
            "//; This is a SYNTHETIC banner for this test only -- no fixture\n"
            "//; in tests/unit/fixtures/asp/ actually contains one.\n"
            "//;--------------------------------------------------------\n"
        )
        plain_body = _read("getdatetime.txt")
        banner_body = synthetic_banner + plain_body

        assert parse_webvar(banner_body) == parse_webvar(plain_body)


class TestMalformedInput:
    def test_empty_body_raises(self):
        with pytest.raises(ProtocolError):
            parse_webvar("")

    def test_whitespace_only_body_raises(self):
        with pytest.raises(ProtocolError):
            parse_webvar("   \n\t  ")

    def test_completely_unrelated_text_raises(self):
        with pytest.raises(ProtocolError):
            parse_webvar("<html><body>not a webvar response at all</body></html>")

    def test_missing_structname_raises(self):
        with pytest.raises(ProtocolError):
            parse_webvar(" WEBVAR_JSONVAR_FOO = { HAPI_STATUS:0 }; ")

    def test_structname_not_followed_by_array_raises(self):
        with pytest.raises(ProtocolError):
            parse_webvar(" WEBVAR_JSONVAR_FOO = { WEBVAR_STRUCTNAME_FOO : 42, HAPI_STATUS:0 }; ")

    def test_unterminated_array_raises(self):
        with pytest.raises(ProtocolError):
            parse_webvar(" WEBVAR_JSONVAR_FOO = { WEBVAR_STRUCTNAME_FOO : [ { 'A' : 1 } , HAPI_STATUS:0 }; ")

    def test_array_containing_invalid_python_syntax_raises(self):
        """Mirrors the real shape of create.txt's corruption (a bare,
        unquoted key immediately followed by a string with no colon) but as
        a synthetic, minimal case rather than the full fixture."""
        with pytest.raises(ProtocolError):
            parse_webvar(" WEBVAR_JSONVAR_FOO = { WEBVAR_STRUCTNAME_FOO : [ { BROKEN'value' },  {} ],  HAPI_STATUS:0 }; ")

    def test_array_of_non_objects_raises(self):
        with pytest.raises(ProtocolError):
            parse_webvar(" WEBVAR_JSONVAR_FOO = { WEBVAR_STRUCTNAME_FOO : [ 1, 2, 3 ],  HAPI_STATUS:0 }; ")

    def test_missing_hapi_status_raises(self):
        with pytest.raises(ProtocolError):
            parse_webvar(" WEBVAR_JSONVAR_FOO = { WEBVAR_STRUCTNAME_FOO : [ { 'A' : 1 },  {} ] }; ")

    def test_non_integer_hapi_status_raises(self):
        with pytest.raises(ProtocolError):
            parse_webvar(" WEBVAR_JSONVAR_FOO = { WEBVAR_STRUCTNAME_FOO : [ { 'A' : 1 },  {} ],  HAPI_STATUS:oops }; ")

    def test_non_string_body_raises(self):
        with pytest.raises(ProtocolError):
            parse_webvar(None)  # type: ignore[arg-type]
