import re
import shlex
import subprocess
import sys
import os
from pathlib import Path
from lufus.drives import states
from lufus.drives import find_usb as fu


def _get_raw_device(drive: str) -> str:
    """Return the raw disk device for a partition node.

    Handles standard SCSI/SATA names (e.g. /dev/sdb1 → /dev/sdb),
    NVMe names (e.g. /dev/nvme0n1p1 → /dev/nvme0n1), and
    MMC/eMMC names (e.g. /dev/mmcblk0p1 → /dev/mmcblk0).
    Falls back to the input unchanged if no pattern matches.
    """
    # NVMe: /dev/nvmeXnYpZ  → /dev/nvmeXnY
    m = re.match(r"^(/dev/nvme\d+n\d+)p\d+$", drive)
    if m:
        return m.group(1)
    # MMC/eMMC: /dev/mmcblkXpY → /dev/mmcblkX
    m = re.match(r"^(/dev/mmcblk\d+)p\d+$", drive)
    if m:
        return m.group(1)
    # Standard SCSI/SATA/USB: /dev/sdXN → /dev/sdX
    m = re.match(r"^(/dev/[a-z]+)\d+$", drive)
    if m:
        return m.group(1)
    return drive


#######


def _get_mount_and_drive():
    """Resolve mount point and drive node from current state or live detection."""
    drive = states.DN
    mount_dict = fu.find_usb()
    mount = next(iter(mount_dict)) if mount_dict else None
    if not drive:
        drive = fu.find_DN()
    return mount, drive, mount_dict


def pkexecNotFound():
    print(
        "Error: The command pkexec or labeling software was not found on your system."
    )


def FormatFail():
    print("Error: Formatting failed. Was the password correct? Is the drive unmounted?")


def UnmountFail():
    print(
        "Error: Unmounting failed. Perhaps either the drive was already unmounted or is in use."
    )


def unexpected():
    print("An unexpected error occurred")


# UNMOUNT FUNCTION
def unmount(drive: str = None):
    if not drive:
        _, drive, _ = _get_mount_and_drive()
    if not drive:
        print("Error: No drive node found. Cannot unmount.")
        return
    try:
        subprocess.run(["umount", drive], check=True)
    except subprocess.CalledProcessError:
        UnmountFail()
    except Exception as e:
        print(f"(UMNTFUNC) DEBUG: Unexpected error type: {type(e).__name__}")
        print(f"DEBUG: Error message: {e}")
        unexpected()


# MOUNT FUNCTION
def remount():
    mount, drive, _ = _get_mount_and_drive()
    if not drive or not mount:
        print("Error: No drive node or mount point found. Cannot remount.")
        return
    try:
        subprocess.run(["mount", drive, mount], check=True)
    except subprocess.CalledProcessError:
        FormatFail()
    except Exception as e:
        print(f"(MNTFUNC) DEBUG: Unexpected error type: {type(e).__name__}")
        print(f"DEBUG: Error message: {e}")
        unexpected()


### DISK FORMATTING ###
def volumecustomlabel():
    newlabel = states.new_label
    # Sanitize label: allow only alphanumeric, spaces, hyphens, and underscores
    import re
    newlabel = re.sub(r'[^a-zA-Z0-9 \-_]', '', newlabel).strip()
    if not newlabel:
        newlabel = "USB_DRIVE"
        
    _, drive, _ = _get_mount_and_drive()
    if not drive:
        print("Error: No drive node found. Cannot relabel.")
        return

    # Sanitize label: strip characters that could be misinterpreted.
    # Since commands are passed as lists (shell=False), shell injection is not
    # possible, but we still quote each argument defensively.
    safe_drive = shlex.quote(drive)
    safe_label = shlex.quote(newlabel)

    # 0 -> NTFS, 1 -> FAT32, 2 -> exFAT, 3 -> ext4
    fs_type = states.currentFS
    cmd_map = {
        0: ["ntfslabel", drive, newlabel],
        1: ["fatlabel", drive, newlabel],
        2: ["fatlabel", drive, newlabel],
        3: ["e2label", drive, newlabel],
    }
    cmd = cmd_map.get(fs_type)
    if cmd is None:
        unexpected()
        return
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        pkexecNotFound()
    except subprocess.CalledProcessError:
        FormatFail()
    except Exception as e:
        print(f"(LABEL) DEBUG: Unexpected error type: {type(e).__name__}")
        print(f"DEBUG: Error message: {e}")
        unexpected()


