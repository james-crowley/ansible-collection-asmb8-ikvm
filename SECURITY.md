# Security policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report privately via GitHub's
[private vulnerability reporting](https://github.com/james-crowley/ansible-collection-asmb8-ikvm/security/advisories/new)
(Security → Report a vulnerability). If that is unavailable to you, open a
minimal public issue saying only *"security report, requesting a private
channel"* with no technical detail, and a maintainer will follow up.

Expect an acknowledgement within a week. There is no bug bounty.

## Why this collection warrants unusual care

A BMC (baseboard management controller) is a management processor that runs
**beneath the operating system**. It is powered whenever the machine has
power (or standby power), it is reachable when the host OS is off, and it
cannot be observed or restricted by anything the host OS runs. Practically:

**ASMB8-iKVM admin credentials are equivalent to physical access to the
machine.** Someone holding them can power it on or off, force a boot-device
override over IPMI, and — with `asmb8_media` — boot it from media of their
own choosing, regardless of what the installed operating system wants. Treat
a leaked BMC password as you would treat handing over the machine.

Two things about this collection's design deserve specific attention while it
is still pre-1.0 and not hardware-qualified (see
[`docs/capability-matrix.md`](docs/capability-matrix.md)).

### The iUSB virtual-media protocol is proprietary, undocumented, and reverse engineered

AMI's iUSB protocol, which `asmb8_media` speaks, has no public specification.
This collection's implementation was built by reverse engineering — a
third-party Go reference client (`BadCoder1337/rd450x-console`,
MIT-licensed), an older Lua/Python artifact for a different device class, and
a local, non-redistributed decompilation of the vendor's own JViewer client
retrieved from the target hardware — not from vendor documentation. See
[NOTICE](NOTICE) for the full, per-file provenance accounting.

That has a direct security consequence, stated in
[`docs/protocol-notes.md`](docs/protocol-notes.md) and
[`docs/capability-matrix.md`](docs/capability-matrix.md) Tier 4 as well:
**no claim about the iUSB channel's authentication or confidentiality
properties beyond what riding on top of the `.asp` web session provides has
been independently verified.** The session token (`-kvmtoken`) is minted by
the same login that authenticates the `.asp` surface; whether the iUSB socket
itself adds any further authentication or confidentiality guarantee of its
own is not measured either way.

### The factory TLS certificate can never be chain-validated on this board

The target board's factory certificate is self-signed **and already
expired** (validity 2016-06-01 to 2026-05-30 — see
[`docs/hardware-evidence-2026-08-08.md`](docs/hardware-evidence-2026-08-08.md)).
`ca_path`/`validate_certs=true` chain validation cannot succeed against it
under any circumstances; `tls_fingerprint` (SHA-256 leaf pinning) is the only
trust mode that actually works without replacing the certificate on the BMC.

Plaintext HTTP is reachable, but only when the caller sets **both**
`use_tls: false` and `allow_insecure_transport: true` — never selected
implicitly, and this collection will not probe for TLS and silently fall
back. When plaintext is used, the session cookie and the iUSB/KVM media
token cross the network recoverable by an on-path attacker; only use it on
an isolated management VLAN.

If you find a path that downgrades transport or skips a configured trust
check without that explicit acknowledgement, that is a vulnerability.

### Power and boot control wrap `pyghmi` directly, not `community.general`

`asmb8_power`, `asmb8_boot`, and `asmb8_info`'s IPMI facts call `pyghmi`
directly rather than wrapping `community.general.ipmi_power`/`ipmi_boot` —
see `galaxy.yml`'s empty `dependencies` and the top-level `README.md`'s
"Requirements" section for why. This collection inherits whatever security
properties (and limitations) `pyghmi` and the underlying IPMI 2.0/RMCP+
protocol have; it does not add or remove authentication guarantees on that
path, and `asmb8_boot` refuses persistent (beyond-next-boot) boot-order
changes outright regardless of what is asked for.

## Credential handling

Report anything that contradicts the following, as each is intended
behaviour:

- `password` is `no_log` in every module's argument spec.
- Every user-visible message and diagnostic passes through the redaction
  layer in `plugins/module_utils/errors.py`'s `redact()`, which strips
  `Authorization`/`Cookie` headers, this BMC's own secret-shaped field names
  (`SESSION_COOKIE`, `WEBVAR_PASSWORD`, `kvmtoken`, `webcookie`, `STOKEN`),
  generic `password=`/`token=`-shaped text, and bounds excerpt length.
- Credentials, session cookies, and iUSB/KVM tokens are never written to
  operation receipts, facts, or the `asmb8_media` session state file — see
  `plugins/module_utils/models.py`'s `OperationReceipt`/`JnlpSession`.
- `asmb8_media` spawns its long-lived background session by forking, not by
  `subprocess`/`exec`, so credentials cross into it as in-memory values and
  never appear in `argv` or the process environment where other local users
  could read them.
- Session state files under `runtime_dir` are created mode `0600`;
  `runtime_dir` itself is created mode `0700`.
- Hardware qualification evidence is redacted before CI publishes it.
  CircleCI masks context values in **log output only** — masking does not
  extend to `store_artifacts` content — so `tests/hardware/redact-evidence.py`
  runs immediately before every `store_artifacts` of
  `tests/hardware/output`, and the published artifact must be safe
  regardless of the project's artifact visibility setting. See
  [`tests/hardware/README.md`](tests/hardware/README.md#evidence-redaction).

## Scope

In scope: this collection's Python code, its roles, and any CI configuration
in this repository.

Out of scope: vulnerabilities in the ASMB8's own MegaRAC firmware itself
(report those to ASUS/American Megatrends), in `ansible-core`, in
`requests`, or in `pyghmi`.

## Supported versions

Pre-1.0 and not hardware-qualified (see `docs/capability-matrix.md`). Only
the latest commit on `main` receives fixes; there are no maintained release
branches yet.
