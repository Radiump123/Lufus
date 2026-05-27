"""Windows installation customization functions.

These modify Windows installation media (boot.wim, autounattend.xml)
to bypass hardware requirements, skip privacy questions, and create
local accounts.
"""

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

# Tools required for registry-based tweaks (boot.wim modification)
_REQUIRED_TWEAK_TOOLS = ("wimmountrw", "wimunmount", "chntpw", "wimlib-imagex")

ET.register_namespace("", _XML_NS)
ET.register_namespace("wcm", _WCM_NS)


def _check_tweak_deps() -> bool:
    missing = [t for t in _REQUIRED_TWEAK_TOOLS if shutil.which(t) is None]
    if missing:
        log.error("Missing required tools for Windows registry tweaks: %s", ", ".join(missing))
        return False
    return True


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


def _get_or_create_settings(root: ET.Element, pass_name: str) -> ET.Element:
    for settings in root.findall(f"{{{_XML_NS}}}settings"):
        if settings.get("pass") == pass_name:
            return settings
    return ET.SubElement(root, f"{{{_XML_NS}}}settings", {"pass": pass_name})


def _get_or_create_component(settings: ET.Element, name: str, arch: str) -> ET.Element:
    for comp in settings.findall(f"{{{_XML_NS}}}component"):
        if comp.get("name") == name:
            return comp
    return ET.SubElement(
        settings,
        f"{{{_XML_NS}}}component",
        {
            "name": name,
            "processorArchitecture": arch,
            "publicKeyToken": "31bf3856ad364e35",
            "language": "neutral",
            "versionScope": "nonSxS",
        },
    )


def _run_command_order(command: ET.Element) -> int:
    order = command.find(f"{{{_XML_NS}}}Order")
    if order is None or not order.text:
        return 0
    try:
        return int(order.text)
    except ValueError:
        return 0


def _add_run_synchronous_commands(
    tree: ET.ElementTree,
    arch: str,
    pass_name: str,
    component_name: str,
    commands: list[str],
    description: str,
) -> None:
    root = tree.getroot()
    settings = _get_or_create_settings(root, pass_name)
    comp = _get_or_create_component(settings, component_name, arch)

    run_sync = comp.find(f"{{{_XML_NS}}}RunSynchronous")
    if run_sync is None:
        run_sync = ET.SubElement(comp, f"{{{_XML_NS}}}RunSynchronous")

    existing_commands = set()
    existing_orders = []
    for command in run_sync.findall(f"{{{_XML_NS}}}RunSynchronousCommand"):
        path = command.find(f"{{{_XML_NS}}}Path")
        if path is not None and path.text:
            existing_commands.add(path.text.strip())
        existing_orders.append(_run_command_order(command))

    next_order = max(existing_orders, default=0) + 1
    for command in commands:
        if command.strip() in existing_commands:
            continue
        cmd_elem = ET.SubElement(
            run_sync,
            f"{{{_XML_NS}}}RunSynchronousCommand",
            {f"{{{_WCM_NS}}}action": "add"},
        )
        ET.SubElement(cmd_elem, f"{{{_XML_NS}}}Description").text = description
        ET.SubElement(cmd_elem, f"{{{_XML_NS}}}Order").text = str(next_order)
        ET.SubElement(cmd_elem, f"{{{_XML_NS}}}Path").text = command
        next_order += 1


_HARDWARE_BYPASS_COMMANDS = [
    r'cmd /c reg add "HKLM\SYSTEM\Setup\LabConfig" /v BypassTPMCheck /t REG_DWORD /d 1 /f',
    r'cmd /c reg add "HKLM\SYSTEM\Setup\LabConfig" /v BypassSecureBootCheck /t REG_DWORD /d 1 /f',
    r'cmd /c reg add "HKLM\SYSTEM\Setup\LabConfig" /v BypassRAMCheck /t REG_DWORD /d 1 /f',
    r'cmd /c reg add "HKLM\SYSTEM\Setup\LabConfig" /v BypassCPUCheck /t REG_DWORD /d 1 /f',
    r'cmd /c reg add "HKLM\SYSTEM\Setup\LabConfig" /v BypassStorageCheck /t REG_DWORD /d 1 /f',
]


