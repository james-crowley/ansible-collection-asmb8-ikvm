<!--
Copyright (c) 2026 Jim Crowley
GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Maintainer runbook: accounts, secrets, and one-time steps

This is the maintainer-side setup for the repository: the accounts, CircleCI
contexts, and secrets that CI itself cannot create. It is excluded from the
published collection artifact (`build_ignore` in `galaxy.yml`), because none
of it is actionable by a consumer. Adapted from the sibling
`james_crowley.intel_amt` collection's file of the same name and purpose --
read that one for the fuller narrative behind each section if this one feels
terse; the mechanics below are unchanged, only the specifics (namespace,
context names, hardware topology) differ.

Each section states the current state first, then whatever action remains.
Several sections below are marked **unconfirmed** rather than **done**: this
document is being written from the repository's own content, not from
GitHub/CircleCI account access, so anything that can only be checked by
looking at those UIs is stated as an open action rather than assumed.

---

## 1. Galaxy namespace

**Decided, and already in use by a sibling collection.** `galaxy.yml`
declares `namespace: james_crowley`, `name: asmb8_ikvm`, so the fully-qualified
collection name is `james_crowley.asmb8_ikvm`. The namespace itself needs no
separate claiming here: `james_crowley.intel_amt` already publishes under it,
which is only possible because the namespace already exists (Galaxy
auto-creates a namespace matching the owner's GitHub login the first time
they sign in with GitHub, mapping hyphens to underscores). Nothing to do.

**Remaining action:** none for the namespace itself. See "Publishing" below
for why this collection specifically has not used it yet.

---

## 2. Publishing, and why it has not happened yet

