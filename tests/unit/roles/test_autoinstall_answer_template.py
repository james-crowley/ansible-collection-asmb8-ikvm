# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Tests for the asmb8_autoinstall_iso role's answer.toml template and its
# disk-safety guards.
#
# Deliberately does NOT invoke Ansible and makes no network call: the
# answer.toml.j2 template is rendered with a plain jinja2.Environment,
# exactly as ansible.builtin.template would fill it in from role variables,
# and then parsed with the standard-library tomllib to prove the output is
# valid TOML with the keys we expect. The disk-safety
# `ansible.builtin.assert` conditions in tasks/validate.yml are extracted
# straight from that YAML file -- not reimplemented here -- and evaluated as
# plain Jinja2 boolean expressions, so a change to the real guard is what
# this test exercises, not a parallel copy of it that could drift. The only
# piece of those conditions that is not vanilla Jinja2 is the
# `search`/`match` regex tests and the `bool` filter ansible-core provides;
# those are shimmed directly (re.search/re.match, and a reimplementation of
# ansible-core's own yes/no/on/off/1/0/true/false coercion table) rather than
# pulling in ansible-core's whole test/filter plugin loader for a handful of
# small functions.
#
# No block device is touched anywhere in this file: every "disk" here is a
# string in a Python dict.

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

try:
    import tomllib  # Python 3.11+: tomllib is stdlib from here on.
except ModuleNotFoundError:  # Python 3.10 (still in this collection's units matrix): no stdlib tomllib yet.
    import tomli as tomllib  # Same read-only API surface as tomllib; test-only backport, see tests/unit/requirements.txt.
import yaml
from jinja2 import Environment, StrictUndefined

ROLE_DIR = Path(__file__).resolve().parents[3] / "roles" / "asmb8_autoinstall_iso"
TEMPLATE_PATH = ROLE_DIR / "templates" / "answer.toml.j2"
VALIDATE_TASKS_PATH = ROLE_DIR / "tasks" / "validate.yml"


# Mirrors ansible.module_utils.parsing.convert_bool.boolean()'s own
# BOOLEANS_TRUE/BOOLEANS_FALSE tables (case-insensitive for strings), so the
# `| bool` filter used by "Assert the destructive-confirmation gate is
# explicitly set to true" behaves identically here to how it behaves under
# real Ansible, including raising on a value that is neither.
_ANSIBLE_BOOLEANS_TRUE = frozenset(("y", "yes", "on", "1", "true", "t", 1, 1.0, True))
_ANSIBLE_BOOLEANS_FALSE = frozenset(("n", "no", "off", "0", "false", "f", 0, 0.0, False))


def _ansible_bool_filter(value: Any) -> bool:
    normalized = value.lower() if isinstance(value, str) else value
    if normalized in _ANSIBLE_BOOLEANS_TRUE:
        return True
    if normalized in _ANSIBLE_BOOLEANS_FALSE:
        return False
    raise ValueError(f"{value!r} is not a boolean-ish value ansible-core's `bool` filter would accept")


def _make_env() -> Environment:
    env = Environment(undefined=StrictUndefined)  # noqa: S701 -- renders TOML, not HTML; autoescape is not applicable here.
    env.tests["search"] = lambda value, pattern: re.search(pattern, str(value)) is not None
    env.tests["match"] = lambda value, pattern: re.match(pattern, str(value)) is not None
    env.filters["bool"] = _ansible_bool_filter
    return env


def render_answer_toml(context: dict[str, Any]) -> str:
    env = _make_env()
    template = env.from_string(TEMPLATE_PATH.read_text())
    return template.render(**context)


