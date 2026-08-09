# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Lifecycle coverage for the destructive bare-metal install path -- the gap
this task exists to close: before this file, ``plugins/module_utils/ipmi.py``
had no fixture at all standing in for the IPMI plane, so
``asmb8_power``/``asmb8_boot`` were only ever exercised against a bare
``unittest.mock.Mock`` that replaced ``build_ipmi_client()`` wholesale (see
``test_asmb8_power.py``/``test_asmb8_boot.py``) -- proving those two modules'
OWN logic, but never proving the real ``IpmiClient`` class integrates
correctly with anything that actually raises pyghmi's real exception types.

Every test in this file instead patches ``ipmi.ipmi_command.Command`` --
one level lower than ``build_ipmi_client`` -- with
``ipmi_server.command_factory(fixture)``, so the REAL ``build_ipmi_client()``
and the REAL ``IpmiClient`` run in every scenario below. See
``tests/integration/mock_servers/ipmi_server.py``'s module docstring for why
that fixture is a same-process test double for ``pyghmi.ipmi.command.Command``
and not a wire-level responder.

Nothing here binds a socket to anything other than 127.0.0.1: the ``.asp``/
iUSB legs of the full-lifecycle test below use ``AspMockServer``/
``IusbMockServer`` exactly as their own self-tests do (ephemeral loopback
ports only); the IPMI leg never binds a socket at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import ipmi
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import iusb as iusb_client
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.asp import AspClient
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import UnsupportedCapabilityError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_boot, asmb8_info, asmb8_power

#: `tests/integration/mock_servers` is not part of the `ansible_collections`
#: namespace (never shipped in the built collection artifact -- see its own
#: files' docstrings), so it needs its own directory on `sys.path` the way
#: `tests/unit/mock_servers/conftest.py` arranges for that directory's own
#: tests. This file lives elsewhere (`tests/unit/plugins/modules/`), with no
#: such conftest of its own, so it does the same insertion itself here rather
#: than importing the fixtures at module level with an import that would
#: otherwise fail before any test runs.
_MOCK_SERVERS_DIR = str(Path(__file__).resolve().parents[3] / "integration" / "mock_servers")


@pytest.fixture(autouse=True)
def _mock_servers_importable():
    if _MOCK_SERVERS_DIR not in sys.path:
        sys.path.insert(0, _MOCK_SERVERS_DIR)


HOST = "198.51.100.10"  # RFC 5737 TEST-NET-2; never a real lab address
USERNAME = "admin"
PASSWORD = "Sup3rSecret!"

BASE_POWER_ARGS = {"host": HOST, "username": USERNAME, "password": PASSWORD, "state": "on"}
BASE_BOOT_ARGS = {"host": HOST, "username": USERNAME, "password": PASSWORD, "device": "optical"}
BASE_INFO_ARGS = {"host": HOST, "username": USERNAME, "password": PASSWORD}


def _set_module_args(args: dict) -> None:
    basic._ANSIBLE_ARGS = to_bytes(json.dumps({"ANSIBLE_MODULE_ARGS": args}))
    basic._ANSIBLE_PROFILE = "legacy"


class AnsibleExitJson(Exception):
    pass


class AnsibleFailJson(Exception):
    pass


def _exit_json(*_args, **kwargs):
    raise AnsibleExitJson(kwargs)


def _fail_json(*_args, **kwargs):
    raise AnsibleFailJson(kwargs)


@pytest.fixture(autouse=True)
def _patch_module_exit(monkeypatch):
    monkeypatch.setattr(basic.AnsibleModule, "exit_json", _exit_json)
    monkeypatch.setattr(basic.AnsibleModule, "fail_json", _fail_json)


def _run(module_obj, args: dict) -> tuple[bool, dict]:
    """Run one real module's `main()` and report (ok, result)."""
    _set_module_args(args)
    try:
        module_obj.main()
    except AnsibleExitJson as exc:
        return True, exc.args[0]
    except AnsibleFailJson as exc:
        return False, exc.args[0]
    raise AssertionError(f"{module_obj.__name__}.main() returned without exit_json()/fail_json()")  # pragma: no cover - defensive


def _run_ok(module_obj, args: dict) -> dict:
    ok, result = _run(module_obj, args)
    assert ok, f"expected success, got a failure result: {result}"
    return result


def _run_fail(module_obj, args: dict) -> dict:
    ok, result = _run(module_obj, args)
    assert not ok, f"expected a failure, got a success result: {result}"
    return result


