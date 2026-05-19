"""Windows installation customization functions.

These modify Windows installation media (boot.wim, autounattend.xml)
to bypass hardware requirements, skip privacy questions, and create
local accounts.
"""

import html
import re
import subprocess
import os
import shutil
import tempfile
from lufus.utils import get_mount_and_drive
from lufus import state
from lufus.lufus_logging import get_logger

log = get_logger(__name__)

# Windows username restrictions: no \/ [ ] : ; | = , + * ? < > " @
# Max 20 characters, cannot be all spaces or empty.
_WIN_USERNAME_RE = re.compile(r'^[^\\\/\[\]:;|=,+*?<>"@\x00-\x1f]{1,20}$')


def _validate_windows_username(name: str) -> str | None:
    """Return a stripped, validated Windows username or None if invalid."""
    name = name.strip()
    if not name:
        log.error("Windows username is empty after stripping.")
        return None
    if not _WIN_USERNAME_RE.match(name):
        log.error("Windows username %r contains forbidden characters or exceeds 20 chars.", name)
        return None
    return name


def _get_mount_and_drive():
    return get_mount_and_drive()


def _resolve_windows_mount(mount: str | None = None) -> str | None:
    if mount:
        return mount
    mount, _, _ = _get_mount_and_drive()
    return mount


def _boot_wim_path(mount: str) -> str:
    return os.path.join(mount, "sources", "boot.wim")


def _modify_boot_wim_registry(mount: str, hive: str, commands: list[str], label: str) -> bool:
    boot_wim = _boot_wim_path(mount)
    if not os.path.exists(boot_wim):
        log.error("%s: boot.wim not found at %s", label, boot_wim)
        return False

    cmd_string = "\n".join(commands) + "\n"
    temp_mount = tempfile.mkdtemp(prefix="lufus-winwim-")
    mounted = False
    try:
        subprocess.run(["wimmountrw", boot_wim, "2", temp_mount], check=True)
        mounted = True
        subprocess.run(
            ["chntpw", "e", os.path.join(temp_mount, "Windows", "System32", "config", hive)],
            input=cmd_string,
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(["wimunmount", temp_mount, "--commit"], check=True)
        mounted = False
        log.info("%s: boot.wim registry changes applied successfully.", label)
        return True
    except subprocess.CalledProcessError as e:
        log.error("%s: command failed: %s", label, e.stderr or e)
        return False
    finally:
        if mounted:
            subprocess.run(["wimunmount", temp_mount, "--discard"], check=False)
        shutil.rmtree(temp_mount, ignore_errors=True)


def win_hardware_bypass(mount: str | None = None) -> bool:
    mount = _resolve_windows_mount(mount)
    if not mount:
        log.error("win_hardware_bypass: no USB mount found")
        return False
    commands = [
        "cd Setup",
        "newkey LabConfig",
        "cd LabConfig",
        "addvalue BypassTPMCheck 4 1",
        "addvalue BypassSecureBootCheck 4 1",
        "addvalue BypassRAMCheck 4 1",
        "save",
        "exit",
    ]
    log.info("win_hardware_bypass: injecting registry keys into boot.wim at %s...", mount)
    return _modify_boot_wim_registry(mount, "SYSTEM", commands, "win_hardware_bypass")


def win_local_acc(mount: str | None = None) -> bool:
    mount = _resolve_windows_mount(mount)
    if not mount:
        log.error("win_local_acc: no USB mount found")
        return False
    commands = ["cd Microsoft\\Windows\\CurrentVersion\\OOBE", "addvalue BypassNRO 4 1", "save", "exit"]
    log.info("win_local_acc: bypassing online account requirement at %s...", mount)
    return _modify_boot_wim_registry(mount, "SOFTWARE", commands, "win_local_acc")


def win_skip_privacy_questions(mount: str | None = None) -> bool:
    mount = _resolve_windows_mount(mount)
    if not mount:
        log.error("win_skip_privacy_questions: no USB mount found")
        return False
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
    xml_path = os.path.join(mount, "autounattend.xml")
    log.info("win_skip_privacy_questions: writing autounattend.xml to %s...", xml_path)
    with open(xml_path, "w") as f:
        f.write(xml_content)
    log.info("win_skip_privacy_questions: autounattend.xml created to skip privacy screens.")
    return True


def win_local_acc_name(mount: str | None = None) -> bool:
    mount = _resolve_windows_mount(mount)
    if not mount:
        log.error("win_local_acc_name: no USB mount found")
        return False
    user_name = _validate_windows_username(state.win_local_acc)
    if user_name is None:
        log.error("win_local_acc_name: invalid username %r, aborting", state.win_local_acc)
        return False
    # html.escape converts < > & " ' so the value is safe to embed in XML.
    safe_name = html.escape(user_name, quote=True)
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
                            <DisplayName>{safe_name}</DisplayName>
                            <Group>Administrators</Group>
                            <Name>{safe_name}</Name>
                        </LocalAccount>
                    </LocalAccounts>
                </UserAccounts>
            </component>
        </settings>
    </unattend>"""
    xml_path = os.path.join(mount, "autounattend.xml")
    log.info("win_local_acc_name: writing autounattend.xml for local account %r to %s...", user_name, xml_path)
    with open(xml_path, "w") as f:
        f.write(xml_template)
    log.info(
        "win_local_acc_name: autounattend.xml created — privacy screens skipped, local account %r created.",
        user_name,
    )
    return True


def apply_windows_tweaks(mount: str) -> bool:
    """Apply selected Windows tweaks to an already-mounted install media root."""
    ok = True
    if getattr(state, "win_hardware_bypass", 0) == 1:
        ok = win_hardware_bypass(mount) and ok
    if getattr(state, "win_microsoft_acc", 0) == 1:
        if getattr(state, "win_local_acc_chk", 0) == 1:
            ok = win_local_acc_name(mount) and ok
        else:
            ok = win_local_acc(mount) and ok
    if getattr(state, "win_privacy", 0) == 1:
        ok = win_skip_privacy_questions(mount) and ok
    return ok
