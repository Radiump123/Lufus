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
        home_dir = None

        try:
            import pwd

            if pkexec_uid:
                pw = pwd.getpwuid(int(pkexec_uid))
                target_user = pw.pw_name
                target_uid = pkexec_uid
                home_dir = pw.pw_dir
            elif sudo_user and sudo_user != "root":
                pw = pwd.getpwnam(sudo_user)
                target_user = sudo_user
                target_uid = str(pw.pw_uid)
                home_dir = pw.pw_dir
        except Exception as e:
            log.warning(f"Could not resolve target user info: {e}")

        if target_user:
            try:
                log.info(f"Attempting to open URL as user {target_user} (UID {target_uid}): {url}")

                # Method 1: Use runuser which is specifically designed for this
                # We need to preserve the environment for the GUI session
                env = {
                    "DISPLAY": os.environ.get("DISPLAY", ":0"),
                    "XAUTHORITY": os.environ.get("XAUTHORITY", ""),
                    "XDG_RUNTIME_DIR": os.environ.get(
                        "XDG_RUNTIME_DIR", f"/run/user/{target_uid}" if target_uid else ""
                    ),
                    "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", ""),
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    # Ensure HOME is set so GUI apps use the target user's profile and config
                    "HOME": home_dir or os.environ.get("HOME", ""),
                }

                # Filter out empty values
                env = {k: v for k, v in env.items() if v}

                # Try runuser -u <user> -- xdg-open <url>
                subprocess.Popen(
                    ["runuser", "-u", target_user, "--", "xdg-open", url],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return
            except Exception as e:
                log.warning(f"Failed to open URL via runuser: {e}")

    # Fallback to standard webbrowser module
    log.info(f"Opening URL via fallback: {url}")
    webbrowser.open(url)
