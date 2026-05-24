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
import shlex
import xml.etree.ElementTree as ET
from lufus.utils import get_mount_and_drive
from lufus import state
from lufus.lufus_logging import get_logger

log = get_logger(__name__)

# Windows username restrictions: no \/ [ ] : ; | = , + * ? < > " @
# Max 20 characters, cannot be all spaces or empty.
_WIN_USERNAME_RE = re.compile(r'^[^\\\/\[\]:;|=,+*?<>"@\x00-\x1f]{1,20}$')

_XML_NS = "urn:schemas-microsoft-com:unattend"
_WCM_NS = "http://schemas.microsoft.com/WMIConfig/2002/State"

ET.register_namespace("", _XML_NS)
ET.register_namespace("wcm", _WCM_NS)


def _detect_arch(mount: str) -> str:
    """Detect Windows architecture from the mounted image.

    Checks for ARM64 EFI bootloader presence; defaults to amd64.
    """
    boot_dir = os.path.join(mount, "EFI", "BOOT")
    if os.path.isdir(boot_dir):
        for entry in os.listdir(boot_dir):
            if entry.lower() == "bootaa64.efi":
                return "arm64"
    return "amd64"


def _get_autounattend(mount: str, arch: str) -> tuple[ET.ElementTree, ET.Element]:
    """Load existing autounattend.xml or create a skeleton.

    Returns (tree, Shell-Setup component element).
    """
    path = os.path.join(mount, "autounattend.xml")
    if os.path.exists(path):
        tree = ET.parse(path)
        root = tree.getroot()
    else:
        root = ET.Element(f"{{{_XML_NS}}}unattend")
        tree = ET.ElementTree(root)

    ns = _XML_NS
    settings = None
    for s in root.findall(f"{{{ns}}}settings"):
        if s.get("pass") == "oobeSystem":
            settings = s
            break
    if settings is None:
        settings = ET.SubElement(root, f"{{{ns}}}settings", {"pass": "oobeSystem"})

    comp = None
    for c in settings.findall(f"{{{ns}}}component"):
        if c.get("name") == "Microsoft-Windows-Shell-Setup":
            comp = c
            break
    if comp is None:
        comp = ET.SubElement(
            settings,
            f"{{{ns}}}component",
            {
                "name": "Microsoft-Windows-Shell-Setup",
                "processorArchitecture": arch,
                "publicKeyToken": "31bf3856ad364e35",
                "language": "neutral",
                "versionScope": "nonSxS",
            },
        )

    return tree, comp


def _save_autounattend(tree: ET.ElementTree, mount: str) -> None:
    """Write autounattend.xml to the mount."""
    path = os.path.join(mount, "autounattend.xml")
    tree.write(path, xml_declaration=True, encoding="utf-8")


def _ensure_oobe(comp: ET.Element, ns: str) -> ET.Element:
    """Return or create the <OOBE> element under the Shell-Setup component."""
    oobe = comp.find(f"{{{ns}}}OOBE")
    if oobe is None:
        oobe = ET.SubElement(comp, f"{{{ns}}}OOBE")
    return oobe


def _set_text(parent: ET.Element, tag: str, text: str) -> None:
    """Set text of an element, creating it if missing."""
    child = parent.find(tag)
    if child is None:
        child = ET.SubElement(parent, tag)
    child.text = text


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


def _get_setup_image_index(boot_wim: str) -> str:
    """Return the index of the Windows Setup image in boot.wim.

    Usually index 2, but we probe with wiminfo to be sure.
    """
    try:
        output = subprocess.check_output(["wimlib-imagex", "info", boot_wim], text=True)
        # Look for the image that has "Microsoft Windows Setup" in its description or name
        # or simply return "2" if we can't be sure, as it's the standard.
        images = output.split("Index:")
        for img in images[1:]:
            if "Microsoft Windows Setup" in img or "Windows Setup" in img:
                index = img.splitlines()[0].strip()
                log.info("_get_setup_image_index: found setup image at index %s", index)
                return index
    except Exception as e:
        log.warning("_get_setup_image_index: failed to probe boot.wim: %s. Falling back to index 2.", e)
    return "2"