def _wire_ipmi_double(monkeypatch, fixture) -> None:
    """Patch `ipmi.ipmi_command.Command` -- the seam `IpmiClient._connect()`
    itself calls -- so the REAL `build_ipmi_client()`/`IpmiClient` run in
    every test below, unlike the sibling per-module test files that replace
    `build_ipmi_client()` wholesale."""
    from ipmi_server import command_factory

    monkeypatch.setattr(ipmi.ipmi_command, "Command", command_factory(fixture))


@pytest.fixture
def ipmi_fixture():
    from ipmi_server import FakeIpmiBmc

    return FakeIpmiBmc(username=USERNAME, password=PASSWORD)


# ===========================================================================
# asmb8_power: idempotence, imperative states, check mode, indeterminate
# timeout -- all through the REAL IpmiClient + double, not a bare Mock.
# ===========================================================================


class TestPowerIdempotenceThroughTheDouble:
    def test_state_on_already_on_is_a_noop(self, monkeypatch, ipmi_fixture):
        ipmi_fixture.state.powerstate = "on"
        _wire_ipmi_double(monkeypatch, ipmi_fixture)
        result = _run_ok(asmb8_power, dict(BASE_POWER_ARGS, state="on"))
        assert result["changed"] is False
        assert ipmi_fixture.state.powerstate == "on"

    def test_state_off_already_off_is_a_noop(self, monkeypatch, ipmi_fixture):
        ipmi_fixture.state.powerstate = "off"
        _wire_ipmi_double(monkeypatch, ipmi_fixture)
        result = _run_ok(asmb8_power, dict(BASE_POWER_ARGS, state="off"))
        assert result["changed"] is False
        assert ipmi_fixture.state.powerstate == "off"

    def test_second_run_of_the_same_task_stays_idempotent(self, monkeypatch, ipmi_fixture):
        ipmi_fixture.state.powerstate = "off"
        _wire_ipmi_double(monkeypatch, ipmi_fixture)
        first = _run_ok(asmb8_power, dict(BASE_POWER_ARGS, state="on"))
        second = _run_ok(asmb8_power, dict(BASE_POWER_ARGS, state="on"))
        assert first["changed"] is True
        assert second["changed"] is False


class TestPowerImperativeStatesThroughTheDouble:
    @pytest.mark.parametrize("state", ["shutdown", "reset", "boot"])
    def test_always_issues_the_request_regardless_of_current_state(self, monkeypatch, ipmi_fixture, state):
        ipmi_fixture.state.powerstate = "on"
        _wire_ipmi_double(monkeypatch, ipmi_fixture)
        result = _run_ok(asmb8_power, dict(BASE_POWER_ARGS, state=state))
        assert result["changed"] is True


class TestPowerCheckModeThroughTheDouble:
    """Check mode's documented contract (`asmb8_power.py`'s own DOCUMENTATION):
    the current state is still read, but `set_power()` is never sent. Verified
    here against the double's own mutable state, not merely against a Mock's
    call count -- proving the module never mutates anything real, not just
    that it never called a particular method name.
    """

    def test_check_mode_never_mutates_when_a_change_would_be_needed(self, monkeypatch, ipmi_fixture):
        ipmi_fixture.state.powerstate = "off"
        _wire_ipmi_double(monkeypatch, ipmi_fixture)
        args = dict(BASE_POWER_ARGS, state="on", _ansible_check_mode=True)
        result = _run_ok(asmb8_power, args)
        assert result["changed"] is True
        assert ipmi_fixture.state.powerstate == "off"

    def test_check_mode_on_an_already_converged_state_reports_no_change(self, monkeypatch, ipmi_fixture):
        ipmi_fixture.state.powerstate = "on"
        _wire_ipmi_double(monkeypatch, ipmi_fixture)
        args = dict(BASE_POWER_ARGS, state="on", _ansible_check_mode=True)
        result = _run_ok(asmb8_power, args)
        assert result["changed"] is False
        assert ipmi_fixture.state.powerstate == "on"