def base_context(**overrides: Any) -> dict[str, Any]:
    """Representative variables mirroring defaults/main.yml -- a complete,
    plausible answer.toml. Individual tests override only the keys they
    care about.
    """
    context: dict[str, Any] = {
        "asmb8_autoinstall_iso_answer_keyboard": "en-us",
        "asmb8_autoinstall_iso_answer_country": "us",
        "asmb8_autoinstall_iso_answer_fqdn": "pve-lab-01.example.invalid",
        "asmb8_autoinstall_iso_answer_mailto": "root@example.invalid",
        "asmb8_autoinstall_iso_answer_timezone": "Etc/UTC",
        "asmb8_autoinstall_iso_answer_root_password": "",
        "asmb8_autoinstall_iso_answer_root_password_hashed": "$6$rounds=656000$abcdefgh$examplehasheddigest",
        "asmb8_autoinstall_iso_answer_reboot_on_error": False,
        "asmb8_autoinstall_iso_answer_reboot_mode": "reboot",
        "asmb8_autoinstall_iso_answer_network_source": "from-dhcp",
        "asmb8_autoinstall_iso_answer_network_cidr": "",
        "asmb8_autoinstall_iso_answer_network_dns": "",
        "asmb8_autoinstall_iso_answer_network_gateway": "",
        "asmb8_autoinstall_iso_answer_network_filter": {},
        "asmb8_autoinstall_iso_answer_disk_filesystem": "zfs",
        "asmb8_autoinstall_iso_answer_disk_list": ["nvme0n1"],
        "asmb8_autoinstall_iso_answer_disk_filter": {},
        "asmb8_autoinstall_iso_answer_disk_filter_match": "any",
        "asmb8_autoinstall_iso_answer_disk_zfs": {"raid": "raid0", "ashift": 12},
        "asmb8_autoinstall_iso_answer_disk_lvm": {},
        "asmb8_autoinstall_iso_answer_disk_btrfs": {},
    }
    context.update(overrides)
    return context


# ---------------------------------------------------------------------------
# answer.toml rendering
# ---------------------------------------------------------------------------


