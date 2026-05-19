"""Pure Python block device operations — no subprocess required.

Provides sysfs-based device queries, ctypes-based mount/umount, ioctl
partition re-read, GPT partition table writing, and raw device I/O.
"""

import os
import struct
import ctypes
import ctypes.util
import errno
from pathlib import Path
from lufus.lufus_logging import get_logger

log = get_logger(__name__)

_SYS_CLASS_BLOCK = Path("/sys/class/block")

# -------- libc for mount/umount --------

_LIBC = None


def _get_libc():
    global _LIBC
    if _LIBC is None:
        path = ctypes.util.find_library("c")
        _LIBC = ctypes.CDLL(path, use_errno=True)
    return _LIBC


_MS_RDONLY = 1
_MS_BIND = 4096
_MS_MOVE = 8192
_MS_REC = 16384
_MS_SILENT = 32768
_MS_LAZYTIME = 1 << 25
_MNT_FORCE = 1
_MNT_DETACH = 2
_MNT_EXPIRE = 4


def _dev_name(device_node: str) -> str:
    return os.path.basename(device_node)


def _sysfs_path(device_node: str, *parts: str) -> Path:
    return _SYS_CLASS_BLOCK / _dev_name(device_node) / Path(*parts)


def get_device_size(device_node: str) -> int | None:
    """Return device size in bytes, or None on failure."""
    try:
        size_path = _sysfs_path(device_node, "size")
        sectors = int(size_path.read_text().strip())
        return sectors * 512
    except Exception as e:
        log.warning("get_device_size(%s) failed: %s", device_node, e)
        return None


def get_device_label(device_node: str) -> str | None:
    """Return filesystem label from sysfs, or None if unavailable."""
    try:
        label_path = _sysfs_path(device_node, "label")
        return label_path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return None


def get_logical_block_size(device_node: str) -> int | None:
    """Return logical block size in bytes, or None on failure."""
    try:
        lbs_path = _sysfs_path(device_node, "queue", "logical_block_size")
        return int(lbs_path.read_text().strip())
    except Exception as e:
        log.warning("get_logical_block_size(%s) failed: %s", device_node, e)
        return None


def get_sysfs_device_size_sectors(device_node: str) -> int | None:
    """Return device size in 512-byte sectors from sysfs, or None on failure."""
    try:
        size_path = _sysfs_path(device_node, "size")
        return int(size_path.read_text().strip())
    except Exception as e:
        log.warning("get_sysfs_device_size_sectors(%s) failed: %s", device_node, e)
        return None


def mount(source: str, target: str, fstype: str | None = None, flags: int = 0, options: str = "") -> bool:
    """Mount a filesystem via the mount(2) syscall.

    Returns True on success, False on failure (errors are logged).
    """
    libc = _get_libc()
    libc.mount.restype = ctypes.c_int
    c_source = ctypes.c_char_p(source.encode() if source else None)
    c_target = ctypes.c_char_p(target.encode())
    c_fstype = ctypes.c_char_p(fstype.encode() if fstype else None)
    c_flags = ctypes.c_ulong(flags)
    c_data = ctypes.c_char_p(options.encode() if options else None)

    ret = libc.mount(c_source, c_target, c_fstype, c_flags, c_data)
    if ret != 0:
        errno = ctypes.get_errno()
        log.error("mount(%s, %s, %s) failed: errno=%d", source, target, fstype or "", errno)
        return False
    log.info("Mounted %s on %s (fstype=%s)", source, target, fstype or "")
    return True


def _unescape_mountinfo(s: str) -> str:
    """Unescape octal escapes (e.g. \\040 for space) in mountinfo paths."""
    chars = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 3 < len(s):
            try:
                chars.append(chr(int(s[i + 1 : i + 4], 8)))
                i += 4
                continue
            except ValueError:
                pass
        chars.append(s[i])
        i += 1
    return "".join(chars)


def _resolve_mount_point(device_or_path: str) -> str | None:
    """Resolve a block device path (e.g. /dev/sda1) to its mount point
    directory by reading /proc/self/mountinfo.

    If *device_or_path* is already a directory it is returned unchanged
    (it may already be a mount point).

    The kernel's umount2(2) accepts a *mount point* path, not a device
    node path, so callers must resolve device paths to mount points first.
    """
    if os.path.isdir(device_or_path):
        return device_or_path
    try:
        st = os.stat(device_or_path)
    except OSError:
        return device_or_path
    dev_id = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 10 and parts[2] == dev_id:
                    return _unescape_mountinfo(parts[4])
    except OSError:
        pass
    return device_or_path