def _modify_boot_wim_registry(mount: str, hive: str, commands: list[str], label: str) -> bool:
    boot_wim = _boot_wim_path(mount)
    if not os.path.exists(boot_wim):
        log.error("%s: boot.wim not found at %s", label, boot_wim)
        return False

    index = _get_setup_image_index(boot_wim)
    cmd_string = "\n".join(commands) + "\n"
    temp_mount = tempfile.mkdtemp(prefix="lufus-winwim-")
    mounted = False
    try:
        # Step 1: Mount the WIM image
        cmd1 = ["wimmountrw", boot_wim, index, temp_mount]
        log.info("Executing: %s", shlex.join(cmd1))
        subprocess.run(cmd1, check=True, capture_output=True, text=True)
        mounted = True

        # Step 2: Edit the registry hive
        hive_path = os.path.join(temp_mount, "Windows", "System32", "config", hive)
        if not os.path.exists(hive_path):
            # Try lowercase windows/system32/config
            hive_path = os.path.join(temp_mount, "windows", "system32", "config", hive.lower())

        if not os.path.exists(hive_path):
            log.error("%s: hive file %s not found in boot.wim image %s", label, hive, index)
            return False

        cmd2 = ["chntpw", "-e", hive_path]
        log.info("Executing: %s (with registry commands)", shlex.join(cmd2))
        # We don't use check=True here because chntpw might exit with non-zero
        # even if commands were successful (e.g. if it didn't like some input).
        # We'll check the output instead.
        proc = subprocess.run(
            cmd2,
            input=cmd_string,
            text=True,
            capture_output=True,
            check=False,
        )

        if "writable" not in proc.stdout.lower() and "opened read only" in proc.stdout.lower():
            log.error("%s: chntpw could only open hive %s in read-only mode!", label, hive)
            return False

        # Step 3: Unmount and commit
        cmd3 = ["wimunmount", temp_mount, "--commit"]
        log.info("Executing: %s", shlex.join(cmd3))
        subprocess.run(cmd3, check=True, capture_output=True, text=True)
        mounted = False
        log.info("%s: boot.wim registry changes applied successfully.", label)
        return True
    except subprocess.CalledProcessError as e:
        log.error("%s: command failed: %s\nStdout: %s\nStderr: %s", label, e, e.stdout, e.stderr)
        return False
    except Exception as e:
        log.error("%s: unexpected error: %s", label, e)
        return False
    finally:
        if mounted:
            cmd_f = ["wimunmount", temp_mount, "--discard"]
            log.info("Cleanup: Executing %s", shlex.join(cmd_f))
            subprocess.run(cmd_f, check=False, capture_output=True, text=True)
        shutil.rmtree(temp_mount, ignore_errors=True)