def cluster():
    """Return (cluster_bytes, sector_bytes, cluster_in_sectors) tuple.

    Falls back to safe defaults when the drive node is unavailable.
    Never crashes — always returns a valid 3-tuple.
    """
    _, drive, mount_dict = _get_mount_and_drive()

    if not mount_dict and not drive:
        print("Error: No USB mount found. Is the drive plugged in and mounted?")
        return 4096, 512, 8

    # Map states.cluster_size index to block size in bytes
    cluster_size_map = {0: 4096, 1: 8192}
    cluster1 = cluster_size_map.get(states.cluster_size, 4096)

    # Logical sector size — 512 bytes is the universal safe default
    cluster2 = 512

    sector = cluster1 // cluster2
    return cluster1, cluster2, sector


def quickformat():
    # detect quick format option ticked or not and put it in a variable
    # the if logic will be implemented later
    pass


def createextended():
    # detect create extended label and icon files check box and put it in a variable
    pass


def checkdevicebadblock():
    """Check the device for bad blocks using badblocks.
    Requires the drive to be unmounted.  The number of passes is determined by
    states.check_bad (0 = 1 pass read-only, 1 = 2 passes read/write).
    """
    _, drive, _ = _get_mount_and_drive()
    if not drive:
        print("Error: No drive node found. Cannot check for bad blocks.")
        return False

    passes = 2 if states.check_bad else 1

    # Probe the device's logical sector size so badblocks uses the real
    # device geometry. Fall back to 4096 bytes if detection fails.
    logical_block_size = 4096
    try:
        probe = subprocess.run(
            ["blockdev", "--getss", drive],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            probed = probe.stdout.strip()
            if probed.isdigit():
                logical_block_size = int(probed)
            else:
                print(
                    f"Warning: Unexpected blockdev output for {drive!r}: {probed!r}. Using default."
                )
        else:
            print(
                f"Warning: blockdev failed for {drive} (exit {probe.returncode}). Using default block size."
            )
    except Exception as exc:
        print(
            f"Warning: Could not probe sector size for {drive}: {exc}. Using default block size."
        )

    # -s = show progress, -v = verbose output
    # -n = non-destructive read-write test (safe default)
    args = ["badblocks", "-sv", "-b", str(logical_block_size)]
    if passes > 1:
        args.append("-n")  # non-destructive read-write
    args.append(drive)

    print(
        f"Checking {drive} for bad blocks ({passes} pass(es), block size {logical_block_size})..."
    )
    try:
        result = subprocess.run(args, capture_output=True, text=True)
        output = result.stdout + result.stderr
        if result.returncode != 0:
            print(f"badblocks exited with code {result.returncode}:\n{output}")
            return False
        # badblocks reports bad block numbers one per line in stderr; a clean
        # run produces no such lines and exits 0. We rely on the exit code as
        # the authoritative result and only scan output for a user-friendly
        # summary — we do NOT parse numeric lines as a bad-block count because
        # the output format may include other numeric status lines.
        bad_lines = [line for line in output.splitlines() if line.strip().isdigit()]
        if bad_lines:
            print(f"WARNING: {len(bad_lines)} bad block(s) found on {drive}!")
            return False
        print(f"No bad blocks found on {drive}.")
        return True
    except FileNotFoundError:
        print("Error: 'badblocks' utility not found. Install e2fsprogs.")
        return False
    except Exception as e:
        print(f"(BADBLOCK) Unexpected error: {type(e).__name__}: {e}")
        unexpected()
        return False


def dskformat():
    cluster1, cluster2, sector = cluster()
    _, drive, _ = _get_mount_and_drive()
    if not drive:
        print("Error: No drive found. Cannot format.")
        return

    # Ensure we have the raw device for partitioning
    raw_device = _get_raw_device(drive)

    fs_type = states.currentFS
    clusters = cluster1
    sectors = sector

    # Build partition table based on scheme before formatting
    _apply_partition_scheme(raw_device)

    # Sync kernel partition table
    try:
        subprocess.run(["partprobe", raw_device], check=False)
        subprocess.run(["udevadm", "settle"], timeout=10, check=False)
    except Exception:
        pass

    # Determine the first partition node
    p_prefix = "p" if "nvme" in raw_device or "mmcblk" in raw_device else ""
    partition = f"{raw_device}{p_prefix}1"

    print(f"Formatting partition {partition}...")

    if fs_type == 0:
        try:
            subprocess.run(
                ["mkfs.ntfs", "-c", str(clusters), "-Q", partition], check=True
            )
            print(f"success format {partition} to ntfs!")
        except FileNotFoundError:
            pkexecNotFound()
        except subprocess.CalledProcessError:
            FormatFail()
        except Exception as e:
            print(f"(NTFS) DEBUG: {type(e).__name__}: {e}")
            unexpected()
    elif fs_type == 1:
        try:
            subprocess.run(
                ["mkfs.vfat", "-s", str(sectors), "-F", "32", partition], check=True
            )
            print(f"success format {partition} to fat32!")
        except FileNotFoundError:
            pkexecNotFound()
        except subprocess.CalledProcessError:
            FormatFail()
        except Exception as e:
            print(f"(FAT32) DEBUG: {type(e).__name__}: {e}")
            unexpected()
    elif fs_type == 2:
        try:
            subprocess.run(["mkfs.exfat", "-b", str(clusters), partition], check=True)
            print(f"success format {partition} to exFAT!")
        except FileNotFoundError:
            pkexecNotFound()
        except subprocess.CalledProcessError:
            FormatFail()
        except Exception as e:
            print(f"(exFAT) DEBUG: {type(e).__name__}: {e}")
            unexpected()
    elif fs_type == 3:
        try:
            subprocess.run(["mkfs.ext4", "-b", str(clusters), partition], check=True)
            print(f"success format {partition} to ext4!")
        except FileNotFoundError:
            pkexecNotFound()
        except subprocess.CalledProcessError:
            FormatFail()
        except Exception as e:
            print(f"(ext4) DEBUG: {type(e).__name__}: {e}")
            unexpected()
    else:
        unexpected()


def _apply_partition_scheme(drive: str):
    """Write a GPT or MBR partition table to the raw disk.

    states.partition_scheme: 0 = GPT, 1 = MBR
    states.target_system:    0 = UEFI (non CSM), 1 = BIOS (or UEFI-CSM)
    """
    raw_device = _get_raw_device(drive)
    scheme = states.partition_scheme  # 0 = GPT, 1 = MBR

    try:
        if scheme == 0:
            # GPT — used for UEFI targets
            subprocess.run(["parted", "-s", raw_device, "mklabel", "gpt"], check=True)
            subprocess.run(
                ["parted", "-s", raw_device, "mkpart", "primary", "1MiB", "100%"],
                check=True,
            )
        else:
            # MBR — used for BIOS/legacy targets
            subprocess.run(["parted", "-s", raw_device, "mklabel", "msdos"], check=True)
            subprocess.run(
                ["parted", "-s", raw_device, "mkpart", "primary", "1MiB", "100%"],
                check=True,
            )
        print(
            f"Partition scheme {'GPT' if scheme == 0 else 'MBR'} applied to {raw_device}"
        )
    except FileNotFoundError:
        print("Error: 'parted' not found. Install parted.")
    except subprocess.CalledProcessError as e:
        print(f"(PARTITION) Failed to apply partition scheme: {e}")
    except Exception as e:
        print(f"(PARTITION) Unexpected error: {type(e).__name__}: {e}")
        unexpected()


def drive_repair():
    _, drive, _ = _get_mount_and_drive()
    if not drive:
        print("Error: No drive node found. Cannot repair.")
        return
    raw_device = _get_raw_device(drive)
    cmd = ["sfdisk", raw_device]
    try:
        subprocess.run(["umount", drive], check=True)
        subprocess.run(cmd, input=b",,0c;\n", check=True)
        subprocess.run(["mkfs.vfat", "-F", "32", "-n", "REPAIRED", drive], check=True)
        print("SUCCESSFULLY REPAIRED DRIVE (FAT32)")
    except Exception:
        print("COULDN'T REPAIR DRIVE")

'''This file is for defining windows tweaks functions, this includes:
1. Hardware Requirements Bypass
2. Making Local Accounts
3. Disabling privacy questions'''
# bypass hardware requirements
def winhardwarebypass():
    mount, _, _ = _get_mount_and_drive()
    commands = [
        "cd Setup",
        "newkey LabConfig",
        "cd LabConfig",
        "addvalue BypassTPMCheck 4 1",
        "addvalue BypassSecureBootCheck 4 1",
        "addvalue BypassRAMCheck 4 1",
        "save",
        "exit"
    ]
    cmd_string = "\n".join(commands) + "\n"
    try:
        #creates temporary mount point for the windows iso
        subprocess.run(['mkdir', '/media/tempwinmnt'], check=True)
        #mounts the boot.wim file using wimlib
        subprocess.run(['wimmountrw', f'{mount}/sources/boot.wim', '2', '/media/tempwinmnt'], check=True)
        #using chntpw to edit the registry file SYSTEM and then also run the commands using stdin
        subprocess.run(['chntpw', 'e', '/media/tempwinmnt/Windows/System32/config/SYSTEM'],  input=cmd_string, text=True, capture_output=True, check=True)
        subprocess.run(['wimunmount', '/media/tempwinmnt', '--commit'], check=True)
        subprocess.run(['rm', '-rf', '/media/tempwinmnt'], check=True)
        print("Success: Registry keys injected.")
    except subprocess.CalledProcessError as e:
        print(f"Error occurred: {e.stderr}")

# ability to make local accounts
def winlocalacc():
    mount, _, _ = _get_mount_and_drive()
    commands = [
        "cd Microsoft\\Windows\\CurrentVersion\\OOBE\n"
        "addvalue BypassNRO 4 1\n"
        "save\n"
        "exit\n"
    ]
    try:
        #creates temporary mount point for the windows iso
        subprocess.run(['mkdir', '/media/tempwinmnt'], check=True)
        #mounts the boot.wim file using wimlib
        subprocess.run(['wimmountrw', f'{mount}/sources/boot.wim', '2', '/media/tempwinmnt'], check=True)
        #using chntpw to edit the registry file SOFTWARE and then also run the commands using stdin
        subprocess.run(['chntpw', 'e', '/media/tempwinmnt/Windows/System32/config/SOFTWARE'],  input=commands, text=True, capture_output=True, check=True)
        subprocess.run(['wimunmount', '/media/tempwinmnt', '--commit'], check=True)
        subprocess.run(['rm', '-rf', '/media/tempwinmnt'], check=True)
        print("Success: Online account bypassed.")
    except subprocess.CalledProcessError as e:
        print(f"Error occurred: {e.stderr}")
        
#skip privacy questions in windows
def winskipprivacyques():
    mount, _, _ = _get_mount_and_drive()
    xml_content = """<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
    <settings pass="oobeSystem">
        <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
            <OOBE>
                <HideEULAPage>true</HideEULAPage>
                <HidePrivacyExperience>true</HidePrivacyExperience>
                <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
                <ProtectYourPC>3</ProtectYourPC>
            </OOBE>
        </component>
    </settings>
</unattend>"""
    with open(os.path.join(mount, "autounattend.xml"), "w") as f:
        f.write(xml_content)
    print("Success: autounattend.xml created to skip privacy screens.")

#creating custom name local account (!) this also includes skip microsoft account (!)
def winlocalaccname():
    mount, _, _ = _get_mount_and_drive()
    user_name = states.winlocalacc
    ## username CANNOT HAVE \/[]:;|=,+*?<> or be empty!!! need to check for that!
    xml_template = f"""<?xml version="1.0" encoding="utf-8"?>
    <unattend xmlns="urn:schemas-microsoft-com:unattend">
        <settings pass="oobeSystem">
            <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
                <OOBE>
                    <HideEULAPage>true</HideEULAPage>
                    <HidePrivacyExperience>true</HidePrivacyExperience>
                    <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
                    <ProtectYourPC>3</ProtectYourPC>
                </OOBE>
                <UserAccounts>
                    <LocalAccounts>
                        <LocalAccount wcm:action="add" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
                            <Password><Value></Value><PlainText>true</PlainText></Password>
                            <Description>Primary Local Account</Description>
                            <DisplayName>{user_name}</DisplayName>
                            <Group>Administrators</Group>
                            <Name>{user_name}</Name>
                        </LocalAccount>
                    </LocalAccounts>
                </UserAccounts>
            </component>
        </settings>
    </unattend>"""
    with open(os.path.join(mount, "autounattend.xml"), "w") as f:
        f.write(xml_content)
    print("Success: autounattend.xml created to skip privacy screens and created a local account with name ", user_name)
