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

## POST-parameterized reads (added 2026-08-10, separate save-action capture)

Two more files were added to this directory after the 54-file batch above,
from a distinct capture of the BMC's own web UI performing a save action --
not part of the 54-file count cited throughout this README,
`plugins/module_utils/webvar.py`, and `tests/unit/plugins/module_utils/test_webvar.py`,
and deliberately excluded from that suite's corpus-wide bookkeeping (see that
test file's own comment) so the "54" figure stays a stable, accurate
description of the original batch rather than being redefined out from under
every place that cites it. Both are read-only, exactly like the batch above,
and were redacted with the same discipline (RFC 5737 `192.0.2.10` for the one
address either file carries):

* `getselentries_post_lasteventid24.txt` -- `POST /rpc/getselentries.asp`,
  `Content-Type: application/x-www-form-urlencoded`, body
  `WEBVAR_LASTEVENTID=24`. This is the SEL's paged, `POST`-only sibling of
  `getallselentries.asp` -- see `plugins/modules/asmb8_sel.py`'s
  `after_event_id` option. `WEBVAR_LASTEVENTID` returns entries **after**
  that event ID; the response body is byte-identical to this directory's
  pre-existing `getselentries.txt` (an empty `[ {} ]` sentinel, no real
  records) because the SEL held exactly 24 entries at capture time and
  nothing follows record 24 -- a correct empty result, not a failure and not
  evidence this endpoint is broken or unreadable. Do not confuse the two
  files: `getselentries.txt` was captured as a bare `GET` with none of this
  endpoint's actual parameters (see that file's own history in
  `plugins/modules/asmb8_sel.py`'s DOCUMENTATION) and is evidence only that a
  parameterless request returns nothing useful; this file is the properly
  parameterized capture that happens to also be empty, for a documented,
  sourced reason.
* `getsessioninfo_post_servicebit4.txt` -- `POST /rpc/getsessioninfo.asp`,
  body `SERVICEBIT=4`. This is the per-service active-session directory --
  see `plugins/modules/asmb8_sessions.py`'s `active_session_services`
  option. `SERVICEBIT` reuses `getallservicescfg.asp`'s own `SERVICEID`
  values (`4` is `cd-media`'s `SERVICEID` in `getallservicescfg.txt`, in this
  same directory) -- two independent captures agreeing on the same value is
  what makes that mapping sourced, not assumed. The one real session record
  this file carries (`SID: 24`, `STYPE: 7`, `UID: 2`, `UNAME: admin`,
  `UPRIV: 4`) is a different session than the one this directory's
  pre-existing `getsessioninfo.txt` shows (`SID: 1`, `STYPE: 1`, captured
  without a `SERVICEBIT` parameter) -- both are kept, deliberately not
  merged or treated as contradictory, because they document two different
  queries against the BMC's own session table, not two measurements of one
  fact.

Both endpoints require `POST`; `AspClient.get_webvar()` remains strictly
`GET`-only (see that method's own docstring), and reading either of these two
endpoints goes through the separate, explicitly-named
`AspClient.post_webvar()` instead -- see `docs/protocol-notes.md`'s
"POST-based reads" section for the general convention this establishes.

## Write replies (added for `asmb8_ntp`, from a distinct save-action capture, 2026-08-10)

`setntpcfg_write.txt` and `setdatetime_write.txt` -- the reply bodies for
this collection's first real `.asp` writes, backing
`plugins/module_utils/asp.py`'s `AspClient.set_webvar()` and
`plugins/modules/asmb8_ntp.py`. The save-action capture that sourced
`setntpcfg.asp`/`setdatetime.asp`'s *request* bodies (see
`docs/protocol-notes.md`'s NTP write-convention section) also reported that
both replied "in the standard WEBVAR envelope with an empty record array and
the result in `HAPI_STATUS`" -- that shape, not verbatim captured bytes, is
what these two files encode. **Be precise about what is and is not sourced
here:** the empty-record-array/`HAPI_STATUS:0` envelope is sourced exactly as
described above; the specific `WEBVAR_JSONVAR_SETNTPCFG`/
`WEBVAR_STRUCTNAME_SETNTPCFG` (and `..._SETDATETIME`) variable names inside
that envelope are this repository's own reconstruction, following the
`WEBVAR_JSONVAR_<NAME>`/`WEBVAR_STRUCTNAME_<NAME>` pattern every one of the
54 `get*.asp` corpus samples above uses with `<NAME>` matching the endpoint
in uppercase (see `webvar.py`'s own docstring) -- not independently confirmed
verbatim text for these two specific replies. A non-zero-`HAPI_STATUS`
failure reply has no capture evidence at all (every real write and read
sample this corpus and this directory contain reports `HAPI_STATUS:0`); the
test for that path (`AspClient.set_webvar()` raising) builds its own
synthetic body inline in `tests/unit/plugins/module_utils/test_asp.py`
rather than adding a fabricated "failure" fixture file here, for the same
reason `test_asmb8_network.py`'s `SYNTHETIC_DNSCFG_WITH_SECRET` lives inline
in that test file instead of in this directory.
