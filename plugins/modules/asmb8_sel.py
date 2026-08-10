#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_sel
short_description: Read the ASMB8-iKVM BMC's IPMI System Event Log over the C(.asp) web interface
description:
  - >-
    Reads C(getallselentries.asp), C(getmaxselentries.asp) and C(getselcfg.asp) over the C(.asp)
    web-management surface and reports the BMC's System Event Log (SEL) entries, its reported
    capacity, and its raw policy setting. Every field this module reports is sourced from a real
    capture checked in under C(tests/unit/fixtures/asp/) -- see that directory's C(README.md) and
    C(module_utils/webvar.py) for how those captures were taken and parsed.
  - >-
    B(This is not this collection's only path to the SEL, and for most callers it is not the
    preferred one.) The same event log is also readable over plain IPMI (netfn C(0x0a), Get SEL
    Info/Get SEL Entry), which C(pyghmi) wraps as C(get_event_log()); that IPMI path has been
    confirmed against the target hardware, returning the same 24 real entries this module's own
    C(getallselentries.asp) fixture shows. IPMI is, in general, the better choice here: it needs no
    C(.asp) login (so no BMC-side web session is created just to read a log), it is a standard,
    portable IPMI command rather than an AMI-specific web endpoint, and this collection has not
    (as of this writing) built a dedicated module around it -- see C(docs/roadmap.md)'s Tier 1 "SEL
    (system event log) read and clear" entry for that standing gap. B(This module exists for the
    cases IPMI does not cover, not to duplicate it): a caller that has only web-management reachability
    to this BMC (no IPMI/RMCP+ path, or a firewall that only opens the web port), or that wants a
    cross-check between the two independent transports reading the same underlying log. If IPMI is
    reachable, prefer it.
  - >-
    Logging in to the C(.asp) web session (C(POST /rpc/WEBSES/create.asp)) to read these endpoints
    creates real, if short-lived, BMC-side session state, exactly as
    M(james_crowley.asmb8_ikvm.asmb8_info)'s C(include_web_session) option documents for the same
    login call. This module has no way to read these endpoints without it. Everything it
    subsequently does over that session is a plain C(GET), and it never mutates board configuration.
  - >-
    B(There is no C(clear) option, and there will not be one on this module.) Clearing the SEL is a
    real, meaningful operation -- conceptually, it would need a POST-based C(.asp) endpoint this
    module's own fixture corpus contains no capture of (every fixture under
    C(tests/unit/fixtures/asp/) is a C(get*)/status/login read, by policy -- see that directory's own
    C(README.md)), or the IPMI path's own C(get_event_log(clear=True))/Clear SEL command. Inventing
    an untested C(.asp) clear endpoint this corpus has no evidence for would violate this
    collection's sourcing policy (README.md, CONTRIBUTING.md); this module is scoped strictly
    read-only, on purpose, and does not offer a flag that would tempt a caller into destructive use
    of an endpoint nobody has actually captured or tested.
  - >-
    B(Pagination.) C(getselentries.asp) is this board's paged sibling of C(getallselentries.asp) --
    it is a C(POST) endpoint, not a C(GET), and this corpus's own C(getselentries.txt) fixture (a
    C(GET) capture made without the POST parameters that endpoint actually needs) returns only the
    empty sentinel, which is evidence that endpoint needs its documented POST form to return
    anything, not evidence of an empty log. C(module_utils/asp.py)'s C(AspClient.get_webvar()) is
    C(GET)-only by deliberate design specifically so it can never be turned into a general request
    escape hatch; this module honours that boundary rather than subverting it, and reads
    C(getallselentries.asp) instead, which returned the BMC's entire 24-entry log in one C(GET) in
    this corpus with no evidence of internal truncation. If this board's log ever grows large enough
    that C(getallselentries.asp) itself stops returning everything in one response, paging through
    C(getselentries.asp) would need a dedicated, properly-named C(POST) method on
    C(AspClient) -- not present today, and out of scope for this read-only module.
  - >-
    RV(sel_policy) is reported exactly as C(getselcfg.asp)'s C(SEL_POLICY) field returns it, with no
    interpretation attached -- this corpus's one sample is always V(0), and nothing sourced here
    documents what any value of this AMI-specific field means (it is not a field IPMI's own
    "Get SEL Info" defines). Treat it as opaque diagnostic data, not a decoded setting.
  - >-
    RV(entries[].timestamp)/RV(entries[].timestamp_epoch) are read from the BMC's own clock, which
    this collection's connection doc fragment already documents as unreliable (observed reporting a
    date years in the past on hardware that was, at the time, running years later). Treat these
    timestamps as diagnostic, not authoritative, and never as a substitute for the controller's own
    clock.
