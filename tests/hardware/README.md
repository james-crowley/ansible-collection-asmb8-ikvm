<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Hardware-in-the-loop qualification

These playbooks log in to, attach virtual media on, arm a boot override on,
power-cycle, and open a KVM channel against a **real** ASMB8-iKVM BMC. Nothing
here runs against a mock server. See [`docs/testing.md`](../../docs/testing.md)
for how this fits into the collection's three testing tiers.

**As of this writing, none of these playbooks have ever run against real
hardware.** They exist so that `.circleci/config.yml`'s `hardware` workflow --
which has named these exact paths since before they existed -- has something
real to invoke, and so the escalation discipline below is enforced by
structure rather than only by comment. See
[`docs/capability-matrix.md`](../../docs/capability-matrix.md) Tier 4 for
exactly what that does and does not change about this collection's evidence.

## Gating

Every hardware job sits behind independent gates, none of which fires on an
ordinary push. See `.circleci/config.yml`'s own `hardware` workflow comment
for the full list; the short version:

1. **Pipeline parameter.** The `hardware` workflow only exists when triggered
   with `run-hardware-tests=true`. A normal push cannot reach it.
2. **A separate manual approval before each escalation** -- login,
   media-attach, boot-once, reset, kvm -- so one human click never authorises
   more than the one class of action it was clicked for.
3. The `asmb8-lab` context, restricted to this project only.
4. `branches: only: main`.
5. `verify-hardware-credentials`, which checks only that the required
   variable *names* are present, never a value.

`hardware-observe` (this directory's [`observe.yml`](observe.yml)) is the one
job with **no approval gate at all**, deliberately: it sends no credentials
that could be rejected and mutates no BMC-side state, which is what lets an
operator learn the TLS fingerprint every later job needs to pin, without
first being approved to do the thing the pin protects.

## Escalation order

Six jobs, one linear chain, **never run in parallel** -- see
`.circleci/config.yml`'s own comment on why this board's web server makes
that a structural requirement, not a style preference. Each escalates past
the last:

| # | Playbook(s) | CI job | Mutates? |
|---|---|---|---|
| 1 | [`observe.yml`](observe.yml) | `hardware-observe` | No |
| 2 | [`login.yml`](login.yml) | `hardware-login` | Creates a BMC-side web session |
| 3 | [`media_attach.yml`](media_attach.yml) + [`media_detach.yml`](media_detach.yml) | `hardware-media-attach` | Yes -- opens (and always, `when: always`, closes) an iUSB session |
| 4 | [`boot_once.yml`](boot_once.yml) | `hardware-boot-once` | Yes -- arms a one-time IPMI boot override |
| 5 | [`reset.yml`](reset.yml) | `hardware-reset` | Yes -- power-cycles the host. **Most destructive. Read [`PREFLIGHT.md`](PREFLIGHT.md) first.** |
| 6 | [`kvm.yml`](kvm.yml) | `hardware-kvm` | No (opens, then best-effort closes, a KVM session) |

Escalation 4 deliberately leaves the optical boot override **armed** rather
than reading it back itself: escalation 5's reset is what actually proves the
override is one-time (it independently confirms the override reverted to
`default` afterwards), and reading it back in escalation 4 without a reset in
between would prove nothing about that claim. See `boot_once.yml`'s and
`reset.yml`'s own header comments.

## Inventory and credentials

There is deliberately **no rendered `inventory.yml`** the way the sibling
`james_crowley.intel_amt` collection has one. That collection's lab grew from
one machine to several, which is what an inventory group buys you. This
collection manages **exactly one board today** (see the top-level
[`README.md`](../../README.md) "Project status"), so every playbook here
targets `hosts: localhost` with `connection: local` and reads its target's
address, credentials, and TLS trust decision from
[`vars/connection.yml`](vars/connection.yml) -- itself nothing but
`lookup('ansible.builtin.env', ...)` expressions, so no secret ever touches
disk. `hardware-limit` in `.circleci/config.yml` is still wired for the day a
second board is added; `vars/connection.yml` is the file that would grow a
real per-host inventory at that point, following the pattern
`tests/hardware/inventory.yml.example` and `render-inventory.sh` establish in
the sibling collection.

Real credentials come from the restricted `asmb8-lab` CircleCI context, or
from your own shell environment for a manual run:

```bash
export ASMB8_HOST=... ASMB8_USERNAME=admin ASMB8_PASSWORD=...
export ASMB8_TLS_FINGERPRINT=...   # from hardware-observe, reviewed by you
ansible-playbook tests/hardware/observe.yml -v
```

Evidence these playbooks produce is written to `tests/hardware/output/`,
which is gitignored; the CircleCI jobs store it as build artifacts instead
(`store_artifacts: path: tests/hardware/output`).

## Evidence redaction

Every playbook here writes JSON evidence into `tests/hardware/output/`.
[`redact-evidence.py`](redact-evidence.py) rewrites every `.json` file in that
directory in place before it is published, for exactly the reason the sibling
`james_crowley.intel_amt` collection's script of the same name exists:
**CircleCI masks context values in log output only, never in
`store_artifacts` content.** Holding `ASMB8_HOST` and friends in the
restricted `asmb8-lab` context does not, by itself, keep them out of a
published evidence file.

Run it by hand the same way CI would, with `when: always` so evidence written
before a failing job is covered too:

```bash
python3 tests/hardware/redact-evidence.py tests/hardware/output
```

See the script's own module docstring for exactly what is redacted (IPv4/IPv6
addresses, MAC addresses, UUIDs, SHA-256 fingerprints/digests, DNS names, and
a handful of identifying keys such as `host`/`username`/`image`) and what is
deliberately preserved (power/boot state, capability flags, byte counters,
error classes, and the JSON structure itself). Unit tests live in
[`tests/unit/hardware/test_redact_evidence.py`](../unit/hardware/test_redact_evidence.py)
-- every fixture there uses obviously fake values (RFC 5737/3849/7042,
`.invalid` domains). **Never put a real board value in a fixture.**

