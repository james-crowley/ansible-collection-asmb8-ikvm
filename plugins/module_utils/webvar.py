# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""A parser for AMI MegaRAC's ``.asp`` WEBVAR/JSONVAR response format.

This is the shape returned by this board's older ``*.asp`` RPC endpoints --
``getdatetime.asp``, ``getallsensors.asp``, ``getfwinfo.asp``, and every
sibling of them -- as distinct from the ``SESSION_COOKIE``/``STOKEN`` login
responses ``asp.py`` already parses with its own narrow regexes, and distinct
from the JNLP document ``asp.py`` parses for the KVM/media session. Nothing
here replaces those; this module exists because several planned modules
(read-only inventory/sensor/config reporting) all need to read this one
recurring shape, and copying a hand-rolled regex into each of them would be
exactly the kind of unsourced, unreviewed drift this collection's own
CONTRIBUTING.md warns against.

AMI has not published a specification for this format, so every claim below
is sourced the same way ``asp.py``'s module docstring sources its own
claims -- to a corpus of real captures, not to inference or to a vendor
document that does not exist:

* "the corpus" / "asp-corpus" = 54 real ``.asp`` response bodies, one per
  endpoint, captured from this collection's target ASUS ASMB8-iKVM BMC's own
  web UI on 2026-08-10, firmware 1.14 (aux 1.14.2). Addresses were replaced
  with RFC 5737 documentation values, MAC addresses with
  ``00:00:5E:00:53:00``, and session cookies with the literal text
  ``<REDACTED>`` before this module's author (an automated contributor) ever
  read them. Copied verbatim into ``tests/unit/fixtures/asp/`` -- see that
  directory's own ``README`` for the same provenance note next to the actual
  bytes. This module's author made zero network requests of its own to any
  BMC while writing this file; every fact below was checked directly against
  those 54 files with a throwaway script, not assumed.

**The shape, as the corpus actually shows it** (not as a JSON reader would
guess): every one of the 54 samples is::

    //Dynamic Data Begin
     WEBVAR_JSONVAR_<NAME> =
     {
     WEBVAR_STRUCTNAME_<NAME> :
     [
     { 'FIELD' : 'value','OTHER' : 123 },  { ... },  {} ],
     HAPI_STATUS:0 };
    //Dynamic data end

This is a **JavaScript object literal**, not JSON, and ``json.loads`` will
never parse it: every one of the 54 samples uses single-quoted keys and
string values, and zero use double quotes anywhere. The outer object's own
two keys (``WEBVAR_STRUCTNAME_<NAME>`` and ``HAPI_STATUS``) are themselves
*unquoted bare identifiers* -- not even valid as Python dict keys without
quoting -- which is why this parser never tries to run the whole outer object
through :func:`ast.literal_eval` in one call. It extracts the two pieces it
actually needs (the array, and the status integer) by direct scanning/regex
instead, and only hands the extracted **array** to
:func:`ast.literal_eval` -- see :func:`parse_webvar` below. The array's own
contents (single-quoted keys, single-quoted or bare-integer values) are
already syntactically valid Python, so no double-quote rewriting is needed or
performed. **Never use ``eval``** for any of this -- ``ast.literal_eval``
only ever constructs literals (strings, numbers, tuples/lists/dicts/sets of
those), so a hostile or merely-corrupted body cannot make it execute
anything.

**Where this parser's approach earns its keep, with corpus evidence for
each:**

* ``tests/unit/fixtures/asp/getauditlog.txt``'s one record is
  ``{'AUDIT_LOG': 'Aug 11 00:48:53 localhost webgo: [2615 INFO]WEBGUI user
  admin login successfully from 192.0.2.10'}`` -- note the literal ``[`` and
  ``]`` *inside* the quoted string value. A naive "count brackets, ignore
  quoting" scanner would think the array closed early, right there in the
  middle of a log line. :func:`_scan_matching_bracket` tracks quote state
  precisely so bracket characters inside a string never affect the count.
  This is not a hypothetical hardening measure -- it is required by a real
  fixture in this corpus.
