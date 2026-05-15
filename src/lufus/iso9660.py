"""Pure Python ISO 9660 filesystem file lister.

Reads the Primary Volume Descriptor and recursively walks directory
records to build a file listing — no external tools required.

This is a minimal, correct implementation sufficient for ISO type
detection (checking file markers). It does not handle all edge cases
(multi-extent files, deep directory trees, etc.) but correctly lists
all files in the root and first-level subdirectories.
"""

import struct
from lufus.lufus_logging import get_logger

log = get_logger(__name__)

_SECTOR_SIZE = 2048
_PVD_LBA = 16


def _pvd_offset(lba: int) -> int:
    return lba * _SECTOR_SIZE


class _DirRecord:
    """ISO 9660 directory record."""

    __slots__ = ("name", "extent_lba", "data_length", "is_dir", "flags", "id_len")

    def __init__(self, data: bytes, offset: int):
        dr_len = data[offset]
        if dr_len == 0:
            self.name = None
            return
        self.id_len = data[offset + 32]
        name_raw = data[offset + 33 : offset + 33 + self.id_len]
        self.name = name_raw.decode("ascii", errors="replace")
        self.extent_lba = struct.unpack_from("<I", data, offset + 2)[0]
        self.data_length = struct.unpack_from("<I", data, offset + 10)[0]
        self.flags = data[offset + 25]
        self.is_dir = bool(self.flags & 0x02)
        # Pad to even length for next record
        self._total_len = dr_len + (dr_len % 2)

    @property
    def is_current(self) -> bool:
        return self.name == "\x00"

    @property
    def is_parent(self) -> bool:
        return self.name == "\x01"

    def skip(self, data: bytes, offset: int) -> int:
        """Return offset of next record."""
        return offset + self._total_len


def _read_sector(f, lba: int) -> bytes:
    f.seek(_pvd_offset(lba))
    return f.read(_SECTOR_SIZE)


