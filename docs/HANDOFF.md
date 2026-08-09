# Handoff: `james_crowley.asmb8_ikvm`

**Audience:** an engineer or AI agent picking this collection up cold and taking it
forward.
**State at handoff:** `v0.1.0` tagged and pushed. Not published to Galaxy. Not
hardware-qualified. Fully tested against mocks; substantially but not completely
verified against one real board.

Read this file first, then `README.md`, then
`docs/hardware-evidence-2026-08-08.md`. That last file is the authority for what
is actually known versus believed, and it ends with an explicit list of things
that must not be claimed.

---

## 1. What this collection is

Out-of-band management of ASUS ASMB8-iKVM baseboard management controllers — AMI
MegaRAC firmware on an ASPEED AST2400 — from Ansible.

The headline capability, and the reason the project exists: **stream a local ISO
from the Ansible controller to the BMC's virtual CD-ROM so a bare-metal host boots
that installer, with no PXE, DHCP, TFTP, NFS or CIFS infrastructure.**

That required implementing AMI's proprietary, undocumented **iUSB** protocol from
scratch. This board generation has no Redfish (a generational boundary — Redfish
arrived with the AST2500/ASMB9), and standard IPMI has no virtual-media capability
at all, so there was no existing path. Power control and one-time boot-device
selection are *not* new work: they wrap IPMI via `pyghmi`, and exist so the
collection presents one consistent interface rather than to reinvent a working
capability.

### Modules

| Module | What it does | New protocol work? |
|---|---|---|
| `asmb8_info` | Read-only capability and state discovery | no |
| `asmb8_power` | Power state, wraps IPMI | no |
| `asmb8_boot` | One-time boot device, wraps IPMI | no |
| `asmb8_media` | Attach/detach a local ISO over iUSB | **yes — the crux** |
| `asmb8_redirection` | Report/toggle BMC service enablement | no |
| `asmb8_console` | Open the KVM/IVTP console channel headlessly | **yes, partial** |

### Roles

| Role | What it does |
|---|---|
| `asmb8_baremetal_install` | End to end: validate, probe, attach ISO, arm one-time boot, reset, observe, hand off, always detach |
| `asmb8_autoinstall_iso` | Prepare an unattended-install ISO (Proxmox VE) |

### The design standard

This collection deliberately mirrors the published sibling
`james_crowley.intel_amt` — same shared `module_utils` layout, same `connection`
doc fragment, same `ErrorClass` taxonomy, same versioned `OperationReceipt`
returned by every mutating module, same `action_groups` entry so consumers can
share connection options via `module_defaults`, same four-tier evidence model in
the capability matrix, same CI shape. **Treat that repository as the north star.**
If you are unsure how something should look here, look at how it looks there.

---

## 2. Current status, honestly

### Green

- **741+ unit tests**, 0 skipped, 0 xfail
- **`ansible-test sanity` exit 0 across all 24 checks**, verified locally and in CI
  on ansible-core **2.17, 2.18, 2.19, 2.20 and 2.21**
- `ruff check` / `ruff format --check` / `yamllint` / `ansible-lint` (production
  profile) all clean
- `ansible-galaxy collection build` produces a clean artifact; a **consumer smoke
  test** installed that artifact via `ansible-galaxy collection install` and
  exercised all six modules from the *installed* copy — docs render, the doc
  fragment merges, the action group resolves, every module fails cleanly with a
  classified `error_class` rather than a traceback. No consumer-facing bugs found.
- Privacy-audited: the built artifact contains no IP addresses, hostnames, or
  lab-specific references.

### Known-failing in CI at handoff

Two jobs, both being addressed, both test-harness rather than functionality:

1. **`units-2.17-3.10`** — `tests/unit/roles/test_autoinstall_answer_template.py`
   imports `tomllib`, which only entered the stdlib in Python **3.11**. 104 tests
   passed on that cell; it is purely a collection error. Fix is a conditional
   import with a `tomli` fallback plus an environment-marked test requirement.