* No sample in the corpus contains a single quote *inside* a quoted string
  value (checked directly: zero occurrences of a backslash-escaped quote or
  of any other in-string quote-survival trick anywhere in the 54 files).
  This parser's quote-tracking honours a backslash escape (``\\'``) the same
  way both JavaScript and Python string literals do, which is what would be
  needed if this board's firmware ever renders one -- see
  ``tests/unit/plugins/module_utils/test_webvar.py``'s
  ``test_escaped_quote_inside_string_value_is_preserved`` for a synthetic
  case proving that path works, clearly marked as synthetic because the real
  corpus does not exercise it.
* **The sentinel.** 49 of the 54 samples end their array with a trailing,
  otherwise-fieldless ``{}`` element after one or more real records (e.g.
  ``{'SECONDS': ..., 'TIMEZONE': 'GMT+8'}, {}``); the remaining 5
  (``getfruinfo.txt``, ``getfwalliprule.txt``, ``getfwallportrule.txt``,
  ``getselentries.txt``, ``getvideoinfo.txt``) have **no real records at
  all** -- their array is just ``[ {} ]``, the sentinel alone. Both shapes
  are the same rule applied to a different record count: *if the array's last
  element is the empty dict ``{}``, it is not data and must be dropped.*
  :func:`parse_webvar` drops it unconditionally when present, which turns the
  5 sentinel-only samples into an honestly-empty ``records`` list rather than
  a list containing one meaningless ``{}``. No sample has ``{}`` as a
  *non-final* element or an array that is empty (``[]``) outright, so this
  parser does not have evidence either way for those shapes; it does not
  invent handling for them.
* **No corpus sample contains a value that is itself an object or an array**
  (checked directly: bracket-depth analysis of every sample's array found a
  maximum nesting depth of 1, i.e. record-level only). Field values are
  always a bare integer or a single-quoted string. :func:`parse_webvar`
  does not reject a hypothetical nested value -- ``ast.literal_eval`` would
  happily construct one -- but nothing in this corpus proves what shape a
  nested value would take, so this docstring makes no claim about it either.
* **``HAPI_STATUS`` is 0 in every one of the 54 samples.** There is no
  corpus evidence for what a non-zero status, or a status carried as
  anything other than a bare integer, looks like on this format. This parser
  reads it as a plain integer (:data:`_HAPI_STATUS_RE`) and raises
  :class:`errors.ProtocolError` if that regex does not match -- it does not
  guess at an error encoding it has never seen.
* **The AMI copyright banner the corpus was expected to have, per this
  module's original task brief, does not actually appear in any of the 54
  samples.** Every one of them begins directly with ``//Dynamic Data
  Begin`` -- there is no preceding block of ``//;``-prefixed comment lines in
  any capture. This is a correction, not a design choice: the brief this
  module was built from asserted the banner as a measured characteristic, and
  it does not hold for this corpus. The parser is unaffected either way,
  because it locates ``WEBVAR_JSONVAR_<NAME>`` with :func:`re.search` rather
  than assuming the assignment starts at offset 0, so arbitrary leading text
  (a real banner, or anything else) is tolerated whether or not the corpus
  happens to contain one. ``test_webvar.py`` exercises this with a
  synthetic banner prepended to real fixture bytes, clearly marked as
  synthetic for the same reason as the escaped-quote test above.
