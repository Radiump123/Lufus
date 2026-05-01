import sys
import os
from lufus.lufus_logging import get_logger, setup_logging
from lufus.drives.find_usb import find_usb
from lufus.utils import elevate_privileges
from lufus import state
from pathlib import Path
from platformdirs import user_config_dir

setup_logging()
log = get_logger(__name__)


def _load_initial_theme():
    # Load theme before elevation so it can be passed via env
    try:
        _theme_cfg = Path(user_config_dir("Lufus")) / "active_theme"
        if _theme_cfg.exists():
            state.theme = _theme_cfg.read_text(encoding="utf-8").strip()
    except Exception:
        pass


def launch_gui_with_usb_data() -> None:
    if os.geteuid() != 0:
        _load_initial_theme()
        elevate_privileges()
        # If elevation failed or was cancelled, we still try to run as user
        # or we might have exited. If we are here, we are either root or user.

    usb_devices = find_usb()
    log.info("Launching GUI with USB devices: %s", usb_devices)

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QTimer
    from lufus.gui.gui import LufusWindow

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