def win_hardware_bypass(mount: str | None = None) -> bool:
    mount = _resolve_windows_mount(mount)
    if not mount:
        log.error("win_hardware_bypass: no USB mount found")
        return False
    # Use exact keys and values that Windows Setup expects in LabConfig.
    # Note: chntpw 'newkey' and 'addvalue' must be precise.
    commands = [
        "cd Setup",
        "newkey LabConfig",
        "cd LabConfig",
        "addvalue BypassTPMCheck 4 1",
        "addvalue BypassSecureBootCheck 4 1",
        "addvalue BypassRAMCheck 4 1",
        "addvalue BypassCPUCheck 4 1",
        "addvalue BypassStorageCheck 4 1",
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
    # This bypasses the NRO (Network Reporting Obligation) requirement during OOBE.
    commands = ["cd Microsoft\\Windows\\CurrentVersion\\OOBE", "addvalue BypassNRO 4 1", "save", "exit"]
    log.info("win_local_acc: bypassing online account requirement at %s...", mount)
    return _modify_boot_wim_registry(mount, "SOFTWARE", commands, "win_local_acc")


def win_skip_privacy_questions(mount: str | None = None) -> bool:
    mount = _resolve_windows_mount(mount)
    if not mount:
        log.error("win_skip_privacy_questions: no USB mount found")
        return False
    arch = _detect_arch(mount)
    try:
        tree, comp = _get_autounattend(mount, arch)
        ns = _XML_NS
        oobe = _ensure_oobe(comp, ns)
        _set_text(oobe, f"{{{ns}}}HideEULAPage", "true")
        _set_text(oobe, f"{{{ns}}}HidePrivacyExperience", "true")
        _set_text(oobe, f"{{{ns}}}HideOnlineAccountScreens", "true")
        _set_text(oobe, f"{{{ns}}}ProtectYourPC", "3")

        # Add bypass commands to the specialize pass for hardware bypass
        if getattr(state, "win_hardware_bypass", 0) == 1:
            log.info("win_skip_privacy_questions: adding hardware bypass to autounattend.xml")
            _add_registry_bypass_to_xml(tree, arch)

        # Ensure the settings are applied to the correct pass
        _save_autounattend(tree, mount)
        log.info("win_skip_privacy_questions: autounattend.xml updated.")
        return True
    except Exception as e:
        log.error("win_skip_privacy_questions: failed to write autounattend.xml: %s", e)
        return False


def _add_registry_bypass_to_xml(tree: ET.ElementTree, arch: str):
    """Add registry commands to bypass TPM/RAM/SecureBoot during windows installation."""
    root = tree.getroot()
    ns = _XML_NS

    # We use the 'specialize' pass to apply registry keys early
    spec_settings = None
    for s in root.findall(f"{{{ns}}}settings"):
        if s.get("pass") == "specialize":
            spec_settings = s
            break
    if spec_settings is None:
        spec_settings = ET.SubElement(root, f"{{{ns}}}settings", {"pass": "specialize"})

    comp = None
    for c in spec_settings.findall(f"{{{ns}}}component"):
        if c.get("name") == "Microsoft-Windows-Deployment":
            comp = c
            break
    if comp is None:
        comp = ET.SubElement(
            spec_settings,
            f"{{{ns}}}component",
            {
                "name": "Microsoft-Windows-Deployment",
                "processorArchitecture": arch,
                "publicKeyToken": "31bf3856ad364e35",
                "language": "neutral",
                "versionScope": "nonSxS",
            },
        )

    run_cmd = comp.find(f"{{{ns}}}RunSynchronous")
    if run_cmd is None:
        run_cmd = ET.SubElement(comp, f"{{{ns}}}RunSynchronous")

    # Command to add the LabConfig keys
    commands = [
        "reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassTPMCheck /t REG_DWORD /d 1 /f",
        "reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassSecureBootCheck /t REG_DWORD /d 1 /f",
        "reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassRAMCheck /t REG_DWORD /d 1 /f",
        "reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassCPUCheck /t REG_DWORD /d 1 /f",
        "reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassStorageCheck /t REG_DWORD /d 1 /f",
    ]

    start_order = 1
    existing = run_cmd.findall(f"{{{ns}}}RunSynchronousCommand")
    if existing:
        start_order = max([int(c.find(f"{{{ns}}}Order").text) for c in existing]) + 1

    for cmd_str in commands:
        cmd_elem = ET.SubElement(run_cmd, f"{{{ns}}}RunSynchronousCommand", {"wcm:action": "add"})
        ET.SubElement(cmd_elem, f"{{{ns}}}Description").text = "Bypass Hardware Check"
        ET.SubElement(cmd_elem, f"{{{ns}}}Order").text = str(start_order)
        ET.SubElement(cmd_elem, f"{{{ns}}}Path").text = cmd_str
        start_order += 1


def win_local_acc_name(mount: str | None = None) -> bool:
    mount = _resolve_windows_mount(mount)
    if not mount:
        log.error("win_local_acc_name: no USB mount found")
        return False
    user_name = _validate_windows_username(state.win_local_acc)
    if user_name is None:
        log.error("win_local_acc_name: invalid username %r, aborting", state.win_local_acc)
        return False
    safe_name = html.escape(user_name, quote=True)

    # Get password if set
    password = getattr(state, "win_local_acc_pwd", "")
    safe_pwd = html.escape(password, quote=True)

    arch = _detect_arch(mount)
    try:
        tree, comp = _get_autounattend(mount, arch)
        wcm = _WCM_NS
        ns = _XML_NS

        # OOBE — privacy settings
        oobe = _ensure_oobe(comp, ns)
        _set_text(oobe, f"{{{ns}}}HideEULAPage", "true")
        _set_text(oobe, f"{{{ns}}}HidePrivacyExperience", "true")
        _set_text(oobe, f"{{{ns}}}HideOnlineAccountScreens", "true")
        _set_text(oobe, f"{{{ns}}}ProtectYourPC", "3")

        # UserAccounts / LocalAccounts
        accounts = comp.find(f"{{{ns}}}UserAccounts")
        if accounts is None:
            accounts = ET.SubElement(comp, f"{{{ns}}}UserAccounts")
        local_accounts = accounts.find(f"{{{ns}}}LocalAccounts")
        if local_accounts is None:
            local_accounts = ET.SubElement(accounts, f"{{{ns}}}LocalAccounts")

        # Add AutoLogon for seamless setup
        autologon = comp.find(f"{{{ns}}}AutoLogon")
        if autologon is None:
            autologon = ET.SubElement(comp, f"{{{ns}}}AutoLogon")
        _set_text(autologon, f"{{{ns}}}Enabled", "true")
        _set_text(autologon, f"{{{ns}}}Username", safe_name)
        pwd_auto = autologon.find(f"{{{ns}}}Password")
        if pwd_auto is None:
            pwd_auto = ET.SubElement(autologon, f"{{{ns}}}Password")
        _set_text(pwd_auto, f"{{{ns}}}Value", safe_pwd)
        _set_text(pwd_auto, f"{{{ns}}}PlainText", "true")

        acct = ET.SubElement(
            local_accounts,
            f"{{{ns}}}LocalAccount",
            {f"{{{wcm}}}action": "add"},
        )
        pwd = ET.SubElement(acct, f"{{{ns}}}Password")
        ET.SubElement(pwd, f"{{{ns}}}Value").text = safe_pwd
        ET.SubElement(pwd, f"{{{ns}}}PlainText").text = "true"
        ET.SubElement(acct, f"{{{ns}}}Description").text = "Primary Local Account"
        ET.SubElement(acct, f"{{{ns}}}DisplayName").text = safe_name
        ET.SubElement(acct, f"{{{ns}}}Group").text = "Administrators"
        ET.SubElement(acct, f"{{{ns}}}Name").text = safe_name

        _save_autounattend(tree, mount)
        log.info("win_local_acc_name: autounattend.xml updated — local account %r created.", user_name)
        return True
    except Exception as e:
        log.error("win_local_acc_name: failed to write autounattend.xml: %s", e)
        return False


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
