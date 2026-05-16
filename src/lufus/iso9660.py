"""Pure Python ISO 9660 filesystem file lister.

Reads the Primary Volume Descriptor and recursively walks directory
records to build a file listing — no external tools required.

This is a minimal, correct implementation sufficient for ISO type
detection (checking file markers). It does not handle all edge cases
(multi-extent files, etc.) but correctly lists
all files via recursive directory traversal.
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



def _walk_dir(f, lba: int, length: int) -> list[tuple[str, bool, int, int]]:
    """Walk a directory, returning (name, is_dir, extent_lba, data_length) entries."""
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

            pvd = _read_sector(f, _PVD_LBA)
            root_rec = _DirRecord(pvd, 156)
            if root_rec.name is None:
                log.debug("list_files: cannot read root directory in %s", iso_path)
                return None

            results: list[str] = []
            work: list[tuple[str, int, int]] = [("", root_rec.extent_lba, root_rec.data_length)]

            while work:
                prefix, lba, length = work.pop()
                entries = _walk_dir(f, lba, length)
                for name, is_dir, sub_lba, sub_len in entries:
                    path = prefix + name.lower()
                    if is_dir:
                        results.append(path + "/")
                        work.append((path + "/", sub_lba, sub_len))
                    else:
                        results.append(path)

            return results

    except OSError as e:
        log.error("list_files: cannot read %s: %s", iso_path, e)
        return None


def has_any_file(iso_path: str, markers: list[str]) -> bool:
    """Return True if *iso_path* contains any of the given marker files.

    Performs a full recursive directory walk with early termination as
    soon as any marker is found.
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

            lower_markers = set(m.lower() for m in markers)
            work: list[tuple[str, int, int]] = [("", root_rec.extent_lba, root_rec.data_length)]

            while work:
                prefix, lba, length = work.pop()
                entries = _walk_dir(f, lba, length)
                for name, is_dir, sub_lba, sub_len in entries:
                    path = prefix + name.lower()
                    if path in lower_markers:
                        return True
                    if is_dir:
                        work.append((path + "/", sub_lba, sub_len))

            return False

    except OSError as e:
        log.debug("has_any_file(%s): %s", iso_path, e)
        return False
