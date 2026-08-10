#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_postcode
short_description: Read the ASMB8-iKVM BMC's current BIOS POST code, optionally sampled over time
description:
  - >-
    Reads C(getpostcode.asp) over the C(.asp) web-management surface and reports the current BIOS
    POST code exactly as the BMC returns it, plus its integer value when that text parses as hex.
  - >-
    B(Why this module exists, and why it is the highest-value module in this batch.) IPMI
    Serial-over-LAN does not work on this board -- the channel-level SOL payload was disabled, then
    enabled, per-user SOL access was already granted, both plausible bitrates were tried, and zero
    bytes ever arrived across repeated resets (C(docs/hardware-evidence-2026-08-08.md),
    "Serial-over-LAN: configured correctly and still silent"). M(james_crowley.asmb8_ikvm.asmb8_console)
    opens a live KVM/video channel but does not decode the AMI/ASPEED video codec into pixels --
    see that module's own description. Between those two facts, this collection currently has B(no
    other) out-of-band signal of boot progress at all: diagnosing a hung or slow boot has meant a
    human physically present, photographing a monitor. A POST code, read here, is the only remote
    signal this collection can give instead.
  - >-
    B(This module does not know what any POST code means, and refuses to guess.) BIOS POST codes
    are vendor- and firmware-specific, AMI has not published a table for this board's BIOS, and
    nothing in this collection's C(.asp) fixture corpus documents one. RV(post_code)/RV(post_code_int)
    are reported exactly as read, with no interpretation attached. Do not build a code-to-meaning
    table on top of this module's output without an independently sourced reference for this exact
    BIOS -- see this collection's README.md and CONTRIBUTING.md for why unsourced protocol facts are
    refused throughout this project.
  - >-
    O(sample=true) turns a single point-in-time read into a bounded time series: this module polls
    O(poll_interval_seconds) apart for up to O(max_duration_seconds) and returns every observation
    plus the distinct codes seen, in the order first seen -- what actually makes this useful for
    watching a boot in progress rather than glancing at one instant.
  - >-
    B(Sampling is deliberately slow, bounded, and impossible to run concurrently with itself.) This
    BMC's C(.asp) web server is HTTP/1.0, keeps no connection alive between requests, caps out at 20
    concurrent web sessions, and has been observed, against the target hardware, to accept a TCP
    connection under concurrent load and then simply never serve it -- wedging even its own web UI
    for several minutes (C(module_utils/asp.py), C(errors.ErrorClass.BMC_BUSY)). O(poll_interval_seconds)
    and O(max_duration_seconds) are both range-checked (see their own documentation for the exact
    bounds) specifically so this module cannot be pointed at this BMC in a way that hammers it, and
    every poll in a sampling run is issued one at a time, sequentially, from the single
    C(.asp) session this module opens for the whole run -- there is no concurrency inside this
    module for a BMC that has already been wedged by concurrency once.
  - >-
    Logging in to the C(.asp) web session (C(POST /rpc/WEBSES/create.asp)) to read C(getpostcode.asp)
    creates real, if short-lived, BMC-side session state, exactly as
    M(james_crowley.asmb8_ikvm.asmb8_info)'s C(include_web_session) option documents for the same
    login call. Unlike that option, this module has no way to read a POST code without it -- there
    is no IPMI equivalent of this endpoint, and requiring a caller to opt in to something this
    module's entire purpose depends on would only add friction, not honesty. Everything this module
    subsequently does over that session is a plain C(GET), and it never mutates board configuration
    or power state.
version_added: 0.1.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
options:
  sample:
    description:
      - >-
        V(false) (the default) reads C(getpostcode.asp) exactly once. V(true) instead polls it
        repeatedly -- see O(poll_interval_seconds)/O(max_duration_seconds) -- and returns the full
        observed sequence under RV(sample) in addition to RV(post_code)/RV(post_code_int), which
        continue to reflect the most recently observed value either way.
    type: bool
    default: false
  poll_interval_seconds:
    description:
      - Seconds to wait between polls when O(sample=true). Ignored when O(sample=false).
      - >-
        Bounded to between 2 and 300 seconds inclusive, and rejected outside that range -- see this
        module's description for why this BMC must never be polled tightly. There is no faster way
        to sample this endpoint through this module, on purpose.
    type: int
    default: 5
  max_duration_seconds:
    description:
      - >-
        Upper bound, in seconds, on how long O(sample=true) keeps polling. The actual run may finish
        slightly before this if the next poll would start after it, and never starts a poll that
        would push the run meaningfully past it.
      - Ignored when O(sample=false).
      - >-
        Bounded to between 1 and 900 seconds (15 minutes) inclusive, and rejected outside that range
        -- long enough to watch a real boot's early stages, short enough that a play cannot be
        accidentally left blocked on this module for hours.
    type: int
    default: 60
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_console
  - module: james_crowley.asmb8_ikvm.asmb8_info
