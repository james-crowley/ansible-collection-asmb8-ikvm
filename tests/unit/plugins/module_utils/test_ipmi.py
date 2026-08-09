# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for the IPMI client wrapper.

Every test in this file mocks ``pyghmi.ipmi.command.Command`` at the
constructor boundary. None of them open a socket, and none of them talk to
any real BMC -- see this collection's CONTRIBUTING.md and
the mandate this test suite was written under. In particular, nothing here
ever constructs a real ``pyghmi.ipmi.command.Command`` pointed at a real host:
that constructor itself performs a live, synchronous IPMI/RMCP+ session
negotiation (see module_utils/ipmi.py's docstring), which is exactly the kind
of network I/O this suite must not risk triggering by accident.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from pyghmi import exceptions as real_ipmi_exceptions

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import ipmi
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import (
    AuthenticationError,
    ConnectionError_,
    RemoteOperationError,
    TimeoutError_,
)
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.ipmi import IpmiClient

PASSWORD = "Sup3rSecret!"
HOST = "198.51.100.10"  # RFC 5737 TEST-NET-2; never a real lab address


def make_client(monkeypatch, fake_command: Mock) -> IpmiClient:
    """Build an :class:`IpmiClient` whose underlying pyghmi Command is a mock.

    Patches the module-level `ipmi_command.Command` name `ipmi.py` itself
    calls, so `IpmiClient.__init__` never reaches real pyghmi/socket code.
    """
    monkeypatch.setattr(ipmi.ipmi_command, "Command", Mock(return_value=fake_command))
    return IpmiClient(host=HOST, username="admin", password=PASSWORD)


class TestClassifySessionError:
    @pytest.mark.parametrize(
        "message",
        [
            "Incorrect password provided",
            "Invalid RAKP4 integrity code (wrong Kg?)",
            "unauthorized name reported in RAKP2",
        ],
    )
    def test_credential_shaped_messages_classify_as_authentication(self, message):
        assert ipmi._classify_session_error(message) is AuthenticationError

    def test_timeout_message_classifies_as_timeout(self):
        assert ipmi._classify_session_error("timeout") is TimeoutError_

    def test_unrecognised_message_defaults_to_connection(self):
        # The conservative default, matching asp.py's
        # _connection_error_is_post_connect: an unrecognised failure should
        # not imply "retrying immediately is fine".
        assert ipmi._classify_session_error("Unable to transmit to specified address") is ConnectionError_

    def test_empty_message_defaults_to_connection(self):
        assert ipmi._classify_session_error("") is ConnectionError_


class TestImportGuard:
    def test_missing_pyghmi_raises_import_error(self, monkeypatch):
        monkeypatch.setattr(ipmi, "HAS_PYGHMI", False)
        monkeypatch.setattr(ipmi, "PYGHMI_IMPORT_ERROR", "No module named 'pyghmi'")
        with pytest.raises(ImportError, match="pyghmi"):
            IpmiClient(host=HOST, username="admin", password=PASSWORD)


class TestConnect:
    def test_successful_connect_builds_a_client_without_touching_a_socket(self, monkeypatch):
        fake_command = Mock()
        client = make_client(monkeypatch, fake_command)
        assert client.endpoint == f"{HOST}:623"

    def test_custom_port_is_reflected_in_endpoint(self, monkeypatch):
        monkeypatch.setattr(ipmi.ipmi_command, "Command", Mock(return_value=Mock()))
        client = IpmiClient(host=HOST, port=6230, username="admin", password=PASSWORD)
        assert client.endpoint == f"{HOST}:6230"

    def test_password_failure_classifies_as_authentication_error(self, monkeypatch):
        monkeypatch.setattr(
            ipmi.ipmi_command,
            "Command",
            Mock(side_effect=real_ipmi_exceptions.IpmiException("Incorrect password provided")),
        )
        with pytest.raises(AuthenticationError):
            IpmiClient(host=HOST, username="admin", password=PASSWORD)

    def test_timeout_failure_classifies_as_timeout_error(self, monkeypatch):
        monkeypatch.setattr(
            ipmi.ipmi_command,
            "Command",
            Mock(side_effect=real_ipmi_exceptions.IpmiException("timeout")),
        )
        with pytest.raises(TimeoutError_) as excinfo:
            IpmiClient(host=HOST, username="admin", password=PASSWORD)
        assert excinfo.value.indeterminate is False

    def test_unrecognised_failure_classifies_as_connection_error(self, monkeypatch):
        monkeypatch.setattr(
            ipmi.ipmi_command,
            "Command",
            Mock(side_effect=real_ipmi_exceptions.IpmiException("Unable to transmit to specified address")),
        )
        with pytest.raises(ConnectionError_):
            IpmiClient(host=HOST, username="admin", password=PASSWORD)

    def test_connect_failure_never_leaks_the_password(self, monkeypatch):
        monkeypatch.setattr(
            ipmi.ipmi_command,
            "Command",
            Mock(side_effect=real_ipmi_exceptions.IpmiException(f"rejected password={PASSWORD}")),
        )
        with pytest.raises(AuthenticationError) as excinfo:
            IpmiClient(host=HOST, username="admin", password=PASSWORD)
        assert PASSWORD not in str(excinfo.value)
        assert "[REDACTED]" in str(excinfo.value)


class TestGetPowerState:
    def test_returns_pyghmi_dict_verbatim(self, monkeypatch):
        fake_command = Mock()
        fake_command.get_power.return_value = {"powerstate": "on"}
        client = make_client(monkeypatch, fake_command)
        assert client.get_power_state() == {"powerstate": "on"}

    def test_pyghmi_failure_becomes_remote_operation_error(self, monkeypatch):
        fake_command = Mock()
        fake_command.get_power.side_effect = real_ipmi_exceptions.IpmiException("bad completion code")
        client = make_client(monkeypatch, fake_command)
        with pytest.raises(RemoteOperationError):
            client.get_power_state()


class TestSetPowerState:
    def test_returns_pyghmi_dict_verbatim_on_success(self, monkeypatch):
        fake_command = Mock()
        fake_command.set_power.return_value = {"powerstate": "on"}
        client = make_client(monkeypatch, fake_command)
        assert client.set_power_state("on", wait=60) == {"powerstate": "on"}
        fake_command.set_power.assert_called_once_with("on", wait=60)

    def test_wait_confirmation_timeout_is_indeterminate(self, monkeypatch):
        # The one message this module treats specially -- see
        # module_utils/ipmi.py's docstring: by the time pyghmi's internal
        # confirmation loop raises this, the underlying power command was
        # already accepted, so the caller must re-probe, not blindly retry.
        fake_command = Mock()
        fake_command.set_power.side_effect = real_ipmi_exceptions.IpmiException("System did not accomplish power state change")
        client = make_client(monkeypatch, fake_command)
        with pytest.raises(TimeoutError_) as excinfo:
            client.set_power_state("on", wait=60)
        assert excinfo.value.indeterminate is True
        assert excinfo.value.error_class == "timeout"

    def test_command_rejected_before_confirmation_is_a_plain_remote_operation_error(self, monkeypatch):
        # A different IpmiException message -- the command itself was never
        # accepted, so this is NOT the indeterminate case above.
        fake_command = Mock()
        fake_command.set_power.side_effect = real_ipmi_exceptions.IpmiException("invalid data field in request")
        client = make_client(monkeypatch, fake_command)
        with pytest.raises(RemoteOperationError) as excinfo:
            client.set_power_state("on", wait=60)
        assert excinfo.value.indeterminate is False


class TestGetBootDevice:
    def test_returns_pyghmi_dict_verbatim(self, monkeypatch):
        fake_command = Mock()
        fake_command.get_bootdev.return_value = {"bootdev": "default", "persistent": True}
        client = make_client(monkeypatch, fake_command)
        assert client.get_boot_device() == {"bootdev": "default", "persistent": True}

    def test_pyghmi_failure_becomes_remote_operation_error(self, monkeypatch):
        fake_command = Mock()
        fake_command.get_bootdev.side_effect = real_ipmi_exceptions.IpmiException("bad completion code")
        client = make_client(monkeypatch, fake_command)
        with pytest.raises(RemoteOperationError):
            client.get_boot_device()


class TestSetBootDevice:
    def test_returns_pyghmi_dict_verbatim_on_success(self, monkeypatch):
        fake_command = Mock()
        fake_command.set_bootdev.return_value = {"bootdev": "hd"}
        client = make_client(monkeypatch, fake_command)
        assert client.set_boot_device("hd", persist=False, uefiboot=True) == {"bootdev": "hd"}
        fake_command.set_bootdev.assert_called_once_with("hd", persist=False, uefiboot=True)

    def test_returned_error_key_raises_remote_operation_error(self, monkeypatch):
        # pyghmi's set_bootdev() does not always raise: an unrecognised device
        # name is *returned* as {'error': ...} -- see module_utils/ipmi.py.
        fake_command = Mock()
        fake_command.set_bootdev.return_value = {"error": "Unknown bootdevice zzz requested"}
        client = make_client(monkeypatch, fake_command)
        with pytest.raises(RemoteOperationError, match="rejected"):
            client.set_boot_device("hd")

    def test_raised_exception_becomes_remote_operation_error(self, monkeypatch):
        fake_command = Mock()
        fake_command.set_bootdev.side_effect = real_ipmi_exceptions.IpmiException("bad completion code")
        client = make_client(monkeypatch, fake_command)
        with pytest.raises(RemoteOperationError):
            client.set_boot_device("hd")


class TestGetMcInfo:
    def test_returns_a_bare_string_not_a_dict(self, monkeypatch):
        # The regression this guards: get_mci() is NOT shaped like
        # get_power()/get_bootdev() -- see module_utils/ipmi.py's docstring.
        fake_command = Mock()
        fake_command.get_mci.return_value = "some-mc-identifier"
        client = make_client(monkeypatch, fake_command)
        result = client.get_mc_info()
        assert result == "some-mc-identifier"
        assert isinstance(result, str)

    def test_none_stays_none(self, monkeypatch):
        fake_command = Mock()
        fake_command.get_mci.return_value = None
        client = make_client(monkeypatch, fake_command)
        assert client.get_mc_info() is None

    def test_pyghmi_failure_becomes_remote_operation_error(self, monkeypatch):
        fake_command = Mock()
        fake_command.get_mci.side_effect = real_ipmi_exceptions.IpmiException("bad completion code")
        client = make_client(monkeypatch, fake_command)
        with pytest.raises(RemoteOperationError):
            client.get_mc_info()


class TestNoCredentialLeakage:
    def test_operation_failures_never_carry_the_password(self, monkeypatch):
        fake_command = Mock()
        fake_command.get_power.side_effect = real_ipmi_exceptions.IpmiException(f"rejected password={PASSWORD}")
        client = make_client(monkeypatch, fake_command)
        with pytest.raises(RemoteOperationError) as excinfo:
            client.get_power_state()
        assert PASSWORD not in str(excinfo.value)
        assert "[REDACTED]" in str(excinfo.value)