## Test media

`media_attach.yml` needs a small local ISO to attach. It is never committed
(`.gitignore` blocks `*.iso`/`*.img`), so [`make-test-media.sh`](make-test-media.sh)
provisions one by fetching iPXE's own `ipxe.iso` -- small, and genuinely
bootable, though this collection's own escalation chain does not (yet) attempt
to observe a boot from it the way the sibling collection's `qualify_media_attach.yml`
does. That script is a request to `boot.ipxe.org`, **never** to the BMC under
test, and `media_attach.yml` runs it automatically (idempotent: an existing
file is reused).

Unlike the sibling collection, there is no second, writable test image to
provision here: this board's CD-ROM channel has no write opcode at all (see
the top-level README's "Virtual media" section), so there is nothing
analogous to that collection's stage 6 to build media for.

## If a machine ends up in a bad state

- **A stray one-time boot override.** IPMI's one-shot boot flag is single-use;
  a plain power cycle (`asmb8_power state=reset` or a physical reset) clears
  it. `reset.yml` itself exercises exactly this.
- **A wedged `cd-media` slot.** This board's virtual-media service allows
  exactly one active session, board-wide, with no server-side timeout to
  reclaim an abandoned one (see the top-level README's "Known limitations").
  `media_detach.yml` is designed to run even after a crashed or cancelled
  attach (`when: always` in CI), but if the slot is still stuck: a BMC cold
  reset (`ipmitool mc reset cold`, or the `pyghmi`/
  `community.general.ipmi_power` equivalent) resets the management
  controller only, not host power, and is the documented escape hatch.
- **A host that will not come back on.** See [`PREFLIGHT.md`](PREFLIGHT.md)'s
  recovery section for `reset.yml` specifically.

## What this does not prove

Same caveat this collection makes everywhere else: qualifying against one
board does not prove every ASMB8-iKVM (or wider AMI MegaRAC) unit behaves
identically. See
[`docs/capability-matrix.md`](../../docs/capability-matrix.md) Tier 4 for the
complete, specific list of what remains unproven, and for the honest note
that none of the playbooks in this directory have actually been run against
real hardware yet -- their existence closes the gap between what
`.circleci/config.yml` names and what is committed, not the gap between "this
collection's own understanding of the protocol" and "confirmed against real
firmware".
