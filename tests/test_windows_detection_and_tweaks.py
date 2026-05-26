from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lufus.writing.windows import detect as detect_module
from lufus.writing.windows import tweaks as tweaks_module


def test_grub_config_alone_is_not_bootable(monkeypatch):
    monkeypatch.setattr(detect_module, "_get_info_via_file_cmd", lambda _p: "ISO 9660 filesystem data")

    assert detect_module.is_bootable(["boot/grub/grub.cfg"], "test.iso") is False


def test_file_not_bootable_phrase_does_not_count(monkeypatch):
    monkeypatch.setattr(detect_module, "_get_info_via_file_cmd", lambda _p: "ISO 9660 data, not bootable")

    assert detect_module.is_bootable([], "test.iso") is False


def test_windows_version_does_not_guess_win11_from_dv9_label(monkeypatch):
    monkeypatch.setattr(detect_module, "_read_pvd_label", lambda _p: "CCCOMA_X64FRE_EN-US_DV9")
    monkeypatch.setattr(detect_module, "_get_info_via_file_cmd", lambda _p: "")
    monkeypatch.setattr(detect_module, "read_file", lambda *_args, **_kwargs: None)

    assert detect_module.get_windows_version("win.iso") is None


def test_windows_version_uses_build_numbers(monkeypatch):
    monkeypatch.setattr(detect_module, "_read_pvd_label", lambda _p: "")
    monkeypatch.setattr(detect_module, "_get_info_via_file_cmd", lambda _p: "Windows build 19045")
    monkeypatch.setattr(detect_module, "read_file", lambda *_args, **_kwargs: None)
    assert detect_module.get_windows_version("win10.iso") == 10

    monkeypatch.setattr(detect_module, "_get_info_via_file_cmd", lambda _p: "Windows build 22631")
    assert detect_module.get_windows_version("win11.iso") == 11

    monkeypatch.setattr(detect_module, "_get_info_via_file_cmd", lambda _p: "MinClient=10.0.22631.1")
    assert detect_module.get_windows_version("win11.iso") == 11


def test_hardware_bypass_writes_autounattend_without_wim_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(tweaks_module, "_detect_arch", lambda _mount: "amd64")

    assert tweaks_module.win_hardware_bypass(str(tmp_path)) is True

    xml_path = tmp_path / "autounattend.xml"
    assert xml_path.exists()
    root = ET.parse(xml_path).getroot()
    body = ET.tostring(root, encoding="unicode")
    assert "BypassTPMCheck" in body
    assert "windowsPE" in body
    assert "wcm:action" in body
