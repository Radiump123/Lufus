import os
import re
import sys
import signal
import fcntl
import logging
import subprocess
from typing import List

log = logging.getLogger("lufus")


class ProcessManager:
    """Track and manage external subprocesses for reliable termination."""

    _procs: List["subprocess.Popen"] = []

    @classmethod
    def register(cls, proc: "subprocess.Popen"):
        cls._procs.append(proc)

    @classmethod
    def unregister(cls, proc: "subprocess.Popen"):
        if proc in cls._procs:
            cls._procs.remove(proc)

    @classmethod
    def kill_all(cls):
        """Terminate all registered subprocesses."""
        import signal

        if not cls._procs:
            return

        log.warning("ProcessManager: terminating %d subprocesses...", len(cls._procs))
        for proc in cls._procs:
            try:
                if proc.poll() is None:  # still running
                    # Try SIGTERM first
                    os.kill(proc.pid, signal.SIGTERM)
            except OSError:
                pass

        # Wait a bit for graceful exit
        import time

        time.sleep(0.5)

        for proc in cls._procs[:]:
            try:
                if proc.poll() is None:
                    log.warning("ProcessManager: forcing SIGKILL on PID %d", proc.pid)
                    os.kill(proc.pid, signal.SIGKILL)
                cls.unregister(proc)
            except OSError:
                cls.unregister(proc)


class InstanceLock:
    """Ensure only one instance of Lufus is running."""

    def __init__(self):
        self.lock_file = "/run/lufus/lufus.lock"
        self.fd = None

    def acquire(self) -> bool:
        try:
            os.makedirs(os.path.dirname(self.lock_file), mode=0o700, exist_ok=True)
            self.fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Write PID to lock file
            os.ftruncate(self.fd, 0)
            os.write(self.fd, str(os.getpid()).encode())
            return True
        except (OSError, IOError):
            if self.fd:
                os.close(self.fd)
                self.fd = None
            return False

    def release(self):
        if self.fd:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
                os.unlink(self.lock_file)
            except Exception:
                pass
            self.fd = None


def elevate_privileges() -> None:
    """Relaunch the application with root privileges using pkexec."""
    import sys
    import subprocess
    from lufus import state

    # LUFUS_THEME is used to pass the current theme to the root process
    # so user-added themes in ~/.config/Lufus/themes are still respected
    # if the app was able to find them before elevation.
    env = os.environ.copy()
    if state.theme:
        # Validate theme is a safe filename/path: no path separators, no shell metacharacters,
        # and must resolve inside the config directory to prevent path traversal.
        theme_val = str(state.theme)
        if re.match(r"^[A-Za-z0-9_\-. ]+$", os.path.basename(theme_val)) and ".." not in theme_val:
            env["LUFUS_THEME"] = theme_val
        else:
            import logging

            logging.getLogger("lufus").warning(
                "elevate_privileges: rejected suspicious LUFUS_THEME value %r",
                theme_val,
            )

    if state.language:
        env["LUFUS_LANGUAGE"] = str(state.language)

    # Preserve DISPLAY and XAUTHORITY for GUI apps under pkexec/sudo
    # Now also takes the detected XDG_DOWNLOAD_DIR of /src/lufus/user_paths.py to put it into LUFUS_DOWNLOAD_DIR
    env_vars = [
        "DISPLAY",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
        "WAYLAND_DISPLAY",
        "LUFUS_THEME",
        "LUFUS_LANGUAGE",
        "LUFUS_DOWNLOAD_DIR",
    ]

    # In dev mode, we need to pass the current PYTHONPATH so the root process
    # can find the lufus package. We only do this if we are running from a
    # source tree (detected by the presence of src/lufus/__init__.py).
    # We restrict it to the absolute path of 'src' to prevent injection.
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    src_dir = os.path.join(project_root, "src")
    if os.path.exists(os.path.join(src_dir, "lufus", "__init__.py")):
        env["PYTHONPATH"] = src_dir
        env_vars.append("PYTHONPATH")

    cmd = ["pkexec", "env"]
    for var in env_vars:
        val = os.environ.get(var) or env.get(var)
        if val:
            cmd.append(f"{var}={val}")

    cmd += [sys.executable] + sys.argv
    try:
        subprocess.run(cmd, check=True)
        sys.exit(0)
    except subprocess.CalledProcessError:
        # User likely cancelled or pkexec failed/isn't installed
        pass
    except Exception as e:
        print(f"Elevation failed: {e}")


def require_root() -> bool:
    """Check if running as root. Returns True if root, False otherwise (with log warning)."""
    if os.geteuid() == 0:
        return True
    import logging

    logging.getLogger("lufus").error("This operation requires root privileges (euid=%d).", os.geteuid())
    return False


def strip_partition_suffix(device: str) -> str:
    """Strip a partition number suffix to get the raw block device.

    Handles NVMe (/dev/nvme0n1p1 -> /dev/nvme0n1), MMC
    (/dev/mmcblk0p1 -> /dev/mmcblk0), and standard SCSI/SATA/USB
    (/dev/sdb1 -> /dev/sdb). Returns the input unchanged if no
    partition suffix is found.
    """
    m = re.match(r"^(/dev/nvme\d+n\d+)p\d+$", device)
    if m:
        return m.group(1)
    m = re.match(r"^(/dev/mmcblk\d+)p\d+$", device)
    if m:
        return m.group(1)
    m = re.match(r"^(/dev/sd[a-z]+)\d+$", device)
    if m:
        return m.group(1)
    return device


def get_mount_and_drive() -> tuple[str | None, str | None, dict]:
    """Resolve the current USB mount path, device node, and mount dict."""
    from lufus import state
    from lufus.drives.find_usb import find_usb, find_device_node

    drive = state.device_node
    mount_dict = find_usb()
    mount = next(iter(mount_dict)) if mount_dict else None
    if not drive:
        drive = find_device_node()
    return mount, drive, mount_dict
