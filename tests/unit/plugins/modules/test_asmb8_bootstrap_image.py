# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for the ``asmb8_bootstrap_image`` module's own decision logic.

``bootstrap_image.build_bootstrap_image`` (the real staging/grub-mkrescue/
size-budget path) is mocked throughout here, the same way
``test_asmb8_http_origin.py`` mocks ``http_origin.spawn_session`` -- that real
path, including the size-budget enforcement itself, is exercised for real
(with ``grub-mkrescue`` mocked at the ``run_command`` seam, never invoked for
real) in ``tests/unit/plugins/module_utils/test_bootstrap_image.py``. These
tests are about this module's own logic: check-mode, the missing-tool failure
path, option validation (network_mode=static's required_if), and how a
size-budget failure surfaces through ``fail_json``.

Nothing here makes a network request or invokes a real ``grub-mkrescue``/
``xorriso``, whether or not either happens to be installed on the machine
running these tests -- ``find_tool``/``build_bootstrap_image`` are always
mocked.
"""

from __future__ import annotations

import json

import pytest
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import ProtocolError
from ansible_collections.james_crowley.asmb8_ikvm.plugins.modules import asmb8_bootstrap_image

MOD_UTILS = "ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.bootstrap_image"


class AnsibleExitJson(Exception):
    def __init__(self, kwargs):
        super().__init__("exit_json")
        self.kwargs = kwargs


class AnsibleFailJson(Exception):
    def __init__(self, kwargs):
        super().__init__("fail_json")
        self.kwargs = kwargs


def _set_module_args(args: dict) -> None:
    basic._ANSIBLE_ARGS = to_bytes(json.dumps({"ANSIBLE_MODULE_ARGS": args}))
    basic._ANSIBLE_PROFILE = "legacy"


def _exit_json(*_args, **kwargs):
    raise AnsibleExitJson(kwargs)


def _fail_json(*_args, **kwargs):
    raise AnsibleFailJson(kwargs)


@pytest.fixture(autouse=True)
def _patch_exit_and_fail(monkeypatch):
    monkeypatch.setattr(basic.AnsibleModule, "exit_json", _exit_json)
    monkeypatch.setattr(basic.AnsibleModule, "fail_json", _fail_json)


@pytest.fixture(autouse=True)
def _tools_present(monkeypatch):
    """Every test defaults to "both tools found" unless it overrides this itself --
    the missing-tool path gets its own dedicated tests below.
    """
    monkeypatch.setattr(f"{MOD_UTILS}.find_tool", lambda name, explicit_path=None: f"/usr/bin/{name}")


def _static_args(**overrides) -> dict:
    args = {
        "origin_url": "http://192.0.2.10:8080/boot.ipxe",
        "ipxe_lkrn_path": "/srv/netboot/ipxe.lkrn",
        "output_path": "/srv/netboot/out.iso",
        "network_mode": "static",
        "address": "192.0.2.50",
        "netmask": "255.255.255.0",
        "gateway": "192.0.2.1",
    }
    args.update(overrides)
    return args


class TestCheckMode:
    def test_check_mode_never_calls_build_and_reports_changed_true(self, monkeypatch):
        called = []
        monkeypatch.setattr(f"{MOD_UTILS}.build_bootstrap_image", lambda **kwargs: called.append(kwargs))
        _set_module_args({**_static_args(), "_ansible_check_mode": True})
        with pytest.raises(AnsibleExitJson) as excinfo:
            asmb8_bootstrap_image.main()
        result = excinfo.value.kwargs
        assert result["changed"] is True
        assert result["size_bytes"] is None
        assert called == []

    def test_check_mode_still_reports_the_rendered_script(self, monkeypatch):
        monkeypatch.setattr(f"{MOD_UTILS}.build_bootstrap_image", lambda **kwargs: pytest.fail("must not be called"))
        _set_module_args({**_static_args(), "_ansible_check_mode": True})
        with pytest.raises(AnsibleExitJson) as excinfo:
            asmb8_bootstrap_image.main()
        assert "chain http://192.0.2.10:8080/boot.ipxe" in excinfo.value.kwargs["script"]

    def test_check_mode_still_fails_on_missing_tool(self, monkeypatch):
        monkeypatch.setattr(f"{MOD_UTILS}.find_tool", lambda name, explicit_path=None: None)
        _set_module_args({**_static_args(), "_ansible_check_mode": True})
        with pytest.raises(AnsibleFailJson) as excinfo:
            asmb8_bootstrap_image.main()
        assert excinfo.value.kwargs["error_class"] == "unsupported_capability"


class TestMissingTool:
    def test_missing_grub_mkrescue_fails_with_unsupported_capability(self, monkeypatch):
        def fake_find_tool(name, explicit_path=None):
            return None if name == "grub-mkrescue" else "/usr/bin/xorriso"

        monkeypatch.setattr(f"{MOD_UTILS}.find_tool", fake_find_tool)
        _set_module_args(_static_args())
        with pytest.raises(AnsibleFailJson) as excinfo:
            asmb8_bootstrap_image.main()
        result = excinfo.value.kwargs
        assert result["error_class"] == "unsupported_capability"
        assert "grub-mkrescue" in result["msg"]
        assert "xorriso" not in result["msg"].split("grub-mkrescue")[0]  # xorriso not falsely named as missing

    def test_missing_xorriso_fails_with_unsupported_capability(self, monkeypatch):
        def fake_find_tool(name, explicit_path=None):
            return None if name == "xorriso" else "/usr/bin/grub-mkrescue"

        monkeypatch.setattr(f"{MOD_UTILS}.find_tool", fake_find_tool)
        _set_module_args(_static_args())
        with pytest.raises(AnsibleFailJson) as excinfo:
            asmb8_bootstrap_image.main()
        assert "xorriso" in excinfo.value.kwargs["msg"]

    def test_missing_tool_never_reaches_build(self, monkeypatch):
        monkeypatch.setattr(f"{MOD_UTILS}.find_tool", lambda name, explicit_path=None: None)
        monkeypatch.setattr(f"{MOD_UTILS}.build_bootstrap_image", lambda **kwargs: pytest.fail("must not be called"))
        _set_module_args(_static_args())
        with pytest.raises(AnsibleFailJson):
            asmb8_bootstrap_image.main()


class TestSuccessfulBuild:
    def test_reports_size_and_output_path_from_the_real_build(self, monkeypatch):
        monkeypatch.setattr(
            f"{MOD_UTILS}.build_bootstrap_image",
            lambda **kwargs: {"size_bytes": 933_888, "output_path": kwargs["output_path"]},
        )
        _set_module_args(_static_args())
        with pytest.raises(AnsibleExitJson) as excinfo:
            asmb8_bootstrap_image.main()
        result = excinfo.value.kwargs
        assert result["changed"] is True
        assert result["size_bytes"] == 933_888
        assert result["output_path"] == "/srv/netboot/out.iso"
        assert result["operation"]["schema"] == "asmb8-ikvm-operation/v1"
        assert result["operation"]["action"] == "asmb8_bootstrap_image.build"
        assert result["operation"]["endpoint"] == "/srv/netboot/out.iso"
        assert result["operation"]["changed"] is True
        assert result["operation"]["error_class"] is None

    def test_passes_the_resolved_grub_mkrescue_path_through_to_build(self, monkeypatch):
        captured = {}

        def fake_build(**kwargs):
            captured.update(kwargs)
            return {"size_bytes": 1, "output_path": kwargs["output_path"]}

        monkeypatch.setattr(f"{MOD_UTILS}.build_bootstrap_image", fake_build)
        _set_module_args(_static_args())
        with pytest.raises(AnsibleExitJson):
            asmb8_bootstrap_image.main()
        assert captured["grub_mkrescue_path"] == "/usr/bin/grub-mkrescue"
        assert captured["ipxe_lkrn_path"] == "/srv/netboot/ipxe.lkrn"
        assert "chain http://192.0.2.10:8080/boot.ipxe" in captured["script_text"]

    def test_dhcp_mode_does_not_require_address_netmask_gateway(self, monkeypatch):
        monkeypatch.setattr(
            f"{MOD_UTILS}.build_bootstrap_image",
            lambda **kwargs: {"size_bytes": 1, "output_path": kwargs["output_path"]},
        )
        _set_module_args(
            {
                "origin_url": "http://192.0.2.10/boot.ipxe",
                "ipxe_lkrn_path": "/srv/ipxe.lkrn",
                "output_path": "/srv/out.iso",
                "network_mode": "dhcp",
            }
        )
        with pytest.raises(AnsibleExitJson) as excinfo:
            asmb8_bootstrap_image.main()
        assert excinfo.value.kwargs["changed"] is True


class TestOptionValidation:
    def test_static_mode_without_address_fails_required_if(self):
        _set_module_args(
            {
                "origin_url": "http://192.0.2.10/boot.ipxe",
                "ipxe_lkrn_path": "/srv/ipxe.lkrn",
                "output_path": "/srv/out.iso",
                "network_mode": "static",
                "netmask": "255.255.255.0",
                "gateway": "192.0.2.1",
            }
        )
        with pytest.raises(AnsibleFailJson) as excinfo:
            asmb8_bootstrap_image.main()
        assert "address" in excinfo.value.kwargs["msg"]

    def test_unknown_network_mode_is_rejected_by_choices(self):
        _set_module_args({**_static_args(), "network_mode": "carrier-pigeon"})
        with pytest.raises(AnsibleFailJson):
            asmb8_bootstrap_image.main()


class TestSizeBudgetFailure:
    def test_size_budget_exceeded_surfaces_as_protocol_error(self, monkeypatch):
        def fake_build(**kwargs):
            raise ProtocolError(
                f"bootstrap image is 99999999 bytes, exceeding the {kwargs['size_budget_bytes']}-byte size budget.",
                operation="asmb8_bootstrap_image.build",
            )

        monkeypatch.setattr(f"{MOD_UTILS}.build_bootstrap_image", fake_build)
        _set_module_args(_static_args(size_budget_bytes=1024))
        with pytest.raises(AnsibleFailJson) as excinfo:
            asmb8_bootstrap_image.main()
        result = excinfo.value.kwargs
        assert result["error_class"] == "protocol"
        assert "exceeding the 1024-byte size budget" in result["msg"]

    def test_grub_mkrescue_failure_surfaces_as_protocol_error_not_a_traceback(self, monkeypatch):
        def fake_build(**kwargs):
            raise ProtocolError("grub-mkrescue exited 1: some tool output", operation="asmb8_bootstrap_image.build")

        monkeypatch.setattr(f"{MOD_UTILS}.build_bootstrap_image", fake_build)
        _set_module_args(_static_args())
        with pytest.raises(AnsibleFailJson) as excinfo:
            asmb8_bootstrap_image.main()
        assert excinfo.value.kwargs["error_class"] == "protocol"
        assert "grub-mkrescue exited 1" in excinfo.value.kwargs["msg"]