version_added: 0.1.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
options:
  limit:
    description:
      - >-
        Return at most this many entries. Applied client-side, after C(getallselentries.asp) has
        already returned its full response -- see this module's description on why this module does
        not attempt to page the underlying request. Omit to return every entry the BMC reported.
      - >-
        Entries are kept in exactly the order C(getallselentries.asp) returned them -- this corpus's
        own fixture returns them highest-C(RecordID)-first (newest first) -- and O(limit) simply
        takes the first O(limit) of that order without resorting it.
    type: int
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_info
  - module: james_crowley.asmb8_ikvm.asmb8_postcode
attributes:
  check_mode:
    description: >-
      A full read runs identically in check mode, since this module never mutates board
      configuration -- see this module's description for the one, unavoidable exception (creating
      the C(.asp) session itself).
    support: full
  diff_mode:
    description: Not supported. There is no prior/after state to diff for a read-only module.
    support: none
requirements:
  - requests >= 2.25.0 (on the Ansible controller)
"""

EXAMPLES = r"""
- name: Read the full SEL
  james_crowley.asmb8_ikvm.asmb8_sel:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
  delegate_to: localhost
  no_log: true
  register: sel

- name: Only the 5 most recent entries
  james_crowley.asmb8_ikvm.asmb8_sel:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    limit: 5
  delegate_to: localhost
  no_log: true
  register: recent_sel

- name: Warn if the log is approaching its reported capacity
  ansible.builtin.debug:
    msg: "SEL has {{ sel.entries_available }} of {{ sel.max_entries }} entries"
  when: sel.entries_available > (sel.max_entries * 0.9)
"""

RETURN = r"""
entries:
  description: >-
    SEL entries from C(getallselentries.asp), most recent first (see O(limit) on ordering), after
    O(limit) has been applied.
  type: list
  elements: dict
  returned: always
  contains:
    record_id:
      description: C(RecordID) -- this entry's unique, monotonically-assigned SEL record identifier.
      type: int
      sample: 24
    record_type:
      description: C(RecordType), the standard IPMI SEL record type byte, reported raw and undecoded.
      type: int
      sample: 2
    timestamp_epoch:
      description: C(TimeStamp), Unix epoch seconds, exactly as the BMC reported it. See this module's description on trusting the BMC's own clock.
      type: int
      sample: 1608171458
    timestamp:
      description: RV(entries[].timestamp_epoch) converted to an ISO-8601 UTC string, purely for readability. Carries the same BMC-clock caveat.
      type: str
      sample: "2020-12-17T02:37:38+00:00"
    generator_id_1:
      description: C(GenID1), the standard IPMI SEL "Generator ID" low byte, reported raw and undecoded.
      type: int
      sample: 32
    generator_id_2:
      description: C(GenID2), the standard IPMI SEL "Generator ID" high byte, reported raw and undecoded.
      type: int
      sample: 0
    event_message_format_version:
      description: C(EvmRev), the standard IPMI "Event Message Format Version" field.
      type: int
      sample: 4
    sensor_type:
      description: >-
        C(SensorType), the standard IPMI sensor type number that generated this event, reported raw
        -- this module does not maintain a sensor-type-to-name lookup table.
      type: int
      sample: 4
    sensor_name:
      description: >-
        C(SensorName) -- AMI's own convenience field on this endpoint (not part of the raw IPMI SEL
        record itself), and the only string-valued field in the record.
      type: str
      sample: REAR_FAN2
    event_dir_type:
      description: >-
        C(EventDirType), the standard IPMI "Event Dir / Event Type" byte, reported raw -- this
        module does not decode its assertion/deassertion bit or event-reading-type field.
      type: int
      sample: 1
    event_data_1:
      description: C(EventData1), reported raw and undecoded.
      type: int
      sample: 82
    event_data_2:
      description: C(EventData2), reported raw and undecoded.
      type: int
      sample: 6
    event_data_3:
      description: C(EventData3), reported raw and undecoded.
      type: int
      sample: 6
    extra:
      description: >-
        Any field C(getallselentries.asp) returned on this entry that is not one of the named
        fields above, keyed exactly as the BMC sent it. Empty for every entry in this module's own
        fixture corpus; kept so a firmware revision that adds a field is still visible here rather
        than silently dropped.
      type: dict