attributes:
  check_mode:
    description: >-
      A full read runs identically in check mode, since this module never mutates board
      configuration or power state -- see this module's description for the one, unavoidable
      exception (creating the C(.asp) session itself) and why it is not gated behind check mode
      any more than reading the POST code itself is.
    support: full
  diff_mode:
    description: Not supported. There is no prior/after state to diff for a read-only module.
    support: none
requirements:
  - requests >= 2.25.0 (on the Ansible controller)
"""

EXAMPLES = r"""
- name: Read the current POST code once
  james_crowley.asmb8_ikvm.asmb8_postcode:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
  delegate_to: localhost
  no_log: true
  register: postcode

- name: Watch the first two minutes of a boot, one poll every 5 seconds
  james_crowley.asmb8_ikvm.asmb8_postcode:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    sample: true
    poll_interval_seconds: 5
    max_duration_seconds: 120
  delegate_to: localhost
  no_log: true
  register: boot_sample

- name: Show the sequence of distinct codes observed
  ansible.builtin.debug:
    var: boot_sample.sample.distinct_post_codes
"""

RETURN = r"""
post_code:
  description: >-
    The current (or, when O(sample=true), most recently observed) POST code exactly as
    C(getpostcode.asp) returned it (C(CurrPostCode)), with no reinterpretation. See this module's
    description for why no meaning is attached to this value.
  type: str
  returned: always
  sample: "00"
post_code_int:
  description: >-
    RV(post_code) parsed as a base-16 integer, or V(null) if it does not parse as hex. Offered
    purely as a convenience for sorting/comparing codes -- it carries no more meaning than
    RV(post_code) itself.
  type: int
  returned: always
  sample: 0
sample:
  description: >-
    Present only when O(sample=true). V(null) otherwise -- see RV(post_code)/RV(post_code_int) for
    the single-read case.
  type: dict
  returned: when O(sample=true)
  contains:
    observations:
      description: Every poll issued during this run, in the order it was observed.
      type: list
      elements: dict
      contains:
        post_code:
          description: The POST code observed at this poll, exactly as returned.
          type: str
        post_code_int:
          description: Same value as RV(post_code_int), for this one observation.
          type: int
        elapsed_seconds:
          description: Seconds since this run's first poll, measured on the controller's own clock.
          type: float
        timestamp:
          description: Controller-clock ISO-8601 UTC timestamp of this poll.
          type: str
    distinct_post_codes:
      description: >-
        The distinct values seen in RV(sample.observations[].post_code), in the order each was
        first observed. A boot that sits on one code for several polls before advancing appears
        here exactly once for that code, not once per poll -- RV(sample.observations) is where the
        repeat count/timing lives.
      type: list
      elements: str
    poll_interval_seconds:
      description: The effective O(poll_interval_seconds) used for this run.
      type: int
    max_duration_seconds:
      description: The effective O(max_duration_seconds) used for this run.
      type: int
    sample_count:
      description: Number of polls actually issued (C(len(observations))).
      type: int
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
      description: Always V(asmb8_postcode.read).
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

import time
from datetime import datetime, timezone

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import HAS_REQUESTS, REQUESTS_IMPORT_ERROR, AspClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import IkvmError, ProtocolError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt

#: The one endpoint this module reads. Sourced from
#: ``tests/unit/fixtures/asp/getpostcode.txt``, a real capture whose one record is
#: ``{'CurrPostCode' : '00'}`` -- see that fixture and ``module_utils/webvar.py``.
_ENDPOINT = "getpostcode"

#: Bounds for O(poll_interval_seconds)/O(max_duration_seconds). See this module's DOCUMENTATION for
#: the reasoning: this BMC's web server has no keep-alive, caps at 20 sessions, and has been wedged
#: by concurrent load before, so sampling must stay slow and bounded no matter what a caller asks for.
MIN_POLL_INTERVAL_SECONDS = 2
MAX_POLL_INTERVAL_SECONDS = 300
MIN_MAX_DURATION_SECONDS = 1
MAX_MAX_DURATION_SECONDS = 900


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
    spec.update(
        {
            "sample": {"type": "bool", "default": False},
            "poll_interval_seconds": {"type": "int", "default": 5},
            "max_duration_seconds": {"type": "int", "default": 60},
        }
    )
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


def parse_post_code_hex(raw: str) -> int | None:
    """Parse a POST code's raw text as base-16, or return ``None`` if it does not parse.

    Deliberately never raises: a POST code this module has not seen a shape for (the corpus has
    exactly one sample, ``'00'``) should degrade to "no integer value", not fail the whole module --
    see this module's DOCUMENTATION on never inventing meaning for a code it cannot interpret.
    """
    try:
        return int(str(raw), 16)
    except (TypeError, ValueError):
        return None