2. **`integration-mock`** — the `asmb8_lifecycle` target looks for the mock
   servers in `ansible-test`'s per-target scratch tree, but `ansible-test
   integration` does not copy `tests/integration/mock_servers/` there. The files
   are committed and present; the fix is to resolve them from
   `ANSIBLE_TEST_CONTENT_ROOT`. The sibling target
   `asmb8_baremetal_install_role` passes and already solves an equivalent problem.

### Not done

- **Not published to Galaxy.** Requires a Galaxy API token. CI has a tag-gated,
  approval-gated `publish` job that asserts the git tag matches `galaxy.yml`'s
  version and uses a `galaxy-publish` context for `GALAXY_API_KEY`. The version
  assertion has been simulated and is correct; the job has never run.
- **No unattended OS install has completed.** See §5.
- **KVM video decoding is unimplemented.** `asmb8_console`'s
  `capture=decoded_frame` fails honestly with `unsupported_capability` rather than
  returning a placeholder. The handshake and framing are complete and tested.
- **Virtual floppy and virtual hard-disk device classes** are unexercised. The
  ports bind; only CD-ROM has been driven.
- **One board, one firmware version.** That is repeatability at best, never a
  compatibility guarantee.

---

## 3. Protocol facts you must not re-derive

All verified. `docs/protocol-notes.md` is the normative reference; this is the
short list of things that will waste your time if you do not know them.

**Transport**

- The BMC's TLS listener offers **TLS 1.2 only**, with exactly one ciphersuite:
  `AES256-GCM-SHA384`. That is static-RSA key exchange with no forward secrecy,
  which modern OpenSSL and Python exclude from their default cipher list. A plain
  `requests` call fails the handshake. **`curl` succeeds against the same endpoint**
  — so "curl works but Python doesn't" is expected here and is not a bug. See
  `BMC_CIPHERS` in `plugins/module_utils/asp.py`.
- The certificate is self-signed **and expired**. CA-chain validation can never
  succeed. Fingerprint pinning or an explicit insecure acknowledgement are the only
  workable trust policies.
- The BMC's own clock is wrong. Never trust a BMC-supplied timestamp.
- The web server is HTTP/1.0, no keep-alive, caps at 20 sessions, and keeps
  **separate worker pools per listener**. Concurrent requests exhausted the port-80
  pool and it then completed TCP handshakes while never serving a request — for a
  long time — while 443 stayed responsive. **Serialise all BMC HTTP access.** This
  is why `ErrorClass.BMC_BUSY` exists. HTTPS is the supported transport.

**Authentication**

- `POST /rpc/WEBSES/create.asp` returns **HTTP 200 on bad credentials**, not 401,
  with a `Failure_Login_*` marker inside `SESSION_COOKIE`. A status-code-only check
  accepts a rejected login. `asp.py` guards this explicitly; do not "simplify" it.
- `GET /rpc/getsessiontoken.asp` returns an empty token and is useless.
- `GET /Java/jviewer.jnlp?EXTRNIP=<ip>&JNLPSTR=JViewer` is what actually
  **allocates the session** and mints a usable 16-character token.

**Ports**

- Media and KVM listeners are **on-demand**. Before a JNLP fetch allocates a
  session, 5120/5122/5123/7578 return TCP **RST**. **A closed port 5120 is not an
  error** — it means no session is allocated. Any health check that treats it as
  one will report a healthy board as broken.

**iUSB media**

- CD-ROM implements exactly **six** SCSI opcodes: `0x00`, `0x1B`, `0x25`, `0x28`,
  `0x43`, `0xA8`. Determined by disassembling the vendor's own native dispatcher,
  not inferred. **INQUIRY is answered by BMC firmware and never forwarded.**
- Block size 2048. LBA and transfer-length fields inside the SCSI CDB are
  **big-endian, inside a little-endian iUSB wrapper**.
- `deviceType` is always `5`, even for floppy and HD.
- Eject is `0x1B` with **exact equality** on `payload[13] == 2`.
- **`cd-media` allows exactly ONE session, board-wide, with NO server-side
  timeout** to reclaim an abandoned one. `asmb8_media` therefore always attempts
  reclamation before attaching. If software reclamation fails, the operator's
  escape hatch is a **BMC cold reset**, which resets the BMC without touching host
  power.
- **Idle is normal and unbounded.** A healthy attached session was measured
  completely silent for **130 consecutive seconds** while the host sat at a
  bootloader menu, then resumed. A host can wait at a prompt indefinitely. No
  wait/poll loop may treat quiet as failure.
- **The guest OS shares the session.** A Linux booted from the virtual CD kept
  reading through the same held session past kernel handoff. So
  `media_release_after_handoff` in the install role is unnecessary, not merely
  experimental.
- Throughput is roughly **800 KB/s – 1 MB/s**.

**Console**

- The KVM channel speaks **IVTP** on plaintext 7578 (7582 secure). The server
  greets first with an 8-byte header; `17 00 00 00 00 00 00 00` was captured
  verbatim. Sending a wrong first frame causes an **immediate silent close** with
  no error frame — so make handshake failures diagnostically loud.
- **IPMI Serial-over-LAN does not work on this board**, despite appearing fully
  configured. The channel payload was enabled, per-user access granted, and both
  plausible bitrates tried; zero bytes arrived across repeated resets. Do not build
  anything that depends on SOL. The technique that *does* work for observing an
  installer is correlating the media channel's own read pattern against the ISO's
  structure — that is how a bootloader stall was diagnosed precisely.

---

## 4. Mistakes already made — do not repeat them

Recorded because each one cost real time.

1. **Concurrent HTTP requests wedged the BMC's web server** and locked its owner
   out of the web UI for an extended period. Serialise everything.
2. **`ruff format` and `ansible-test`'s pep8 check actively disagree** about
   ellipsis-only `Protocol` stub bodies. `ruff` collapses them to one line;
   `ansible-test` re-enables `E704` and rejects that. Use `raise
   NotImplementedError` on its own line. Symptom if you get this wrong: files that
   appear to spontaneously revert.
3. **`asmb8_redirection` was originally implemented as a console client** under a
   name borrowed from the sibling collection's service-enablement module. Renamed
   before publication because renaming after a Galaxy release is breaking. If you
   add modules, check the sibling's semantics for that name first.
4. **`no_log` is not legal in a DOCUMENTATION block** — only in the argument spec.
   `O(...)` markup may only name an option of the same module; use `M(fqcn)` for a
   module. A return-value directive may only name a declared return value. And do
   not write the literal characters `O` or `RV` followed by a parenthesis in prose,
   or the markup parser treats them as directives.
5. **`auto-installer-mode.toml`'s `mode` value was guessed** as `"included"`. The
   real value is `"iso"`. A wrong value fails in a maximally misleading way: the
   ISO boots, the menu times out into the automated entry, the installer starts,
   silently falls back to the **interactive** installer, and waits at a licence
   prompt — producing a byte-identical media read pattern to booting a completely
   unprepared ISO. It was misdiagnosed three times as a disk-selection problem.
   **Derive vendor file formats by running the vendor tool, not by reading structs.**
6. **A stock Proxmox ISO never times out.** Its `set timeout` lives inside
   `if [ -f auto-installer-mode.toml ]`, and a stock image lacks that marker. So
   booting an unprepared installer ISO does not produce an unattended install.
7. **The GRUB timeout fires around two minutes** on this hardware, not at its
   configured value. Any "is this stuck?" heuristic needs a floor well above that,
   or it aborts working installs. One working attempt was killed seconds early.

---

## 5. Where to pick up

### Immediate

1. **Land the two CI fixes** (§2) and confirm the pipeline goes fully green.
2. **Publish to Galaxy.** Add `GALAXY_API_KEY` to a CircleCI context named
   `galaxy-publish` (the context already exists org-wide, used by the sibling
   collection), then approve the tag-gated `publish` job. Or publish locally with
   `ansible-galaxy collection publish dist/<artifact>.tar.gz --api-key <token>`.
3. **Complete an unattended install.** Everything needed is now known: use the
   vendor's `proxmox-auto-install-assistant prepare-iso --fetch-from iso
   --answer-file <file>` rather than hand-placing files, target a disk explicitly
   (`disk-list` and `filter` are mutually exclusive, and ext4 requires exactly one
   matching disk), and expect ~2 minutes of GRUB wait before streaming starts.

### Then

4. **KVM video decoding** for `asmb8_console` — the AMI/ASPEED VQ + JPEG/DCT
   hybrid, optionally RC4-obfuscated. The handshake and framing are done; this is
   the pixel layer only. `NOTICE` names the MIT-licensed reference implementation
   whose `docs/kvm-protocol.md` describes it.
5. **Exercise the floppy and hard-disk device classes.** Their ports bind. A
   writable floppy would also give an answer-file delivery path that needs no
   network.
6. **`tests/hardware/`** — the CI config already names hardware playbooks behind an
   approval chain. Build them following the sibling collection's structure.
7. **Integrate into infrastructure.** Deliberately deferred until publication,
   since dependency pinning wants a published artifact or a real commit SHA.

---

## 6. How to work on this

```bash
# Stage the collection the way ansible-test requires. Used identically by CI,
# CONTRIBUTING.md and ad hoc pytest runs -- do not invent a second way.
./scripts/setup-collection-tree.sh          # prints the staged collection path

# From inside the staged tree, with PYTHONPATH set to the dir containing
# ansible_collections/:
pytest tests/unit -q
ansible-test sanity --python 3.13
ansible-test units --python 3.13
ansible-test integration --venv --python 3.12

# Lint, from the repo root:
ruff check plugins tests && ruff format --check plugins tests
yamllint -c .yamllint .
ansible-lint --offline

# Build:
ansible-galaxy collection build --output-path dist --force
```

Documented traps live in `CONTRIBUTING.md`. Changelog entries are fragments under
`changelogs/fragments/` rendered by `antsibull-changelog` — do not hand-edit
`CHANGELOG.md`.

**Two standing rules.** Keep this repository generic: no IP addresses, hostnames,
or references to any particular lab. Public facts about the motherboard and BMC
model are welcome. And keep the documentation honest — the capability matrix uses
explicit evidence tiers, and the value of that is destroyed by a single inflated
claim. If something is unproven, say so.
