import sys
import os
import shutil
from lufus.lufus_logging import get_logger, setup_logging
from lufus.drives.find_usb import find_usb

setup_logging()
log = get_logger(__name__)


def launch_gui_with_usb_data() -> None:
    # Require root for operations, relaunch if needed
    if os.geteuid() != 0:
        pkexec_path = shutil.which("pkexec")
        if pkexec_path:
            # Preserve environment for GUI
            gui_env = {
                "DISPLAY":          os.environ.get("DISPLAY"),
                "XAUTHORITY":       os.environ.get("XAUTHORITY") or os.path.expanduser("~/.Xauthority"),
                "WAYLAND_DISPLAY":  os.environ.get("WAYLAND_DISPLAY"),
                "XDG_RUNTIME_DIR":  os.environ.get("XDG_RUNTIME_DIR"),
                "PATH":             os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
                "PYTHONPATH":       os.environ.get("PYTHONPATH", ""),
            }
            env_args = ["env"]
            for key, value in gui_env.items():
                if value:
                    env_args.append(f"{key}={value}")

            executable = sys.executable
            # If running via briefcase dev, sys.argv might need adjustment, but sys.argv[:] is usually safe
            args = sys.argv[:]

            cmd = [pkexec_path] + env_args + [executable] + args
            log.info("Relaunching as root via pkexec...")
            try:
                os.execvp(pkexec_path, cmd)
            except Exception as e:
                log.error("Failed to relaunch as root via pkexec: %s", e)
        else:
            log.warning("pkexec not found, cannot elevate to root automatically.")

    usb_devices = find_usb()
    log.info("Launching GUI with USB devices: %s", usb_devices)

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QTimer
    from lufus.gui.gui import lufus as LufusWindow

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    autoflash_path = None
    if "--flash-now" in sys.argv:
        idx = sys.argv.index("--flash-now")
        if idx + 1 < len(sys.argv):
            autoflash_path = sys.argv[idx + 1]

    window = LufusWindow(usb_devices)
    if autoflash_path:
        window._autoflash_path = autoflash_path
        QTimer.singleShot(0, window._do_autoflash)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    launch_gui_with_usb_data()
