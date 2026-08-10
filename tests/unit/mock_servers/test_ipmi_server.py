# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Self-tests for the mock ``pyghmi.ipmi.command.Command`` double.

A broken fixture silently invalidates everything downstream -- the same
reasoning ``test_asp_server.py``/``test_iusb_server.py`` are built under, and
the same two-part split:

* Direct tests against :class:`ipmi_server.FakeIpmiBmc`/:class:`FakeIpmiCommand`,
  proving the double itself behaves the way this fixture's own docstring
  claims (in particular, the one-time boot-device-reverts-after-reset
  transition, and the cross-process ``sync_path`` persistence the shim relies
  on).
* ``TestRealIpmiClientAgainstDouble``, which drives the collection's own
  ``IpmiClient`` (``plugins/module_utils/ipmi.py``) against this double via
  the exact seam ``test_ipmi.py`` already patches with a bare ``Mock`` --
  proving the real client's classification logic (auth failure ->
  ``AuthenticationError``, unreachable -> ``TimeoutError_``, the power-wait
  timeout -> ``TimeoutError_`` with ``indeterminate=True``) reacts correctly to
  something that actually raises pyghmi's own real exception *type*, not just
  something a test asserts about in isolation.

Nothing in this file binds a socket or forks a process: see ``ipmi_server.py``'s
module docstring for why -- this is a same-process test double, not a wire
responder, and this file's own tests are consistent with that.
"""

from __future__ import annotations

import json

import pytest
from ipmi_server import (
    AUTH_FAILURE_MESSAGE,
    BLINK_UNSUPPORTED_MESSAGE,
    DEFAULT_MC_INFO,
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    SET_POWER_WAIT_TIMEOUT_MESSAGE,
    UNREACHABLE_MESSAGE,
    FakeIpmiBmc,
    FakeIpmiCommand,
    command_factory,
    connect,
)
from pyghmi import exceptions as real_ipmi_exceptions

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import ipmi
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import (
    AuthenticationError,
    ConnectionError_,
    RemoteOperationError,
    TimeoutError_,
    UnsupportedCapabilityError,
)
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.ipmi import IpmiClient

USERNAME = "admin"
PASSWORD = "correct-horse-battery-staple"
HOST = "198.51.100.10"  # RFC 5737 TEST-NET-2; never a real lab address


def _connect(fixture: FakeIpmiBmc, **overrides) -> FakeIpmiCommand:
    # Defaults match FakeIpmiBmc()'s OWN constructor defaults (DEFAULT_USERNAME/
    # DEFAULT_PASSWORD), not this file's USERNAME/PASSWORD constants, so a test
    # that builds a plain `FakeIpmiBmc()` (most of them, where credentials are
    # not the point) can call `_connect(fixture)` unchanged. Tests that DO care
    # about credentials build `FakeIpmiBmc(username=USERNAME, password=PASSWORD)`
    # explicitly and pass matching overrides here.
    kwargs = {"bmc": HOST, "userid": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD, "port": 623}
    kwargs.update(overrides)
    return connect(fixture, **kwargs)


class TestConnect:
    def test_correct_credentials_return_a_command(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        assert isinstance(command, FakeIpmiCommand)
        assert command.endpoint == f"{HOST}:623"

    def test_wrong_password_raises_the_real_auth_failure_message(self):
        fixture = FakeIpmiBmc()
        with pytest.raises(real_ipmi_exceptions.IpmiException, match=AUTH_FAILURE_MESSAGE):
            _connect(fixture, password="wrong")

    def test_wrong_username_also_raises_the_auth_failure_message(self):
        fixture = FakeIpmiBmc()
        with pytest.raises(real_ipmi_exceptions.IpmiException, match=AUTH_FAILURE_MESSAGE):
            _connect(fixture, userid="somebody-else")

    def test_force_auth_failure_rejects_even_correct_credentials(self):
        fixture = FakeIpmiBmc()
        fixture.faults.force_auth_failure = True
        with pytest.raises(real_ipmi_exceptions.IpmiException, match=AUTH_FAILURE_MESSAGE):
            _connect(fixture)

    def test_force_unreachable_raises_the_timeout_marker(self):
        fixture = FakeIpmiBmc()
        fixture.faults.force_unreachable = True
        with pytest.raises(real_ipmi_exceptions.IpmiException, match=UNREACHABLE_MESSAGE):
            _connect(fixture)

    def test_force_unreachable_takes_priority_over_auth_failure(self):
        # Both flags set: an unreachable BMC never gets far enough to reject
        # credentials at all.
        fixture = FakeIpmiBmc()
        fixture.faults.force_unreachable = True
        fixture.faults.force_auth_failure = True
        with pytest.raises(real_ipmi_exceptions.IpmiException, match=UNREACHABLE_MESSAGE):
            _connect(fixture)

    def test_force_generic_exception_raises_that_exact_message(self):
        fixture = FakeIpmiBmc()
        fixture.faults.force_generic_exception = "bad completion code"
        with pytest.raises(real_ipmi_exceptions.IpmiException, match="bad completion code"):
            _connect(fixture)

    def test_raised_exception_is_a_real_pyghmi_ipmi_exception_not_a_lookalike(self):
        fixture = FakeIpmiBmc()
        with pytest.raises(real_ipmi_exceptions.IpmiException) as excinfo:
            _connect(fixture, password="wrong")
        assert isinstance(excinfo.value, real_ipmi_exceptions.PyghmiException)


class TestGetPower:
    def test_returns_powerstate_dict_matching_initial_state(self):
        fixture = FakeIpmiBmc()
        fixture.state.powerstate = "on"
        command = _connect(fixture)
        assert command.get_power() == {"powerstate": "on"}

    def test_default_initial_state_is_off(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        assert command.get_power() == {"powerstate": "off"}


class TestSetPowerConvergence:
    def test_on_when_already_on_is_convergent_and_returns_powerstate(self):
        fixture = FakeIpmiBmc()
        fixture.state.powerstate = "on"
        command = _connect(fixture)
        assert command.set_power("on") == {"powerstate": "on"}

    def test_off_when_already_off_is_convergent(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        assert command.set_power("off") == {"powerstate": "off"}

    def test_unknown_state_raises(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        with pytest.raises(real_ipmi_exceptions.IpmiException):
            command.set_power("explode")


class TestSetPowerImperative:
    def test_on_from_off_applies_and_returns_pending_without_wait(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        assert command.set_power("on") == {"pendingpowerstate": "on"}
        assert fixture.state.powerstate == "on"

    def test_on_from_off_with_wait_confirms(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        assert command.set_power("on", wait=5) == {"powerstate": "on"}

    def test_shutdown_reports_off_and_applies_off(self):
        fixture = FakeIpmiBmc()
        fixture.state.powerstate = "on"
        command = _connect(fixture)
        assert command.set_power("shutdown", wait=5) == {"powerstate": "off"}
        assert fixture.state.powerstate == "off"

    def test_reset_does_not_change_reported_powerstate(self):
        fixture = FakeIpmiBmc()
        fixture.state.powerstate = "on"
        command = _connect(fixture)
        assert command.set_power("reset") == {"pendingpowerstate": "reset"}
        assert fixture.state.powerstate == "on"

    def test_boot_resolves_to_on_when_off(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        assert command.set_power("boot", wait=5) == {"powerstate": "on"}

    def test_boot_resolves_to_reset_when_on(self):
        fixture = FakeIpmiBmc()
        fixture.state.powerstate = "on"
        command = _connect(fixture)
        assert command.set_power("boot") == {"pendingpowerstate": "reset"}


class TestSetPowerWaitTimeoutFault:
    def test_force_power_wait_timeout_raises_the_exact_pyghmi_message(self):
        fixture = FakeIpmiBmc()
        fixture.faults.force_power_wait_timeout = True
        command = _connect(fixture)
        with pytest.raises(real_ipmi_exceptions.IpmiException, match=SET_POWER_WAIT_TIMEOUT_MESSAGE):
            command.set_power("on", wait=5)

    def test_the_transition_still_applies_despite_the_confirmation_fault(self):
        # The single most operationally important nuance ipmi.py's own
        # docstring calls out: confirmation failing is not the same as the
        # command being rejected.
        fixture = FakeIpmiBmc()
        fixture.faults.force_power_wait_timeout = True
        command = _connect(fixture)
        with pytest.raises(real_ipmi_exceptions.IpmiException):
            command.set_power("on", wait=5)
        assert fixture.state.powerstate == "on"

    def test_non_confirmable_states_are_unaffected_by_the_fault(self):
        fixture = FakeIpmiBmc()
        fixture.state.powerstate = "on"
        fixture.faults.force_power_wait_timeout = True
        command = _connect(fixture)
        # reset is never confirmed regardless of `wait` -- see ipmi.py's docstring.
        assert command.set_power("reset", wait=5) == {"pendingpowerstate": "reset"}


class TestGetBootDevice:
    def test_default_state_matches_the_verified_live_shape(self):
        # VERIFIED LIVE per this collection's task brief: all three keys
        # present, persistent=False, uefimode=False -- see ipmi_server.py's
        # provenance section.
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        assert command.get_bootdev() == {"bootdev": "default", "persistent": False, "uefimode": False}

    def test_omit_uefimode_when_default_models_the_alternate_shape(self):
        fixture = FakeIpmiBmc(omit_uefimode_when_default=True)
        command = _connect(fixture)
        result = command.get_bootdev()
        assert result == {"bootdev": "default", "persistent": True}
        assert "uefimode" not in result

    def test_omit_uefimode_when_default_stops_applying_once_a_device_has_ever_been_set(self):
        fixture = FakeIpmiBmc(omit_uefimode_when_default=True)
        command = _connect(fixture)
        command.set_bootdev("optical")
        command.set_bootdev("default")  # explicitly set back to default
        result = command.get_bootdev()
        assert "uefimode" in result
        assert result == {"bootdev": "default", "persistent": False, "uefimode": False}


class TestSetBootDevice:
    def test_sets_device_persist_and_uefi(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        assert command.set_bootdev("optical", persist=False, uefiboot=True) == {"bootdev": "optical"}
        assert command.get_bootdev() == {"bootdev": "optical", "persistent": False, "uefimode": True}

    def test_cd_alias_reads_back_as_optical(self):
        # The exact example this task's brief calls out.
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        assert command.set_bootdev("cd", persist=False) == {"bootdev": "optical"}
        assert command.get_bootdev()["bootdev"] == "optical"

    def test_unknown_device_returns_an_error_key_rather_than_raising(self):
        # pyghmi's own set_bootdev() does not raise for this -- see ipmi.py's docstring.
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        result = command.set_bootdev("zzz")
        assert "error" in result
        assert "zzz" in result["error"]


class TestOneTimeBootRevertsAfterReset:
    """The single most important behaviour this whole fixture exists to pin."""

    def test_one_time_override_reverts_after_a_reset(self):
        fixture = FakeIpmiBmc()
        fixture.state.powerstate = "on"
        command = _connect(fixture)
        command.set_bootdev("cd", persist=False)
        assert command.get_bootdev()["bootdev"] == "optical"

        command.set_power("reset")

        assert command.get_bootdev() == {"bootdev": "default", "persistent": False, "uefimode": False}

    def test_one_time_override_reverts_after_a_power_on_transition(self):
        fixture = FakeIpmiBmc()  # starts off
        command = _connect(fixture)
        command.set_bootdev("optical", persist=False)

        command.set_power("on")

        assert command.get_bootdev()["bootdev"] == "default"

    def test_persistent_override_survives_a_reset(self):
        # Out of scope for this collection's own asmb8_boot (which refuses
        # persistent=True before ever reaching this double -- see
        # asmb8_boot.py's reject_persistent()), but pyghmi's own API allows
        # it, and this fixture stays honest about that boundary.
        fixture = FakeIpmiBmc()
        fixture.state.powerstate = "on"
        command = _connect(fixture)
        command.set_bootdev("optical", persist=True)

        command.set_power("reset")

        assert command.get_bootdev()["bootdev"] == "optical"

    def test_boot_resolving_to_reset_also_consumes_the_override(self):
        fixture = FakeIpmiBmc()
        fixture.state.powerstate = "on"
        command = _connect(fixture)
        command.set_bootdev("hd", persist=False)

        command.set_power("boot")  # resolves to reset, since already on

        assert command.get_bootdev()["bootdev"] == "default"

    def test_reverting_to_default_explicitly_is_not_mistaken_for_a_pending_revert(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        command.set_bootdev("default", persist=False)
        command.set_power("on")
        # No error, no double-revert weirdness -- just stays default.
        assert command.get_bootdev()["bootdev"] == "default"


class TestGetMcInfo:
    def test_returns_a_bare_string_not_a_dict(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        result = command.get_mci()
        assert result == DEFAULT_MC_INFO
        assert isinstance(result, str)

    def test_none_is_supported(self):
        fixture = FakeIpmiBmc()
        fixture.state.mc_info = None
        command = _connect(fixture)
        assert command.get_mci() is None


class TestResetBmc:
    def test_cold_reset_succeeds_with_no_error(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        command.reset_bmc()  # must not raise
        assert fixture.state.reset_count == 1
        assert fixture.state.last_reset_mode == "cold"

    def test_warm_reset_via_raw_command_succeeds_with_an_empty_response(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        assert command.raw_command(netfn=0x06, command=0x03, retry=False) == {}
        assert fixture.state.reset_count == 1
        assert fixture.state.last_reset_mode == "warm"

    def test_force_reset_rejected_makes_cold_reset_raise(self):
        fixture = FakeIpmiBmc()
        fixture.faults.force_reset_rejected = "reset request rejected"
        command = _connect(fixture)
        with pytest.raises(real_ipmi_exceptions.IpmiException, match="reset request rejected"):
            command.reset_bmc()
        assert fixture.state.reset_count == 0

    def test_force_reset_rejected_makes_warm_reset_return_an_error_key(self):
        fixture = FakeIpmiBmc()
        fixture.faults.force_reset_rejected = "reset request rejected"
        command = _connect(fixture)
        response = command.raw_command(netfn=0x06, command=0x03, retry=False)
        assert response == {"error": "reset request rejected"}
        assert fixture.state.reset_count == 0

    def test_an_unmodelled_raw_command_raises(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        with pytest.raises(real_ipmi_exceptions.IpmiException, match="not modelled"):
            command.raw_command(netfn=0x04, command=0x2D, retry=False)


class TestSetIdentify:
    def test_on_indefinitely_succeeds_with_no_return_value(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        assert command.set_identify(on=True) is None
        assert fixture.state.last_identify_on is True
        assert fixture.state.last_identify_duration is None
        assert fixture.state.identify_count == 1

    def test_off_succeeds_with_no_return_value(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        assert command.set_identify(on=False) is None
        assert fixture.state.last_identify_on is False

    def test_bounded_duration_ignores_the_on_flag(self):
        # The exact footgun ipmi.py's docstring calls out: pyghmi's own
        # standard-command fallback ignores `on` entirely whenever `duration`
        # is not None.
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        command.set_identify(on=False, duration=30)
        assert fixture.state.last_identify_on is True
        assert fixture.state.last_identify_duration == 30

    def test_duration_zero_is_off_regardless_of_on(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        command.set_identify(on=True, duration=0)
        assert fixture.state.last_identify_on is False
        assert fixture.state.last_identify_duration == 0

    def test_duration_above_255_is_clamped(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        command.set_identify(on=True, duration=1000)
        assert fixture.state.last_identify_duration == 255

    def test_blink_true_always_raises_the_exact_pyghmi_message(self):
        fixture = FakeIpmiBmc()
        command = _connect(fixture)
        with pytest.raises(real_ipmi_exceptions.IpmiException, match=BLINK_UNSUPPORTED_MESSAGE):
            command.set_identify(on=True, blink=True)
        assert fixture.state.identify_count == 0

    def test_force_identify_rejected_raises_that_exact_message(self):
        fixture = FakeIpmiBmc()
        fixture.faults.force_identify_rejected = "identify request rejected"
        command = _connect(fixture)
        with pytest.raises(real_ipmi_exceptions.IpmiException, match="identify request rejected"):
            command.set_identify(on=True)
        assert fixture.state.identify_count == 0


class TestCommandFactory:
    def test_factory_matches_the_real_command_constructor_signature(self):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        factory = command_factory(fixture)
        command = factory(bmc=HOST, userid=USERNAME, password=PASSWORD, port=623)
        assert isinstance(command, FakeIpmiCommand)

    def test_factory_propagates_connect_faults(self):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        fixture.faults.force_unreachable = True
        factory = command_factory(fixture)
        with pytest.raises(real_ipmi_exceptions.IpmiException):
            factory(bmc=HOST, userid=USERNAME, password=PASSWORD, port=623)


class TestCrossProcessPersistence:
    """The mechanism `run_ipmi_mock.py`'s generated shim depends on: a later,
    genuinely separate `FakeIpmiBmc` pointed at the same `sync_path` must see
    an earlier one's mutations, since a real Ansible lifecycle issues
    `asmb8_boot`/`asmb8_power`/`asmb8_info` as three separate OS processes.
    This test never forks a real process -- it proves the file-backed
    load/save contract two independent OBJECTS rely on, which is exactly what
    two independent PROCESSES would also rely on.
    """

    def test_a_second_fixture_pointed_at_the_same_file_sees_the_first_ones_mutation(self, tmp_path):
        sync_path = tmp_path / "ipmi_state.json"
        first = FakeIpmiBmc(sync_path=sync_path)
        first_command = _connect(first)
        first_command.set_bootdev("optical", persist=False)

        second = FakeIpmiBmc(sync_path=sync_path)  # a fresh object, standing in for a fresh process
        second_command = _connect(second)
        assert second_command.get_bootdev()["bootdev"] == "optical"

    def test_constructing_a_second_fixture_does_not_clobber_the_first_ones_state(self, tmp_path):
        # The bug this specifically guards against: FakeIpmiBmc.__init__ must
        # NOT unconditionally re-save default state over a file a prior
        # construction already established, or every later process's shim
        # import would silently reset the fixture back to its defaults.
        sync_path = tmp_path / "ipmi_state.json"
        first = FakeIpmiBmc(sync_path=sync_path, username="admin", password="first-password")
        first.state.powerstate = "on"
        first.save()

        FakeIpmiBmc(sync_path=sync_path, username="admin", password="different-default-password")

        reloaded = FakeIpmiBmc(sync_path=sync_path)
        reloaded.load()
        assert reloaded.state.powerstate == "on"
        assert reloaded.state.password == "first-password"

    def test_one_time_boot_revert_survives_across_independent_fixture_objects(self, tmp_path):
        # The exact cross-process scenario run_ipmi_mock.py's shim exists for:
        # asmb8_boot arms the override (one object/process), asmb8_power
        # consumes it (a second, independent object/process), asmb8_info
        # observes the revert (a third).
        sync_path = tmp_path / "ipmi_state.json"
        arm_fixture = FakeIpmiBmc(sync_path=sync_path)
        _connect(arm_fixture).set_bootdev("optical", persist=False)

        power_fixture = FakeIpmiBmc(sync_path=sync_path)
        _connect(power_fixture).set_power("on")

        observe_fixture = FakeIpmiBmc(sync_path=sync_path)
        assert _connect(observe_fixture).get_bootdev()["bootdev"] == "default"

    def test_faults_also_persist_across_independent_fixture_objects(self, tmp_path):
        sync_path = tmp_path / "ipmi_state.json"
        writer = FakeIpmiBmc(sync_path=sync_path)
        writer.faults.force_auth_failure = True
        writer.save()

        reader = FakeIpmiBmc(sync_path=sync_path)
        with pytest.raises(real_ipmi_exceptions.IpmiException, match=AUTH_FAILURE_MESSAGE):
            _connect(reader)

    def test_state_file_is_valid_json_of_the_documented_shape(self, tmp_path):
        sync_path = tmp_path / "ipmi_state.json"
        fixture = FakeIpmiBmc(sync_path=sync_path)
        fixture.state.powerstate = "on"
        fixture.save()

        payload = json.loads(sync_path.read_text())
        assert payload["state"]["powerstate"] == "on"
        assert "faults" in payload


def _make_client(monkeypatch, fixture: FakeIpmiBmc) -> IpmiClient:
    """Wire the real `IpmiClient` to this double via the same seam
    `test_ipmi.py` patches with a bare `Mock`."""
    monkeypatch.setattr(ipmi.ipmi_command, "Command", command_factory(fixture))
    return IpmiClient(host=HOST, username=USERNAME, password=PASSWORD)


class TestRealIpmiClientAgainstDouble:
    """Drives the collection's own IpmiClient against this double -- the only
    way to prove the client's classification logic reacts correctly to
    something that actually raises pyghmi's real exception type, not just
    something a test asserts about in isolation.
    """

    def test_successful_connect_and_get_power_state_round_trip(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        fixture.state.powerstate = "on"
        client = _make_client(monkeypatch, fixture)
        assert client.get_power_state() == {"powerstate": "on"}

    def test_wrong_password_raises_authentication_error(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        monkeypatch.setattr(ipmi.ipmi_command, "Command", command_factory(fixture))
        with pytest.raises(AuthenticationError):
            IpmiClient(host=HOST, username=USERNAME, password="wrong")

    def test_force_unreachable_raises_timeout_error_not_indeterminate(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        fixture.faults.force_unreachable = True
        monkeypatch.setattr(ipmi.ipmi_command, "Command", command_factory(fixture))
        with pytest.raises(TimeoutError_) as excinfo:
            IpmiClient(host=HOST, username=USERNAME, password=PASSWORD)
        # A connect-time timeout is the ordinary kind -- nothing was ever
        # accepted, so it is not the indeterminate case set_power's own
        # confirmation timeout is.
        assert excinfo.value.indeterminate is False

    def test_generic_exception_classifies_as_connection_error(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        fixture.faults.force_generic_exception = "bad completion code"
        monkeypatch.setattr(ipmi.ipmi_command, "Command", command_factory(fixture))
        with pytest.raises(ConnectionError_):
            IpmiClient(host=HOST, username=USERNAME, password=PASSWORD)

    def test_set_power_wait_timeout_survives_as_indeterminate_through_the_real_client(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        fixture.faults.force_power_wait_timeout = True
        client = _make_client(monkeypatch, fixture)
        with pytest.raises(TimeoutError_) as excinfo:
            client.set_power_state("on", wait=5)
        assert excinfo.value.indeterminate is True
        assert excinfo.value.error_class == "timeout"
        # The mutation applied despite the confirmation fault -- observable
        # through the real client's own get_power_state(), not just the
        # fixture's internal attribute.
        assert client.get_power_state() == {"powerstate": "on"}

    def test_one_time_boot_revert_through_the_real_client(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        client = _make_client(monkeypatch, fixture)
        client.set_boot_device("optical", persist=False)
        assert client.get_boot_device()["bootdev"] == "optical"

        client.set_power_state("on")

        assert client.get_boot_device() == {"bootdev": "default", "persistent": False, "uefimode": False}

    def test_get_mc_info_is_a_bare_string_through_the_real_client(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        client = _make_client(monkeypatch, fixture)
        result = client.get_mc_info()
        assert isinstance(result, str)
        assert result == DEFAULT_MC_INFO

    def test_set_boot_device_unknown_device_becomes_remote_operation_error(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        client = _make_client(monkeypatch, fixture)
        with pytest.raises(RemoteOperationError):
            client.set_boot_device("zzz")

    def test_cold_reset_through_the_real_client(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        client = _make_client(monkeypatch, fixture)
        result = client.reset_bmc("cold")
        assert result == {"mode": "cold"}
        assert fixture.state.reset_count == 1
        assert fixture.state.last_reset_mode == "cold"

    def test_warm_reset_through_the_real_client(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        client = _make_client(monkeypatch, fixture)
        result = client.reset_bmc("warm")
        assert result == {"mode": "warm"}
        assert fixture.state.reset_count == 1
        assert fixture.state.last_reset_mode == "warm"

    def test_rejected_reset_becomes_remote_operation_error_through_the_real_client(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        fixture.faults.force_reset_rejected = "reset request rejected"
        client = _make_client(monkeypatch, fixture)
        with pytest.raises(RemoteOperationError):
            client.reset_bmc("cold")

    def test_credentials_never_leak_through_a_failure_raised_by_this_double(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        monkeypatch.setattr(ipmi.ipmi_command, "Command", command_factory(fixture))
        with pytest.raises(AuthenticationError) as excinfo:
            IpmiClient(host=HOST, username=USERNAME, password="wrong")
        assert PASSWORD not in str(excinfo.value)
        assert "wrong" not in str(excinfo.value)

    def test_set_identify_on_indefinitely_through_the_real_client(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        client = _make_client(monkeypatch, fixture)
        assert client.set_identify(on=True) is None
        assert fixture.state.last_identify_on is True
        assert fixture.state.last_identify_duration is None

    def test_set_identify_bounded_duration_through_the_real_client(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        client = _make_client(monkeypatch, fixture)
        client.set_identify(on=True, duration=120)
        assert fixture.state.last_identify_duration == 120

    def test_set_identify_off_through_the_real_client(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        client = _make_client(monkeypatch, fixture)
        client.set_identify(on=False)
        assert fixture.state.last_identify_on is False

    def test_rejected_identify_becomes_remote_operation_error_through_the_real_client(self, monkeypatch):
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        fixture.faults.force_identify_rejected = "identify request rejected"
        client = _make_client(monkeypatch, fixture)
        with pytest.raises(RemoteOperationError):
            client.set_identify(on=True)


class TestModuleLevelLifecycleThroughTheRealClient:
    """One `IpmiClient` instance driven the way `asmb8_boot`'s DOCUMENTATION
    says a caller should refuse to use it -- proving `UnsupportedCapabilityError`
    is this collection's own module-level policy, not something this double
    could ever be asked to relax.
    """

    def test_this_double_has_no_opinion_on_persistent_boot_devices_the_module_layer_enforces(self, monkeypatch):
        # ipmi_server.py itself will happily honour persist=True (see
        # TestOneTimeBootRevertsAfterReset.test_persistent_override_survives_a_reset)
        # -- it is asmb8_boot.reject_persistent() that refuses it, before ever
        # reaching this far. Documented here so nobody mistakes this double's
        # permissiveness for a gap in that refusal.
        fixture = FakeIpmiBmc(username=USERNAME, password=PASSWORD)
        client = _make_client(monkeypatch, fixture)
        client.set_boot_device("hd", persist=True)
        assert fixture.state.boot_persist is True
        with pytest.raises(UnsupportedCapabilityError):
            from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules.asmb8_boot import reject_persistent

            reject_persistent(True)