def _add_windows_pe_hardware_bypass(tree: ET.ElementTree, arch: str) -> None:
    _add_run_synchronous_commands(
        tree,
        arch,
        "windowsPE",
        "Microsoft-Windows-Setup",
        _HARDWARE_BYPASS_COMMANDS,
        "Bypass Windows 11 hardware checks",
    )


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
    if not _check_tweak_deps():
        return False

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
            hive_path = os.path.join(temp_mount, "windows", "system32", "config", hive.lower())

        if not os.path.exists(hive_path):
            log.error("%s: hive file %s not found in boot.wim image %s", label, hive, index)
            return False

        cmd2 = ["chntpw", "-e", hive_path]
        log.info("Executing: %s (with registry commands)", shlex.join(cmd2))
        proc = subprocess.run(
            cmd2,
            input=cmd_string,
            text=True,
            capture_output=True,
            check=False,
        )

        out_lower = proc.stdout.lower()
        err_lower = proc.stderr.lower()

        # Detect if hive was opened read-only
        if "readonly" in out_lower and "no write access" in out_lower:
            log.error("%s: chntpw could only open hive %s in read-only mode!", label, hive)
            return False

        # Detect command-level errors (chntpw -e prints errors to stdout)
        if "error" in out_lower or "error" in err_lower:
            log.error("%s: chntpw reported errors\nStdout: %s\nStderr: %s", label, proc.stdout, proc.stderr)
            return False

        # If return code is non-zero and we have stderr output, treat as failure
        if proc.returncode != 0 and proc.stderr.strip():
            log.warning("%s: chntpw exit code %d with stderr: %s", label, proc.returncode, proc.stderr.strip())

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
    arch = _detect_arch(mount)
    try:
        tree, _ = _get_autounattend(mount, arch)
        _add_windows_pe_hardware_bypass(tree, arch)
        _save_autounattend(tree, mount)
        log.info("win_hardware_bypass: autounattend.xml hardware bypass commands written at %s", mount)
        return True
    except Exception as e:
        log.error("win_hardware_bypass: failed to write autounattend.xml: %s", e)
        return False


def win_local_acc(mount: str | None = None) -> bool:
    mount = _resolve_windows_mount(mount)
    if not mount:
        log.error("win_local_acc: no USB mount found")
        return False
    arch = _detect_arch(mount)
    try:
        tree, comp = _get_autounattend(mount, arch)
        oobe = _ensure_oobe(comp, _XML_NS)
        _set_text(oobe, f"{{{_XML_NS}}}HideOnlineAccountScreens", "true")
        _set_text(oobe, f"{{{_XML_NS}}}HideWirelessSetupInOOBE", "true")
        _set_text(oobe, f"{{{_XML_NS}}}ProtectYourPC", "3")
        _save_autounattend(tree, mount)
        log.info("win_local_acc: autounattend.xml online-account bypass settings written at %s", mount)
        return True
    except Exception as e:
        log.error("win_local_acc: failed to write autounattend.xml: %s", e)
        return False


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
        _set_text(oobe, f"{{{ns}}}HideWirelessSetupInOOBE", "true")
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
    _add_windows_pe_hardware_bypass(tree, arch)


def win_local_acc_name(mount: str | None = None) -> bool:
    mount = _resolve_windows_mount(mount)
    if not mount:
        log.error("win_local_acc_name: no USB mount found")
        return False
    user_name = _validate_windows_username(state.win_local_acc)
    if user_name is None:
        log.error("win_local_acc_name: invalid username %r, aborting", state.win_local_acc)
        return False
    password = getattr(state, "win_local_acc_pwd", "")

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
        _set_text(oobe, f"{{{ns}}}HideWirelessSetupInOOBE", "true")
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
        _set_text(autologon, f"{{{ns}}}Username", user_name)
        pwd_auto = autologon.find(f"{{{ns}}}Password")
        if pwd_auto is None:
            pwd_auto = ET.SubElement(autologon, f"{{{ns}}}Password")
        _set_text(pwd_auto, f"{{{ns}}}Value", password)
        _set_text(pwd_auto, f"{{{ns}}}PlainText", "true")

        acct = ET.SubElement(
            local_accounts,
            f"{{{ns}}}LocalAccount",
            {f"{{{wcm}}}action": "add"},
        )
        pwd = ET.SubElement(acct, f"{{{ns}}}Password")
        ET.SubElement(pwd, f"{{{ns}}}Value").text = password
        ET.SubElement(pwd, f"{{{ns}}}PlainText").text = "true"
        ET.SubElement(acct, f"{{{ns}}}Description").text = "Primary Local Account"
        ET.SubElement(acct, f"{{{ns}}}DisplayName").text = user_name
        ET.SubElement(acct, f"{{{ns}}}Group").text = "Administrators"
        ET.SubElement(acct, f"{{{ns}}}Name").text = user_name

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
        log.info("apply_windows_tweaks: applying hardware bypass")
        ok = win_hardware_bypass(mount) and ok
    if getattr(state, "win_local_acc_chk", 0) == 1:
        log.info("apply_windows_tweaks: applying local account creation")
        ok = win_local_acc_name(mount) and ok
    elif getattr(state, "win_microsoft_acc", 0) == 1:
        log.info("apply_windows_tweaks: applying Microsoft account bypass")
        ok = win_local_acc(mount) and ok
    if getattr(state, "win_privacy", 0) == 1:
        log.info("apply_windows_tweaks: applying privacy question bypass")
        ok = win_skip_privacy_questions(mount) and ok
    return ok
