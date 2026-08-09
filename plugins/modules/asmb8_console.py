#!/usr/bin/python
# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
module: asmb8_console
short_description: Open an ASMB8-iKVM console/KVM (IVTP) session headlessly
description:
  - >-
    Opens an ASMB8-iKVM BMC's KVM/console-redirection channel the same way the vendor's Java
    C(JViewer) client does, but headlessly -- no Java, no JRE, and no on-screen window. It logs in
    to the C(.asp) web-management surface, fetches C(jviewer.jnlp) to mint a fresh C(-kvmtoken) and
    allocate a video session, then speaks AMI's proprietary IVTP protocol directly over
    O(kvm_port) to complete the session handshake.
  - >-
    B(How this differs from M(james_crowley.asmb8_ikvm.asmb8_redirection).) This module opens a
    live console/video channel -- it logs in, allocates a session, and speaks IVTP. It has nothing
    to say about whether the BMC's C(kvm) service (or any other service) is currently enabled at
    the configuration level, beyond what actually opening a session reveals in passing; use
    M(james_crowley.asmb8_ikvm.asmb8_redirection) to report (and, once a real RPC exists, toggle)
    a service's enablement without ever opening a channel at all. The two modules were originally
    one, confusingly named after this one's behaviour while carrying the other's name -- this
    module is what moved when that was split apart; see C(docs/asmb8_console.md) and
    C(docs/asmb8_redirection.md) for the full accounting.
  - >-
    B(What this module actually proves, honestly.) The full AMI/ASPEED video codec (a hybrid
    vector-quantisation + JPEG/DCT tile stream, optionally RC4-obfuscated) is B(not) implemented by
    this collection -- porting it is a large, separate undertaking this module does not attempt,
    and it never fabricates or approximates a decode. O(capture=handshake_only) (the default) only
    proves the channel is live: it completes the session handshake and returns the negotiated
    facts in RV(channel). O(capture=raw_frame) additionally waits for and saves one complete video
    frame's raw, still-encoded bytes -- B(not) a viewable image -- to O(output_path), clearly
    labelled as undecoded in RV(frame). O(capture=decoded_frame) always fails with
    RV(ignore:error_class) V(unsupported_capability): asking for a decoded image is refused rather
    than answered with a placeholder.
  - >-
    B(TLS for the KVM socket is governed by O(kvm_secure), never inferred from O(kvm_port).)
    Observed directly from the vendor's own decompiled client: whether the video socket is
    TLS-wrapped is carried by a boolean flag, completely independent of which TCP port is dialled.
    When O(token) is not supplied, O(kvm_secure) defaults to whatever the C(jviewer.jnlp) fetch
    itself reported for this session (which follows the scheme O(use_tls) selects for that fetch --
    see the connection fragment's note on O(use_tls)); set O(kvm_secure) explicitly to override it,
    and it B(must) be set explicitly whenever O(token) is supplied, since there is then no JNLP
    response to read it from.
  - >-
    B(O(kvm_port) refuses connections until a session is allocated, and that is normal.) On the
    target hardware this listener is on-demand: it binds only after the C(.asp) login plus the
    C(jviewer.jnlp) fetch allocate a session. This module's own flow always performs that
    allocation before dialling O(kvm_port), so this is transparent to a normal run; it is noted
    here only so an operator manually probing O(kvm_port) outside of this module is not alarmed to
    find it closed with no session active. M(james_crowley.asmb8_ikvm.asmb8_redirection) documents
    this same on-demand behaviour from its own read-only, no-session-opened point of view.
  - >-
    B(This BMC's KVM service is far less prone to slot exhaustion than virtual media.) Per the task
    brief this collection was built against: the C(kvm) service allows B(4 concurrent sessions)
    with an B(1800-second) server-side inactivity timeout, unlike M(james_crowley.asmb8_ikvm.asmb8_media)'s
    virtual-media channel, which permits exactly one session with no server-side reclaim at all.
    RV(ignore:error_class) V(bmc_busy) can still surface here -- it is inherited from the
    C(.asp) login/JNLP-allocation step this module performs before ever touching O(kvm_port) (see
    C(module_utils/asp.py)) -- but hitting the KVM slot limit itself would require 4 other sessions
    already attached, not merely one stale prior session, which is M(james_crowley.asmb8_ikvm.asmb8_media)'s
    far easier-to-hit failure mode.
  - >-
    This module never mutates persistent BMC state -- RV(changed) is always V(false), the same
    convention M(james_crowley.asmb8_ikvm.asmb8_info) uses for its own C(include_web_session). A
    KVM session is opened and, best-effort, closed again (C(STOP_SESSION_IMMEDIATE) is sent, then
    the socket is closed) before this module returns; nothing about the BMC's standing
    configuration is changed by running it.
version_added: 0.1.0
author:
  - Jim Crowley (@james-crowley)
extends_documentation_fragment:
  - james_crowley.asmb8_ikvm.connection
options:
  kvm_port:
    description:
      - TCP port of the BMC's IVTP KVM/console-redirection listener.
      - >-
        Confirmed on the target board's current configuration: this port is plaintext-only there
        (the paired secure port, 7582 on this protocol family, is refused outright because
        media/KVM encryption is disabled in this BMC's configuration) -- see O(kvm_secure).
    type: int
    default: 7578
  kvm_secure:
    description:
      - >-
        Whether the KVM socket itself is TLS-wrapped. B(Independent of O(kvm_port)) -- see the
        module description. When O(token) is not set, defaults to whatever the C(jviewer.jnlp)
        fetch reports for O(kvm_secure)/C(vmsecure) on this session; set this explicitly to
        override that. B(Required) when O(token) is set, since this module then has no JNLP
        response of its own to read the flag from.
      - >-
        On the target hardware this is V(false): the secure KVM port is refused outright because
        media/KVM encryption is disabled in this BMC's configuration, so the plaintext channel is
        what actually works there today.
    type: bool
  token:
    description:
      - >-
        A pre-existing C(-kvmtoken) (16 characters on the target hardware), if the caller already
        holds one from a prior C(jviewer.jnlp) fetch (for example, one minted by a concurrently
        running M(james_crowley.asmb8_ikvm.asmb8_media) session) and wants to open a KVM channel
        without a second web login. B(Requires) O(kvm_secure) to be set explicitly alongside it.
      - >-
        When omitted (the default), this module performs its own C(.asp) login and
        C(jviewer.jnlp) fetch using O(username)/O(password), exactly as
        M(james_crowley.asmb8_ikvm.asmb8_media) does for virtual media, and mints a fresh token
        for this call only.
      - This value is never written to the RV(channel) facts, the RV(operation) receipt, or any error message.
      - >-
        Marked as a secret in this module's argument spec, so Ansible redacts it
        from its own output. That marking is declared in the argument spec rather
        than in this documentation block on purpose - the key is not valid here,
        and ansible-test's validate-modules rejects it with "extra keys not
        allowed". Do not add it back to this block.
    type: str
  client_username:
    description:
      - >-
        The client-side username presented in the C(VALIDATE_VIDEO_SESSION) handshake packet.
        Per the decompiled vendor client, this is the B(local machine's) OS username
        (C(System.getProperty("user.name"))), B(not) the BMC account named by O(username) -- this
        module follows that same convention by default, detecting the controller's own OS
        username. Override only if that default does not make sense for your environment; this
        field's effect on the BMC's behaviour has not been confirmed against live hardware either
        way.
    type: str
  send_get_web_token:
    description:
      - >-
        Whether to send an extra C(GET_WEB_TOKEN) (opcode 21) packet, carrying O(token), before
        C(VALIDATE_VIDEO_SESSION). The decompiled vendor client sends this on its standalone-app
        code path, which this headless module is closest to -- but whether the BMC actually
        requires it, as opposed to merely tolerating it, has not been confirmed against live
        hardware. Kept as an option, rather than hardcoded either way, specifically so that can be
        tested without a code change once hardware access is available.
    type: bool
    default: true
  capture:
    description:
      - >-
        What to do once the KVM session handshake completes. V(handshake_only) (the default) does
        nothing further and reports RV(channel) only -- this is the "confirm the channel is live"
        path. V(raw_frame) additionally waits for one complete video frame and saves its raw,
        still-encoded bytes to O(output_path) -- see RV(frame) for why this is explicitly not a
        viewable image. V(decoded_frame) always fails immediately, before any network is touched,
        with RV(ignore:error_class) V(unsupported_capability): decoding the AMI/ASPEED VQ+JPEG/DCT
        (optionally RC4-obfuscated) video codec into pixels is not implemented by this collection,
        and this module refuses to fabricate one rather than silently downgrading to
        V(handshake_only) or returning a placeholder image.
    type: str
    choices: [handshake_only, raw_frame, decoded_frame]
    default: handshake_only
  output_path:
    description:
      - >-
        Path, on the Ansible controller, to write the captured frame's raw bytes to. B(Required)
        for O(capture=raw_frame); ignored otherwise. Written mode C(0600). See RV(frame) for the
        exact, undecoded shape of what is written here.
    type: path
  handshake_timeout:
    description:
      - >-
        Seconds to wait for each step of the IVTP session handshake (the BMC's initial greeting,
        and its response to C(VALIDATE_VIDEO_SESSION)). Distinct from O(connect_timeout), which
        only bounds the TCP/TLS connect itself.
    type: int
    default: 15
  frame_timeout:
    description:
      - >-
        Seconds to wait for one complete video frame once O(capture=raw_frame). The ASPEED video
        engine only sends fragments for changed screen content, so an idle/unchanged host display
        may not produce a full frame within this bound even though the channel itself is healthy
        -- a timeout here is reported as RV(ignore:error_class) V(timeout), not necessarily a
        protocol fault. Raise this value, or ensure the target host's display is actually changing,
        if this is hit routinely.
    type: int
    default: 20
seealso:
  - module: james_crowley.asmb8_ikvm.asmb8_redirection
  - module: james_crowley.asmb8_ikvm.asmb8_media
  - module: james_crowley.asmb8_ikvm.asmb8_info
attributes:
  check_mode:
    description: >-
      Supported. Validates options (including that O(output_path)'s parent directory exists for
      O(capture=raw_frame), and that O(capture=decoded_frame) is rejected) but never logs in, never
      fetches the JNLP, and never opens a connection to O(kvm_port).
    support: full
  diff_mode:
    description: Not supported. Use RV(channel)/RV(frame) and the C(operation) receipt instead.
    support: none
requirements:
  - requests >= 2.25.0 (on the Ansible controller)
"""

EXAMPLES = r"""
- name: Confirm the KVM channel is live without capturing anything
  james_crowley.asmb8_ikvm.asmb8_console:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    capture: handshake_only
  delegate_to: localhost
  no_log: true
  register: kvm_check

- name: Capture one raw (undecoded) console frame for offline inspection
  james_crowley.asmb8_ikvm.asmb8_console:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    tls_fingerprint: "{{ asmb8_tls_fingerprint }}"
    capture: raw_frame
    output_path: /tmp/asmb8-frame.raw
  delegate_to: localhost
  no_log: true
  register: kvm_frame

- name: Requesting a decoded image fails honestly instead of faking one
  james_crowley.asmb8_ikvm.asmb8_console:
    host: "{{ asmb8_host }}"
    username: "{{ asmb8_username }}"
    password: "{{ asmb8_password }}"
    capture: decoded_frame
    output_path: /tmp/asmb8-frame.png
  delegate_to: localhost
  no_log: true
  register: kvm_decode_attempt
  ignore_errors: true

- name: Assert the decode attempt failed the honest way
  ansible.builtin.assert:
    that:
      - kvm_decode_attempt is failed
      - kvm_decode_attempt.error_class == 'unsupported_capability'
"""

RETURN = r"""
changed:
  description: Always V(false) -- see the module description.
  type: bool
  returned: always
capture:
  description: The O(capture) mode this call ran with.
  type: str
  returned: always
channel:
  description: >-
    The negotiated handshake facts, present whenever the handshake completed (i.e. whenever this
    module did not fail before RV(operation.error_class) V(null)).
  type: dict
  returned: on success
  contains:
    session_accepted:
      description: Always V(true) on success -- the BMC's initial greeting was SESSION_ACCEPTED.
      type: bool
    greeting_body_len:
      description: Byte length of the greeting's own body (an active-client list this module does not parse).
      type: int
    validate_status:
      description: The raw C(VALIDATE_VIDEO_SESSION_RESPONSE) status byte. V(1) is VALID_SESSION.
      type: int
    validate_status_name:
      description: A human-readable name for RV(channel.validate_status).
      type: str
    validate_sub_status:
      description: >-
        A second status byte the BMC sometimes includes; V(null) when absent. The decompiled
        vendor client never names what this means, and neither does this module.
      type: int
    resumed:
      description: Always V(true) on success -- C(RESUME_REDIRECTION) was sent after validation.
      type: bool
frame:
  description: Present only when O(capture=raw_frame) succeeded.
  type: dict
  returned: when O(capture=raw_frame)
  contains:
    decoded:
      description: >-
        Always V(false). The bytes at RV(frame.output_path) are the raw, still-encoded
        (VQ+JPEG/DCT tile stream, possibly RC4-obfuscated) fragment data for one frame -- B(not) a
        viewable image, and not something an image viewer or browser can open. Decoding this is not
        implemented by this collection; see the module description.
      type: bool
    bytes_written:
      description: Number of raw bytes written to RV(frame.output_path).
      type: int
    output_path:
      description: Mirrors O(output_path).
      type: str
operation:
  description: >-
    The C(asmb8-ikvm-operation/v1) receipt for this call, in the same nested shape every other
    module in this collection returns it under.
  type: dict
  returned: always
  contains:
    schema:
      description: Always V(asmb8-ikvm-operation/v1).
      type: str
    action:
      description: Always V(asmb8_console.capture).
      type: str
    endpoint:
      description: The C(host:kvm_port) this call connected (or attempted to connect) to.
      type: str
    changed:
      description: Always V(false).
      type: bool
    observed:
      description: Mirrors RV(channel), or V(null) if the handshake never completed (including check mode).
      type: dict
    error_class:
      description: A stable machine-readable failure class. V(null) on success.
      type: str
"""

import contextlib
import dataclasses
import getpass
import os
from pathlib import Path

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import ivtp
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import HAS_REQUESTS, REQUESTS_IMPORT_ERROR, AspClient, TlsTrustPolicy
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import IkvmError, ProtocolError, UnsupportedCapabilityError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.iusb import resolve_local_ip
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.models import OperationReceipt

CAPTURE_HANDSHAKE_ONLY = "handshake_only"
CAPTURE_RAW_FRAME = "raw_frame"
CAPTURE_DECODED_FRAME = "decoded_frame"


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
            "kvm_port": {"type": "int", "default": 7578},
            "kvm_secure": {"type": "bool"},
            "token": {"type": "str", "no_log": True},
            "client_username": {"type": "str"},
            "send_get_web_token": {"type": "bool", "default": True},
            "capture": {"type": "str", "choices": [CAPTURE_HANDSHAKE_ONLY, CAPTURE_RAW_FRAME, CAPTURE_DECODED_FRAME], "default": CAPTURE_HANDSHAKE_ONLY},
            "output_path": {"type": "path"},
            "handshake_timeout": {"type": "int", "default": 15},
            "frame_timeout": {"type": "int", "default": 20},
        }
    )
    return spec


def build_asp_client(params: dict) -> AspClient:
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


def default_client_username() -> str:
    """The controller's own OS username, matching the decompiled vendor client's
    ``getClientUserName()`` (``System.getProperty("user.name")``) -- see O(client_username)'s docs
    for why this is deliberately not the BMC account name.
    """
    try:
        return getpass.getuser()
    except OSError:  # pragma: no cover - only reachable on a controller with no resolvable user identity.
        return "ansible"


def validate_output_path(output_path: str | None) -> None:
    """Fail fast, before any network is touched, if O(output_path) could never be written to.

    Mirrors ``media_session.validate_image``'s reasoning: a bad path should be a synchronous,
    obvious failure, not something that only shows up after a session was already opened and
    a frame already captured.
    """
    if not output_path:
        raise ProtocolError("asmb8_console capture=raw_frame requires output_path to be set", operation="asmb8_console.capture")
    parent = Path(output_path).parent
    if not parent.is_dir():
        raise ProtocolError(f"asmb8_console: output_path's parent directory {parent!r} does not exist", operation="asmb8_console.capture")


def resolve_token_and_security(params: dict) -> tuple[str, bool, str]:
    """Obtain a KVM token (minting one via login+JNLP if the caller did not supply one) plus the
    O(kvm_secure) flag and the client IP to present in the handshake.

    Returns ``(token, kvm_secure, client_ip)``. Never logs or returns the token itself.
    """
    client_ip = resolve_local_ip(params["host"])
    token = params.get("token")
    kvm_secure = params.get("kvm_secure")

    if token is not None:
        if kvm_secure is None:
            # Backstop: argument_spec's required_by should already have refused this combination.
            raise ProtocolError("asmb8_console: kvm_secure must be set explicitly when token is supplied", operation="asmb8_console.capture")
        return token, kvm_secure, client_ip

    asp_client = build_asp_client(params)
    asp_client.login()
    jnlp = asp_client.allocate_media_session(client_ip=client_ip, secure=kvm_secure)
    if jnlp.kvm_token is None:
        # allocate_media_session() itself already raises ProtocolError when no token is found in
        # the JNLP; this is an extra, structural guard so a future change there cannot silently
        # let a None token reach the IVTP layer.
        raise ProtocolError("jviewer.jnlp allocation did not yield a usable KVM token", endpoint=asp_client.endpoint, operation="asmb8_console.capture")
    if kvm_secure is None:
        kvm_secure = bool(jnlp.kvm_secure)
    return jnlp.kvm_token, kvm_secure, client_ip


def build_kvm_transport(params: dict, *, kvm_secure: bool) -> ivtp.SocketTransport:
    ssl_context = None
    if kvm_secure:
        policy = TlsTrustPolicy.create(
            validate_certs=params["validate_certs"],
            ca_path=params["ca_path"],
            tls_fingerprint=params["tls_fingerprint"],
        )
        ssl_context = policy.build_ssl_context()
    return ivtp.SocketTransport.connect(params["host"], params["kvm_port"], timeout=params["connect_timeout"], ssl_context=ssl_context)


def channel_facts_dict(facts: ivtp.ChannelFacts) -> dict:
    return dataclasses.asdict(facts)


def write_frame(output_path: str, frame: bytes) -> None:
    """Write ``frame``'s raw bytes to ``output_path``, mode 0600, create-or-truncate."""
    fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(frame)


def run_capture(params: dict, *, endpoint: str) -> dict:
    token, kvm_secure, client_ip = resolve_token_and_security(params)

    transport = build_kvm_transport(params, kvm_secure=kvm_secure)
    frame_info: dict | None = None
    try:
        username = params.get("client_username") or default_client_username()
        facts = ivtp.open_channel(
            transport,
            token=token,
            client_ip=client_ip,
            username=username,
            handshake_timeout=float(params["handshake_timeout"]),
            send_get_web_token=params["send_get_web_token"],
        )

        if params["capture"] == CAPTURE_RAW_FRAME:
            frame = ivtp.capture_one_frame(transport, frame_timeout=float(params["frame_timeout"]))
            output_path = params["output_path"]
            write_frame(output_path, frame)
            frame_info = {
                "decoded": False,
                "bytes_written": len(frame),
                "output_path": output_path,
            }
    finally:
        with contextlib.suppress(Exception):
            transport.send_all(ivtp.build_stop_session())
        transport.close()

    del token

    receipt = OperationReceipt(
        action="asmb8_console.capture",
        endpoint=endpoint,
        changed=False,
        previous=None,
        desired=params["capture"],
        observed=facts,
    )
    return {
        "changed": False,
        "capture": params["capture"],
        "channel": channel_facts_dict(facts),
        "frame": frame_info,
        "operation": receipt.to_dict(),
    }


def main() -> None:
    module = AnsibleModule(
        argument_spec=argument_spec(),
        required_if=[("capture", CAPTURE_RAW_FRAME, ["output_path"])],
        required_by={"token": ["kvm_secure"]},
        supports_check_mode=True,
    )
    params = module.params

    if not HAS_REQUESTS:
        module.fail_json(msg=missing_required_lib("requests"), exception=REQUESTS_IMPORT_ERROR)
        return

    endpoint = f"{params['host']}:{params['kvm_port']}"

    try:
        if params["capture"] == CAPTURE_DECODED_FRAME:
            raise UnsupportedCapabilityError(
                "capture=decoded_frame is not implemented. Decoding the AMI/ASPEED VQ+JPEG(DCT), "
                "optionally RC4-obfuscated, video codec into pixels is a large separate undertaking this "
                "collection has not completed; use capture=raw_frame to save the raw, still-encoded frame "
                "bytes instead, or capture=handshake_only to confirm the channel is live without saving anything.",
                endpoint=endpoint,
                operation="asmb8_console.capture",
            )

        if params["capture"] == CAPTURE_RAW_FRAME:
            validate_output_path(params.get("output_path"))

        if module.check_mode:
            receipt = OperationReceipt(
                action="asmb8_console.capture",
                endpoint=endpoint,
                changed=False,
                previous=None,
                desired=params["capture"],
                observed=None,
            )
            module.exit_json(changed=False, capture=params["capture"], channel=None, frame=None, operation=receipt.to_dict())
            return

        result = run_capture(params, endpoint=endpoint)
    except IkvmError as err:
        module.fail_json(**err.to_result())
        return

    module.exit_json(**result)


if __name__ == "__main__":
    main()
