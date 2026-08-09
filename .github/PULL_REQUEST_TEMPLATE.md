<!--
Thanks for contributing. This collection is pre-1.0 and nothing in it is
hardware-qualified yet -- see README.md "Project status". The checklist below
is short on purpose; expect it to grow real teeth (specific commands, specific
test tiers) as modules and CI actually land.
-->

## What this changes

<!-- And why. If it fixes an issue, "Closes #N". -->

## How it was verified

<!--
Paste the actual results rather than asserting they pass. "Tests pass" is not
reviewable; the counts and exit codes are.
-->

```
pytest tests/unit -q        ->
ansible-test sanity --venv  ->
ansible-test integration    ->
```

## Checklist

- [ ] `ruff check` / `ruff format --check` / `yamllint` / `ansible-lint --offline`
      are clean.
- [ ] `ansible-test sanity --venv` exits 0, once there is module content to sanity
      test.
- [ ] A changelog fragment is added under `changelogs/fragments/`.
- [ ] Nothing in a message, receipt, fact, or state file can contain a credential.
- [ ] If you changed a module's return shape, every consumer is updated:
      `roles/`, `tests/integration/targets/`, `tests/hardware/`, `docs/`, `README.md`.
- [ ] Documentation says only what is actually true. Nothing here is
      hardware-proven yet -- if something is unverified against real firmware,
      it is described that way, not implied otherwise.

## If this touches the wire protocol

The iUSB virtual-media protocol is proprietary AMI firmware behaviour, being
reverse engineered rather than implemented from a public spec.

- [ ] `docs/protocol-notes.md` is updated, and cites evidence (a firmware
      response, a packet capture, or a reference implementation such as
      `BadCoder1337/rd450x-console`) rather than inference.
- [ ] You have considered whether a mock server *could* catch a regression here,
      and whether this collection has one yet.

## If this is destructive

Power, boot, and media changes can strand a machine.

- [ ] Check mode makes no mutation, and there is a test asserting it.
- [ ] An uncertain outcome is reported as such rather than retried.