def umount(target: str, flags: int = 0) -> bool:
    """Unmount a filesystem via the umount(2) syscall.

    Accepts either a mount point directory or a block device path.
    Returns True on success, False on failure.
    """
    mount_point = _resolve_mount_point(target)
    libc = _get_libc()
    libc.umount2.restype = ctypes.c_int
    c_target = ctypes.c_char_p(mount_point.encode())
    c_flags = ctypes.c_int(flags)

    ret = libc.umount2(c_target, c_flags)
    if ret != 0:
        errno = ctypes.get_errno()
        log.warning("umount(%s) failed: errno=%d", target, errno)
        return False
    log.info("Unmounted %s", target)
    return True


def umount_lazy(target: str) -> bool:
    """Lazy unmount (equivalent to umount -l). Returns True on success."""
    return umount(target, flags=_MNT_DETACH)


# -------- BLKRRPART ioctl --------

_BLKRRSET = 0x1262  # BLKRRPART — re-read partition table
_BLKPBSZ = 0x127B  # BLKPBSZ — get physical block size


def _open_device_ro(device: str) -> int | None:
    try:
        return os.open(device, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as e:
        log.error("Cannot open %s: %s", device, e)
        return None


def reread_partitions(device: str) -> bool:
    """Re-read partition table via BLKRRPART ioctl.

    Equivalent to partprobe (but non-interactive, faster).
    Returns True on success.
    """
    import fcntl

    fd = _open_device_ro(device)
    if fd is None:
        return False
    try:
        fcntl.ioctl(fd, _BLKRRSET)
        log.info("Partition table re-read on %s", device)
        return True
    except OSError as e:
        log.warning("BLKRRPART on %s failed: %s", device, e)
        return False
    finally:
        os.close(fd)


# -------- Loop device helpers (pure Python replacement for losetup) --------

_LOOP_CTL_GET_FREE = 0x4C82
_LOOP_SET_FD = 0x4C00
_LOOP_CLR_FD = 0x4C01

_loop_devices: dict[str, str] = {}  # mount_point -> loop_device


def _setup_loop(file_path: str) -> str | None:
    """Associate a regular file with a free loop device.

    Returns the loop device path (e.g. /dev/loop0) or None on failure.
    """
    import fcntl

    try:
        ctl_fd = os.open("/dev/loop-control", os.O_RDWR | os.O_CLOEXEC)
        try:
            idx = fcntl.ioctl(ctl_fd, _LOOP_CTL_GET_FREE)
        finally:
            os.close(ctl_fd)
    except OSError as e:
        log.error("Cannot get free loop device: %s", e)
        return None

    loop_dev = f"/dev/loop{idx}"
    try:
        file_fd = os.open(file_path, os.O_RDONLY | os.O_CLOEXEC)
        loop_fd = os.open(loop_dev, os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.ioctl(loop_fd, _LOOP_SET_FD, file_fd)
        except OSError as e:
            log.error("LOOP_SET_FD on %s failed: %s", loop_dev, e)
            os.close(loop_fd)
            os.close(file_fd)
            return None
        os.close(loop_fd)
        os.close(file_fd)
    except OSError as e:
        log.error("Cannot set up loop for %s: %s", file_path, e)
        return None

    log.info("Loop device %s <- %s", loop_dev, file_path)
    return loop_dev


def _detach_loop(loop_dev: str) -> None:
    """Detach a loop device."""
    import fcntl

    try:
        fd = os.open(loop_dev, os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.ioctl(fd, _LOOP_CLR_FD)
            log.info("Loop device %s detached", loop_dev)
        except OSError as e:
            log.warning("Cannot detach loop device %s: %s", loop_dev, e)
        finally:
            os.close(fd)
    except OSError:
        pass


def mount_iso(iso_path: str, mount_point: str) -> bool:
    """Mount an ISO file on *mount_point* using a loop device.

    Creates the mount point directory if needed.
    Returns True on success.
    """
    os.makedirs(mount_point, exist_ok=True)
    loop_dev = _setup_loop(iso_path)
    if loop_dev is None:
        return False

    # Windows installer ISOs normally store the real payload in UDF. Their
    # ISO9660 compatibility view may contain only a small readme.txt.
    for fstype in ("udf", "iso9660"):
        if mount(loop_dev, mount_point, fstype=fstype, flags=_MS_RDONLY):
            _loop_devices[mount_point] = loop_dev
            log.info("ISO %s mounted as %s on %s", iso_path, fstype, mount_point)
            return True

    log.error("mount(2) failed for loop device %s on %s as UDF or ISO9660", loop_dev, mount_point)
    _detach_loop(loop_dev)
    return False


def umount_iso(mount_point: str) -> bool:
    """Unmount an ISO mounted via mount_iso and detach its loop device."""
    ok = umount(mount_point)
    loop_dev = _loop_devices.pop(mount_point, None)
    if loop_dev:
        _detach_loop(loop_dev)
    return ok


# -------- GPT partition table writer --------

_GPT_SIGNATURE = b"EFI PART"
_GPT_REVISION = struct.pack("<I", 0x00010000)
_GPT_HEADER_SIZE = struct.pack("<I", 92)
_GPT_PARTITION_ENTRY_SIZE = 128
_GPT_PARTITIONS_DEFAULT_COUNT = 128  # standard GPT allows up to 128 entries

# Partition type GUIDs (binary, little-endian format)
_PART_TYPE_GUID = {
    "data": bytes.fromhex("a2a0d0ebe5b9334487c068b6b72699c7"),  # Microsoft Basic Data
    "efi": bytes.fromhex("28732ac11ff8d211ba4b00a0c93ec93b"),  # EFI System
    "bios": bytes.fromhex("4861682149646f6e744e656564454649"),  # BIOS Boot
}


def _crc32(data: bytes) -> int:
    import binascii

    return binascii.crc32(data) & 0xFFFFFFFF


def _gpt_header_crc(header: bytes, header_size: int = 92) -> int:
    """Return a GPT header CRC over HeaderSize bytes with the CRC field zeroed."""
    crc_data = bytearray(header[:header_size])
    crc_data[16:20] = b"\x00\x00\x00\x00"
    return _crc32(bytes(crc_data))


def _write_gpt(device: str, disk_guid: bytes, partitions: list[dict]) -> bool:
    """Write a GPT partition table to device.

    *partitions* is a list of dicts with keys:
      - role (str): 'efi', 'data', 'bios' — selects the type GUID
      - start_lba (int): first sector of the partition
      - size_lba (int): number of sectors (can be None for rest of disk)
      - name (str): UTF-16LE partition name (optional, defaults to role)
    """
    size_sectors = get_sysfs_device_size_sectors(device)
    if size_sectors is None or size_sectors < 34:
        log.error("Device %s too small for GPT", device)
        return False
    total_lba = size_sectors

    header_lba = 1
    partition_entries_lba = 2
    partition_entries_size = _GPT_PARTITIONS_DEFAULT_COUNT * _GPT_PARTITION_ENTRY_SIZE
    partition_entries_sectors = (partition_entries_size + 511) // 512
    first_usable = partition_entries_lba + partition_entries_sectors
    last_usable = total_lba - 1 - partition_entries_sectors - 1  # -1 for backup header
    backup_header_lba = total_lba - 1

    # Build partition entries
    entry_data = b""
    for i, part in enumerate(partitions):
        type_guid = _PART_TYPE_GUID.get(part.get("role", "data"))
        if type_guid is None:
            log.error("Unknown partition role: %s", part.get("role"))
            return False
        unique_guid = os.urandom(16)  # random UUID for each partition
        # Override with provided guid if given
        if "guid" in part:
            unique_guid = part["guid"]

        start = part["start_lba"]
        size = part.get("size_lba")
        if size is None:
            end = last_usable
        else:
            end = start + size - 1

        name_str = part.get("name", part["role"])
        name_utf16 = name_str.encode("utf-16-le").ljust(72, b"\x00")[:72]

        entry = (
            type_guid
            + unique_guid
            + struct.pack("<Q", start)
            + struct.pack("<Q", end)
            + struct.pack("<Q", 0)  # attributes
            + name_utf16
        )
        assert len(entry) == _GPT_PARTITION_ENTRY_SIZE
        entry_data += entry

    # Pad entry area
    entry_data = entry_data.ljust(partition_entries_size, b"\x00")
    partition_entries_crc = _crc32(entry_data)

    # Build GPT header
    header = (
        _GPT_SIGNATURE
        + _GPT_REVISION
        + _GPT_HEADER_SIZE
        + struct.pack("<I", 0)  # header CRC — placeholder
        + struct.pack("<I", 0)  # reserved
        + struct.pack("<Q", header_lba)
        + struct.pack("<Q", backup_header_lba)
        + struct.pack("<Q", first_usable)
        + struct.pack("<Q", last_usable)
        + disk_guid
        + struct.pack("<Q", partition_entries_lba)
        + struct.pack("<I", _GPT_PARTITIONS_DEFAULT_COUNT)
        + struct.pack("<I", _GPT_PARTITION_ENTRY_SIZE)
        + struct.pack("<I", partition_entries_crc)
        + b"\x00" * (512 - 92)  # pad to 512 bytes
    )
    assert len(header) == 512

    # Compute and set header CRC. GPT requires the CRC to cover only
    # HeaderSize bytes, not the full 512-byte sector.
    header_crc = _gpt_header_crc(header, 92)
    header = header[:16] + struct.pack("<I", header_crc) + header[20:]

    # Protective MBR
    mbr = b"\x00" * 446
    # Partition entry 1 (16 bytes): GPT protective
    mbr += struct.pack("<B", 0x00)  # boot indicator
    mbr += struct.pack("<BBB", 0x00, 0x02, 0x00)  # start CHS
    mbr += struct.pack("<B", 0xEE)  # partition type: GPT protective
    mbr += struct.pack("<BBB", 0xFF, 0xFF, 0xFF)  # end CHS (LBA-assist)
    mbr += struct.pack("<I", 1)  # start LBA
    mbr += struct.pack("<I", min(total_lba - 1, 0xFFFFFFFF))  # size LBA
    # Three empty partition entries
    mbr += b"\x00" * 48
    mbr += b"\x55\xaa"

    backup_entries_lba = backup_header_lba - partition_entries_sectors

    # Build backup GPT header
    backup_header = (
        _GPT_SIGNATURE
        + _GPT_REVISION
        + _GPT_HEADER_SIZE
        + struct.pack("<I", 0)  # CRC placeholder
        + struct.pack("<I", 0)
        + struct.pack("<Q", backup_header_lba)
        + struct.pack("<Q", header_lba)
        + struct.pack("<Q", first_usable)
        + struct.pack("<Q", last_usable)
        + disk_guid
        + struct.pack("<Q", backup_entries_lba)
        + struct.pack("<I", _GPT_PARTITIONS_DEFAULT_COUNT)
        + struct.pack("<I", _GPT_PARTITION_ENTRY_SIZE)
        + struct.pack("<I", partition_entries_crc)
        + b"\x00" * (512 - 92)
    )
    backup_crc = _gpt_header_crc(backup_header, 92)
    backup_header = backup_header[:16] + struct.pack("<I", backup_crc) + backup_header[20:]

    try:
        fd = os.open(device, os.O_WRONLY | os.O_CLOEXEC)
        try:
            # Write protective MBR (LBA 0)
            os.write(fd, mbr)
            # Write primary GPT header (LBA 1)
            os.lseek(fd, header_lba * 512, os.SEEK_SET)
            os.write(fd, header)
            # Write partition entries
            os.lseek(fd, partition_entries_lba * 512, os.SEEK_SET)
            os.write(fd, entry_data)
            # Write backup partition entries
            os.lseek(fd, backup_entries_lba * 512, os.SEEK_SET)
            os.write(fd, entry_data)
            # Write backup GPT header (last sector)
            os.lseek(fd, backup_header_lba * 512, os.SEEK_SET)
            os.write(fd, backup_header)
            os.fsync(fd)
        finally:
            os.close(fd)
        log.info("GPT written to %s (%d partitions)", device, len(partitions))
        return True
    except OSError as e:
        log.error("Failed to write GPT to %s: %s", device, e)
        return False


def write_gpt(device: str, partitions: list[dict]) -> bool:
    """Write a GPT partition table to *device* with the given partitions.

    *partitions* is a list of dicts:
      - role (str): 'efi', 'data', 'bios' — selects type GUID
      - start_lba (int): first sector
      - size_lba (int, optional): number of sectors (None = rest of disk)
      - name (str, optional): partition name, defaults to role
      - guid (bytes, optional): 16-byte unique GUID

    Returns True on success.
    """
    disk_guid = os.urandom(16)
    return _write_gpt(device, disk_guid, partitions)


def write_mbr_table(device: str) -> bool:
    """Write a single-partition MBR (msdos) partition table.

    Creates one partition starting at LBA 2048 (1 MiB alignment) using
    the rest of the disk with type 0x07 (NTFS/exFAT/data).
    """
    size_sectors = get_sysfs_device_size_sectors(device)
    if size_sectors is None or size_sectors < 34:
        log.error("Device %s too small for MBR", device)
        return False

    start_lba = 2048
    size = size_sectors - start_lba

    # Build MBR
    mbr = b"\x00" * 446
    # Partition entry 1
    mbr += struct.pack("<B", 0x00)  # boot indicator
    mbr += struct.pack("<BBB", 0x00, 0x02, 0x00)  # start CHS
    mbr += struct.pack("<B", 0x07)  # type: NTFS/exFAT/data
    mbr += struct.pack("<BBB", 0xFF, 0xFF, 0xFF)  # end CHS (LBA-assist)
    mbr += struct.pack("<I", start_lba)
    mbr += struct.pack("<I", min(size, 0xFFFFFFFF))
    # Three empty partition entries
    mbr += b"\x00" * 48
    mbr += b"\x55\xaa"

    try:
        with os.fdopen(os.open(device, os.O_WRONLY | os.O_CLOEXEC), "wb") as f:
            f.write(mbr)
        log.info("MBR written to %s", device)
        return True
    except OSError as e:
        log.error("Failed to write MBR to %s: %s", device, e)
        return False


def write_single_partition_table(device: str, scheme: str = "gpt") -> bool:
    """Write a partition table with one partition filling the disk.

    Args:
        device: Block device path.
        scheme: 'gpt' (default) or 'mbr'.

    Returns True on success, False on failure.
    """
    if scheme == "gpt":
        disk_guid = os.urandom(16)
        size = get_sysfs_device_size_sectors(device)
        if size is None:
            return False
        return _write_gpt(
            device,
            disk_guid,
            [
                {"role": "data", "start_lba": 2048, "name": "Primary"},
            ],
        )
    elif scheme == "mbr":
        return write_mbr_table(device)
    else:
        log.error("Unknown partition scheme: %s", scheme)
        return False


def wipe_superblock(device: str, size_mb: int = 5) -> bool:
    """Zero out the first and last *size_mb* MB of a device.

    This removes filesystem signatures and partition tables without
    needing wipefs(8).  Equivalent to:
      dd if=/dev/zero of=device bs=1M count=size_mb conv=notrunc
    """
    try:
        total_size = get_device_size(device)
        size = size_mb * 1024 * 1024
        zeros = b"\x00" * 4096
        with os.fdopen(os.open(device, os.O_WRONLY | os.O_CLOEXEC), "wb", buffering=0) as f:
            # Zero first size_mb MB
            written = 0
            while written < size:
                f.write(zeros)
                written += len(zeros)
            # Zero last size_mb MB
            if total_size and total_size > size * 2:
                f.seek(-size, os.SEEK_END)
                written = 0
                while written < size:
                    f.write(zeros)
                    written += len(zeros)
        log.info("Wiped superblock on %s (%d MB)", device, size_mb)
        return True
    except OSError as e:
        log.error("Failed to wipe superblock on %s: %s", device, e)
        return False


def write_device_image(src_path: str, device: str, bs: int = 4194304, progress_cb=None, status_cb=None) -> int:
    """Write an image file to a block device using pure Python I/O.

    Uses buffered writes with fsync (equivalent to dd with conv=fsync).
    Avoids O_DIRECT alignment issues by relying on the kernel buffer cache,
    then flushing with fsync.

    Returns 0 on success, or a negative errno-style code on failure.
    """
    try:
        total = os.path.getsize(src_path)
    except OSError as e:
        log.error("Cannot stat source %s: %s", src_path, e)
        return -errno.EIO

    try:
        src_fd = os.open(src_path, os.O_RDONLY | os.O_CLOEXEC)
        dst_fd = os.open(device, os.O_WRONLY | os.O_CLOEXEC)
    except OSError as e:
        log.error("Cannot open source/destination: %s", e)
        return -errno.EIO

    try:
        written = 0
        last_pct = -1
        while True:
            chunk = os.read(src_fd, bs)
            if not chunk:
                break
            os.write(dst_fd, chunk)
            written += len(chunk)
            if total > 0 and progress_cb:
                pct = int(written * 100 / total)
                if pct != last_pct:
                    last_pct = pct
                    progress_cb(pct)
                    if status_cb:
                        status_cb(f"Writing: {written:,} / {total:,} bytes ({pct}%)")

        os.fsync(dst_fd)
        if progress_cb:
            progress_cb(100)
        if status_cb:
            status_cb(f"Write completed: {written:,} bytes written")
        return 0
    except OSError as e:
        log.error("I/O error during write: %s", e)
        return -errno.EIO
    finally:
        os.close(src_fd)
        os.close(dst_fd)
