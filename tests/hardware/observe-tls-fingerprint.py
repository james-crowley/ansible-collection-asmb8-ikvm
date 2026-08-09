# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

# Intentionally has no shebang and is not executable: ansible-test's `shebang`
# sanity test rejects a non-module shebang inside a collection. Invoke it as
#   python3 tests/hardware/observe-tls-fingerprint.py <host> <port> [<port> ...]

"""Observe and report the SHA-256 leaf-certificate fingerprint of an ASMB8-iKVM BMC.

Adapted from the sibling ``james_crowley.intel_amt`` collection's script of the
same name and purpose; read that file's docstring if you have not, the
reasoning below is unchanged. This exists to bootstrap fingerprint pinning:
this collection requires an explicit TLS trust decision, and for this board
that decision can only ever be a pinned leaf certificate -- the factory
certificate is self-signed **and already expired** (see
``docs/hardware-evidence-2026-08-08.md``'s "Transport and trust" section, valid
2016-06-01 to 2026-05-30), so chain and hostname verification (O(ca_path),
O(validate_certs)) cannot succeed against it regardless of what this script
finds. A pin still has to come from somewhere, and the only way to learn it is
to look once.

So this script looks, and prints what it saw. It does **not** write the value
anywhere or configure anything. A human reads the output, decides whether the
endpoint is the board they think it is, and stores the value as
``ASMB8_TLS_FINGERPRINT`` in the restricted CI context. That review is what
makes it a *reviewed* pin rather than blind trust-on-first-use.

Deliberately sends no credentials -- it completes a TLS handshake and reads
the certificate the server offers, nothing more. This is exactly the
``hardware-observe`` job's own justification for needing no approval gate: it
sends nothing that could be rejected and mutates no BMC-side state.

Usage:
    python3 tests/hardware/observe-tls-fingerprint.py <host> <port> [<port> ...]
"""

from __future__ import annotations

import hashlib
import socket
import ssl
import sys

#: Generous, but bounded. This BMC's web server answers quickly on a quiet
#: LAN; anything approaching this means the port is filtered.
CONNECT_TIMEOUT_SECONDS = 10.0


def observe(host: str, port: int) -> tuple[str, str] | None:
    """Return ``(sha256_hex, summary)`` for the leaf certificate at host:port.

    Returns ``None`` and prints the reason on any failure. Verification is
    disabled on purpose: the entire point is to inspect a certificate that
    cannot yet be verified, because we do not know what to trust yet.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS) as raw:
            with context.wrap_socket(raw, server_hostname=None) as tls:
                der = tls.getpeercert(binary_form=True)
                negotiated = tls.version() or "unknown"
                cipher = tls.cipher()
    except TimeoutError:
        print(f"  {host}:{port}  TIMEOUT after {CONNECT_TIMEOUT_SECONDS:.0f}s -- port filtered, or TLS not enabled here")
        return None
    except ConnectionRefusedError:
        print(f"  {host}:{port}  REFUSED -- nothing listening.")
        return None
    except ssl.SSLError as exc:
        print(f"  {host}:{port}  TLS ERROR -- {exc}")
        print("                  This board offers TLS 1.2 only, with exactly one ciphersuite")
        print("                  (AES256-GCM-SHA384, static RSA, no forward secrecy) -- see")
        print("                  docs/hardware-evidence-2026-08-08.md. A modern OpenSSL build")
        print("                  excludes that ciphersuite by default, which can surface here as")
        print("                  a handshake failure even though the port is genuinely listening.")
        return None
    except OSError as exc:
        print(f"  {host}:{port}  UNREACHABLE -- {exc}")
        return None

    if not der:
        print(f"  {host}:{port}  connected but offered no certificate")
        return None

    digest = hashlib.sha256(der).hexdigest()
    colon_form = ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
    cipher_name = cipher[0] if cipher else "unknown"
    print(f"  {host}:{port}  {negotiated}, {cipher_name}")
    print(f"      sha256: {digest}")
    print(f"      colons: {colon_form}")
    return digest, f"{host}:{port}"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2

    host = argv[1]
    ports = []
    for raw_port in argv[2:]:
        try:
            ports.append(int(raw_port))
        except ValueError:
            print(f"not a port number: {raw_port}", file=sys.stderr)
            return 2

    print(f"Observing TLS certificates offered by {host} (no credentials sent):")
    print()
    observed: dict[int, str] = {}
    for port in ports:
        result = observe(host, port)
        if result is not None:
            observed[port] = result[0]
        print()

    if not observed:
        print("No certificate could be observed on any port.")
        print("Nothing to pin, so the credentialed qualification jobs cannot run over TLS.")
        return 1

    distinct = set(observed.values())
    if len(distinct) > 1:
        print("WARNING: the ports did not all present the same certificate.")
        for port, digest in sorted(observed.items()):
            print(f"  port {port}: {digest}")
        print("Investigate before pinning: an on-path device may be terminating TLS.")
        return 1

    fingerprint = next(iter(distinct))
    print("=" * 72)
    print("All observed ports present the same leaf certificate.")
    print()
    print("This board's factory certificate is self-signed AND already expired")
    print("(see docs/hardware-evidence-2026-08-08.md) -- that is expected, and pinning")
    print("this exact leaf fingerprint is the only trust mode that works regardless.")
    print()
    print("Review this value, confirm the endpoint is the board you expect, then store it:")
    print()
    print("  circleci context store-secret asmb8-lab --org gh/james-crowley ASMB8_TLS_FINGERPRINT")
    print(f"  # value: {fingerprint}")
    print()
    print("Until it is stored, the credentialed hardware jobs will refuse to run, because")
    print("verify-hardware-credentials requires either this or an explicit")
    print("ASMB8_ALLOW_INSECURE=true acknowledgement. That refusal is deliberate.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