class TestPowerIndeterminateTimeoutThroughTheDouble:
    def test_wait_confirmation_timeout_survives_as_indeterminate(self, monkeypatch, ipmi_fixture):
        ipmi_fixture.state.powerstate = "off"
        ipmi_fixture.faults.force_power_wait_timeout = True
        _wire_ipmi_double(monkeypatch, ipmi_fixture)

        result = _run_fail(asmb8_power, dict(BASE_POWER_ARGS, state="on", wait_timeout=5))

        assert result["error_class"] == "timeout"
        assert result["indeterminate"] is True

    def test_the_transition_still_applied_despite_the_reported_failure(self, monkeypatch, ipmi_fixture):
        # The exact distinction that lets a caller safely re-probe instead of
        # dangerously retrying a power operation -- see ipmi.py's own
        # docstring and ipmi_server.py's provenance section.
        ipmi_fixture.state.powerstate = "off"
        ipmi_fixture.faults.force_power_wait_timeout = True
        _wire_ipmi_double(monkeypatch, ipmi_fixture)

        _run_fail(asmb8_power, dict(BASE_POWER_ARGS, state="on", wait_timeout=5))

        assert ipmi_fixture.state.powerstate == "on"

    def test_reset_is_never_indeterminate_since_pyghmi_never_confirms_it(self, monkeypatch, ipmi_fixture):
        ipmi_fixture.state.powerstate = "on"
        ipmi_fixture.faults.force_power_wait_timeout = True
        _wire_ipmi_double(monkeypatch, ipmi_fixture)

        result = _run_ok(asmb8_power, dict(BASE_POWER_ARGS, state="reset", wait_timeout=5))

        assert result["changed"] is True


# ===========================================================================
# asmb8_boot: one-time override applied then reverting after reset, and the
# persistent=true refusal happening before any connection is opened.
# ===========================================================================


class TestBootOneTimeOverrideThroughTheDouble:
    def test_override_is_applied_then_reverts_after_a_power_boot_transition(self, monkeypatch, ipmi_fixture):
        ipmi_fixture.state.powerstate = "off"
        _wire_ipmi_double(monkeypatch, ipmi_fixture)

        armed = _run_ok(asmb8_boot, dict(BASE_BOOT_ARGS, device="optical"))
        assert armed["operation"]["changed"] is True
        assert ipmi_fixture.state.boot_device == "optical"

        _run_ok(asmb8_power, dict(BASE_POWER_ARGS, state="boot"))  # off -> on, a real boot transition

        reverted = _run_ok(asmb8_boot, dict(BASE_BOOT_ARGS, device="optical", _ansible_check_mode=True))
        # check mode only READS -- if the override had not reverted, this
        # would report changed=False (already 'optical'); it reports
        # changed=True precisely because the double's own state is back to
        # 'default'.
        assert reverted["operation"]["changed"] is True
        assert reverted["previous"]["bootdev"] == "default"

    def test_override_reverts_after_a_reset_while_already_on(self, monkeypatch, ipmi_fixture):
        ipmi_fixture.state.powerstate = "on"
        _wire_ipmi_double(monkeypatch, ipmi_fixture)

        _run_ok(asmb8_boot, dict(BASE_BOOT_ARGS, device="hd"))
        assert ipmi_fixture.state.boot_device == "hd"

        _run_ok(asmb8_power, dict(BASE_POWER_ARGS, state="reset"))

        assert ipmi_fixture.state.boot_device == "default"

    def test_info_module_observes_the_reverted_override_too(self, monkeypatch, ipmi_fixture):
        ipmi_fixture.state.powerstate = "off"
        _wire_ipmi_double(monkeypatch, ipmi_fixture)

        _run_ok(asmb8_boot, dict(BASE_BOOT_ARGS, device="optical"))
        _run_ok(asmb8_power, dict(BASE_POWER_ARGS, state="boot"))
        info = _run_ok(asmb8_info, dict(BASE_INFO_ARGS))

        assert info["asmb8"]["ipmi"]["boot_device"] == {"bootdev": "default", "persistent": False, "uefimode": False}
        assert info["asmb8"]["ipmi"]["power_state"] == {"powerstate": "on"}


class TestPersistentRejectedBeforeAnyConnection:
    def test_persistent_true_fails_before_the_double_is_ever_touched(self, monkeypatch, ipmi_fixture):
        # A poisoned Command constructor: if asmb8_boot's persistent=True
        # refusal did not run BEFORE build_ipmi_client(), this raises an
        # AssertionError that propagates straight out of main() uncaught --
        # a much stronger signal than merely asserting a Mock's call count,
        # since it fails the test even if some future refactor routed the
        # refusal through a code path this test does not otherwise inspect.
        def _poisoned(*_args, **_kwargs):
            raise AssertionError("pyghmi.ipmi.command.Command must not be constructed when persistent=True is rejected up front")

        monkeypatch.setattr(ipmi.ipmi_command, "Command", _poisoned)

        result = _run_fail(asmb8_boot, dict(BASE_BOOT_ARGS, persistent=True))

        assert result["error_class"] == "unsupported_capability"

    def test_persistent_true_refuses_even_in_check_mode_before_any_connection(self, monkeypatch, ipmi_fixture):
        def _poisoned(*_args, **_kwargs):
            raise AssertionError("must not be constructed")

        monkeypatch.setattr(ipmi.ipmi_command, "Command", _poisoned)

        result = _run_fail(asmb8_boot, dict(BASE_BOOT_ARGS, persistent=True, _ansible_check_mode=True))

        assert result["error_class"] == "unsupported_capability"

    def test_reject_persistent_helper_is_the_mechanism(self):
        asmb8_boot.reject_persistent(False)  # must not raise
        with pytest.raises(UnsupportedCapabilityError):
            asmb8_boot.reject_persistent(True)