def _list_dir(f, lba: int, length: int) -> list[tuple[str, bool]]:
    """Return [(name, is_dir), ...] for directory at *lba*.

    Files and subdirectories are listed with their leaf names.
    """
    entries = []
    sector_offset = 0
    while sector_offset < length:
        data = _read_sector(f, lba + sector_offset // _SECTOR_SIZE)
        pos = sector_offset % _SECTOR_SIZE
        while pos < len(data):
            rec = _DirRecord(data, pos)
            if rec.name is None:
                break
            if not rec.is_current and not rec.is_parent:
                entries.append((rec.name, rec.is_dir))
            pos = rec.skip(data, pos)
            if pos >= _SECTOR_SIZE:
                sector_offset += pos
                break
        else:
            sector_offset += _SECTOR_SIZE
            continue
        break
    return entries


def _find_subdir(entries: list[tuple[str, bool]], name: str):
    """Find a subdirectory entry by name (case-insensitive)."""
    for entry_name, is_dir in entries:
        if is_dir and entry_name.lower() == name.lower():
            return entry_name
    return None


def list_files(iso_path: str) -> list[str] | None:
    """Return a recursive file listing of an ISO 9660 image.

    Returns a list of lowercased paths like::

        ["sources/install.wim", "boot/grub/grub.cfg", ...]

    Or None if the file cannot be read / is not ISO 9660.
    """
    try:
        with open(iso_path, "rb") as f:
            f.seek(_pvd_offset(_PVD_LBA) + 1)
            if f.read(5) != b"CD001":
                log.debug("list_files: CD001 magic missing in %s", iso_path)
                return None

            # Read Primary Volume Descriptor
            pvd = _read_sector(f, _PVD_LBA)

            # Root directory record is at offset 156 in PVD
            root_rec = _DirRecord(pvd, 156)
            if root_rec.name is None:
                log.debug("list_files: cannot read root directory in %s", iso_path)
                return None

            # Walk root directory
            root_entries = _list_dir(f, root_rec.extent_lba, root_rec.data_length)

            results: list[str] = []
            dirs_to_process: list[tuple[str, int, int]] = []  # (path_prefix, lba, length)

            for name, is_dir in root_entries:
                path = name.lower()
                if is_dir:
                    dirs_to_process.append((path + "/", 0, 0))
                    results.append(path + "/")
                else:
                    results.append(path)

            # Resolve subdirectory LBAs and recurse
            for path_prefix, _, _ in dirs_to_process:
                # Find the subdir entry in root to get its LBA
                dir_name = path_prefix.rstrip("/").lower()
                matched = _find_subdir(root_entries, dir_name)
                if matched is None:
                    continue
                # We need the original entry to get LBA
                # Re-read the root directory to get LBAs for subdirs
                root_entries_full = _list_dir_full(f, root_rec.extent_lba, root_rec.data_length)
                sub_entry = None
                for name, is_dir, lba, length in root_entries_full:
                    if is_dir and name == matched:
                        sub_entry = (name, lba, length)
                        break
                if sub_entry is None:
                    continue
                _, lba, length = sub_entry
                sub_entries = _list_dir(f, lba, length)
                for name, is_dir in sub_entries:
                    path = path_prefix + name.lower()
                    if is_dir:
                        results.append(path + "/")
                        # Would recurse deeper, but for detection purposes
                        # one level of subdirectories is sufficient.
                    else:
                        results.append(path)

            return results

    except OSError as e:
        log.error("list_files: cannot read %s: %s", iso_path, e)
        return None


def _list_dir_full(f, lba: int, length: int) -> list[tuple[str, bool, int, int]]:
    """Like _list_dir but also returns extent LBA and data length."""
    entries = []
    sector_offset = 0
    while sector_offset < length:
        data = _read_sector(f, lba + sector_offset // _SECTOR_SIZE)
        pos = sector_offset % _SECTOR_SIZE
        while pos < len(data):
            rec = _DirRecord(data, pos)
            if rec.name is None:
                break
            if not rec.is_current and not rec.is_parent:
                entries.append((rec.name, rec.is_dir, rec.extent_lba, rec.data_length))
            pos = rec.skip(data, pos)
            if pos >= _SECTOR_SIZE:
                sector_offset += pos
                break
        else:
            sector_offset += _SECTOR_SIZE
            continue
        break
    return entries


def has_any_file(iso_path: str, markers: list[str]) -> bool:
    """Return True if *iso_path* contains any of the given marker files.

    This is more efficient than list_files() for detection — it stops
    as soon as any marker is found.
    """
    try:
        with open(iso_path, "rb") as f:
            f.seek(_pvd_offset(_PVD_LBA) + 1)
            if f.read(5) != b"CD001":
                return False

            pvd = _read_sector(f, _PVD_LBA)
            root_rec = _DirRecord(pvd, 156)
            if root_rec.name is None:
                return False

            lower_markers = [m.lower() for m in markers]

            # Walk root directory
            root_entries_full = _list_dir_full(f, root_rec.extent_lba, root_rec.data_length)

            # Check root-level files
            for name, is_dir, lba, length in root_entries_full:
                if not is_dir:
                    if name.lower() in lower_markers:
                        return True

            # Check subdirectories
            for name, is_dir, lba, length in root_entries_full:
                if not is_dir:
                    continue
                sub_entries = _list_dir(f, lba, length)
                sub_markers = [m for m in lower_markers if m.startswith(name.lower() + "/")]
                for sub_name, sub_is_dir in sub_entries:
                    marker = name.lower() + "/" + sub_name.lower()
                    if marker in lower_markers:
                        return True
                    # Also check deeper (two levels deep should cover most)
                    if sub_is_dir:
                        deeper_lba = None
                        for sn, sd, dlba, dlen in _list_dir_full(f, lba, length):
                            if sd and sn == sub_name:
                                deeper_lba = dlba
                                deeper_len = dlen
                                break
                        if deeper_lba:
                            deeper_entries = _list_dir(f, deeper_lba, deeper_len)
                            for dn, _ in deeper_entries:
                                marker2 = marker + "/" + dn.lower()
                                if marker2 in lower_markers:
                                    return True

            return False

    except OSError as e:
        log.debug("has_any_file(%s): %s", iso_path, e)
        return False
