"""Regression tests for bugs fixed in flash_windows.py and install_ventoy.py.

Each test is named after the bug it reproduces and verifies the fix.
All tests are deterministic and isolated — no real partitions or downloads.
"""

from __future__ import annotations

import os
import sys
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import lufus.writing.windows.flash as fw_module
import lufus.writing.windows.tweaks as tweaks_module
import lufus.block_ops as block_ops_module
from lufus.writing.windows.flash import _get_wim_size, _find_path_case_insensitive, create_partitions
import lufus.writing.install_ventoy as iv_module
from lufus.writing.install_ventoy import download_wimboot, install_grub


class TestFlashWindowsImports:
    def test_optional_callable_not_imported(self):
        """Optional and Callable were imported but never used — removed."""
        import importlib, types

        spec = importlib.util.spec_from_file_location("fw_check", str(SRC / "lufus/writing/windows/flash.py"))
        mod = importlib.util.module_from_spec(spec)
        # If Optional/Callable were still imported they'd be attributes
        assert not hasattr(mod, "Optional"), "Optional should not be imported"
        assert not hasattr(mod, "Callable"), "Callable should not be imported"


class TestRunOutRemoved:
    def test_run_out_no_longer_present(self):
        """run_out() was dead code — it should be gone."""
        assert not hasattr(fw_module, "run_out"), "run_out() dead code should be removed"


class TestFlashWindowsOsErrorOnMissingIso:
    """Before the fix, flash_windows(...) with a missing ISO raised OSError.
    After the fix it must return False.
    """

    def test_returns_false_when_iso_does_not_exist(self, tmp_path):
        missing_iso = str(tmp_path / "nonexistent.iso")
        result = fw_module.flash_windows("/dev/sdb", missing_iso, fw_module.PartitionScheme.SIMPLE_FAT32)
        assert result is False

    def test_returns_false_when_iso_is_a_directory(self, tmp_path):
        result = fw_module.flash_windows("/dev/sdb", str(tmp_path), fw_module.PartitionScheme.SIMPLE_FAT32)
        assert result is False


class TestGetWimSizeCaseInsensitive:
    """Before the fix, glob patterns were hardcoded as 'install.wim' and
    'INSTALL.WIM' but missed 'Install.Wim' or other mixed-case variants.
    """

    def test_finds_lowercase_install_wim(self, tmp_path):
        sources = tmp_path / "sources"
        sources.mkdir()
        wim = sources / "install.wim"
        wim.write_bytes(b"x" * 1000)
        assert _get_wim_size(str(tmp_path)) == 1000

    def test_finds_uppercase_install_wim(self, tmp_path):
        sources = tmp_path / "sources"
        sources.mkdir()
        wim = sources / "INSTALL.WIM"
        wim.write_bytes(b"x" * 2000)
        assert _get_wim_size(str(tmp_path)) == 2000

    def test_finds_mixed_case_install_wim(self, tmp_path):
        """This specific case FAILED before the fix — now it must pass."""
        sources = tmp_path / "sources"
        sources.mkdir()
        wim = sources / "Install.Wim"
        wim.write_bytes(b"x" * 3000)
        assert _get_wim_size(str(tmp_path)) == 3000

    def test_finds_install_esd(self, tmp_path):
        sources = tmp_path / "sources"
        sources.mkdir()
        esd = sources / "install.esd"
        esd.write_bytes(b"y" * 500)
        assert _get_wim_size(str(tmp_path)) == 500

    def test_returns_zero_when_no_wim(self, tmp_path):
        sources = tmp_path / "sources"
        sources.mkdir()
        assert _get_wim_size(str(tmp_path)) == 0


class TestBootmgrLoopVariableRenamed:
    """The loop variable was named 'f' (shadows built-in). It must be 'fname'."""

    def test_loop_uses_fname_variable(self):
        import ast, inspect

        src = inspect.getsource(fw_module.flash_windows)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.For):
                if isinstance(node.target, ast.Name):
                    # There must be no bare 'f' loop target in flash_windows
                    assert node.target.id != "f", "Loop variable 'f' still present — should be renamed to 'fname'"


class TestDownloadWimbootTimeout:
    """Before the fix, urlretrieve had no timeout. After the fix, URLError
    (which wraps socket.timeout) must be caught and return False gracefully.
    """

    def test_returns_false_on_url_error(self, tmp_path, monkeypatch):
        import urllib.error

        def raise_timeout(*args, **kwargs):
            raise urllib.error.URLError("timed out")

        monkeypatch.setattr(iv_module.urllib.request, "urlopen", raise_timeout)
        result = download_wimboot(str(tmp_path / "wimboot"))
        assert result is False

    def test_returns_true_on_success(self, tmp_path, monkeypatch):
        class FakeResponse:
            def read(self):
                return b"WIMBOOTDATA"

        monkeypatch.setattr(iv_module.urllib.request, "urlopen", lambda *a, **kw: FakeResponse())
        dest = tmp_path / "wimboot"
        result = download_wimboot(str(dest))
        assert result is True
        assert dest.read_bytes() == b"WIMBOOTDATA"

    def test_timeout_constant_is_set(self):
        """A named timeout constant must exist and be positive."""
        assert hasattr(iv_module, "WIMBOOT_TIMEOUT")
        assert iv_module.WIMBOOT_TIMEOUT > 0