entries_available:
  description: >-
    Number of entries C(getallselentries.asp) actually returned in this call, before O(limit) was
    applied. This module has no evidence that endpoint itself paginates -- its own fixture returned
    every entry (24) in one response -- so, today, this is simply "how many entries the BMC
    currently has", not "how many were on this page".
  type: int
  returned: always
  sample: 24
entries_returned:
  description: C(len(entries)) -- how many entries are actually present in RV(entries) after O(limit).
  type: int
  returned: always
  sample: 24
max_entries:
  description: >-
    C(getmaxselentries.asp)'s C(COUNT) field: the SEL's reported total capacity, not its current
    entry count (see RV(entries_available) for that).
  type: int
  returned: always
  sample: 3000
sel_policy:
  description: >-
    C(getselcfg.asp)'s C(SEL_POLICY) field, reported exactly as returned. See this module's
    description -- no meaning is attached to this value; it is not a field IPMI's own SEL
    configuration commands define.
  type: int
  returned: always
  sample: 0
operation:
  description: >-
    The non-secret C(asmb8-ikvm-operation/v1) receipt for this read, in the same nested shape every
    other module in this collection returns it under.
  type: dict
  returned: always
  contains:
    schema:
      description: Always V(asmb8-ikvm-operation/v1).
      type: str
    action:
      description: Always V(asmb8_sel.read).
      type: str
    endpoint:
      description: The C(host:port) this read was performed against.
      type: str
    changed:
      description: Always V(false).
      type: bool
    error_class:
      description: A stable machine-readable failure class. V(null) on success.
      type: str
"""

from datetime import datetime, timezone
from typing import Any

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import HAS_REQUESTS, REQUESTS_IMPORT_ERROR, AspClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import IkvmError, ProtocolError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt

#: Endpoints this module reads. All three are sourced from real captures under
#: ``tests/unit/fixtures/asp/`` -- see that directory's README and this module's DOCUMENTATION for
#: why ``getselentries`` (the paged POST sibling of ``getallselentries``) is deliberately not among
#: them.
_ENTRIES_ENDPOINT = "getallselentries"
_MAX_ENTRIES_ENDPOINT = "getmaxselentries"
_CONFIG_ENDPOINT = "getselcfg"

#: The field names ``getallselentries.asp`` is known (from the fixture corpus) to use on every
#: entry. Anything else present on a record lands in that entry's own ``extra`` dict instead of
#: being silently dropped -- see :func:`parse_entry`.
_KNOWN_ENTRY_FIELDS = frozenset(
    {
        "RecordID",
        "RecordType",
        "TimeStamp",
        "GenID1",
        "GenID2",
        "EvmRev",
        "SensorType",
        "SensorName",
        "EventDirType",
        "EventData1",
        "EventData2",
        "EventData3",
    }
)


def _connection_argument_spec() -> dict[str, dict]:
    return {
        "host": {"type": "str", "required": True},
        "port": {"type": "int", "default": 443},
        "username": {"type": "str", "default": "admin"},
        "password": {"type": "str", "required": True, "no_log": True},
        "use_tls": {"type": "bool", "default": True},
        "allow_insecure_transport": {"type": "bool", "default": False},
        "validate_certs": {"type": "bool", "default": True},
        "ca_path": {"type": "path"},
        "tls_fingerprint": {"type": "str"},
        "timeout": {"type": "int", "default": 30},
        "connect_timeout": {"type": "int", "default": 10},
    }


def argument_spec() -> dict[str, dict]:
    spec = _connection_argument_spec()
    spec["limit"] = {"type": "int"}
    return spec


def build_asp_client(params: dict) -> AspClient:
    """Construct an :class:`AspClient` from the module's connection parameters."""
    return AspClient(
        host=params["host"],
        port=params["port"],
        username=params["username"],
        password=params["password"],
        use_tls=params["use_tls"],
        validate_certs=params["validate_certs"],
        ca_path=params["ca_path"],
        tls_fingerprint=params["tls_fingerprint"],
        allow_insecure_transport=params["allow_insecure_transport"],
        timeout=params["timeout"],
        connect_timeout=params["connect_timeout"],
    )


