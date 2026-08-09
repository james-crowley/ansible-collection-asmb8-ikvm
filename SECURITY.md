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
**beneath the operating system**. It is powered whenever the machine has power
(or has standby power), it is reachable when the host OS is off, and it cannot
be observed or restricted by anything the host OS runs. Practically:

**ASMB8 admin credentials are equivalent to physical access to the machine.**
Someone holding them can power it on or off, force a boot device override over
IPMI, and — once the virtual-media path works — boot it from media of their
choosing, regardless of what the installed operating system wants. Treat a
leaked BMC password as you would treat handing over the machine.

Two things about this collection's design deserve specific attention while it
is still pre-1.0.

### The iUSB virtual-media protocol is proprietary and undocumented

AMI's iUSB protocol, which the (not yet implemented) `asmb8_media` module is
meant to speak, has no public specification. This collection's implementation
is being built by reverse engineering, informed in part by a third-party Go
reference client (`BadCoder1337/rd450x-console`, MIT-licensed — see
[`licenses/MIT.txt`](licenses/MIT.txt)) rather than from vendor documentation.

That has a direct security consequence: until real hardware evidence says
otherwise, **no claim in this document about the iUSB channel's authentication
or confidentiality properties should be taken as verified.**

<!-- TODO: fill in once the protocol is characterised against real hardware —
     does the iUSB session carry its own authentication independent of the
     BMC web session, is traffic ever encrypted, can a session be hijacked by
     an on-path party. Do not guess at these; leave the TODO until there is
     evidence. -->

### Transport is plaintext by default on this board

This board is normally run in a plaintext, single-port management mode rather
than with TLS on a separate port. Concretely: credentials and virtual-media
traffic should be assumed to cross the network unencrypted and recoverable by
an on-path attacker unless and until this collection's documentation says
otherwise with evidence. Put the BMC on an isolated management network segment,
the same advice given for IPMI generally.

### Power and boot control wrap IPMI, not a purpose-built implementation

`asmb8_power` and `asmb8_boot` are planned to wrap
`community.general.ipmi_power` / `ipmi_boot` rather than reimplement IPMI. This
collection inherits whatever security properties (and limitations) those
modules and the underlying `ipmitool`/IPMI protocol have; it does not add or
remove authentication guarantees on that path.

## Credential handling

TODO: once `plugins/module_utils/errors.py` and the connection doc fragment
exist, this section should state, and be kept honest about, at minimum:

- whether `password` is `no_log` in every module argument spec;
- whether every user-visible message and diagnostic passes through a
  redaction layer that strips passwords, `Authorization` headers, and similar
  secrets;
- whether credentials are ever written to operation receipts, facts, or state
  files (they should not be);
- what file mode any session state file is created with.

This section is deliberately left as a checklist rather than a set of
assertions, because none of that code exists yet and this document must not
claim behaviour that has not been implemented.

## Scope

In scope: this collection's Python code, its role, and any CI configuration in
this repository.

Out of scope: vulnerabilities in the ASMB8's own MegaRAC firmware itself
(report those to ASUS), in `ansible-core`, in `requests`, or in
`community.general` (the IPMI modules this collection wraps).

## Supported versions

Pre-1.0. Only the latest commit on `main` receives fixes; there are no
maintained release branches yet.