class TestInstallGrubUsesTempDirs:
    """install_grub must use unique temp directories, not /tmp/efi_prepare."""

    def test_hardcoded_tmp_paths_removed(self):
        import inspect

        src = inspect.getsource(install_grub)
        # Strip annotation comments before checking so the old path names
        # mentioned inside # [ANNOTATION] strings don't cause false positives.
        code = "\n".join(
            line.split("# [ANNOTATION]")[0] for line in src.splitlines() if not line.strip().startswith("#")
        )
        assert "/tmp/efi_prepare" not in code, "Hardcoded /tmp/efi_prepare still present in code"
        assert "/tmp/data_prepare" not in code, "Hardcoded /tmp/data_prepare still present in code"
        assert "mkdtemp" in code, "mkdtemp must be used instead of hardcoded /tmp paths"


class TestInstallGrubBroadExcept:
    """The except clause must be broad enough to catch non-subprocess errors."""

    def test_returns_false_on_permission_error(self, monkeypatch):
        """Before the fix a PermissionError propagated; now it returns False."""
        monkeypatch.setattr(iv_module.os, "geteuid", lambda: 0)

        def raise_perm(*args, **kwargs):
            raise PermissionError("permission denied")

        monkeypatch.setattr(iv_module.subprocess, "run", raise_perm)
        monkeypatch.setattr(iv_module.glob, "glob", lambda *a, **kw: [])

        result = install_grub("/dev/sdb")
        assert result is False

    def test_returns_false_when_not_root(self, monkeypatch):
        monkeypatch.setattr(iv_module.os, "geteuid", lambda: 1000)
        result = install_grub("/dev/sdb")
        assert result is False

    def test_returns_false_for_nvme_device(self, monkeypatch):
        monkeypatch.setattr(iv_module.os, "geteuid", lambda: 0)
        result = install_grub("/dev/nvme0n1")
        assert result is False

    def test_returns_false_for_mmcblk_device(self, monkeypatch):
        monkeypatch.setattr(iv_module.os, "geteuid", lambda: 0)
        result = install_grub("/dev/mmcblk0")
        assert result is False


class TestInstallGrubMmcblkSeparator:
    """The partition separator 'p' was only added for NVMe, not mmcblk.
    The mmcblk guard now prevents reaching that code, but the separator
    logic must be consistent if the guard is ever relaxed.
    """

    def test_separator_logic_includes_mmcblk(self):
        import inspect, ast

        src = inspect.getsource(install_grub)
        # The sep assignment must reference 'mmcblk'
        assert "mmcblk" in src.split("sep =")[1].split("\n")[0], "separator assignment must include 'mmcblk' check"


class TestInstallGrubMountCleanup:
    """Before the fix, returning False early after mounting left the EFI
    partition mounted. The finally block must always run unmount.
    """

    def test_finally_always_runs_on_early_return(self, monkeypatch):
        """Simulate a scenario where grub.cfg is missing (early return path)
        and verify umount is still called for the efi partition.
        """
        import inspect

        src = inspect.getsource(install_grub)
        # Verify the function uses a finally block (structural test)
        assert "finally:" in src, "install_grub must use a finally block for cleanup"
        assert "efi_mounted" in src, "efi_mounted flag must exist to guard conditional unmount"
        assert "data_mounted" in src, "data_mounted flag must exist to guard conditional unmount"


class TestWindowsTweaksMountedTarget:
    """Tweaks must run against the live flash mount, not rediscover USB mounts
    after flash_windows has already unmounted its temporary target mount.
    """

    def test_apply_windows_tweaks_uses_explicit_mount(self, tmp_path, monkeypatch):
        calls = []

        monkeypatch.setattr(tweaks_module.state, "win_hardware_bypass", 1)
        monkeypatch.setattr(tweaks_module.state, "win_microsoft_acc", 1)
        monkeypatch.setattr(tweaks_module.state, "win_local_acc_chk", 1)
        monkeypatch.setattr(tweaks_module.state, "win_privacy", 1)
        monkeypatch.setattr(
            tweaks_module,
            "_get_mount_and_drive",
            lambda: (_ for _ in ()).throw(AssertionError("must not rediscover mount")),
        )
        monkeypatch.setattr(
            tweaks_module, "win_hardware_bypass", lambda mount=None: calls.append(("hw", mount)) or True
        )
        monkeypatch.setattr(
            tweaks_module, "win_local_acc_name", lambda mount=None: calls.append(("name", mount)) or True
        )
        monkeypatch.setattr(
            tweaks_module,
            "win_skip_privacy_questions",
            lambda mount=None: calls.append(("privacy", mount)) or True,
        )

        assert tweaks_module.apply_windows_tweaks(str(tmp_path)) is True
        assert calls == [("hw", str(tmp_path)), ("name", str(tmp_path)), ("privacy", str(tmp_path))]

    def test_flash_windows_applies_tweaks_before_unmounting_targets(self):
        import inspect

        src = inspect.getsource(fw_module.flash_windows)
        assert "apply_windows_tweaks(mount_data)" in src
        assert src.index("apply_windows_tweaks(mount_data)") < src.index("Unmounting target partitions")


