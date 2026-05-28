from __future__ import annotations

import sys
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lufus.writing.windows import detect as detect_module
from lufus.writing.windows import tweaks as tweaks_module
from lufus.iso9660 import has_el_torito_boot_catalog


def test_grub_config_alone_is_not_bootable(monkeypatch):
    monkeypatch.setattr(detect_module, "_get_info_via_file_cmd", lambda _p: "ISO 9660 filesystem data")

    assert detect_module.is_bootable(["boot/grub/grub.cfg"], "test.iso") is False


def test_file_not_bootable_phrase_does_not_count(monkeypatch):
    monkeypatch.setattr(detect_module, "_get_info_via_file_cmd", lambda _p: "ISO 9660 data, not bootable")

    assert detect_module.is_bootable([], "test.iso") is False


def test_file_not_bootable_phrase_overrides_bootloader_files(monkeypatch):
    monkeypatch.setattr(detect_module, "_get_info_via_file_cmd", lambda _p: "ISO 9660 data, not bootable")

    assert detect_module.is_bootable(["bootmgr", "efi/boot/bootx64.efi"], "test.iso") is False


def test_eltorito_boot_catalog_detected_without_filename_markers(monkeypatch):
    monkeypatch.setattr(detect_module, "_get_info_via_file_cmd", lambda _p: "ISO 9660 filesystem data")
    monkeypatch.setattr(detect_module, "has_el_torito_boot_catalog", lambda _p: True)

    assert detect_module.is_bootable([], "test.iso") is True


def test_has_el_torito_boot_catalog_reads_iso_metadata(tmp_path):
    iso = tmp_path / "bootable.iso"
    catalog_lba = 19
    payload = bytearray((catalog_lba + 1) * 2048)

    pvd = bytearray(2048)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    payload[16 * 2048 : 17 * 2048] = pvd

    boot_record = bytearray(2048)
    boot_record[0] = 0
    boot_record[1:6] = b"CD001"
    boot_record[6] = 1
    boot_record[7:39] = b"EL TORITO SPECIFICATION".ljust(32, b"\0")
    struct.pack_into("<I", boot_record, 71, catalog_lba)
    payload[17 * 2048 : 18 * 2048] = boot_record

    terminator = bytearray(2048)
    terminator[0] = 255
    terminator[1:6] = b"CD001"
    terminator[6] = 1
    payload[18 * 2048 : 19 * 2048] = terminator

    validation = bytearray(32)
    validation[0] = 1
    validation[30:32] = b"\x55\xAA"
    checksum = (-sum(struct.unpack_from("<16H", validation))) & 0xFFFF
    struct.pack_into("<H", validation, 28, checksum)

    catalog = bytearray(2048)
    catalog[:32] = validation
    catalog[32] = 0x88
    payload[catalog_lba * 2048 : (catalog_lba + 1) * 2048] = catalog
    iso.write_bytes(payload)

    assert has_el_torito_boot_catalog(str(iso)) is True


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


def test_windows_version_reads_archive_metadata_fallback(monkeypatch):
    monkeypatch.setattr(detect_module, "_read_pvd_label", lambda _p: "CCCOMA_X64FRE_EN-US_DV9")
    monkeypatch.setattr(detect_module, "_get_info_via_file_cmd", lambda _p: "")
    monkeypatch.setattr(detect_module, "read_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        detect_module,
        "_read_file_via_archive_tools",
        lambda _iso, path, **_kwargs: b"MinClient=10.0.22631.1" if path == "sources/cversion.ini" else None,
    )

    assert detect_module.get_windows_version("win11.iso") == 11


def test_hardware_bypass_handles_readonly_existing_xml(tmp_path, monkeypatch):
    monkeypatch.setattr(tweaks_module, "_detect_arch", lambda _mount: "amd64")
    # Disable boot.wim modification for this test to focus on XML
    monkeypatch.setattr(tweaks_module, "_check_tweak_deps", lambda: False)

    xml_path = tmp_path / "autounattend.xml"
    xml_path.write_text("<unattend/>")
    # Make it read-only
    xml_path.chmod(0o444)

    assert tweaks_module.win_hardware_bypass(str(tmp_path)) is True

    # Verify it was written to
    root = ET.parse(xml_path).getroot()
    assert "BypassTPMCheck" in ET.tostring(root, encoding="unicode")