# ===========================================================================
# The full ordered lifecycle: attach media, arm boot, reset, observe, detach
# -- driven against the IPMI double together with the REAL .asp/iUSB mocks.
# ===========================================================================

ASP_USERNAME = "admin"
ASP_PASSWORD = "media-fixture-password-not-real"


@pytest.fixture
def asp_server():
    from asp_server import AspMockServer

    with AspMockServer(username=ASP_USERNAME, password=ASP_PASSWORD) as server:
        yield server


@pytest.fixture
def iusb_server(asp_server):
    from iusb_server import IusbMockServer

    # The real board's own single-session vmedia slot mints its -kvmtoken from
    # the SAME .asp login session a real asmb8_media attach performs -- see
    # asp_server.py's AspState.kvm_token. Matching the two mocks' tokens here
    # is what makes the iUSB auth handshake below succeed for real, not just
    # self-consistently.
    with IusbMockServer(expected_token=asp_server.state.kvm_token) as server:
        yield server


def _perform_attach(asp_server, iusb_server):
    """The media leg of the lifecycle, over the REAL `.asp`/iUSB wire
    protocols against the REAL mock servers -- `AspClient.login()` +
    `allocate_media_session()`, then the real iUSB auth handshake
    (`iusb.Session.connect()`). Deliberately not the full `asmb8_media`
    module/background-daemon machinery (that module's own established test
    convention -- see `test_asmb8_media.py` -- mocks the fork boundary itself
    rather than driving a real double-forked daemon inside a unit test);
    what this lifecycle test needs from the media leg is that it is a REAL
    wire-level attach/detach bracketing the REAL IPMI calls, not a second
    exercise of asmb8_media's own already-covered attach/detach logic.
    """
    asp_client = AspClient(
        host="127.0.0.1",
        port=asp_server.port,
        username=ASP_USERNAME,
        password=ASP_PASSWORD,
        use_tls=False,
        allow_insecure_transport=True,
        max_retries=0,
    )
    asp_client.login()
    jnlp = asp_client.allocate_media_session(client_ip="203.0.113.5")
    session = iusb_client.Session.connect("127.0.0.1", iusb_server.port, jnlp.kvm_token, device_type=iusb_client.DEVICE_CDROM, timeout=5.0)
    iusb_server.wait_for_handshake()
    return session


def _run_lifecycle(*, asp_server, iusb_server, ipmi_fixture, fail_at: str | None = None, order: list[str] | None = None) -> list[str]:
    """Attach -> arm boot -> reset -> observe -> detach, in order, with detach
    ALWAYS running -- the same block/rescue/always guarantee
    `roles/asmb8_baremetal_install/tasks/main.yml` documents, expressed here
    directly in Python against the real modules/clients rather than through
    that (frozen, unmodifiable for this task) role.

    `fail_at` injects a real double-triggered failure at `"arm_boot"`,
    `"reset"`, or `"observe"` (never `"attach"`: that leg has no IPMI double
    to fault-inject through, and is not this test's concern -- the media
    wire protocol's own fault paths are already covered by
    `test_asp_server.py`/`test_iusb_server.py`).

    `order` -- when the caller passes its OWN list -- is appended to in
    place, so the sequence executed so far is still visible to the caller
    even when this function raises partway through (unlike a plain return
    value, which a raised exception never delivers).
    """
    if order is None:
        order = []
    media_session = None
    try:
        order.append("attach")
        media_session = _perform_attach(asp_server, iusb_server)

        order.append("arm_boot")
        if fail_at == "arm_boot":
            ipmi_fixture.faults.force_generic_exception = "boom-arm-boot"
        ok, result = _run(asmb8_boot, dict(BASE_BOOT_ARGS, device="optical"))
        if not ok:
            raise RuntimeError(f"arm_boot failed: {result}")

        order.append("reset")
        if fail_at == "reset":
            ipmi_fixture.faults.force_generic_exception = "boom-reset"
        ok, result = _run(asmb8_power, dict(BASE_POWER_ARGS, state="boot"))
        if not ok:
            raise RuntimeError(f"reset failed: {result}")

        order.append("observe")
        if fail_at == "observe":
            ipmi_fixture.faults.force_generic_exception = "boom-observe"
        ok, result = _run(asmb8_info, dict(BASE_INFO_ARGS))
        if not ok:
            raise RuntimeError(f"observe failed: {result}")
    finally:
        order.append("detach")
        if media_session is not None:
            media_session.transport.close()
    return order


