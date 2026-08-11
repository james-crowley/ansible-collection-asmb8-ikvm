#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_info
short_description: Gather ASMB8-iKVM capability and state facts
description:
  - >-
    Reads IPMI-observed facts from an ASMB8-iKVM BMC -- power state, boot-device
    override, and management-controller identity -- over the same already-working
    IPMI path M(james_crowley.asmb8_ikvm.asmb8_power)/M(james_crowley.asmb8_ikvm.asmb8_boot) use, plus (only when explicitly
    requested) a small set of read-only facts from the C(.asp) web-management
    surface.
  - This module B(never mutates IPMI state) and always reports C(changed=false).
  - >-
    The one exception to "never mutates" is O(include_web_session), and it is
    exactly that: an exception, opted into explicitly, never on by default.
    Authenticating against this BMC's web session (C(POST /rpc/WEBSES/create.asp))
    creates BMC-side session state even though everything this module subsequently
    reads over that session is itself read-only -- see O(include_web_session)
    below. Nothing else this module does touches the BMC in a way that leaves any
    trace.
  - >-
    RV(asmb8.capabilities) is deliberately honest about what has and has not been
    proven against this hardware. Virtual media and remote console/KVM redirection
    are B(not) reported as available here -- neither has been proven working
    against the target board yet, and this module does not claim otherwise just
    because C(module_utils/asp.py) has code that could, in principle, attempt
    them.
  - >-
    This module never fetches the KVM/media session JNLP (C(/Java/jviewer.jnlp)).
    Doing so is documented in C(module_utils/asp.py) as allocating a BMC-side
    KVM/media session as a side effect of the fetch itself -- not a read, no
    matter how read-only the intent -- and a module documented as never mutating
    anything must not perform it implicitly. RV(asmb8.media.port_mode) is
    therefore always V(unknown) here; determining it for real requires fetching
    that JNLP, which is out of scope for a read-only module and left to a future
    virtual-media module.
  - >-
    O(include_media_preconditions) (requires O(include_web_session=true)) additionally reads
    C(getremotesession.asp) and C(getvmediacfg.asp) and reports, as RV(asmb8.media.preconditions),
    the settings that actually gate whether a virtual-media attach can succeed -- B(without)
    attempting an attach. This exists because a bare protocol rejection (a status-3 "redirection
    not accepted", or RV(operation.error_class)=V(bmc_busy)) does not by itself say why. Per
    C(docs/hardware-evidence-2026-08-08.md)'s "Redirection rejection status 3 means bad token"
    section, two wrong theories were chased for hours -- a media session stranded by a network
    outage, then a BMC cold reset having reverted media settings -- costing two wasted boot cycles
    and an unnecessary reset, when reading these two endpoints would have shown neither theory's
    suspected setting had actually changed, in one call each.
  - >-
    RV(asmb8.media.preconditions.encryption.media_encryption_enabled) (C(MEDIAENCRYPTION), via
    C(getremotesession.asp)) and RV(asmb8.media.preconditions.encryption.secure_channel_enabled)
    (C(V_STR_SECURE_CHANNEL), via C(getvmediacfg.asp)) are the single most actionable field this
    module reports for diagnosing a failed attach: this collection's iUSB client only speaks the
    plaintext variant of the protocol, so either one reading non-V(false) means an attach cannot
    succeed against this client, independent of every other precondition reported alongside it.
  - >-
    RV(asmb8.media.preconditions.encryption.media_encryption_enabled) and
    RV(asmb8.media.preconditions.attach.attach_raw) are sourced from C(getremotesession.asp), which
    this project's own testing found answers a programmatic client with a session-expired-looking
    HTML page B(even immediately after a fresh, successful login) -- the identical request sequence
    works from a browser. B(Correction:) an earlier version of this description said what a
    programmatic client additionally needs "has not been identified" -- GitHub issue #5
    (2026-08-11, live hardware) identified a general mechanism for exactly this symptom: a missing
    C(CSRFTOKEN) header, confirmed for five other endpoints and now attached by C(asp.py) to every
    non-C(WEBSES) request. Whether C(getremotesession.asp) itself is one of the endpoints that
    enforces C(CSRFTOKEN) is B(not) itself confirmed either way -- it was not one of the five issue
    #5 tested -- so this module continues to treat it as a documented, unverified gap rather than
    assuming the general fix also covers this specific endpoint; see
    M(james_crowley.asmb8_ikvm.asmb8_sessions)'s own description for the same, independently
    observed gap. This module therefore treats a parse failure on that one endpoint as expected, not
    fatal: on failure, both fields above degrade to V(null) and
    RV(asmb8.media.preconditions.remote_session_read.outcome) reports V(failed), rather than this
    module failing outright. Treat RV(asmb8.media.preconditions.remote_session_read) as the honest
    statement that reading C(getremotesession.asp) working at all, on any given run, is unverified,
    not guaranteed. C(getvmediacfg.asp) has shown no equivalent failure mode in this project's
    testing, so a failure reading it is B(not) degraded the same way -- it fails this module
    outright, the same as this module's existing O(include_web_session) diagnostic read already
    does.
  - >-
    B(A session-expired body can never produce RV(web_management.logged_in)=V(true).) Earlier,
    C(hoststatus.asp)'s response was returned verbatim and unchecked, so a session-expired HTML page
    (see C(module_utils/asp.py)'s C(looks_like_session_expired_html)) would have been reported as a
    successful C(host_status_raw) read alongside V(logged_in)=V(true) -- a confident wrong answer,
    and precisely the false positive GitHub issue #5 reported. C(AspClient.get_host_status()) now
    raises C(errors.ProtocolError) on that shape instead, which this module does not catch: it
    propagates and fails the whole module, exactly like a rejected login already does under
    O(include_web_session) (see that option's own documentation for why this module does not hide
    that class of failure behind a successful-looking IPMI-only result).
  - >-
    RV(asmb8.media.preconditions.status_raw) (C(V_MEDIA_STATUS), via C(getvmediacfg.asp)) is
    reported B(raw only, with no interpretation, and is never used by this module to decide
    whether an attach can succeed). An earlier note in this project's own history wrongly guessed
    this field tracked live media-attach state; a later capture showed the BMC's own web UI writing
    it, which a live attach-state field would have no reason for a UI to do. Do not read a change
    in this value as evidence that an attach succeeded or failed -- it is settable configuration
    whose actual meaning this project does not have a source for.
version_added: 0.1.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
options:
  ipmi_port:
    description:
      - >-
        UDP port of the BMC's IPMI-over-LAN (RMCP+) listener, used for every IPMI
        fact this module gathers. B(Not) the same thing as O(port), which is this
        collection's shared web-management HTTPS port (443) and is only consulted
        when O(include_web_session=true).
    type: int
    default: 623
  include_web_session:
    description:
      - >-
        Whether to additionally authenticate against the C(.asp) web-management
        session and read a small amount of diagnostic state over it, using
        O(host), O(port), O(username), O(password) and the rest of the TLS-related
        options from the shared connection fragment.
      - >-
        Defaults to V(false) B(on purpose). C(POST /rpc/WEBSES/create.asp) --
        this BMC's login endpoint -- creates real session state on the BMC even
        though this module's own use of that session afterwards is strictly
        read-only; see C(module_utils/asp.py). A caller that only wants IPMI facts
        should not pay that cost, and no caller should have it happen without
        asking.
      - >-
        When V(true) and the login itself fails, this module fails -- it does not
        silently degrade RV(asmb8.web_management) to V(null), because a caller
        that explicitly opted in almost certainly wants to know that the
        credentials or connection it is relying on elsewhere in the same play are
        broken, not have that failure hidden behind a successful-looking IPMI-only
        result.
    type: bool
    default: false
  include_media_preconditions:
    description:
      - >-
        Whether to additionally read C(getremotesession.asp) and C(getvmediacfg.asp) over the same
        C(.asp) web-management session and report, as RV(asmb8.media.preconditions), the settings
        that gate whether a virtual-media attach can succeed -- without attempting one. See the
        module description for why this exists and what each field means.
      - >-
        Defaults to V(false). Requires O(include_web_session=true): this module fails immediately,
        before reading anything, if this is V(true) while O(include_web_session) is not -- these
        preconditions are read over the same authenticated session O(include_web_session) already
        creates, and O(include_web_session) is documented as B(the) one exception to this module
        never mutating anything; a second, independent option that could also create that session
        would make that no longer true.
      - >-
        Costs two additional C(GET) requests against the BMC over and above O(include_web_session)
        alone. This BMC's web server has a small per-listener worker pool and no keep-alive; see
        C(module_utils/asp.py) for why every request this module's C(.asp) client issues is already
        serialized, and avoid combining this option with concurrent plays against the same BMC.
    type: bool
    default: false
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_power
  - module: james_crowley.asmb8_ikvm.asmb8_boot
attributes:
  check_mode:
    description: A full read runs identically in check mode, since this module never mutates IPMI state.
    support: full
  diff_mode:
    description: Not supported. There is no prior/after state to diff for a read-only module.
    support: none
requirements:
  - pyghmi (on the Ansible controller)
  - requests >= 2.25.0 (on the Ansible controller, only when O(include_web_session=true))
"""

EXAMPLES = r"""
- name: Read IPMI facts only (no BMC-side session created)
  james_crowley.asmb8_ikvm.asmb8_info:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
  delegate_to: localhost
  no_log: true
  register: asmb8

- name: Require IPMI to be reachable before attempting a power/boot change
  ansible.builtin.assert:
    that:
      - asmb8.asmb8.reachable
      - asmb8.asmb8.ipmi.power_state is not none

- name: Do not attempt virtual media -- this module reports it as unproven, not available
  ansible.builtin.assert:
    that:
      - not asmb8.asmb8.capabilities.virtual_media.supported

- name: Also read a small amount of web-management diagnostic state
  james_crowley.asmb8_ikvm.asmb8_info:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    include_web_session: true
  delegate_to: localhost
  no_log: true
  register: asmb8_with_web

- name: Diagnose a failed virtual-media attach before re-attempting it or resetting the BMC
  james_crowley.asmb8_ikvm.asmb8_info:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    include_web_session: true
    include_media_preconditions: true
  delegate_to: localhost
  no_log: true
  register: asmb8_media_preconditions

- name: Rule out media encryption before suspecting a stranded session or a reverted BMC setting
  ansible.builtin.assert:
    that:
      # media_encryption_enabled is null (not false) when getremotesession.asp could not be
      # read this run -- see remote_session_read -- so this checks it is false, not merely falsy.
      - asmb8_media_preconditions.asmb8.media.preconditions.encryption.media_encryption_enabled == false
      - asmb8_media_preconditions.asmb8.media.preconditions.encryption.secure_channel_enabled == false
"""

RETURN = r"""
asmb8:
  description: IPMI-observed (and, optionally, C(.asp)-observed) facts.
  returned: success
  type: dict
  contains:
    reachable:
      description: Whether the IPMI session could be established at all.
      type: bool
      returned: always
      sample: true
    ipmi:
      description: Facts read directly over IPMI.
      type: dict
      returned: always
      contains:
        power_state:
          description: >-
            Pyghmi's C(get_power()) result verbatim, or V(null) if this
            particular read failed after the IPMI session was otherwise
            established (see RV(operation.ipmi_reads)).
          type: dict
          returned: when available
          sample: {powerstate: "on"}
        boot_device:
          description: >-
            Pyghmi's C(get_bootdev()) result verbatim, or V(null) if this
            particular read failed. C(uefimode) may be absent from this dict on
            the branch where the BMC reports no standing override at all -- see
            C(module_utils/ipmi.py).
          type: dict
          returned: when available
          sample: {bootdev: default, persistent: false, uefimode: false}
        mc_info:
          description: >-
            Pyghmi's C(get_mci()) result -- a bare string, B(not) a dict, unlike
            RV(asmb8.ipmi.power_state)/RV(asmb8.ipmi.boot_device) above. V(null)
            if this read failed or the BMC reported nothing.
          type: str
          returned: when available
    web_management:
      description: >-
        Read-only C(.asp) web-management facts. V(null) unless O(include_web_session=true).
      type: dict
      returned: when O(include_web_session=true)
      contains:
        logged_in:
          description: >-
            Whether C(create.asp) accepted the credentials B(and) C(hoststatus.asp) returned
            something other than a session-expired-looking body. Always V(true) when present --
            either kind of failure (a rejected login, or C(hoststatus.asp) answering with the HTML
            shape C(module_utils/asp.py)'s C(looks_like_session_expired_html) detects) fails the
            whole module rather than being reported here (see O(include_web_session)). This field
            can therefore never be V(true) alongside a session-expired
            RV(asmb8.web_management.host_status_raw) -- see GitHub issue #5, which reported exactly
            that false-positive combination before C(get_host_status()) started raising on it.
          type: bool
        host_status_raw:
          description: >-
            Raw C(hoststatus.asp) response text, truncated, for diagnosis only.
            C(module_utils/asp.py) documents this endpoint's response shape as
            B(unverified) -- it is not parsed, and no field within it is claimed
            to mean anything specific. It is, however, checked for the
            session-expired HTML shape before this module ever sees it -- see
            RV(asmb8.web_management.logged_in).
          type: str
    capabilities:
      description: >-
        What this board supports, and whether that support has actually been
        proven against the target hardware or is only expected. Every entry
        follows the same C(supported)/C(proven) shape so "not proven" and "not
        supported" are never conflated.
      type: dict
      returned: always
      contains:
        ipmi_power:
          description: Power control over IPMI. Proven live against the target board.
          type: dict
        ipmi_boot_device:
          description: Boot-device override over IPMI. Proven live against the target board.
          type: dict
        ipmi_mc_info:
          description: Management-controller identification over IPMI. Proven live against the target board.
          type: dict
        web_management:
          description: >-
            The read-only C(.asp) facts under RV(asmb8.web_management). C(proven)
            is V(true) only when O(include_web_session=true) B(and) the login
            actually succeeded on this run -- it does not mean "proven at some
            point in the past".
          type: dict
        virtual_media:
          description: >-
            Attaching a local image to the BMC's virtual CD-ROM/floppy over the
            proprietary iUSB protocol. B(Not) supported here: C(supported) is
            V(null) (unknown, not V(false)) and C(proven) is always V(false) --
            this has not yet been proven against the target hardware. Do not read
            this as a firmware limitation finding.
          type: dict
        remote_console:
          description: >-
            KVM/remote-console redirection. Same caveat as
            RV(asmb8.capabilities.virtual_media) -- not yet proven.
          type: dict
        redfish:
          description: >-
            Always C(supported=false), C(proven=true): the target board is an
            ASPEED AST2400, and Redfish support on this firmware family arrived
            with AST2500/ASMB9. This is a confirmed hardware-generation fact, not
            a live probe -- no Redfish endpoint is ever contacted by this module.
          type: dict
    media:
      description: Virtual-media/KVM session facts this module can report without allocating a session.
      type: dict
      returned: always
      contains:
        port_mode:
          description: >-
            Always V(unknown). Determining V(single_port) vs V(dedicated_ports)
            (see C(module_utils/models.py)'s C(JnlpSession)) requires fetching the
            KVM/media JNLP, which allocates a BMC-side session as a side effect --
            a mutation this read-only module refuses to perform. See this module's
            description.
          type: str
        preconditions:
          description: >-
            Media-attach preconditions read from C(getremotesession.asp)/C(getvmediacfg.asp).
            V(null) unless O(include_media_preconditions=true). See the module description for why
            O(include_media_preconditions) requires O(include_web_session=true), and for the
            C(getremotesession.asp) degradation reflected in
            RV(asmb8.media.preconditions.remote_session_read) below.
          type: dict
          returned: when O(include_media_preconditions=true)
          contains:
            encryption:
              description: >-
                The single most actionable precondition this module reports -- see the module
                description. This collection's iUSB client cannot speak the encrypted variant of
                the protocol, so a non-V(false) value on either field here means an attach cannot
                succeed against this client.
              type: dict
              contains:
                media_encryption_enabled:
                  description: >-
                    Whether C(MEDIAENCRYPTION) is set, via C(getremotesession.asp). V(null) if that
                    endpoint could not be read this run -- see
                    RV(asmb8.media.preconditions.remote_session_read).
                  type: bool
                secure_channel_enabled:
                  description: Whether C(V_STR_SECURE_CHANNEL) is set, via C(getvmediacfg.asp).
                  type: bool
            licensing:
              description: Virtual-media licensing, via C(getvmediacfg.asp).
              type: dict
              contains:
                license_status_raw:
                  description: Raw C(V_MEDIA_LICENSE_STATUS).
                  type: int
            attach:
              description: Whether media is currently attached, via C(getremotesession.asp).
              type: dict
              contains:
                attach_raw:
                  description: >-
                    Raw C(VMEDIAATTACH). V(null) if C(getremotesession.asp) could not be read this
                    run -- see RV(asmb8.media.preconditions.remote_session_read).
                  type: int
            device_counts:
              description: Configured device instance counts, via C(getvmediacfg.asp).
              type: dict
              contains:
                cd:
                  description: Raw C(V_NUM_CD).
                  type: int
                fd:
                  description: Raw C(V_NUM_FD).
                  type: int
                hd:
                  description: Raw C(V_NUM_HD).
                  type: int
            sessions:
              description: >-
                Decoded CD-ROM virtual-media session capacity, via C(getvmediacfg.asp). Decoded
                with the same B(+128) offset M(james_crowley.asmb8_ikvm.asmb8_sessions) documents
                and applies to C(getallservicescfg.asp)'s own C(cd-media) C(MAXSESS)/C(CURSESS) --
                see that module's description for the two independent measurements confirming that
                offset. C(getvmediacfg.asp)'s raw C(V_MAX_CD_SESSIONS)/C(V_ACTIVE_CD_SESSIONS)
                values are the same B(129)/B(128) that endpoint's own C(cd-media) record reports,
                which is exactly why the same offset applies here too. The raw value is never
                reported; only the decoded count is.
              type: dict
              contains:
                cd:
                  description: Decoded CD-ROM session capacity.
                  type: dict
                  contains:
                    max:
                      description: Decoded C(V_MAX_CD_SESSIONS) (raw value minus V(128)).
                      type: int
                    current:
                      description: Decoded C(V_ACTIVE_CD_SESSIONS) (raw value minus V(128)).
                      type: int
            status_raw:
              description: >-
                Raw C(V_MEDIA_STATUS), via C(getvmediacfg.asp). B(Meaning unsourced) -- see the
                module description. Never used by this module to decide whether an attach can
                succeed; do not read a change in this value as evidence that one did.
              type: int
            remote_session_read:
              description: >-
                The outcome of reading C(getremotesession.asp) for this precondition group, for the
                same "tell a null apart from a failed read" reason as RV(operation.ipmi_reads). See
                the module description for why V(failed) is an expected, documented possibility on
                this one endpoint, not necessarily a fault.
              type: dict
              contains:
                outcome:
                  description: V(read) if C(getremotesession.asp) was parsed, V(failed) otherwise.
                  type: str
                  choices: [read, failed]
                error_class:
                  description: The failure class, on V(failed) only; V(null) otherwise.
                  type: str
operation:
  description: >-
    The non-secret C(asmb8-ikvm-operation/v1) receipt for this read, in the same
    nested shape every other module in this collection returns it under.
    The receipt's C(previous) and C(desired) members are always C(null) here: a
    read has no prior state and no intended change to report. See RV(asmb8) for
    what was actually observed. Those two members are referenced above as literal
    code rather than as return-value directives on purpose - they are not declared
    as documented return values on this read-only module, and validate-modules
    rejects a return-value directive naming something undeclared.
  type: dict
  returned: always
  contains:
    schema:
      description: Always V(asmb8-ikvm-operation/v1).
      type: str
    action:
      description: Always V(get_facts).
      type: str
    endpoint:
      description: The C(host:ipmi_port) this read was performed against.
      type: str
    changed:
      description: Always V(false).
      type: bool
    error_class:
      description: A stable machine-readable failure class. V(null) on success.
      type: str
    ipmi_reads:
      description: >-
        Per-field outcome for each IPMI fact this module attempted, keyed by the
        RV(asmb8.ipmi) field name. Present so a V(null) fact value can be told
        apart from "this firmware does not support the read" versus "the read
        itself failed for a reason worth seeing".
      type: dict
      contains:
        outcome:
          description: V(read) if the field was populated, V(failed) if the read raised.
          type: str
          choices: [read, failed]
        error_class:
          description: The failure class, on V(failed) only; V(null) otherwise.
          type: str
      sample:
        power_state: {outcome: read, error_class: null}
        boot_device: {outcome: read, error_class: null}
        mc_info: {outcome: failed, error_class: remote_operation}
"""

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import HAS_REQUESTS, REQUESTS_IMPORT_ERROR, AspClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import IkvmError, ProtocolError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.ipmi import DEFAULT_IPMI_PORT, HAS_PYGHMI, PYGHMI_IMPORT_ERROR, IpmiClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt

#: Bound on the diagnostic text this module keeps from hoststatus.asp. Its
#: response shape is documented in module_utils/asp.py as unverified -- this is
#: purely so a caller can look at *something* when include_web_session=true,
#: never a value this module parses or attaches meaning to.
_HOST_STATUS_DIAGNOSTIC_LIMIT = 2048

#: Same +128 session-count offset plugins/modules/asmb8_sessions.py documents and independently
#: confirms twice (getallservicescfg.asp's web MAXSESS 148 -> 20, matching errors.py's documented
#: 20-session cap; cd-media's MAXSESS 129 -> 1, matching that service's single-occupancy slot).
#: Duplicated here rather than imported: an Ansible module ships to the target with only its own
#: file plus module_utils, so one plugins/modules file cannot import another at runtime. This is
#: safe to apply to getvmediacfg.asp's V_MAX_CD_SESSIONS/V_ACTIVE_CD_SESSIONS specifically because
#: they are the exact same raw values (129/128) getallservicescfg.asp's own cd-media record
#: reports for MAXSESS/CURSESS -- see RV(asmb8.media.preconditions.sessions) and
#: asmb8_sessions.py's DOCUMENTATION for the underlying citations. Never report the raw 129/128 for
#: these two fields; only the decoded count.
_MEDIA_SESSION_COUNT_OFFSET = 128

#: Same "not applicable" sentinel asmb8_sessions.py documents for MAXSESS/CURSESS (raw 255,
#: observed on ssh/telnet, which would decode to a nonsensical 127 sessions and is therefore
#: treated as "no reported cap" instead). Not observed on getvmediacfg.asp's CD fields in this
#: corpus, but applied here for the same reason and with the same evidence as that module:
#: consistency with a sourced rule, not a second, independently invented one.
_MEDIA_SESSION_COUNT_SENTINEL = 255


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
        "ipmi_port": {"type": "int", "default": DEFAULT_IPMI_PORT},
    }


def argument_spec() -> dict[str, dict]:
    spec = _connection_argument_spec()
    spec["include_web_session"] = {"type": "bool", "default": False}
    spec["include_media_preconditions"] = {"type": "bool", "default": False}
    return spec


def build_ipmi_client(params: dict) -> IpmiClient:
    """Construct an :class:`IpmiClient` from the module's connection parameters."""
    return IpmiClient(host=params["host"], port=params["ipmi_port"], username=params["username"], password=params["password"])


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


def _read_ipmi_field(reads: dict, field: str, getter) -> object:
    """Run one IPMI read, degrading a failure to ``None`` rather than failing the whole module.

    Records the outcome in ``reads`` (this module's C(operation.ipmi_reads)) so
    a V(null) fact can be told apart from "did not attempt" and "attempted and
    failed for a reason worth seeing". Only :class:`errors.IkvmError` is caught
    here -- the IPMI session itself was already established by the time any of
    these run, so a failure at this point is a per-field refusal, not a
    connection problem this module should hide.
    """
    try:
        value = getter()
    except IkvmError as err:
        reads[field] = {"outcome": "failed", "error_class": err.error_class}
        return None
    reads[field] = {"outcome": "read", "error_class": None}
    return value


def gather_ipmi_facts(client: IpmiClient) -> tuple[dict, dict]:
    """Read every IPMI fact this module reports, degrading per-field rather than all-or-nothing."""
    reads: dict[str, dict] = {}
    facts = {
        "power_state": _read_ipmi_field(reads, "power_state", client.get_power_state),
        "boot_device": _read_ipmi_field(reads, "boot_device", client.get_boot_device),
        "mc_info": _read_ipmi_field(reads, "mc_info", client.get_mc_info),
    }
    return facts, reads


def gather_web_management_facts(asp_client: AspClient) -> dict:
    """Read a small amount of read-only diagnostic state over an already-authenticated ``.asp`` session.

    Only called when O(include_web_session=true) -- see that option's
    documentation for why creating this session is a deliberate, opted-in
    exception to this module never mutating anything. Login itself happens
    once, in :func:`main`, not here -- so the same session can be shared with
    :func:`gather_media_preconditions` when O(include_media_preconditions=true)
    is also set, rather than this module paying for a second
    C(POST /rpc/WEBSES/create.asp) authentication. A failure reading
    C(hoststatus.asp) is allowed to propagate: see O(include_web_session)'s
    own documentation for why this module does not swallow it. That now
    includes C(AspClient.get_host_status()) raising C(errors.ProtocolError) on a
    session-expired-looking body (see C(module_utils/asp.py)'s
    C(looks_like_session_expired_html)) -- deliberately not caught here, so it
    joins a rejected login as a reason this call, and this dict's V(True)
    C(logged_in), never gets built at all. See GitHub issue #5, which reported
    the previous, uncorrected behaviour: V(True) reported alongside a
    C(host_status_raw) that was really this HTML page.
    """
    host_status_raw = asp_client.get_host_status()
    if len(host_status_raw) > _HOST_STATUS_DIAGNOSTIC_LIMIT:
        host_status_raw = host_status_raw[:_HOST_STATUS_DIAGNOSTIC_LIMIT]
    return {"logged_in": True, "host_status_raw": host_status_raw}


def decode_media_session_count(raw: object) -> int | None:
    """Decode one V_MAX_CD_SESSIONS/V_ACTIVE_CD_SESSIONS value. See :data:`_MEDIA_SESSION_COUNT_OFFSET`."""
    if raw is None:
        return None
    if raw == _MEDIA_SESSION_COUNT_SENTINEL:
        return None
    return raw - _MEDIA_SESSION_COUNT_OFFSET


def _decode_remote_session_preconditions(record: dict) -> dict:
    """Decode the two ``getremotesession.asp`` fields RV(asmb8.media.preconditions) needs.

    See plugins/modules/asmb8_sessions.py's ``decode_remote_session_config()`` for this same
    endpoint's full ten-field decode -- this module only needs two of them.
    """
    return {
        "media_encryption_enabled": bool(record.get("MEDIAENCRYPTION")),
        "attach_raw": record.get("VMEDIAATTACH"),
    }


def fetch_remote_session_preconditions(asp_client: AspClient) -> tuple[dict | None, dict]:
    """Read ``getremotesession.asp`` for RV(asmb8.media.preconditions), degrading gracefully.

    Mirrors plugins/modules/asmb8_sessions.py's ``fetch_remote_session_config()``: this endpoint
    has been observed, against the target hardware, to answer a fresh and otherwise-successful
    login with a session-expired-looking page. GitHub issue #5 identified the general mechanism
    behind that symptom for five *other* endpoints (a missing ``CSRFTOKEN`` header, now attached by
    ``AspClient`` to every non-``WEBSES`` request) -- but whether ``getremotesession.asp`` itself is
    one of the endpoints that enforces that header is **not** confirmed either way, so this remains
    a documented, unverified gap for this specific endpoint rather than something the general fix
    is known to have closed. Only :class:`errors.ProtocolError` is degraded here, exactly like that
    sibling function -- a connection/authentication/timeout failure at this point is a real problem
    with the run, not this endpoint's documented quirk, and is allowed to propagate.
    """
    try:
        response = asp_client.get_webvar("getremotesession")
    except ProtocolError as err:
        return None, {"outcome": "failed", "error_class": err.error_class}
    if not response.records:
        return None, {"outcome": "failed", "error_class": None}
    return _decode_remote_session_preconditions(response.records[0]), {"outcome": "read", "error_class": None}


def _decode_vmediacfg_preconditions(record: dict) -> dict:
    """Decode the ``getvmediacfg.asp`` fields RV(asmb8.media.preconditions) needs.

    ``status_raw`` (C(V_MEDIA_STATUS)) is kept raw and unpaired with any other field here on
    purpose -- see this module's description for why its meaning is unsourced and it must never be
    used to infer live attach state.
    """
    return {
        "secure_channel_enabled": bool(record.get("V_STR_SECURE_CHANNEL")),
        "license_status_raw": record.get("V_MEDIA_LICENSE_STATUS"),
        "device_counts": {
            "cd": record.get("V_NUM_CD"),
            "fd": record.get("V_NUM_FD"),
            "hd": record.get("V_NUM_HD"),
        },
        "sessions": {
            "cd": {
                "max": decode_media_session_count(record.get("V_MAX_CD_SESSIONS")),
                "current": decode_media_session_count(record.get("V_ACTIVE_CD_SESSIONS")),
            },
        },
        "status_raw": record.get("V_MEDIA_STATUS"),
    }


def fetch_vmediacfg_preconditions(asp_client: AspClient) -> dict:
    """Read ``getvmediacfg.asp`` for RV(asmb8.media.preconditions).

    Unlike :func:`fetch_remote_session_preconditions`, a failure here is allowed to propagate and
    fail the whole module: this endpoint has shown no equivalent of ``getremotesession.asp``'s
    session-expired-page quirk in this project's testing, so silently degrading it to ``None`` would
    hide a real problem behind a result that looks like a clean, if empty, read. This matches
    :func:`gather_web_management_facts`'s own hard-fail behaviour for its one diagnostic read under
    O(include_web_session).
    """
    response = asp_client.get_webvar("getvmediacfg")
    if not response.records:
        raise ProtocolError(
            "getvmediacfg.asp returned no records",
            endpoint=asp_client.endpoint,
            operation="get_webvar:getvmediacfg",
        )
    return _decode_vmediacfg_preconditions(response.records[0])


def gather_media_preconditions(asp_client: AspClient) -> dict:
    """Assemble RV(asmb8.media.preconditions) over an already-authenticated ``.asp`` session.

    Only called when O(include_media_preconditions=true) -- which itself requires
    O(include_web_session=true); see :func:`main` for where that is enforced.
    """
    remote_fields, remote_session_read = fetch_remote_session_preconditions(asp_client)
    vmediacfg_fields = fetch_vmediacfg_preconditions(asp_client)

    return {
        "encryption": {
            "media_encryption_enabled": remote_fields["media_encryption_enabled"] if remote_fields else None,
            "secure_channel_enabled": vmediacfg_fields["secure_channel_enabled"],
        },
        "licensing": {"license_status_raw": vmediacfg_fields["license_status_raw"]},
        "attach": {"attach_raw": remote_fields["attach_raw"] if remote_fields else None},
        "device_counts": vmediacfg_fields["device_counts"],
        "sessions": vmediacfg_fields["sessions"],
        "status_raw": vmediacfg_fields["status_raw"],
        "remote_session_read": remote_session_read,
    }


def build_capabilities(*, web_management: dict | None, include_web_session: bool) -> dict:
    """Report what this board supports and, separately, what this collection has actually proven.

    See this module's DOCUMENTATION for the exact evidence behind each entry.
    Nothing here is inferred from this run's own reads -- IPMI capability
    entries are proven regardless of whether a particular field happened to
    read cleanly this time (that is what RV(operation.ipmi_reads) is for).
    """
    return {
        "ipmi_power": {
            "supported": True,
            "proven": True,
            "note": "Verified live against the target board: get_power() returned {'powerstate': 'on'}.",
        },
        "ipmi_boot_device": {
            "supported": True,
            "proven": True,
            "note": "Verified live against the target board: get_bootdev() returned a real override dict.",
        },
        "ipmi_mc_info": {
            "supported": True,
            "proven": True,
            "note": "Verified live: get_mci() returns a bare string, not a dict -- see module_utils/ipmi.py.",
        },
        "web_management": {
            "supported": True,
            "proven": bool(include_web_session and web_management is not None),
            "note": (
                "Proven for this run only when include_web_session=true and the login succeeded; not attempted otherwise."
                if not include_web_session
                else "Login succeeded this run."
            ),
        },
        "virtual_media": {
            "supported": None,
            "proven": False,
            "note": (
                "Not yet proven against this hardware. A null (not false) 'supported' value means unknown, not unsupported. "
                "See asmb8.media.preconditions (include_media_preconditions=true) for the settings that gate whether an "
                "attach could succeed, read without attempting one."
            ),
        },
        "remote_console": {
            "supported": None,
            "proven": False,
            "note": "Not yet proven against this hardware. A null (not false) 'supported' value means unknown, not unsupported.",
        },
        "redfish": {
            "supported": False,
            "proven": True,
            "note": "ASPEED AST2400 (this board's generation) predates Redfish support, which arrived with AST2500/ASMB9.",
        },
    }


def main() -> None:
    module = AnsibleModule(argument_spec=argument_spec(), supports_check_mode=True)

    if not HAS_PYGHMI:
        module.fail_json(msg=missing_required_lib("pyghmi"), exception=PYGHMI_IMPORT_ERROR)
        return
    if module.params["include_web_session"] and not HAS_REQUESTS:
        module.fail_json(msg=missing_required_lib("requests"), exception=REQUESTS_IMPORT_ERROR)
        return

    include_web_session = module.params["include_web_session"]
    include_media_preconditions = module.params["include_media_preconditions"]

    if include_media_preconditions and not include_web_session:
        # See include_media_preconditions's own documentation: media preconditions are read over
        # the same authenticated .asp session include_web_session creates, and this module never
        # creates that session implicitly -- include_web_session is documented as *the* one
        # exception to "never mutates", and a second, independent path to the same session would
        # make that no longer true. Checked before anything is contacted, so this fails fast rather
        # than after an IPMI read that would otherwise have succeeded.
        module.fail_json(
            msg=(
                "include_media_preconditions=true requires include_web_session=true: media preconditions are read "
                "over the same authenticated .asp session include_web_session creates, and this module never creates "
                "that session implicitly."
            )
        )
        return

    try:
        client = build_ipmi_client(module.params)
        ipmi_facts, ipmi_reads = gather_ipmi_facts(client)

        web_management = None
        media_preconditions = None
        if include_web_session:
            asp_client = build_asp_client(module.params)
            asp_client.login()
            web_management = gather_web_management_facts(asp_client)
            if include_media_preconditions:
                media_preconditions = gather_media_preconditions(asp_client)
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    asmb8 = {
        "reachable": True,
        "ipmi": ipmi_facts,
        "web_management": web_management,
        "capabilities": build_capabilities(web_management=web_management, include_web_session=include_web_session),
        "media": {"port_mode": "unknown", "preconditions": media_preconditions},
    }

    receipt = OperationReceipt(
        action="get_facts",
        endpoint=client.endpoint,
        changed=False,
        extra={"ipmi_reads": ipmi_reads},
    )
    module.exit_json(changed=False, asmb8=asmb8, operation=receipt.to_dict())


if __name__ == "__main__":
    main()