class TestAnswerTomlRendering:
    def test_zfs_answer_is_valid_toml_with_expected_global_and_disk_keys(self):
        data = tomllib.loads(render_answer_toml(base_context()))

        assert data["global"]["keyboard"] == "en-us"
        assert data["global"]["country"] == "us"
        assert data["global"]["fqdn"] == "pve-lab-01.example.invalid"
        assert data["global"]["mailto"] == "root@example.invalid"
        assert data["global"]["timezone"] == "Etc/UTC"
        assert data["global"]["root-password-hashed"] == "$6$rounds=656000$abcdefgh$examplehasheddigest"
        assert "root-password" not in data["global"]
        assert data["global"]["reboot-on-error"] is False
        assert data["global"]["reboot-mode"] == "reboot"

        assert data["network"]["source"] == "from-dhcp"

        assert data["disk-setup"]["filesystem"] == "zfs"
        assert data["disk-setup"]["disk-list"] == ["nvme0n1"]
        assert data["disk-setup"]["zfs"]["raid"] == "raid0"
        assert data["disk-setup"]["zfs"]["ashift"] == 12
        assert "lvm" not in data["disk-setup"]
        assert "btrfs" not in data["disk-setup"]

    def test_plaintext_root_password_rendered_only_when_hash_not_supplied(self):
        ctx = base_context(
            asmb8_autoinstall_iso_answer_root_password="plaintext-in-a-test-fixture-only",
            asmb8_autoinstall_iso_answer_root_password_hashed="",
        )
        data = tomllib.loads(render_answer_toml(ctx))

        assert data["global"]["root-password"] == "plaintext-in-a-test-fixture-only"
        assert "root-password-hashed" not in data["global"]

    def test_hashed_password_preferred_when_both_are_set(self):
        ctx = base_context(
            asmb8_autoinstall_iso_answer_root_password="should-not-appear",
            asmb8_autoinstall_iso_answer_root_password_hashed="$6$should$appear",
        )
        data = tomllib.loads(render_answer_toml(ctx))

        assert data["global"]["root-password-hashed"] == "$6$should$appear"
        assert "root-password" not in data["global"]

    def test_mailto_omitted_entirely_when_blank(self):
        data = tomllib.loads(render_answer_toml(base_context(asmb8_autoinstall_iso_answer_mailto="")))
        assert "mailto" not in data["global"]

    def test_static_network_renders_cidr_dns_gateway(self):
        ctx = base_context(
            asmb8_autoinstall_iso_answer_network_source="from-answer",
            asmb8_autoinstall_iso_answer_network_cidr="192.0.2.10/24",
            asmb8_autoinstall_iso_answer_network_dns="192.0.2.1",
            asmb8_autoinstall_iso_answer_network_gateway="192.0.2.1",
        )
        data = tomllib.loads(render_answer_toml(ctx))

        assert data["network"]["source"] == "from-answer"
        assert data["network"]["cidr"] == "192.0.2.10/24"
        assert data["network"]["dns"] == "192.0.2.1"
        assert data["network"]["gateway"] == "192.0.2.1"

    def test_dhcp_network_omits_static_fields(self):
        data = tomllib.loads(render_answer_toml(base_context()))
        assert "cidr" not in data["network"]
        assert "dns" not in data["network"]
        assert "gateway" not in data["network"]

    def test_network_filter_table_rendered_when_present(self):
        ctx = base_context(asmb8_autoinstall_iso_answer_network_filter={"ID_NET_NAME_MAC": "24:8a:07:1e:05:bc"})
        data = tomllib.loads(render_answer_toml(ctx))
        assert data["network"]["filter"]["ID_NET_NAME_MAC"] == "24:8a:07:1e:05:bc"

    def test_lvm_ext4_renders_lvm_table_not_zfs(self):
        ctx = base_context(
            asmb8_autoinstall_iso_answer_disk_filesystem="ext4",
            asmb8_autoinstall_iso_answer_disk_zfs={},
            asmb8_autoinstall_iso_answer_disk_lvm={"hdsize": 200, "swapsize": 8},
        )
        data = tomllib.loads(render_answer_toml(ctx))

        assert data["disk-setup"]["filesystem"] == "ext4"
        assert data["disk-setup"]["lvm"]["hdsize"] == 200
        assert data["disk-setup"]["lvm"]["swapsize"] == 8
        assert "zfs" not in data["disk-setup"]

    def test_btrfs_renders_btrfs_table(self):
        ctx = base_context(
            asmb8_autoinstall_iso_answer_disk_filesystem="btrfs",
            asmb8_autoinstall_iso_answer_disk_zfs={},
            asmb8_autoinstall_iso_answer_disk_btrfs={"raid": "raid1", "compress": "zstd"},
        )
        data = tomllib.loads(render_answer_toml(ctx))

        assert data["disk-setup"]["btrfs"]["raid"] == "raid1"
        assert data["disk-setup"]["btrfs"]["compress"] == "zstd"

    def test_disk_filter_renders_filter_table_and_match_mode_instead_of_disk_list(self):
        ctx = base_context(
            asmb8_autoinstall_iso_answer_disk_list=[],
            asmb8_autoinstall_iso_answer_disk_filter={"ID_SERIAL_SHORT": "S3Z8NB0M123456"},
            asmb8_autoinstall_iso_answer_disk_filter_match="all",
        )
        data = tomllib.loads(render_answer_toml(ctx))

        assert "disk-list" not in data["disk-setup"]
        assert data["disk-setup"]["filter"]["ID_SERIAL_SHORT"] == "S3Z8NB0M123456"
        assert data["disk-setup"]["filter-match"] == "all"

    def test_arc_max_key_is_rendered_in_current_kebab_case_spelling(self):
        # arc_max (snake_case) is accepted as a role-variable spelling for
        # convenience, but the schema's current key is zfs.arc-max -- see
        # defaults/main.yml and the kebab-case migration note in
        # answer.toml.j2.
        ctx = base_context(asmb8_autoinstall_iso_answer_disk_zfs={"raid": "raid1", "arc_max": 2048})
        data = tomllib.loads(render_answer_toml(ctx))

        assert data["disk-setup"]["zfs"]["arc-max"] == 2048
        assert "arc_max" not in data["disk-setup"]["zfs"]

    def test_quotes_and_backslashes_in_strings_survive_the_round_trip(self):
        tricky = 'pve"quote\\backslash.example.invalid'
        data = tomllib.loads(render_answer_toml(base_context(asmb8_autoinstall_iso_answer_fqdn=tricky)))
        assert data["global"]["fqdn"] == tricky

    def test_numeric_disk_values_render_unquoted_and_survive_the_round_trip(self):
        # A caller who mistakenly quotes a numeric option in YAML ("12"
        # instead of 12) gets a TOML string, not an integer -- this test
        # pins the type-preserving behaviour for the correct (unquoted)
        # case so a future template change can't silently start quoting
        # everything.
        ctx = base_context(asmb8_autoinstall_iso_answer_disk_zfs={"raid": "raid1", "ashift": 12, "copies": 2})
        data = tomllib.loads(render_answer_toml(ctx))
        assert isinstance(data["disk-setup"]["zfs"]["ashift"], int)
        assert isinstance(data["disk-setup"]["zfs"]["copies"], int)