def _epoch_to_iso(epoch: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        # Not this module's job to fail a whole read over one unparseable timestamp -- see
        # read_entries()'s docstring on per-record honesty vs. per-field robustness.
        return None


def parse_entry(record: dict[str, Any]) -> dict[str, Any]:
    """Translate one raw C(getallselentries.asp) record into this module's RV(entries) shape.

    Every named field is read with C(dict.get) rather than indexing, so a record missing a field
    this corpus has always seen present degrades that one field to ``None`` instead of failing the
    whole read -- the field set observed across all 24 corpus entries is consistent, but nothing
    guarantees every future entry will be.
    """
    extra = {key: value for key, value in record.items() if key not in _KNOWN_ENTRY_FIELDS}
    timestamp_epoch = record.get("TimeStamp")
    return {
        "record_id": record.get("RecordID"),
        "record_type": record.get("RecordType"),
        "timestamp_epoch": timestamp_epoch,
        "timestamp": _epoch_to_iso(timestamp_epoch) if timestamp_epoch is not None else None,
        "generator_id_1": record.get("GenID1"),
        "generator_id_2": record.get("GenID2"),
        "event_message_format_version": record.get("EvmRev"),
        "sensor_type": record.get("SensorType"),
        "sensor_name": record.get("SensorName"),
        "event_dir_type": record.get("EventDirType"),
        "event_data_1": record.get("EventData1"),
        "event_data_2": record.get("EventData2"),
        "event_data_3": record.get("EventData3"),
        "extra": extra,
    }


def read_entries(client: AspClient, *, limit: int | None) -> tuple[list[dict], int]:
    """Read C(getallselentries.asp) and return ``(entries, entries_available)``.

    ``entries_available`` is the count before O(limit) is applied -- see this module's
    DOCUMENTATION on why that is "how many entries the BMC currently has", not "how many were on
    this page": this endpoint has shown no evidence of internal pagination in the fixture corpus.
    """
    response = client.get_webvar(_ENTRIES_ENDPOINT)
    entries = [parse_entry(record) for record in response.records]
    entries_available = len(entries)
    if limit is not None:
        entries = entries[:limit]
    return entries, entries_available


def read_max_entries(client: AspClient) -> int:
    """Read C(getmaxselentries.asp)'s C(COUNT) field."""
    response = client.get_webvar(_MAX_ENTRIES_ENDPOINT)
    if not response.records or "COUNT" not in response.records[0]:
        raise ProtocolError(
            f"{_MAX_ENTRIES_ENDPOINT}.asp did not return a COUNT field",
            endpoint=client.endpoint,
            operation="asmb8_sel.read",
        )
    return int(response.records[0]["COUNT"])


def read_sel_policy(client: AspClient) -> int:
    """Read C(getselcfg.asp)'s C(SEL_POLICY) field."""
    response = client.get_webvar(_CONFIG_ENDPOINT)
    if not response.records or "SEL_POLICY" not in response.records[0]:
        raise ProtocolError(
            f"{_CONFIG_ENDPOINT}.asp did not return a SEL_POLICY field",
            endpoint=client.endpoint,
            operation="asmb8_sel.read",
        )
    return int(response.records[0]["SEL_POLICY"])


def main() -> None:
    module = AnsibleModule(argument_spec=argument_spec(), supports_check_mode=True)

    if not HAS_REQUESTS:
        module.fail_json(msg=missing_required_lib("requests"), exception=REQUESTS_IMPORT_ERROR)
        return

    params = module.params
    limit = params["limit"]
    if limit is not None and limit < 0:
        module.fail_json(msg=f"limit must not be negative; got {limit}")
        return

    client = build_asp_client(params)

    try:
        client.login()
        entries, entries_available = read_entries(client, limit=limit)
        max_entries = read_max_entries(client)
        sel_policy = read_sel_policy(client)
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    receipt = OperationReceipt(
        action="asmb8_sel.read",
        endpoint=client.endpoint,
        changed=False,
        observed={"entries_available": entries_available, "max_entries": max_entries, "sel_policy": sel_policy},
    )
    module.exit_json(
        changed=False,
        entries=entries,
        entries_available=entries_available,
        entries_returned=len(entries),
        max_entries=max_entries,
        sel_policy=sel_policy,
        operation=receipt.to_dict(),
    )


if __name__ == "__main__":
    main()
