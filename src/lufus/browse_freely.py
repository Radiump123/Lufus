import os
import subprocess
import webbrowser
from lufus.lufus_logging import get_logger

log = get_logger("browser")

def open_url_non_root(url: str) -> None:
    """
    Opens a URL in the browser as the regular user if running as root.
    Firefox and other browsers often refuse to run as root in a regular session.
    """
    # Lufus uses pkexec which sets PKEXEC_UID. sudo sets SUDO_USER.
    pkexec_uid = os.environ.get("PKEXEC_UID")
    sudo_user = os.environ.get("SUDO_USER")
    
    if os.geteuid() == 0:
        target_user = None
        target_uid = None
        
        if pkexec_uid:
            target_uid = pkexec_uid
            try:
                import pwd
                target_user = pwd.getpwuid(int(pkexec_uid)).pw_name
            except Exception:
                pass
        elif sudo_user and sudo_user != "root":
            target_user = sudo_user

        if target_user:
            try:
                log.info(f"Attempting to open URL as user {target_user} (UID {target_uid}): {url}")
                
                # Method 1: Use runuser which is specifically designed for this
                # We need to preserve the environment for the GUI session
                env = {
                    "DISPLAY": os.environ.get("DISPLAY", ":0"),
                    "XAUTHORITY": os.environ.get("XAUTHORITY", ""),
                    "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{target_uid}" if target_uid else ""),
                    "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", ""),
                    "PATH": "/usr/local/bin:/usr/bin:/bin"
                }
                
                # Filter out empty values
                env = {k: v for k, v in env.items() if v}

                # Try runuser -u <user> -- xdg-open <url>
                subprocess.Popen(
                    ["runuser", "-u", target_user, "--", "xdg-open", url],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
                return
            except Exception as e:
                log.warning(f"Failed to open URL via runuser: {e}")

    # Fallback to standard webbrowser module
    log.info(f"Opening URL via fallback: {url}")
    webbrowser.open(url)
