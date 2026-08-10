<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# `.asp` WEBVAR/JSONVAR response corpus

54 real response bodies, one file per endpoint, captured from a live
ASMB8-iKVM BMC's own web UI on **2026-08-10**, firmware **1.14** (aux
**1.14.2**).

Every file was captured from a **read-only** endpoint (a `get*.asp`/status
query, or the one-time `create.asp` login this collection's `asp.py` already
needs to parse) -- nothing that mutates board state (power control, media
attach/detach, user or network configuration changes, firmware update) was
ever invoked to produce this corpus.

## Redaction

Before this corpus left the machine that captured it, and before this
collection's automated contributor ever read a single byte of it:

* IPv4/IPv6 addresses were replaced with RFC 5737/RFC 3849 documentation
  values (`192.0.2.10` appears throughout; `::` appears where the board
  reported no IPv6 address configured).
* MAC addresses were replaced with the IEEE documentation value
  `00:00:5E:00:53:00`.
* Session cookies were replaced with the literal text `<REDACTED>`.

One additional redaction was applied locally, after the corpus arrived and
before it was committed here: `create.txt`'s `CSRFTOKEN` field carried a real,
if short-lived and already-expired, anti-CSRF token value. It was not caught
by the session-cookie redaction pass above (a CSRF token is a distinct field
from the session cookie) and was blanked to the same `<REDACTED>` literal
before this directory was ever added to git. Every other file in this
directory was checked (`grep -inE "token|password|passwd|secret|apikey"`) and
contains no comparable value -- the only other match, `getactivedircfg.txt`'s
`AD_SECRETUSER`, is an empty string on this board, not a real secret.

`plugins/module_utils/webvar.py`'s own docstring documents one side effect of
that last substitution worth knowing about if you open `create.txt`: the
redaction pass also ate the surrounding quotes and colon around the
`SESSION_COOKIE` key, leaving behind text (`SESSION_COOKIE'<REDACTED>'`) that
is not valid under any single- or double-quote convention. That file is kept
exactly as captured, redaction artifact and all, because it is real evidence
that `webvar.py`'s parser raises on malformed input rather than silently
returning a partial structure -- see that module's docstring and
`tests/unit/plugins/module_utils/test_webvar.py`.

## What this corpus is used for

`tests/unit/plugins/module_utils/test_webvar.py` parses every file in this
directory and asserts it succeeds (with `create.txt` as the one deliberate
exception above) and yields a variable name, a record list, and a
`HAPI_STATUS` value, plus a set of focused tests against specific fixtures
for the interesting shapes the corpus contains (the empty-`{}` sentinel, a
genuinely empty record list, mixed int/str field types, and so on -- see that
file and `webvar.py`'s own docstring for exactly which fixture backs which
claim).

Do not add a file to this directory that was not captured this way. If a
future capture is needed (a new endpoint, a different firmware revision), it
must go through the same redaction discipline as this batch before it is
committed here.
