import os
import re
import shlex
from lufus.utils import strip_partition_suffix
from lufus.writing.check_file_sig import check_iso_signature
from lufus.writing.windows.detect import detect_iso_type, IsoType, is_windows_iso
from lufus.writing.windows.flash import flash_windows
from lufus.lufus_logging import get_logger
from lufus.writing.partition_scheme import PartitionScheme
from lufus.block_ops import write_device_image

log = get_logger(__name__)


# TODO: Decide if these are needed — currently never called in this module
# def pkexec_not_found():
#     log.error("The command pkexec or labeling software was not found on your system.")
#
# def format_fail():
#     log.error("Formatting failed. Was the password correct? Is the drive unmounted?")
#
# def log_unexpected_error():
#     log.error("An unexpected error occurred")


def flash_usb(
    device: str, iso_path: str, scheme: PartitionScheme = PartitionScheme.SIMPLE_FAT32, progress_cb=None, status_cb=None
) -> bool:
    def _status(msg: str) -> None:
        log.info(msg)
        if status_cb:
            status_cb(msg)

    _status(f"flash_usb called: iso={iso_path}, device={device}")

    # Strip any partition suffix first (e.g. /dev/nvme0n1p1 -> /dev/nvme0n1,
    # /dev/sdb1 -> /dev/sdb) so that validation operates on the whole-disk path.
    original_device = device
    device = strip_partition_suffix(device)
    if device != original_device:
        _status(f"Stripped partition suffix: {original_device} -> {device}")

    # Validate the (already-stripped) device path before any operation —
    # prevents accidental writes to system disks if a bad options file or
    # UI bug passes a wrong path.
    if not re.match(r"^/dev/(sd[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+)$", device):
        log.error("flash_usb: invalid device path %r — aborting", device)
        _status(f"Flash aborted: invalid device path {device!r}")
        return False

    try:
        iso_size = os.path.getsize(iso_path)
        _status(f"File size: {iso_size:,} bytes ({iso_size / (1024**3):.2f} GiB)")

        if iso_path.lower().endswith(".iso"):
            _status(f"Validating ISO9660 signature for: {iso_path}")
            if not check_iso_signature(iso_path):
                log.error("ISO signature check FAILED for %s, aborting flash", iso_path)
                _status(f"ISO signature check FAILED for {iso_path}, aborting flash")
                return False
            _status("ISO signature check passed")
        else:
            _status(f"Not an ISO file ({os.path.basename(iso_path)}), skipping ISO signature check")

        _status("Checking if image contains installation markers...")
        if is_windows_iso(iso_path):
            _status("Windows Installation media detected, routing to flash_windows (ISO mode)")
            return flash_windows(
                device,
                iso_path,
                scheme,
                progress_cb=progress_cb,
                status_cb=status_cb,
            )

        iso_type = detect_iso_type(iso_path)
        if iso_type == IsoType.LINUX:
            _status("Linux Installation media detected, will use dd for flashing")
        else:
            _status("Generic or unknown image, will use dd for flashing")

        _status(f"Writing {iso_size:,} bytes to {shlex.quote(device)}, this may take several minutes...")

        ret = write_device_image(
            iso_path,
            device,
            bs=4194304,
            progress_cb=progress_cb,
            status_cb=_status,
        )
        if ret == 0:
            _status(f"Write completed successfully: {iso_path} -> {device}")
            return True
        else:
            log.error("Write failed with error code %d", ret)
            _status(f"Flash failed with error code {ret}")
            return False

    except OSError as e:
        log.error("Flash failed with OSError: %s", e)
        _status(f"Flash failed with OSError: {e}")
        return False