* **One fixture is genuinely malformed, and not because of anything this
  board's firmware did.** ``tests/unit/fixtures/asp/create.txt`` (the
  ``/rpc/WEBSES/create.asp`` login response) contains the fragment
  ``{ SESSION_COOKIE'<REDACTED>','BMC_IP_ADDR' : '192.0.2.10', ... }``. The
  real field is normally ``'SESSION_COOKIE' : '<value>'`` (see
  ``asp.py``'s ``_SESSION_COOKIE_RE``, which still expects exactly that
  shape); the corpus's own redaction pass, in the course of blanking the
  session cookie's value, also ate the key's surrounding quotes and its
  colon, leaving behind text that is not valid under any single-quote/
  double-quote convention -- a redaction artifact, not a wire-format
  variant. Rather than special-case recovery for damage introduced by the
  redaction tool rather than by the BMC, :func:`parse_webvar` raises
  :class:`errors.ProtocolError` on it, exactly as it would for any other
  unparseable array. This fixture is this module's one real-world (if
  accidental) example of the "malformed input must raise, not silently
  half-parse" requirement, alongside the synthetic malformed-input cases in
  the test file.
* **No sample exhibits AMI's ``Failure_Login``/session-expired shape.** That
  shape is specific to the ``SESSION_COOKIE`` field's *value* (see
  ``asp.py``'s ``_FAILURE_LOGIN_PREFIX``/``_looks_like_failed_login``), not
  to this array format in general, and ``asp.py`` already owns detecting it
  at the point where it actually matters (immediately after login, before a
  cookie is trusted). This module does not duplicate or reimplement that
  check -- doing so here, against a shape this corpus never shows, would be
  inventing behaviour rather than sourcing it.
* ``getfwinfo.txt``'s one record carries an IPMI-style ``'CompletionCode' :
  0`` field. This parser treats it as an ordinary integer field like any
  other -- it is not a wrapper/envelope status distinct from
  ``HAPI_STATUS``, at least not in any sample this corpus contains evidence
  for, so no special handling is applied to it here. See
  ``docs/protocol-notes.md``'s WEBVAR/JSONVAR section for what that same
  record's ``FirmwareRevision1``/``FirmwareRevision2`` fields mean --
  BCD-encoding of the second field is a fact about that specific field's
  *semantics*, not about how this generic parser reads integers, so it is
  documented there rather than special-cased here.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import ProtocolError

#: Matches the JS assignment that introduces every sample in the corpus,
#: e.g. ``WEBVAR_JSONVAR_GETDATETIME =``. Captures the ``<NAME>`` portion.
#: Deliberately searched for with :func:`re.search`, not anchored to the
#: start of the body -- see this module's docstring on the copyright banner
#: this format was expected to carry but that no corpus sample actually has;
#: searching rather than anchoring means arbitrary leading text is tolerated
#: either way.
_JSONVAR_RE = re.compile(r"WEBVAR_JSONVAR_(\w+)\s*=")

#: Matches the ``WEBVAR_STRUCTNAME_<NAME> :`` that introduces the array in
#: every one of the 54 samples. Searched starting immediately after the
#: JSONVAR match, not from the start of the body, so a record that happened
#: to contain this literal text in a string value could not be mistaken for
#: the real declaration (no sample does this, but nothing rules it out for a
#: firmware revision this corpus has not seen either).
_STRUCTNAME_RE = re.compile(r"WEBVAR_STRUCTNAME_(\w+)\s*:")

#: Matches the bare-integer ``HAPI_STATUS`` field that terminates every one
#: of the 54 samples' outer object, e.g. ``HAPI_STATUS:0``. Every sample
#: reports ``0`` (see this module's docstring); the leading ``-?`` is kept
#: only because IPMI completion/status codes are conventionally signed, not
#: because any sample has shown a negative value.
_HAPI_STATUS_RE = re.compile(r"HAPI_STATUS\s*:\s*(-?\d+)")

#: The sentinel element every array-closing scan checks for. All 54 samples
#: use the empty object literal, never (for example) a dict with an explicit
#: "end of list" marker field -- see this module's docstring.
_SENTINEL: dict[str, Any] = {}


