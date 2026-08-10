# Copyright (c) 2026 Jim Crowley
# GNU General Public License v3.0 or later (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for ``module_utils/bootstrap_image.py``'s pure logic.

Every real filesystem operation here stays on loopback-equivalent, local
temp-directory I/O -- nothing here makes a network request, and
``grub-mkrescue`` is always mocked via the ``run_command`` injection point
(see ``build_bootstrap_image``'s own docstring for why that seam exists) so
these tests never depend on -- or invoke -- a real ``grub-mkrescue``/
``xorriso`` install, even when one happens to be present on the machine
running them.
"""

from __future__ import annotations

import subprocess

import pytest

from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils import bootstrap_image
from ansible_collections.james_crowley.asmb8_ikvm.plugins.module_utils.errors import ProtocolError, UnsupportedCapabilityError


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["grub-mkrescue"], returncode=returncode, stdout=stdout, stderr=stderr)


class TestRenderIpxeScript:
    def test_static_renders_set_commands_and_never_a_bare_dhcp(self):
        network = bootstrap_image.NetworkConfig(mode="static", address="192.0.2.50", netmask="255.255.255.0", gateway="192.0.2.1")
        script = bootstrap_image.render_ipxe_script(origin_url="http://192.0.2.10/boot.ipxe", network=network)
        assert script.startswith("#!ipxe\n")
        assert "set net0/ip 192.0.2.50" in script
        assert "set net0/netmask 255.255.255.0" in script
        assert "set net0/gateway 192.0.2.1" in script
        assert "ifopen net0" in script
        assert "chain http://192.0.2.10/boot.ipxe" in script
        assert "\ndhcp\n" not in script
        assert not script.strip().startswith("dhcp")

    def test_static_with_dns_renders_dns_line(self):
        network = bootstrap_image.NetworkConfig(mode="static", address="a", netmask="b", gateway="c", dns="192.0.2.1")
        script = bootstrap_image.render_ipxe_script(origin_url="http://x/boot.ipxe", network=network)
        assert "set net0/dns 192.0.2.1" in script

    def test_static_without_dns_omits_dns_line(self):
        network = bootstrap_image.NetworkConfig(mode="static", address="a", netmask="b", gateway="c")
        script = bootstrap_image.render_ipxe_script(origin_url="http://x/boot.ipxe", network=network)
        assert "net0/dns" not in script

    def test_dhcp_mode_renders_bare_dhcp_and_no_static_lines(self):
        network = bootstrap_image.NetworkConfig(mode="dhcp")
        script = bootstrap_image.render_ipxe_script(origin_url="http://192.0.2.10/boot.ipxe", network=network)
        assert "\ndhcp\n" in script
        assert "net0/ip" not in script
        assert "chain http://192.0.2.10/boot.ipxe" in script

    def test_chain_is_the_only_boot_action(self):
        network = bootstrap_image.NetworkConfig(mode="dhcp")
        script = bootstrap_image.render_ipxe_script(origin_url="http://origin/boot.ipxe", network=network)
        non_blank_lines = [line for line in script.splitlines() if line.strip()]
        assert non_blank_lines[-1] == "chain http://origin/boot.ipxe"


class TestFindTool:
    def test_finds_a_real_tool_on_path(self):
        # python3 is always present in this test's own environment -- a stand-in
        # for "some tool genuinely on PATH", not a claim about grub-mkrescue.
        assert bootstrap_image.find_tool("python3") is not None

    def test_returns_none_for_a_tool_that_does_not_exist(self):
        assert bootstrap_image.find_tool("asmb8-definitely-not-a-real-tool-xyz") is None

    def test_explicit_path_wins_when_executable(self, tmp_path):
        fake_tool = tmp_path / "fake-grub-mkrescue"
        fake_tool.write_text("#!/bin/sh\nexit 0\n")
        fake_tool.chmod(0o755)
        assert bootstrap_image.find_tool("grub-mkrescue", explicit_path=str(fake_tool)) == str(fake_tool)

    def test_explicit_path_rejected_when_not_executable(self, tmp_path):
        fake_tool = tmp_path / "not-executable"
        fake_tool.write_text("not a script")
        fake_tool.chmod(0o644)
        assert bootstrap_image.find_tool("grub-mkrescue", explicit_path=str(fake_tool)) is None

    def test_explicit_path_rejected_when_missing(self, tmp_path):
        assert bootstrap_image.find_tool("grub-mkrescue", explicit_path=str(tmp_path / "nope")) is None


class TestMissingToolError:
    def test_names_every_missing_tool_and_is_unsupported_capability(self):
        err = bootstrap_image.missing_tool_error(["grub-mkrescue", "xorriso"])
        assert isinstance(err, UnsupportedCapabilityError)
        assert "grub-mkrescue" in err.message
        assert "xorriso" in err.message
        assert "apt-get install" in err.message


class TestBuildBootstrapImage:
    def _lkrn(self, tmp_path, size: int = 391065) -> str:
        lkrn = tmp_path / "ipxe.lkrn"
        lkrn.write_bytes(b"\x00" * size)
        return str(lkrn)

    def test_missing_lkrn_raises_protocol_error(self, tmp_path):
        with pytest.raises(ProtocolError, match="ipxe_lkrn_path"):
            bootstrap_image.build_bootstrap_image(
                ipxe_lkrn_path=str(tmp_path / "does-not-exist.lkrn"),
                script_text="#!ipxe\n",
                output_path=str(tmp_path / "out.iso"),
                size_budget_bytes=bootstrap_image.DEFAULT_SIZE_BUDGET_BYTES,
                grub_mkrescue_path="grub-mkrescue",
            )

    def test_successful_build_reports_size_and_writes_output(self, tmp_path):
        lkrn_path = self._lkrn(tmp_path)
        output_path = tmp_path / "out.iso"

        def fake_run(argv, **kwargs):
            assert argv[0] == "grub-mkrescue"
            assert any(a.startswith("--output=") for a in argv)
            # Simulate grub-mkrescue actually producing the ISO.
            output_path.write_bytes(b"\x00" * 900_000)
            return _completed(returncode=0)

        result = bootstrap_image.build_bootstrap_image(
            ipxe_lkrn_path=lkrn_path,
            script_text="#!ipxe\nchain http://origin/boot.ipxe\n",
            output_path=str(output_path),
            size_budget_bytes=bootstrap_image.DEFAULT_SIZE_BUDGET_BYTES,
            grub_mkrescue_path="grub-mkrescue",
            work_dir=str(tmp_path),
            run_command=fake_run,
        )
        assert result["size_bytes"] == 900_000
        assert result["output_path"] == str(output_path)
        assert output_path.is_file()

    def test_nonzero_exit_raises_protocol_error_with_stderr_excerpt(self, tmp_path):
        lkrn_path = self._lkrn(tmp_path)
        output_path = tmp_path / "out.iso"

        def fake_run(argv, **kwargs):
            return _completed(returncode=1, stderr="grub-mkrescue: error: xorriso not found.\n")

        with pytest.raises(ProtocolError, match="xorriso not found"):
            bootstrap_image.build_bootstrap_image(
                ipxe_lkrn_path=lkrn_path,
                script_text="#!ipxe\n",
                output_path=str(output_path),
                size_budget_bytes=bootstrap_image.DEFAULT_SIZE_BUDGET_BYTES,
                grub_mkrescue_path="grub-mkrescue",
                work_dir=str(tmp_path),
                run_command=fake_run,
            )
        assert not output_path.exists()

    def test_success_but_no_output_file_raises_protocol_error(self, tmp_path):
        lkrn_path = self._lkrn(tmp_path)
        output_path = tmp_path / "out.iso"

        def fake_run(argv, **kwargs):
            # rc=0 but never actually writes output_path -- must still be caught.
            return _completed(returncode=0)

        with pytest.raises(ProtocolError, match="was not produced"):
            bootstrap_image.build_bootstrap_image(
                ipxe_lkrn_path=lkrn_path,
                script_text="#!ipxe\n",
                output_path=str(output_path),
                size_budget_bytes=bootstrap_image.DEFAULT_SIZE_BUDGET_BYTES,
                grub_mkrescue_path="grub-mkrescue",
                work_dir=str(tmp_path),
                run_command=fake_run,
            )

    def test_oversized_result_is_deleted_and_raises_protocol_error(self, tmp_path):
        lkrn_path = self._lkrn(tmp_path)
        output_path = tmp_path / "out.iso"
        budget = 1024

        def fake_run(argv, **kwargs):
            output_path.write_bytes(b"\x00" * (budget * 2))
            return _completed(returncode=0)

        with pytest.raises(ProtocolError, match=r"exceeding the .* size budget"):
            bootstrap_image.build_bootstrap_image(
                ipxe_lkrn_path=lkrn_path,
                script_text="#!ipxe\n",
                output_path=str(output_path),
                size_budget_bytes=budget,
                grub_mkrescue_path="grub-mkrescue",
                work_dir=str(tmp_path),
                run_command=fake_run,
            )
        assert not output_path.exists(), "an oversized 'bootstrap' image must never be left in place"

    def test_result_within_budget_is_not_deleted(self, tmp_path):
        lkrn_path = self._lkrn(tmp_path)
        output_path = tmp_path / "out.iso"
        budget = 1024 * 1024

        def fake_run(argv, **kwargs):
            output_path.write_bytes(b"\x00" * 1000)
            return _completed(returncode=0)

        result = bootstrap_image.build_bootstrap_image(
            ipxe_lkrn_path=lkrn_path,
            script_text="#!ipxe\n",
            output_path=str(output_path),
            size_budget_bytes=budget,
            grub_mkrescue_path="grub-mkrescue",
            work_dir=str(tmp_path),
            run_command=fake_run,
        )
        assert result["size_bytes"] == 1000
        assert output_path.exists()

    def test_never_invokes_a_real_subprocess(self, tmp_path, monkeypatch):
        """Belt-and-suspenders: fail loudly if this test file's own use of
        build_bootstrap_image ever falls through to a real subprocess call,
        rather than silently trying (and likely failing to find) a real
        grub-mkrescue on whatever machine runs this suite.
        """

        def _forbidden(*_args, **_kwargs):
            raise AssertionError("build_bootstrap_image must only be exercised through the run_command seam in tests")

        monkeypatch.setattr(subprocess, "run", _forbidden)

        lkrn_path = self._lkrn(tmp_path)
        output_path = tmp_path / "out.iso"

        def fake_run(argv, **kwargs):
            output_path.write_bytes(b"\x00" * 100)
            return _completed(returncode=0)

        bootstrap_image.build_bootstrap_image(
            ipxe_lkrn_path=lkrn_path,
            script_text="#!ipxe\n",
            output_path=str(output_path),
            size_budget_bytes=bootstrap_image.DEFAULT_SIZE_BUDGET_BYTES,
            grub_mkrescue_path="grub-mkrescue",
            work_dir=str(tmp_path),
            run_command=fake_run,
        )