# ---------------------------------------------------------------------------
# Disk safety: exercising the real ansible.builtin.assert conditions from
# tasks/validate.yml, not a reimplementation of them.
# ---------------------------------------------------------------------------


def _load_validate_tasks() -> list[dict[str, Any]]:
    tasks = yaml.safe_load(VALIDATE_TASKS_PATH.read_text())
    assert isinstance(tasks, list) and tasks, f"{VALIDATE_TASKS_PATH} did not parse as a non-empty task list"
    return tasks


def _find_task(tasks: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for task in tasks:
        if task.get("name") == name:
            return task
    raise AssertionError(f"no task named {name!r} found in {VALIDATE_TASKS_PATH}")


def _assert_conditions_hold(task: dict[str, Any], context: dict[str, Any]) -> bool:
    """Evaluate every string in task['assert']['that'] as a Jinja2 boolean
    expression, exactly mirroring ansible.builtin.assert's own semantics:
    true only if every condition is true. The task's own `vars:` (e.g.
    _forbidden_disk_tokens) are merged in first, same as Ansible would.
    """
    env = _make_env()
    merged: dict[str, Any] = dict(task.get("vars", {}))
    merged.update(context)
    # The task uses the FQCN module name (ansible.builtin.assert), as
    # ansible-lint's production profile requires -- fall back to the bare
    # name too so this loader does not silently start passing everything if
    # that FQCN-ness ever changes.
    assert_args = task.get("ansible.builtin.assert") or task.get("assert")
    if assert_args is None:
        raise AssertionError(f"task {task.get('name')!r} has no assert/ansible.builtin.assert key")
    for condition in assert_args["that"]:
        compiled = env.compile_expression(condition, undefined_to_none=False)
        if not compiled(**merged):
            return False
    return True


class TestDestructiveConfirmationGate:
    TASK_NAME = "Assert the destructive-confirmation gate is explicitly set to true"

    def test_boolean_true_is_accepted(self):
        task = _find_task(_load_validate_tasks(), self.TASK_NAME)
        assert _assert_conditions_hold(task, {"asmb8_autoinstall_iso_confirm_destructive": True}) is True

    def test_boolean_false_default_is_rejected(self):
        # False is the documented default (defaults/main.yml) -- this pins
        # that leaving the gate at its default does not silently pass.
        task = _find_task(_load_validate_tasks(), self.TASK_NAME)
        assert _assert_conditions_hold(task, {"asmb8_autoinstall_iso_confirm_destructive": False}) is False

    @pytest.mark.parametrize("truthy_spelling", ["true", "True", "yes", "on", "1"])
    def test_command_line_style_string_spellings_of_true_are_accepted(self, truthy_spelling):
        # This gate uses the `| bool` filter -- deliberately matching
        # asmb8_baremetal_install_confirm_destructive's own gate in this
        # collection -- specifically so `-e
        # asmb8_autoinstall_iso_confirm_destructive=true` on the command
        # line (where extra-vars always arrive as strings) actually works,
        # not just a real YAML boolean.
        task = _find_task(_load_validate_tasks(), self.TASK_NAME)
        ctx = {"asmb8_autoinstall_iso_confirm_destructive": truthy_spelling}
        assert _assert_conditions_hold(task, ctx) is True

    @pytest.mark.parametrize("falsy_spelling", ["false", "False", "no", "off", "0"])
    def test_command_line_style_string_spellings_of_false_are_rejected(self, falsy_spelling):
        task = _find_task(_load_validate_tasks(), self.TASK_NAME)
        ctx = {"asmb8_autoinstall_iso_confirm_destructive": falsy_spelling}
        assert _assert_conditions_hold(task, ctx) is False

    def test_a_value_that_is_not_boolean_ish_at_all_errors_rather_than_silently_passing(self):
        task = _find_task(_load_validate_tasks(), self.TASK_NAME)
        ctx = {"asmb8_autoinstall_iso_confirm_destructive": "please"}
        with pytest.raises(ValueError):
            _assert_conditions_hold(task, ctx)


class TestDiskTargetingMechanism:
    TASK_NAME = "Assert exactly one disk-targeting mechanism is configured"

    def test_neither_disk_list_nor_disk_filter_set_is_rejected(self):
        task = _find_task(_load_validate_tasks(), self.TASK_NAME)
        ctx = {"asmb8_autoinstall_iso_answer_disk_list": [], "asmb8_autoinstall_iso_answer_disk_filter": {}}
        assert _assert_conditions_hold(task, ctx) is False

    def test_both_disk_list_and_disk_filter_set_is_rejected(self):
        task = _find_task(_load_validate_tasks(), self.TASK_NAME)
        ctx = {
            "asmb8_autoinstall_iso_answer_disk_list": ["nvme0n1"],
            "asmb8_autoinstall_iso_answer_disk_filter": {"ID_SERIAL_SHORT": "abc"},
        }
        assert _assert_conditions_hold(task, ctx) is False

    def test_disk_list_only_is_accepted(self):
        task = _find_task(_load_validate_tasks(), self.TASK_NAME)
        ctx = {"asmb8_autoinstall_iso_answer_disk_list": ["nvme0n1"], "asmb8_autoinstall_iso_answer_disk_filter": {}}
        assert _assert_conditions_hold(task, ctx) is True

    def test_disk_filter_only_is_accepted(self):
        task = _find_task(_load_validate_tasks(), self.TASK_NAME)
        ctx = {"asmb8_autoinstall_iso_answer_disk_list": [], "asmb8_autoinstall_iso_answer_disk_filter": {"ID_SERIAL_SHORT": "abc"}}
        assert _assert_conditions_hold(task, ctx) is True


class TestDiskListEntryGuards:
    TASK_NAME = "Assert every asmb8_autoinstall_iso_answer_disk_list entry is a concrete, non-wildcard kernel device name"

    def test_concrete_kernel_device_name_is_accepted(self):
        task = _find_task(_load_validate_tasks(), self.TASK_NAME)
        assert _assert_conditions_hold(task, {"asmb8_autoinstall_iso_answer_disk_list": ["nvme0n1"]}) is True

    @pytest.mark.parametrize(
        "bad_disk_list",
        [
            ["*"],
            ["sd*"],
            ["nvme?n1"],
            [""],
            ["sda", ""],
            ["  "],
            ["first"],
            ["any"],
            ["ANY"],
            ["default"],
            ["auto"],
            ["/dev/sda"],
        ],
    )
    def test_wildcards_blanks_and_placeholders_are_rejected(self, bad_disk_list):
        task = _find_task(_load_validate_tasks(), self.TASK_NAME)
        assert _assert_conditions_hold(task, {"asmb8_autoinstall_iso_answer_disk_list": bad_disk_list}) is False


class TestDiskFilterValueGuards:
    TASK_NAME = "Assert every asmb8_autoinstall_iso_answer_disk_filter value is concrete and non-wildcard"

    def test_concrete_serial_value_is_accepted(self):
        task = _find_task(_load_validate_tasks(), self.TASK_NAME)
        ctx = {"asmb8_autoinstall_iso_answer_disk_filter": {"ID_SERIAL_SHORT": "S3Z8NB0M123456"}}
        assert _assert_conditions_hold(task, ctx) is True

    @pytest.mark.parametrize("bad_value", ["*", "", "  ", "any", "ANY", "default", "auto", "first"])
    def test_wildcards_blanks_and_placeholders_are_rejected(self, bad_value):
        task = _find_task(_load_validate_tasks(), self.TASK_NAME)
        ctx = {"asmb8_autoinstall_iso_answer_disk_filter": {"ID_SERIAL_SHORT": bad_value}}
        assert _assert_conditions_hold(task, ctx) is False


class TestDiskSafetyGuardsAcceptTheDocumentedGoodExample:
    """A guard tight enough to reject bad input is only useful if it also
    lets legitimate input through -- this proves the "good" values used
    throughout this file (and README.md's own examples) actually pass.
    """

    def test_representative_targets_pass_every_relevant_check(self):
        tasks = _load_validate_tasks()
        ctx = {
            "asmb8_autoinstall_iso_confirm_destructive": True,
            "asmb8_autoinstall_iso_answer_disk_list": ["nvme0n1"],
            "asmb8_autoinstall_iso_answer_disk_filter": {},
        }
        for name in (
            "Assert the destructive-confirmation gate is explicitly set to true",
            "Assert exactly one disk-targeting mechanism is configured",
            "Assert every asmb8_autoinstall_iso_answer_disk_list entry is a concrete, non-wildcard kernel device name",
        ):
            assert _assert_conditions_hold(_find_task(tasks, name), ctx) is True, name
