# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

# Intentionally has no shebang and is not executable: ansible-test's `shebang`
# sanity test rejects a non-module shebang inside a collection. Invoke it as
#   python3 tests/hardware/redact-evidence.py [output-dir]

"""Redact identifying lab/board data from hardware qualification evidence, in place.

Adapted from the sibling ``james_crowley.intel_amt`` collection's script of the
same name; read that file's module docstring for the full reasoning, which
applies here unchanged. The short version: the hardware playbooks write JSON
evidence into ``tests/hardware/output/``, and CI publishes that directory with
``store_artifacts``. CircleCI masks context values (``ASMB8_HOST`` and
friends) *in log output only* -- never in artifact content -- so an evidence
file describing a real BMC on a real network has to be made safe on its own,
not merely trusted to stay behind a project visibility setting that is a
checkbox someone can flip.

**What is preserved, deliberately.** Over-redaction is its own failure:
evidence nobody can read is evidence not worth keeping. Power/boot state,
capability flags, byte counters, error classes, session ids, booleans, and
the JSON structure itself (same keys, same nesting, same types) all survive
untouched.

**Pseudonyms, not deletion.** Each distinct value maps to a stable token
(``<redacted-ipv4-1>``) for the whole run, so two evidence files describing
the same board are still recognisably the same board. Tokens are assigned in
first-seen order and are deliberately *not* derived from the value: a hash of
an IPv4 address on a known /24 is a reversible oracle.

This collection's evidence shape is simpler than the sibling collection's --
no platform UUID, no per-property shape census, no event-log raw-byte
records -- so this script carries a correspondingly smaller set of
categories and exemptions. Add to it, rather than importing the sibling's
copy wholesale, if a future module's evidence needs one: importing would
couple this stdlib-only script to a collection install it does not otherwise
need.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

#: Default evidence directory, relative to this script rather than to the
#: caller's working directory, so the CI step and a hand run agree on what
#: gets redacted.
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "output"

#: Keys whose value identifies the board or the run but matches no general
#: pattern -- an ASMB8 hostname is a bare label, and a local media path spells
#: out the account and workspace the job ran under.
_IDENTIFYING_KEYS: dict[str, str] = {
    "hostname": "hostname",
    "host_name": "hostname",
    "host": "hostname",
    "asmb8_host": "hostname",
    "username": "username",
    "user": "username",
    "asmb8_username": "username",
    "client_username": "username",
    # Local filesystem paths. image/output_path reach the evidence through
    # asmb8_media's operation.observed.image and asmb8_console's
    # frame.output_path -- both are absolute paths on whichever machine ran
    # the playbook, which spells out an account name and workspace layout.
    # Paths match no general pattern (the FQDN rule explicitly rejects
    # anything ending in a filename label), so only the key catches this.
    "path": "path",
    "image": "path",
    "image_path": "path",
    "iso_path": "path",
    "output_path": "path",
    "runtime_dir": "path",
    "state_file": "path",
}

#: Keys whose string value must survive completely untouched, not merely
#: exempt from the identifying-key table above but exempt from pattern
#: matching too.
#:
#: ``action`` and ``note`` are exempt for the same reason the sibling
#: collection's script gives: every module writes ``operation.action`` from a
#: dotted literal in its own source (``asmb8_media.attach``,
#: ``asmb8_redirection.report``, ...), and a dotted lowercase token is exactly
#: what the ``fqdn`` pattern below matches. Redacting it would leave an
#: artifact that no longer says what it recorded. Safe only while every
#: `note` in tests/hardware/*.yml is a literal, non-templated string -- see
#: test_no_hardware_note_is_templated in
#: tests/unit/hardware/test_redact_evidence.py, which is the condition this
#: exemption depends on.
_EXEMPT_KEYS: frozenset[str] = frozenset({"action", "note"})

#: Public standards domains that could appear inside a diagnostic message.
#: This collection has no WS-Man resource URIs, but keeping the same
#: exemption shape as the sibling script costs nothing and future-proofs
#: against a diagnostic that quotes one.
_PRESERVED_DOMAIN_SUFFIXES: tuple[str, ...] = ("w3.org",)

#: Labels that mark a dotted string as a filename rather than a DNS name.
#: Evidence carries prose `note`/`diagnostic` fields that reference repository
#: paths ("README.md#..."), and "md"/"yml"/"iso" are perfectly good
#: TLD-shaped labels.
_FILENAME_LABELS: frozenset[str] = frozenset(
    {"cfg", "conf", "csv", "html", "img", "ini", "iso", "json", "log", "md", "py", "rst", "sh", "txt", "xml", "yaml", "yml"}
)

#: Matches a token this script already produced, so a second pass is a no-op.
_TOKEN_RE = re.compile(r"<redacted-[a-z0-9_]+-\d+>")

_HEX = "[0-9A-Fa-f]"
_OCTET = "(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"

# Order matters: colon-separated forms are tried longest-first, so a SHA-256
# fingerprint (32 colon-separated pairs) is never eaten piecewise by the MAC
# pattern (6 pairs).
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fingerprint", re.compile(rf"(?<!{_HEX})(?:{_HEX}{{2}}:){{31}}{_HEX}{{2}}(?!:?{_HEX})")),
    ("digest", re.compile(rf"(?<![0-9A-Za-z]){_HEX}{{64}}(?![0-9A-Za-z])")),
    ("mac", re.compile(rf"(?<![0-9A-Za-z:-])(?:{_HEX}{{2}}[:-]){{5}}{_HEX}{{2}}(?![0-9A-Za-z:-])")),
    ("uuid", re.compile(rf"(?<![0-9A-Za-z-]){_HEX}{{8}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{12}}(?![0-9A-Za-z-])")),
    ("uuid", re.compile(rf"(?<![0-9A-Za-z]){_HEX}{{32}}(?![0-9A-Za-z])")),
    ("ipv4", re.compile(rf"(?<![0-9A-Za-z.])(?:{_OCTET}\.){{3}}{_OCTET}(?![0-9A-Za-z.])")),
    (
        "ipv6",
        re.compile(
            rf"(?<![0-9A-Fa-f:.])(?:"
            rf"(?:{_HEX}{{1,4}}:){{7}}{_HEX}{{1,4}}"
            rf"|(?:{_HEX}{{1,4}}:){{1,7}}:"
            rf"|(?:{_HEX}{{1,4}}:){{1,6}}:{_HEX}{{1,4}}"
            rf"|(?:{_HEX}{{1,4}}:){{1,5}}(?::{_HEX}{{1,4}}){{1,2}}"
            rf"|(?:{_HEX}{{1,4}}:){{1,4}}(?::{_HEX}{{1,4}}){{1,3}}"
            rf"|(?:{_HEX}{{1,4}}:){{1,3}}(?::{_HEX}{{1,4}}){{1,4}}"
            rf"|(?:{_HEX}{{1,4}}:){{1,2}}(?::{_HEX}{{1,4}}){{1,5}}"
            rf"|{_HEX}{{1,4}}:(?::{_HEX}{{1,4}}){{1,6}}"
            rf"|::(?:{_HEX}{{1,4}}:){{0,6}}{_HEX}{{1,4}}"
            rf")(?![0-9A-Fa-f:.])"
        ),
    ),
    ("fqdn", re.compile(r"(?<![0-9A-Za-z.@_-])(?:[0-9A-Za-z_-]+\.)*[0-9A-Za-z_-]*[A-Za-z][0-9A-Za-z_-]*\.[A-Za-z]{2,}(?![0-9A-Za-z.-])")),
)

_CATEGORY_ORDER: tuple[str, ...] = ("ipv4", "ipv6", "mac", "uuid", "fingerprint", "digest", "fqdn", "hostname", "username", "path")


def _is_dns_name(candidate: str) -> bool:
    """Whether a dotted candidate should be treated as a DNS name at all."""
    lowered = candidate.lower()
    if any(lowered == suffix or lowered.endswith("." + suffix) for suffix in _PRESERVED_DOMAIN_SUFFIXES):
        return False
    return not any(label in _FILENAME_LABELS for label in lowered.split("."))


class Redactor:
    """Assigns and remembers a stable pseudonym per distinct value, per run."""

    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str], str] = {}
        self._counts: dict[str, int] = {}
        #: Occurrences replaced, per category -- what a reviewer reads in the
        #: job log to confirm the step actually did something.
        self.occurrences: dict[str, int] = {}

    def token_for(self, category: str, value: str) -> str:
        key = (category, value)
        token = self._tokens.get(key)
        if token is None:
            self._counts[category] = self._counts.get(category, 0) + 1
            token = f"<redacted-{category}-{self._counts[category]}>"
            self._tokens[key] = token
        self.occurrences[category] = self.occurrences.get(category, 0) + 1
        return token

    @property
    def distinct(self) -> dict[str, int]:
        return dict(self._counts)

    def redact_text(self, text: str) -> str:
        """Replace every identifying pattern in ``text``, leaving the rest alone."""
        for category, pattern in _PATTERNS:

            def replace(match: re.Match[str], category: str = category) -> str:
                value = match.group(0)
                if _TOKEN_RE.fullmatch(value):
                    return value
                if category == "fqdn" and not _is_dns_name(value):
                    return value
                return self.token_for(category, value)

            text = pattern.sub(replace, text)
        return text

    def redact_value(self, value: Any, key: str | None = None) -> Any:
        """Redact ``value`` in place-of-structure: same keys, nesting, and types.

        ``key`` is the mapping key this value was found under, used only for
        the identifying keys no pattern can catch. Keys themselves are never
        rewritten.
        """
        if isinstance(value, dict):
            return {child_key: self.redact_value(child, key=child_key if isinstance(child_key, str) else None) for child_key, child in value.items()}
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        if isinstance(value, str):
            if key is not None and key.lower() in _EXEMPT_KEYS:
                return value
            # Patterns first, even under an identifying key: an `endpoint` and
            # `network.ip_address` can hold the same address, and they have to
            # end up as the same token or the artifact stops showing that.
            redacted = self.redact_text(value)
            if redacted != value:
                return redacted
            if key is not None and value and not _TOKEN_RE.fullmatch(value):
                category = _IDENTIFYING_KEYS.get(key.lower())
                if category is not None:
                    return self.token_for(category, value)
            return redacted
        # bool/int/float/None are left exactly as they are: byte counters,
        # power states and every flag in this evidence are numbers, and none
        # of them identify anything.
        return value


def redact_file(path: Path, redactor: Redactor) -> str:
    """Rewrite one evidence file in place. Returns "json", "text", or "empty"."""
    original = path.read_text(encoding="utf-8")
    if not original.strip():
        return "empty"

    try:
        parsed = json.loads(original)
    except json.JSONDecodeError as exc:
        print(f"  {path.name}: not valid JSON ({exc.msg}); redacted as raw text instead")
        path.write_text(redactor.redact_text(original), encoding="utf-8")
        return "text"

    redacted = redactor.redact_value(parsed)
    path.write_text(json.dumps(redacted, indent=4, sort_keys=False) + "\n", encoding="utf-8")
    return "json"


def main(argv: list[str]) -> int:
    """Redact every JSON file under the evidence directory. Never fails the job."""
    output_dir = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_OUTPUT_DIR

    if not output_dir.is_dir():
        # Reached whenever a job failed before writing anything. This step
        # runs with `when: always` precisely so it covers that case, so
        # "nothing to do" is a normal outcome and must not turn a red job
        # into a differently red one.
        print(f"redact-evidence: no evidence directory at {output_dir}; nothing to redact.")
        return 0

    json_paths = sorted(p for p in output_dir.rglob("*.json") if p.is_file())
    other_paths = sorted(p for p in output_dir.rglob("*") if p.is_file() and p.suffix != ".json")

    redactor = Redactor()
    print(f"redact-evidence: scanning {output_dir}")
    for path in json_paths:
        kind = redact_file(path, redactor)
        print(f"  redacted {path.relative_to(output_dir)} ({kind})")

    total = sum(redactor.occurrences.values())
    print(f"redact-evidence: {len(json_paths)} JSON file(s) rewritten, {total} value(s) redacted")
    for category in _CATEGORY_ORDER:
        occurrences = redactor.occurrences.get(category, 0)
        if occurrences:
            print(f"  {category}: {occurrences} occurrence(s), {redactor.distinct.get(category, 0)} distinct value(s)")
    if total == 0 and json_paths:
        print("  nothing matched -- check this is really the evidence directory before publishing it")

    if other_paths:
        # Only .json is rewritten, because that is all the playbooks write.
        # If that ever stops being true, store_artifacts would publish the
        # new file unredacted, so name it here rather than pass over it
        # silently.
        print(f"redact-evidence: WARNING -- {len(other_paths)} non-JSON file(s) present and NOT redacted:")
        for path in other_paths:
            print(f"  {path.relative_to(output_dir)}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