@dataclass(frozen=True, slots=True)
class WebVarResponse:
    """The result of parsing one ``.asp`` WEBVAR/JSONVAR response body.

    ``variable_name`` and ``struct_name`` are kept as two separate fields,
    not collapsed into one, even though every one of the 54 corpus samples
    has them identical (``WEBVAR_JSONVAR_GETDATETIME`` /
    ``WEBVAR_STRUCTNAME_GETDATETIME``, matching in all 54). That equality is
    an *observation* about this corpus, not an invariant this parser
    enforces -- a firmware revision this corpus has not seen may yet report
    them differently, and collapsing the two fields now would silently lose
    the evidence needed to notice that if it ever happens.
    """

    #: The ``<NAME>`` from ``WEBVAR_JSONVAR_<NAME>``, e.g. ``GETDATETIME``.
    variable_name: str
    #: The ``<NAME>`` from ``WEBVAR_STRUCTNAME_<NAME>``. See the class
    #: docstring: equal to ``variable_name`` in every corpus sample, but not
    #: asserted equal by this parser.
    struct_name: str
    #: The array's elements, in order, with the trailing/sole ``{}``
    #: sentinel (see module docstring) removed. Each element is a ``dict``
    #: whose values are ``int`` or ``str`` in every corpus sample; this
    #: parser does not narrow the type further than ``Any``, since a
    #: nested object/array value is not something this corpus has evidence
    #: for or against.
    records: list[dict[str, Any]]
    #: The integer from ``HAPI_STATUS:<N>``. ``0`` in every corpus sample --
    #: see module docstring for why this parser does not attempt to
    #: interpret non-zero values.
    hapi_status: int


