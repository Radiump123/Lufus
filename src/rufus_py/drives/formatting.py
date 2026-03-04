import subprocess
import sys
from pathlib import Path
from rufus_py.drives import states
from rufus_py.drives import find_usb as fu

def get_usb_info():
    mount_dict = fu.find_usb()
    if not mount_dict:
        return None, None
    try:
        mount = next(iter(mount_dict))
        drive = fu.find_DN()
        return mount, drive
    except Exception:
        return None, None

def pkexecNotFound():
    print("Error: The command pkexec or labeling software was not found on your system.")

def FormatFail():
    print("Error: Formatting failed. Was the password correct? Is the drive unmounted?")

def unexpected():
    print("An unexpected error occurred")

def no_usb_device():
    print("Error: No USB device found")

# UNMOUNT FUNCTION
def unmount():
    mount, drive = get_usb_info()
    if not drive:
        no_usb_device()
        return
    try:
        subprocess.run(["pkexec", "umount", drive], check=True)
    except FileNotFoundError:
        pkexecNotFound()
    except subprocess.CalledProcessError:
        FormatFail()
    except Exception as e:
        print(f"(UMNTFUNC) Unexpected error: {e}")

# MOUNT FUNCTION
def remount():
    mount, drive = get_usb_info()
    if not drive or not mount:
        no_usb_device()
        return
    try:
        subprocess.run(["pkexec", "mount", drive, mount], check=True)
    except FileNotFoundError:
        pkexecNotFound()
    except subprocess.CalledProcessError:
        FormatFail()
    except Exception as e:
        print(f"(MNTFUNC) Unexpected error: {e}")

### DISK FORMATTING ###
def volumecustomlabel():
    mount, drive = get_usb_info()
    if not drive:
        no_usb_device()
        return
    newlabel = states.new_label
    fs_type = states.currentFS

    try:
        if fs_type == 0:  # NTFS
            subprocess.run(["pkexec", "ntfslabel", drive, newlabel], check=True)
        elif fs_type in (1, 2):  # FAT32 or exFAT
            subprocess.run(["pkexec", "fatlabel", drive, newlabel], check=True)
        elif fs_type == 3:  # ext4
            subprocess.run(["pkexec", "e2label", drive, newlabel], check=True)
    except FileNotFoundError:
        pkexecNotFound()
    except subprocess.CalledProcessError:
        FormatFail()
    except Exception as e:
        print(f"Labeling error: {e}")
        unexpected()

def cluster():
    if states.cluster_size == 0:
        cluster1 = 4096
    elif states.cluster_size == 1:
        cluster1 = 8192
    else:
        print("Warning: Using default cluster size")
        cluster1 = 4096
    return cluster1, 512, cluster1 // 512  # Standard sector size is 512

def quickformat():
    pass

def createextended():
    pass

def checkdevicebadblock():
    pass

def dskformat():
    mount, drive = get_usb_info()
    if not drive:
        no_usb_device()
        return
        
    cluster1, _, sector = cluster()
    fs_type = states.currentFS

    try:
        if fs_type == 0:  # NTFS
            cmd = ["pkexec", "mkfs.ntfs", "-c", str(cluster1), "-Q", drive]
        elif fs_type == 1:  # FAT32
            cmd = ["pkexec", "mkfs.vfat", "-s", str(sector), "-F", "32", drive]
        elif fs_type == 2:  # exFAT
            cmd = ["pkexec", "mkfs.exfat", "-b", str(cluster1), drive]
        elif fs_type == 3:  # ext4
            cmd = ["pkexec", "mkfs.ext4", "-b", str(cluster1), drive]
        else:
            unexpected()
            return
            
        subprocess.run(cmd, check=True)
        print(f"Successfully formatted to {['NTFS','FAT32','exFAT','ext4'][fs_type]}!")
        
    except FileNotFoundError:
        pkexecNotFound()
    except subprocess.CalledProcessError:
        FormatFail()
    except Exception as e:
        print(f"Format error: {e}")
        unexpected()