class TestFullOrderedLifecycleSequence:
    def test_happy_path_runs_every_phase_in_order(self, monkeypatch, ipmi_fixture, asp_server, iusb_server):
        ipmi_fixture.state.powerstate = "off"
        _wire_ipmi_double(monkeypatch, ipmi_fixture)

        order = _run_lifecycle(asp_server=asp_server, iusb_server=iusb_server, ipmi_fixture=ipmi_fixture)

        assert order == ["attach", "arm_boot", "reset", "observe", "detach"]
        # The one-time override was armed by arm_boot and consumed by the
        # reset -- observed here through the double's own state, the same
        # cross-step effect asmb8_info's own read in the lifecycle proved.
        assert ipmi_fixture.state.boot_device == "default"
        assert ipmi_fixture.state.powerstate == "on"

    def test_detach_still_runs_when_reset_fails(self, monkeypatch, ipmi_fixture, asp_server, iusb_server):
        ipmi_fixture.state.powerstate = "off"
        _wire_ipmi_double(monkeypatch, ipmi_fixture)

        order: list[str] = []
        with pytest.raises(RuntimeError, match="reset failed"):
            _run_lifecycle(asp_server=asp_server, iusb_server=iusb_server, ipmi_fixture=ipmi_fixture, fail_at="reset", order=order)

        # observe never ran, but attach/arm_boot/reset were attempted in
        # order, and detach ran anyway despite the exception -- this is the
        # "always detach" guarantee under test.
        assert order == ["attach", "arm_boot", "reset", "detach"]

    def test_detach_still_runs_when_arm_boot_fails(self, monkeypatch, ipmi_fixture, asp_server, iusb_server):
        ipmi_fixture.state.powerstate = "off"
        _wire_ipmi_double(monkeypatch, ipmi_fixture)

        order: list[str] = []
        with pytest.raises(RuntimeError, match="arm_boot failed"):
            _run_lifecycle(asp_server=asp_server, iusb_server=iusb_server, ipmi_fixture=ipmi_fixture, fail_at="arm_boot", order=order)

        assert order == ["attach", "arm_boot", "detach"]
        # A failure this early must not have issued the reset at all -- the
        # double's power state must still read the pristine initial value.
        assert ipmi_fixture.state.powerstate == "off"

    def test_detach_still_runs_when_observe_fails(self, monkeypatch, ipmi_fixture, asp_server, iusb_server):
        ipmi_fixture.state.powerstate = "off"
        _wire_ipmi_double(monkeypatch, ipmi_fixture)

        order: list[str] = []
        with pytest.raises(RuntimeError, match="observe failed"):
            _run_lifecycle(asp_server=asp_server, iusb_server=iusb_server, ipmi_fixture=ipmi_fixture, fail_at="observe", order=order)

        assert order == ["attach", "arm_boot", "reset", "observe", "detach"]
        # The reset itself succeeded before observe failed -- the boot
        # override was still consumed.
        assert ipmi_fixture.state.boot_device == "default"


# ===========================================================================
# Credential safety -- consistent with every other module test file's own
# equivalent class, repeated here because this file drives all three modules.
# ===========================================================================


class TestNoCredentialLeakage:
    def test_power_failure_never_carries_the_password(self, monkeypatch, ipmi_fixture):
        ipmi_fixture.faults.force_generic_exception = f"rejected password={PASSWORD}"
        _wire_ipmi_double(monkeypatch, ipmi_fixture)
        result = _run_fail(asmb8_power, dict(BASE_POWER_ARGS, state="on"))
        assert PASSWORD not in json.dumps(result)

    def test_boot_failure_never_carries_the_password(self, monkeypatch, ipmi_fixture):
        ipmi_fixture.faults.force_generic_exception = f"rejected password={PASSWORD}"
        _wire_ipmi_double(monkeypatch, ipmi_fixture)
        result = _run_fail(asmb8_boot, dict(BASE_BOOT_ARGS))
        assert PASSWORD not in json.dumps(result)