def _skip_whitespace(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def _scan_matching_bracket(text: str, open_pos: int, open_char: str, close_char: str) -> int:
    """Return the index just past the bracket that matches the one at ``open_pos``.

    A hand-rolled scanner rather than a regex, because a regex cannot count
    nesting depth in general, and because the closing bracket must be found
    while ignoring any ``open_char``/``close_char`` that appears *inside* a
    quoted string. That is not a hypothetical precaution: see this module's
    docstring for ``getauditlog.txt``'s ``AUDIT_LOG`` value, which contains a
    literal ``[2615 INFO]`` inside its own quoted string -- a naive
    depth-counter that did not track quote state would find a ``]`` there
    and stop early, well before the array actually ends.

    A backslash inside a string escapes the following character (so
    ``\\'`` does not end the string) for both single- and double-quoted
    strings, matching how both JavaScript and this parser's eventual
    :func:`ast.literal_eval` step interpret it. No corpus sample uses this,
    but ``test_webvar.py`` proves it works with a synthetic value.

    Raises :class:`ValueError` (never :class:`errors.ProtocolError` --
    that translation is :func:`parse_webvar`'s job, with the endpoint/
    operation context it has and this function does not) if ``open_pos``
    is not actually ``open_char``, or if the text ends before the bracket
    closes.
    """
    if text[open_pos] != open_char:
        raise ValueError(f"expected {open_char!r} at position {open_pos}, found {text[open_pos]!r}")

    depth = 0
    in_string: str | None = None
    i = open_pos
    length = len(text)
    while i < length:
        ch = text[i]
        if in_string is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
        elif ch in ("'", '"'):
            in_string = ch
        elif ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1

    raise ValueError(f"unterminated {open_char!r}...{close_char!r} starting at position {open_pos}: no matching {close_char!r} found")


def parse_webvar(
    body: str,
    *,
    endpoint: str | None = None,
    operation: str | None = None,
) -> WebVarResponse:
    """Parse one ``.asp`` WEBVAR/JSONVAR response body.

    Never returns a half-parsed result: any shape this function does not
    recognise, or any array text that does not evaluate as a Python literal,
    raises :class:`errors.ProtocolError` (this collection's class for "malformed
    or unparseable response" -- see ``errors.py``'s docstring and how
    ``asp.py`` uses it for the same failure category on its own, narrower
    regex-based parses) rather than returning ``None`` or an empty/partial
    structure a caller might mistake for a genuinely empty response.

    ``endpoint``/``operation`` are forwarded to :class:`errors.ProtocolError`
    exactly as ``asp.py`` does for its own parse failures, so a caller that
    already knows which BMC and which RPC produced this body can attribute
    the failure without this function needing to know anything about HTTP.

    Deliberately does **not** call ``eval`` anywhere, and only ever calls
    :func:`ast.literal_eval` on the text of the array itself (never the full
    response body, which is not valid Python -- see module docstring) --
    ``literal_eval`` only ever constructs literal Python values, so it cannot
    be made to execute arbitrary code even if ``body`` is hostile or merely
    corrupted.
    """
    if not isinstance(body, str) or not body.strip():
        raise ProtocolError(
            "webvar response body is empty",
            endpoint=endpoint,
            operation=operation,
        )

    name_match = _JSONVAR_RE.search(body)
    if not name_match:
        raise ProtocolError(
            "no WEBVAR_JSONVAR_<NAME> assignment found in response body",
            endpoint=endpoint,
            operation=operation,
            diagnostic=body,
        )
    variable_name = name_match.group(1)

    struct_match = _STRUCTNAME_RE.search(body, name_match.end())
    if not struct_match:
        raise ProtocolError(
            f"no WEBVAR_STRUCTNAME_<NAME> array declaration found for WEBVAR_JSONVAR_{variable_name}",
            endpoint=endpoint,
            operation=operation,
            diagnostic=body,
        )
    struct_name = struct_match.group(1)

    array_start = _skip_whitespace(body, struct_match.end())
    if array_start >= len(body) or body[array_start] != "[":
        raise ProtocolError(
            f"WEBVAR_STRUCTNAME_{struct_name} is not followed by an array literal",
            endpoint=endpoint,
            operation=operation,
            diagnostic=body[struct_match.end() : struct_match.end() + 80],
        )

    try:
        array_end = _scan_matching_bracket(body, array_start, "[", "]")
    except ValueError as exc:
        raise ProtocolError(
            f"WEBVAR_STRUCTNAME_{struct_name}'s array literal is never closed",
            endpoint=endpoint,
            operation=operation,
            diagnostic=body[array_start:],
        ) from exc

    array_text = body[array_start:array_end]
    try:
        records_raw = ast.literal_eval(array_text)
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError) as exc:
        # SyntaxError/ValueError are what literal_eval documents raising for
        # malformed/non-literal input; TypeError/MemoryError/RecursionError
        # are called out by its own docs as also possible for sufficiently
        # hostile input. All five collapse to the same ProtocolError here --
        # a caller does not need to distinguish "syntactically broken" from
        # "too deeply nested", only "this did not parse".
        raise ProtocolError(
            f"WEBVAR_STRUCTNAME_{struct_name}'s array literal could not be parsed as a Python literal",
            endpoint=endpoint,
            operation=operation,
            diagnostic=array_text,
        ) from exc

    if not isinstance(records_raw, list) or not all(isinstance(record, dict) for record in records_raw):
        raise ProtocolError(
            f"WEBVAR_STRUCTNAME_{struct_name}'s array literal did not evaluate to a list of objects",
            endpoint=endpoint,
            operation=operation,
            diagnostic=array_text,
        )

    records: list[dict[str, Any]] = list(records_raw)
    if records and records[-1] == _SENTINEL:
        records = records[:-1]

    status_match = _HAPI_STATUS_RE.search(body, array_end)
    if not status_match:
        raise ProtocolError(
            f"no HAPI_STATUS field found after WEBVAR_STRUCTNAME_{struct_name}'s array",
            endpoint=endpoint,
            operation=operation,
            diagnostic=body[array_end : array_end + 200],
        )
    hapi_status = int(status_match.group(1))

    return WebVarResponse(
        variable_name=variable_name,
        struct_name=struct_name,
        records=records,
        hapi_status=hapi_status,
    )
