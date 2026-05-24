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
    # Check environment variable first (passed during elevation)
    env_theme = os.environ.get("LUFUS_THEME")
    if env_theme:
        state.theme = env_theme
        return

    # Load theme before elevation so it can be passed via env
    if getattr(state, "theme", "") and state.theme != "default":
        return
    try:
        _theme_cfg = Path(user_config_dir("Lufus")) / "active_theme"
        if _theme_cfg.exists():
            state.theme = _theme_cfg.read_text(encoding="utf-8").strip()
    except Exception:
        pass


def _load_initial_language():
    # If already set via environment (e.g. during elevation), prioritize it
    env_lang = os.environ.get("LUFUS_LANGUAGE")
    if env_lang:
        state.language = env_lang
        return

    # Load persisted language preference; fall back to system locale detection
    try:
        _lang_cfg = Path(user_config_dir("Lufus")) / "active_language"
        if _lang_cfg.exists():
            state.language = _lang_cfg.read_text(encoding="utf-8").strip()
        else:
            from lufus.gui.i18n import detect_system_language

            state.language = detect_system_language()
    except Exception:
        state.language = "English"


def _show_lock_error(lang: str) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox
    from lufus.gui.i18n import load_translations

    app = QApplication.instance() or QApplication(sys.argv)
    t = load_translations(lang)
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Icon.Critical)
    msg.setWindowTitle(t.get("msgbox_already_running_title", "Already Running"))
    msg.setText(t.get("msgbox_already_running_body", "Another instance of Lufus is already running."))
    msg.setStandardButtons(QMessageBox.StandardButton.Ok)
    msg.exec()


def _show_root_warning(lang: str) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox
    from lufus.gui.i18n import load_translations
    import sys

    app = QApplication.instance() or QApplication(sys.argv)
    t = load_translations(lang)
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Icon.Warning)
    msg.setWindowTitle(t.get("msgbox_root_required_title", "Root Privileges Required"))
    msg.setText(t.get("msgbox_root_required_body", "This application must run as root."))

    info_text = t.get(
        "msgbox_root_required_info",
        "To run Lufus as root, you need:\n"
        "• pkexec (from polkit package)\n"
        "• polkit (policy kit) installed on your system\n\n"
        "Please install these packages via your distribution's package manager.",
    )
    msg.setInformativeText(info_text)
    msg.setStandardButtons(QMessageBox.StandardButton.Ok)
    msg.exec()


def launch_gui_with_usb_data() -> None:
    # 1. Early initialization
    _load_initial_language()

    # 2. Instance Lock check (before any popups or elevation)
    from lufus.utils import InstanceLock

    lock = InstanceLock()
    if not lock.acquire():
        from PySide6.QtWidgets import QApplication

        app = QApplication(sys.argv)
        _show_lock_error(state.language)
        sys.exit(0)

    elevation_attempted = False
    if os.geteuid() != 0:
        # Capture user context (XDG_DOWNLOAD_DIR path) before pkexec elevation
        from lufus.user_paths import get_best_starting_dir, ENV_DOWNLOAD_DIR

        try:
            detected_path = get_best_starting_dir()
            os.environ[ENV_DOWNLOAD_DIR] = detected_path
            log.info("Captured user starting directory: %s", detected_path)
        except Exception as e:
            log.warning("Could not capture user starting directory: %s", e)

        _load_initial_theme()
        elevation_attempted = True
        elevate_privileges(lock=lock)

    if elevation_attempted and os.geteuid() != 0:
        from PySide6.QtWidgets import QApplication

        app = QApplication(sys.argv)
        _show_root_warning(state.language)
        lock.release()
        sys.exit(1)

    # If we are root (either from start or after elevation), load/restore settings
    if os.geteuid() == 0:
        _load_initial_theme()
        # _load_initial_language() already called

    usb_devices = find_usb()
    log.info("Launching GUI with USB devices: %s", usb_devices)

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from lufus.gui.gui import LufusWindow

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")

    autoflash_path = None
    if "--flash-now" in sys.argv:
        idx = sys.argv.index("--flash-now")
        if idx + 1 < len(sys.argv):
            autoflash_path = sys.argv[idx + 1]

    window = LufusWindow(usb_devices)
    window.lock = lock  # Pass the lock to the window so it can be released on close
    if autoflash_path:
        window._autoflash_path = autoflash_path
        QTimer.singleShot(0, window._do_autoflash)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    launch_gui_with_usb_data()
