# due to some issues it's only working with linux don't add without proper changing
import hashlib
import re
import subprocess
import sys
import os
import shutil
import tempfile
import time
import urllib.request
import urllib.error
import glob
from lufus.block_ops import (
    mount as block_mount,
    umount as block_umount,
    write_gpt,
    wipe_superblock,
    reread_partitions,
)

"""
   This script installs grub in a way that lets users to copy distro iso to the usb device and
   boot of any copied iso's in the usb.
"""

WIMBOOT_URL = "https://github.com/ipxe/wimboot/releases/latest/download/wimboot"
WIMBOOT_TIMEOUT = 60

# Pin the expected SHA-256 of the wimboot binary.
# UPDATE THIS HASH whenever you update the pinned wimboot version.
# Obtain it with: sha256sum wimboot
# An empty string disables verification and logs a security warning (dev only).
_WIMBOOT_SHA256 = ""

# Only permit canonical block-device paths to prevent sfdisk script injection.
_DEVICE_RE = re.compile(r"^/dev/[a-z][a-z0-9]*$")


def _verify_sha256(path: str, expected: str) -> bool:
    """Return True if the SHA-256 of *path* matches *expected*."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().lower() == expected.strip().lower()


def download_wimboot(dest_path: str) -> bool:
    """Downloads wimboot, a bootloader necessary to boot into Windows.

    Args:
        dest_path: Download destination path.

    Returns:
        True on success, False on failure.
    """
    print("--- Downloading wimboot ---")
    try:
        req = urllib.request.urlopen(WIMBOOT_URL, timeout=WIMBOOT_TIMEOUT)
        with open(dest_path, "wb") as fh:
            fh.write(req.read())

        if _WIMBOOT_SHA256:
            if not _verify_sha256(dest_path, _WIMBOOT_SHA256):
                os.unlink(dest_path)
                print(
                    "ERROR: wimboot SHA-256 verification failed — file deleted. "
                    "Check the download source or update _WIMBOOT_SHA256."
                )
                return False
        else:
            print(
                "WARNING: wimboot downloaded without hash verification. "
                "Set _WIMBOOT_SHA256 before shipping to production."
            )

        print("wimboot downloaded successfully.")
        return True
    except urllib.error.URLError as e:
        print(f"WARNING: Could not download wimboot (network error): {e}")
        print("Windows ISO booting will not work.")
        return False
    except Exception as e:
        print(f"WARNING: Could not download wimboot: {e}")
        print("Windows ISO booting will not work.")
        return False


def _is_usb_device(device: str) -> bool:
    """Return True if *device* is on the USB bus, as reported by sysfs.

    This correctly handles USB NVMe SSDs and USB SD-card readers in addition
    to standard USB mass-storage drives, without blocking them based solely
    on their device-node prefix.
    """
    dev_name = os.path.basename(device)
    sys_block = f"/sys/class/block/{dev_name}"
    try:
        real_path = os.path.realpath(sys_block)
        return "/usb" in real_path
    except OSError:
        return False


def install_grub(target_device: str) -> bool:
    """Prepares the USB drive with a hybrid GRUB bootloader for multi-ISO booting.

    This function performs partitioning via sfdisk, formats partitions to
    FAT32 and exFAT, and installs GRUB to both the MBR and EFI partitions.

    Args:
        target_device: The system path to the disk (e.g., /dev/sdX).

    Returns:
        bool: True if the installation succeeded, False otherwise.

    Raises:
        subprocess.CalledProcessError: If a system command fails.
    """

    # Root check
    if os.geteuid() != 0:
        print("ERROR: This script must be run with sudo.")
        return False

    # Validate device path before it is interpolated into the sfdisk script.
    # This prevents newline injection and ensures we operate on a real block device.
    if not _DEVICE_RE.match(target_device):
        print(f"ERROR: Invalid device path: {target_device!r}")
        return False

    # Safety: only allow USB devices. This properly handles USB NVMe SSDs
    # (which were previously blocked by an overly broad nvme/mmcblk check).
    if not _is_usb_device(target_device):
        print(f"Aborting: {target_device} does not appear to be a USB device.")
        return False

    # Cleanup to avoid "Device Busy"
    print(f"--- Cleaning up {target_device} ---")
    for partition in glob.glob(f"{target_device}*"):
        block_umount(partition)

    # Partition separator: NVMe and MMC block devices use 'p' between the
    # disk name and the partition number (e.g. /dev/nvme0n1p1, /dev/mmcblk0p1).
    # Standard SCSI/SATA/USB drives use no separator (e.g. /dev/sdb1).
    sep = "p" if re.search(r"(nvme\d+n\d+|mmcblk\d+)$", target_device) else ""

    # Use unique temp dirs instead of hardcoded /tmp paths to avoid stale-mount collisions.
    efi_mount = tempfile.mkdtemp(prefix="lufus_efi_")
    data_mount = tempfile.mkdtemp(prefix="lufus_data_")
    efi_mounted = False
    data_mounted = False

    try:
        print(f"--- Partitioning {target_device} ---")
        wipe_superblock(target_device, size_mb=1)
        partitions_spec = [
            {"role": "bios", "start_lba": 2048, "size_lba": 2048, "name": "BIOS Boot"},
            {"role": "efi", "start_lba": 4096, "size_lba": 204800, "name": "EFI System"},
            {"role": "data", "start_lba": 208896, "name": "OS Data"},
        ]
        from lufus.block_ops import write_gpt

        disk_guid = os.urandom(16)
        if not write_gpt(target_device, partitions_spec):
            raise RuntimeError(f"Failed to write GPT to {target_device}")

        reread_partitions(target_device)
        os.sync()
        time.sleep(1)

        # Wait for device nodes to be created
        efi_part = f"{target_device}{sep}2"
        data_part = f"{target_device}{sep}3"
        for _ in range(10):
            if os.path.exists(data_part):
                break
            time.sleep(1)
        else:
            print(f"Error: {data_part} did not appear. Aborting.")
            return False

        # Formatting
        print(f"--- Formatting {efi_part} and {data_part} ---")
        subprocess.run(["mkfs.exfat", "-L", "OS_PART", data_part], check=True)

        # GRUB Installation
        block_mount(efi_part, efi_mount, fstype="vfat")
        efi_mounted = True

        print("--- Installing GRUB (Legacy + UEFI) ---")
        subprocess.run(
            ["grub-install", "--target=i386-pc", f"--boot-directory={efi_mount}/boot", target_device], check=True
        )
        subprocess.run(
            [
                "grub-install",
                "--target=x86_64-efi",
                f"--efi-directory={efi_mount}",
                f"--boot-directory={efi_mount}/boot",
                "--removable",
            ],
            check=True,
        )

        # Copy grub.cfg
        script_dir = os.path.dirname(os.path.abspath(__file__))
        cfg_path = os.path.join(script_dir, "grub.cfg")
        if not os.path.exists(cfg_path):
            print("ERROR: grub.cfg not found next to the script.")
            return False
        shutil.copy(cfg_path, f"{efi_mount}/boot/grub/grub.cfg")

        # Download wimboot
        block_mount(data_part, data_mount, fstype="exfat")
        data_mounted = True
        download_wimboot(f"{data_mount}/wimboot")

        print("\nSUCCESS: USB is ready. Copy .iso files to 'OS_PART'.")
        return True

    except Exception as e:
        print(f"\nCommand failed: {e}")
        return False
    finally:
        if efi_mounted:
            block_umount(efi_mount)
        if data_mounted:
            block_umount(data_mount)
        for d in (efi_mount, data_mount):
            try:
                os.rmdir(d)
            except OSError:
                pass


# this part is for testing the script
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: sudo python3 script.py /dev/sdX")
    else:
        if install_grub(sys.argv[1]):
            sys.exit(0)
        else:
            sys.exit(1)