**Not yet published to Ansible Galaxy** -- see the top-level `README.md`'s
"Installation" section, which says so plainly rather than describing an
install command that would not work. `.circleci/config.yml`'s `publish` job
is fully wired (tag-triggered, gated by `publish-approval`, cross-checks the
git tag against `galaxy.yml`'s declared version before uploading anything),
the same pattern `james_crowley.intel_amt` uses -- but it has never run,
because this collection has never been tagged.

### The `galaxy-publish` CircleCI context

**Unconfirmed from this repository alone.** `.circleci/config.yml`'s
`publish` job reads `GALAXY_API_KEY` from a context literally named
`galaxy-publish` and fails fast ("`GALAXY_API_KEY` is not set; is the
`galaxy-publish` context attached?") if it is missing or misnamed. Whether
that context already exists for *this* project -- as opposed to existing for
`james_crowley.intel_amt`, which is a separate CircleCI project even though it
shares a Galaxy namespace -- needs confirming in the CircleCI UI before the
first tag is pushed. If it needs creating:

```bash
circleci context store-secret galaxy-publish --org gh/james-crowley GALAXY_API_KEY
```

Restrict it to this project only, the same way `asmb8-lab` is restricted (see
section 4 below) -- **do not** reuse `james_crowley.intel_amt`'s context
wholesale even if the underlying Galaxy API key is the same account: a
context restricted to the wrong project (or to none) lets any other project
in the org publish under this collection's name.

### How a release will actually publish, once tagged

Publishing triggers on a `v*` tag push and is gated twice:

1. `publish-approval`, a manual approval that only exists on tag pushes.
2. The `publish` job asserts the tag matches `galaxy.yml`'s `version` before
   uploading anything, and uploads one exact filename rather than a
   `./dist/*.tar.gz` glob.

That check exists because **a Galaxy version is immutable once published**:
it cannot be replaced, only superseded by a higher version. Tagging `v0.2.0`
while `galaxy.yml` still says `0.1.0` would be irrecoverable. Since this
collection has never published at all, there is no historical version to
protect yet -- but the check is there from the first release, not added after
a near-miss.

---

## 3. Renovate

**`renovate.json` now exists** (adapted from the sibling collection's file of
the same name, with this collection's own paths and its single ansible-core
pin). Whether the Renovate GitHub App is actually **installed and running**
on this repository is unconfirmed from here -- that is a GitHub App
installation, not a file in this repository, and needs checking in GitHub's
own settings.

**What `renovate.json` is configured to manage here:**

- **Does:** `requirements.txt`, `requirements-dev.txt`,
  `tests/unit/requirements.txt`, `tests/integration/requirements.txt`, and
  the single `ansible-core~=2.19.0` pin `.circleci/config.yml`'s
  `install-hardware-venv` command uses for the hardware-in-the-loop jobs.
- **Does not:** the `sanity`/`units` matrices in `.circleci/config.yml`,
  because those are comma-separated *lists* of supported versions rather than
  single pins -- there is no "current version" for a bot to bump. Widening or
  narrowing that support matrix is a policy decision a human makes.
- **Does not:** the `cimg/python:<< parameters.python-version >>` image
  reference -- it is a CircleCI parameter substitution, not a resolvable
  version string, so a package rule targeting it would match nothing while
  reading as though the image were managed.

**Remaining action:** confirm the GitHub App is installed on this specific
repository (installing it against `james_crowley.intel_amt` does not cover a
different repository), and, once it has run once, check its Dependency
Dashboard issue for anything it could not resolve automatically.

---

## 4. The `asmb8-lab` CircleCI context

**Unconfirmed from this repository alone; referenced but not verifiable
here.** `.circleci/config.yml`'s `hardware` workflow reads
`ASMB8_HOST`/`ASMB8_USERNAME`/`ASMB8_PASSWORD`/`ASMB8_TLS_FINGERPRINT` (and
optionally `ASMB8_PORT`/`ASMB8_CD_PORT`/`ASMB8_KVM_PORT`/
`ASMB8_ALLOW_INSECURE`) from a context literally named `asmb8-lab`, restricted
to this project only, the same way `amt-lab-runner` is restricted for the
sibling collection. Whether it has actually been created and populated needs
confirming in the CircleCI UI -- **the hardware workflow's own gating (see
`tests/hardware/README.md`) makes an unpopulated or missing context a hard,
loud failure** (`verify-hardware-credentials` names the missing variables)
rather than a silent skip, so this is safe to leave unconfirmed until the
first deliberate hardware run, but worth checking before that run rather than
during it.

To populate it:

```bash
circleci context store-secret asmb8-lab --org gh/james-crowley ASMB8_HOST
circleci context store-secret asmb8-lab --org gh/james-crowley ASMB8_USERNAME
circleci context store-secret asmb8-lab --org gh/james-crowley ASMB8_PASSWORD
# ASMB8_TLS_FINGERPRINT: run the hardware-observe job first (needs no approval
# and sends no credentials -- see tests/hardware/README.md), review the
# fingerprint it prints, then store it here.
circleci context store-secret asmb8-lab --org gh/james-crowley ASMB8_TLS_FINGERPRINT
```

Also needs its own self-hosted machine runner
(`resource_class: crowley/asmb8-runner`), which -- unlike the context above --
is infrastructure that has to exist on the lab side, not just in CircleCI's
account settings:

```bash
circleci runner resource-class create crowley/asmb8-runner "ASMB8-iKVM hardware runner" --generate-token
# then install the machine runner 3 agent on the lab host with that token
```

---

## 5. Repository visibility and branch protection

**Unconfirmed from this repository alone.** Whether this repository is public
and whether branch protection on `main` requires the CI status checks this
document's sibling relies on both need checking in GitHub's own settings --
neither is something a file in the repository can confirm. If both are
already true, note the practical consequence the sibling collection's own
maintainer-setup calls out and that applies identically here: because the
repository would then be public, this collection's own hardware topology
(board address, credentials, TLS fingerprint) must stay out of it entirely.
`vars/connection.yml` (see `tests/hardware/README.md`) is what keeps that
true here -- every value in it is a `lookup('ansible.builtin.env', ...)`
expression, never a literal.

List the current required-check set rather than trusting a copy of it, once
the repository exists on GitHub with branch protection configured:

```bash
gh api repos/james-crowley/ansible-collection-asmb8-ikvm/branches/main/protection \
  --jq '.required_status_checks.contexts'
```

Keep that list in step with `.circleci/config.yml`'s `test` workflow job
names. A job renamed in CI but not in the protection rule silently stops
being required; a required check that no longer exists blocks every merge
instead.

---

## 6. A second board

**Not applicable yet, and deliberately not pre-built.** This collection
manages **exactly one** ASMB8-iKVM board today (see the top-level `README.md`
"Project status"). `.circleci/config.yml`'s `hardware-limit` pipeline
parameter is already wired for the day a second board is added -- the same
reasoning the sibling `james_crowley.intel_amt` collection's own file gives
for carrying that capability from day one rather than retrofitting it under
pressure -- but there is no second board's credentials to add, and no
rendered multi-host inventory to maintain, because there is only one board.
When a second one is added, `tests/hardware/vars/connection.yml` is the file
that grows into a real per-host inventory; see `tests/hardware/README.md`'s
note on this for the shape that change would take, mirroring the sibling
collection's `inventory.yml.example`/`render-inventory.sh` pattern.

---

## Already in place (no action needed)

- **The weekly `sanity-devel` canary schedule**, if configured the same way
  as the sibling collection's `weekly-drift-detection` -- **unconfirmed from
  this repository alone**, since scheduled triggers live in CircleCI project
  settings, not `.circleci/config.yml`. Inspect with:

  ```bash
  circleci api "/api/v2/project/gh/james-crowley/ansible-collection-asmb8-ikvm/schedule"
  ```

## Summary: what is left

| Item | Status | Remaining |
|---|---|---|
| Galaxy namespace | Decided, already in use by `james_crowley.intel_amt` | Nothing |
| `galaxy-publish` context | Referenced by CI; existence for **this** project unconfirmed | Confirm or create, before the first tag push |
| Renovate | Config committed (`renovate.json`) | Confirm the GitHub App is installed on this repository |
| `asmb8-lab` context + runner | Referenced by CI; existence unconfirmed | Confirm or create, before the first hardware run |
| Public repo + branch protection | Unconfirmed | Check GitHub settings; keep the required-check list in step with CI |
| Second board | Not applicable | Nothing -- one board today, by design |
