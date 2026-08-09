# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations


class ModuleDocFragment:
    # Shared connection options for the ASMB8-iKVM BMC's web management plane
    # (the AMI ``.asp`` RPC surface and the JNLP-mediated KVM/media session
    # built on top of it -- see module_utils/asp.py). Every module in this
    # collection extends this fragment so the options are documented once.
    DOCUMENTATION = r"""
options:
  host:
    description:
      - Hostname or IP address of the ASMB8-iKVM BMC's web management interface.
      - This is the address of the BMC itself, which is usually distinct from the
        address of any operating system running on the same machine.
    type: str
    required: true
  port:
    description:
      - TCP port of the BMC's web management interface.
    type: int
    default: 443
  username:
    description:
      - BMC account used to authenticate against the AMI web session (C(WEBVAR_USERNAME)
        on the login form).
    type: str
    default: admin
  password:
    description:
      - Password for O(username) (C(WEBVAR_PASSWORD) on the login form).
      - Always supply this from a vaulted variable. Never inline it in a playbook.
      - This value is marked C(no_log) in every module's argument spec, so it is
        redacted from Ansible's own output. That redaction is declared in the
        argument spec rather than here on purpose - C(no_log) is not a valid key
        in a DOCUMENTATION block, and ansible-test's validate-modules rejects it
        with "extra keys not allowed". Do not add it back to this fragment.
    type: str
    required: true
  use_tls:
    description:
      - Whether to use TLS for the management connection.
      - >-
        This also selects the scheme used to fetch the JNLP document that allocates
        the KVM/media session (see O(allow_insecure_transport)). Observed directly on
        the target board, minutes apart on the same session -- fetching the JNLP over
        plain HTTP returns C(-kvmport 80 -kvmsecure 0 -vmsecure 0), while fetching it
        over HTTPS returns C(-kvmport 443 -kvmsecure 1 -vmsecure 1). Whether the
        resulting KVM/media session is itself encrypted is therefore a direct
        consequence of this option, not a separate, persistent board setting -- do not
        assume C(-vmsecure) reflects anything you configured on the BMC's own admin
        pages.
    type: bool
    default: true
  allow_insecure_transport:
    description:
      - Explicit acknowledgement required to talk to the BMC over unencrypted HTTP.
      - Never selected implicitly. When plaintext is used, the session cookie and the
        KVM/media token cross the network in a form an on-path attacker can recover.
        Only do this on an isolated management VLAN.
    type: bool
    default: false
  validate_certs:
    description:
      - Whether to verify the BMC's TLS certificate chain and hostname.
      - Only meaningful when O(use_tls=true). Ignored when O(tls_fingerprint) is set,
        because pinning is itself the trust decision.
      - >-
        In practice, chain validation V(true) without O(tls_fingerprint) cannot
        succeed against this board's factory certificate; see the note below. This
        option exists for the case of a certificate that has since been replaced with
        one issued by a real CA, not as the expected default posture.
    type: bool
    default: true
  ca_path:
    description:
      - Path to a CA bundle used to verify the BMC's certificate chain.
      - Selects CA trust mode. Mutually exclusive with O(tls_fingerprint).
    type: path
  tls_fingerprint:
    description:
      - SHA-256 fingerprint of the expected leaf certificate, for pinning.
      - >-
        This is the recommended trust mode for this board. The factory certificate
        observed on the target hardware is self-signed (subject and issuer both
        C(CN=AMI, O=American Megatrends Inc)) with a validity window of
        2016-06-01 to 2026-05-30 -- i.e. it is already expired at the time of
        writing, and chain validation cannot succeed against it regardless of
        O(ca_path). Fingerprint pinning is the only trust mode that works without
        either replacing the certificate on the BMC or falling back to
        O(allow_insecure_transport).
      - Accepted with or without colon separators and in any case. An optional
        V(sha256:) prefix is allowed.
      - Mutually exclusive with O(ca_path).
    type: str
  timeout:
    description:
      - Timeout in seconds for an individual HTTP request to the BMC.
      - >-
        This BMC's web server is HTTP/1.0 with no keep-alive and a worker pool that
        saturates under concurrent load; when that happens it completes the TCP
        handshake and then never serves the request. This module's client
        distinguishes that condition (V(bmc_busy)) from an ordinary timeout, but this
        option still bounds how long a call waits before giving up either way.
    type: int
    default: 30
  connect_timeout:
    description:
      - Timeout in seconds for establishing the TCP and TLS connection.
    type: int
    default: 10
notes:
  # Block scalars are required on any item containing a colon-space sequence,
  # such as C(delegate_to: localhost). In a plain scalar that reads as a YAML
  # mapping key and fails the yamllint sanity test.
  - >-
    An ASMB8-iKVM BMC is firmware and cannot execute a Python payload, so these
    modules run on the Ansible controller. Use C(delegate_to: localhost) (or
    C(connection: local)) on every task. No agent, SSH access, or Python interpreter
    is required on the target.
  - >-
    Because the modules run on the controller, the C(requests) library must be
    installed there, not on the managed node.
  - >-
    This board's TLS listener was observed, against the target hardware, to offer
    TLS 1.2 only (TLS 1.0/1.1/1.3 are all refused) and exactly one ciphersuite,
    C(AES256-GCM-SHA384) -- static RSA key exchange with no forward secrecy. Modern
    OpenSSL/Python builds exclude non-forward-secret ciphersuites from their default
    list, so a plain C(requests) call fails the handshake outright
    (C(SSLV3_ALERT_HANDSHAKE_FAILURE)); this is not a bug in this collection, and
    C(curl), which is more permissive by default, will appear to work where a naive
    Python client does not. This collection's client re-enables the required cipher
    itself, so this is transparent to playbooks -- it is documented here only so a
    report of "TLS fails outside this collection's modules" is not mistaken for a
    regression in them.
  - >-
    The BMC's own clock was observed reporting a date years in the past (2018) on
    hardware that was, at the time, running in 2026. Never rely on a timestamp the
    BMC itself reports (event logs, certificate validity as seen from the BMC's own
    perspective, etc.) as authoritative; treat the controller's clock as the source
    of truth for anything this collection timestamps.
  - >-
    Power and boot operations are physically disruptive. Delegating to localhost
    does not stop Ansible from fanning a task out across every host in the play,
    so pair these tasks with C(serial: 1) and an explicit single-target selection
    when mutating state.
  - >-
    This collection serializes every request to a given BMC and bounds its own
    retries rather than issuing concurrent requests, specifically because
    concurrent load against this board's web server has been observed, against the
    target hardware, to exhaust its per-listener worker pool and lock out the BMC's
    own web UI for several minutes. Do not run multiple tasks against the same BMC
    in parallel (C(strategy: free), looped async tasks, etc.) expecting this
    collection's serialization to protect you across separate module invocations --
    it only serializes requests made through the same client instance.
requirements:
  - requests >= 2.25.0 (on the Ansible controller)
"""

    # Attribute descriptions, kept separate so modules can document check-mode and
    # diff-mode support consistently.
    ATTRIBUTES = r"""
options: {}
attributes:
  check_mode:
    description: Can run in C(check_mode) and return a changed-status prediction without modifying the target.
  diff_mode:
    description: Returns details on what has changed, or would change in C(check_mode).
"""