def read_post_code(client: AspClient) -> dict:
    """Read C(getpostcode.asp) once and return ``{"post_code": str, "post_code_int": int | None}``.

    Raises :class:`errors.ProtocolError` if the response has no record or no ``CurrPostCode``
    field -- both would mean this endpoint has drifted away from the one shape the fixture corpus
    documents, not a value this module should silently paper over.
    """
    response = client.get_webvar(_ENDPOINT)
    if not response.records:
        raise ProtocolError(
            f"{_ENDPOINT}.asp returned no records",
            endpoint=client.endpoint,
            operation="asmb8_postcode.read",
        )
    record = response.records[0]
    if "CurrPostCode" not in record:
        raise ProtocolError(
            f"{_ENDPOINT}.asp's record did not contain CurrPostCode: {record!r}",
            endpoint=client.endpoint,
            operation="asmb8_postcode.read",
        )
    raw = record["CurrPostCode"]
    return {"post_code": str(raw), "post_code_int": parse_post_code_hex(raw)}


def sample_post_codes(
    client: AspClient,
    *,
    poll_interval_seconds: int,
    max_duration_seconds: int,
    sleep=time.sleep,
    monotonic=time.monotonic,
    wall_clock=lambda: datetime.now(timezone.utc),
) -> list[dict]:
    """Poll :func:`read_post_code` every ``poll_interval_seconds`` for up to ``max_duration_seconds``.

    Always issues at least one poll (at ``elapsed_seconds`` 0) before ever sleeping. Never starts a
    poll that would land after ``max_duration_seconds`` has elapsed -- the bound is honoured by not
    scheduling the next poll, not by cutting one off partway through.

    ``sleep``/``monotonic``/``wall_clock`` are injected so tests can drive this deterministically
    without a real sampling run ever actually sleeping in this collection's test suite.
    """
    start = monotonic()
    observations: list[dict] = []
    while True:
        elapsed = monotonic() - start
        reading = read_post_code(client)
        observations.append(
            {
                "post_code": reading["post_code"],
                "post_code_int": reading["post_code_int"],
                "elapsed_seconds": round(elapsed, 3),
                "timestamp": wall_clock().isoformat(),
            }
        )
        elapsed_after = monotonic() - start
        if elapsed_after + poll_interval_seconds > max_duration_seconds:
            return observations
        sleep(poll_interval_seconds)


def distinct_post_codes_in_order(observations: list[dict]) -> list[str]:
    """The distinct C(post_code) values in ``observations``, in first-seen order."""
    seen: set[str] = set()
    distinct: list[str] = []
    for observation in observations:
        code = observation["post_code"]
        if code not in seen:
            seen.add(code)
            distinct.append(code)
    return distinct


def validate_sampling_bounds(*, poll_interval_seconds: int, max_duration_seconds: int) -> str | None:
    """Return an error message if the sampling bounds are out of range, or ``None`` if they are fine.

    Only meaningful (and only called) when O(sample=true) -- see argument_spec(); an out-of-range
    value on an ignored option should not fail a single-read call that never uses it.
    """
    if not (MIN_POLL_INTERVAL_SECONDS <= poll_interval_seconds <= MAX_POLL_INTERVAL_SECONDS):
        return (
            f"poll_interval_seconds must be between {MIN_POLL_INTERVAL_SECONDS} and {MAX_POLL_INTERVAL_SECONDS} seconds inclusive; got {poll_interval_seconds}"
        )
    if not (MIN_MAX_DURATION_SECONDS <= max_duration_seconds <= MAX_MAX_DURATION_SECONDS):
        return f"max_duration_seconds must be between {MIN_MAX_DURATION_SECONDS} and {MAX_MAX_DURATION_SECONDS} seconds inclusive; got {max_duration_seconds}"
    return None


def main() -> None:
    module = AnsibleModule(argument_spec=argument_spec(), supports_check_mode=True)

    if not HAS_REQUESTS:
        module.fail_json(msg=missing_required_lib("requests"), exception=REQUESTS_IMPORT_ERROR)
        return

    params = module.params
    sample = params["sample"]
    poll_interval_seconds = params["poll_interval_seconds"]
    max_duration_seconds = params["max_duration_seconds"]

    if sample:
        error = validate_sampling_bounds(poll_interval_seconds=poll_interval_seconds, max_duration_seconds=max_duration_seconds)
        if error:
            module.fail_json(msg=error)
            return

    client = build_asp_client(params)

    try:
        client.login()
        if sample:
            observations = sample_post_codes(
                client,
                poll_interval_seconds=poll_interval_seconds,
                max_duration_seconds=max_duration_seconds,
            )
            latest = observations[-1]
            post_code = latest["post_code"]
            post_code_int = latest["post_code_int"]
            sample_result = {
                "observations": observations,
                "distinct_post_codes": distinct_post_codes_in_order(observations),
                "poll_interval_seconds": poll_interval_seconds,
                "max_duration_seconds": max_duration_seconds,
                "sample_count": len(observations),
            }
        else:
            reading = read_post_code(client)
            post_code = reading["post_code"]
            post_code_int = reading["post_code_int"]
            sample_result = None
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    receipt = OperationReceipt(
        action="asmb8_postcode.read",
        endpoint=client.endpoint,
        changed=False,
        observed={"post_code": post_code, "post_code_int": post_code_int},
    )
    module.exit_json(
        changed=False,
        post_code=post_code,
        post_code_int=post_code_int,
        sample=sample_result,
        operation=receipt.to_dict(),
    )


if __name__ == "__main__":
    main()
