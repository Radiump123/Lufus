from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lufus.writing.windows import detect as detect_module
from lufus.writing.windows.detect import is_linux_iso


def test_is_linux_iso_detects_marker(monkeypatch):
    """Verify that the list_files-based detection finds linux markers."""
    monkeypatch.setattr(
        detect_module,
        "list_files",
        lambda _p: ["isolinux/isolinux.cfg", "vmlinuz", "initrd.img"],
    )
    assert is_linux_iso("test.iso") is True


def test_is_linux_iso_fails_without_marker(monkeypatch):
    monkeypatch.setattr(
        detect_module,
        "list_files",
        lambda _p: ["random/file/not/linux.txt"],
    )
    assert is_linux_iso("test.iso") is False


def test_is_linux_iso_list_files_none(monkeypatch):
    """When list_files returns None, detection should fall through to OTHER."""
    monkeypatch.setattr(
        detect_module,
        "list_files",
        lambda _p: None,
    )
    assert is_linux_iso("test.iso") is False