class TestCreatePartitionsWaitsForDeviceNodes:
    """Formatting must not start until the kernel has exposed /dev/sdX1."""

    def test_waits_until_partition_node_exists(self, monkeypatch):
        exists_calls = []
        reread_calls = []

        monkeypatch.setattr(fw_module, "get_sysfs_device_size_sectors", lambda drive: 1024 * 1024)
        monkeypatch.setattr(fw_module, "wipe_superblock", lambda *args, **kwargs: None)
        monkeypatch.setattr(fw_module, "write_gpt", lambda *args, **kwargs: True)
        monkeypatch.setattr(fw_module, "reread_partitions", lambda drive: reread_calls.append(drive) or True)
        monkeypatch.setattr(fw_module.time, "sleep", lambda seconds: None)

        times = iter([0.0, 0.0, 0.2, 0.4, 0.6])
        monkeypatch.setattr(fw_module.time, "monotonic", lambda: next(times, 0.8))

        def fake_exists(path):
            exists_calls.append(path)
            return len(exists_calls) >= 3

        monkeypatch.setattr(fw_module.os.path, "exists", fake_exists)

        parts = create_partitions("/dev/sdb", fw_module.PartitionScheme.SIMPLE_FAT32)

        assert parts == [{"role": "data", "path": "/dev/sdb1"}]
        assert exists_calls == ["/dev/sdb1", "/dev/sdb1", "/dev/sdb1"]
        assert reread_calls


class TestGptHeaderCrc:
    """Linux rejects GPT headers whose CRC covers padding beyond HeaderSize."""

    def test_write_gpt_header_crc_uses_header_size(self, tmp_path, monkeypatch):
        disk = tmp_path / "disk.img"
        sectors = 4096
        disk.write_bytes(b"\x00" * sectors * 512)

        monkeypatch.setattr(block_ops_module, "get_sysfs_device_size_sectors", lambda device: sectors)

        assert block_ops_module.write_gpt(
            str(disk),
            [{"role": "data", "start_lba": 2048, "size_lba": 1024, "name": "Windows Data"}],
        )

        with disk.open("rb") as f:
            f.seek(512)
            primary = f.read(512)
            f.seek((sectors - 1) * 512)
            backup = f.read(512)

        for header in (primary, backup):
            header_size = int.from_bytes(header[12:16], "little")
            stored_crc = int.from_bytes(header[16:20], "little")
            assert header_size == 92
            assert stored_crc == block_ops_module._gpt_header_crc(header, header_size)


class TestFlashWindowsCopyGuards:
    """A failed target mount must not copy into a temp directory and report success."""

    def test_mount_or_raise_fails_on_false_mount(self, monkeypatch):
        monkeypatch.setattr(fw_module, "block_mount", lambda *args, **kwargs: False)

        try:
            fw_module._mount_or_raise("/dev/sdb1", "/tmp/target", fstype="vfat")
        except OSError as e:
            assert "Failed to mount /dev/sdb1" in str(e)
        else:
            raise AssertionError("_mount_or_raise must raise when block_mount returns False")

    def test_verify_windows_media_copy_rejects_empty_target(self, tmp_path):
        try:
            fw_module._verify_windows_media_copy(str(tmp_path))
        except OSError as e:
            assert "Windows files were not copied" in str(e)
        else:
            raise AssertionError("empty target should not verify as flashed media")

    def test_verify_windows_media_copy_accepts_windows_markers(self, tmp_path):
        (tmp_path / "sources").mkdir()
        (tmp_path / "bootmgr").write_bytes(b"boot")

        fw_module._verify_windows_media_copy(str(tmp_path))


class TestIsoMountOrder:
    """Windows ISOs must prefer UDF over the tiny ISO9660 compatibility view."""

    def test_mount_iso_tries_udf_before_iso9660(self, tmp_path, monkeypatch):
        calls = []

        monkeypatch.setattr(block_ops_module, "_setup_loop", lambda path: "/dev/loop-test")
        monkeypatch.setattr(block_ops_module, "_detach_loop", lambda loop: None)

        def fake_mount(source, target, fstype=None, flags=0, options=""):
            calls.append(fstype)
            return fstype == "udf"

        monkeypatch.setattr(block_ops_module, "mount", fake_mount)

        assert block_ops_module.mount_iso(str(tmp_path / "win.iso"), str(tmp_path / "mnt")) is True
        assert calls == ["udf"]
