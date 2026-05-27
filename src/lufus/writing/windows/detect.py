"""ISO type detection — Windows, Linux, or Other.

Detection runs in this order for each call to detect_iso_type():

  1. PVD label  — pure Python, zero subprocesses, instant.
                  Most distros and all Microsoft ISOs brand the label clearly.

  2. File tree  — via the pure Python ISO reader first, then optional 7z or
                  bsdtar for UDF-heavy images. Uses markers that are
                  *exclusive* to each OS family so the two sets never overlap.

is_windows_iso() and is_linux_iso() are thin wrappers kept for backward
compatibility.  Prefer calling detect_iso_type() directly when possible so
the file listing is only fetched once.
"""

import re
import shutil
import subprocess
from enum import Enum

from lufus.lufus_logging import get_logger
from lufus.iso9660 import has_el_torito_boot_catalog, list_files, read_file

log = get_logger(__name__)

_ARCHIVE_TOOL_TIMEOUT = 10


# ---------------------------------------------------------------------------
# IsoType enum
# ---------------------------------------------------------------------------


class IsoType(str, Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    OTHER = "other"


# ---------------------------------------------------------------------------
# PVD (Primary Volume Descriptor) helpers — pure Python, no subprocess
# ---------------------------------------------------------------------------

# ISO 9660: sector 16 = byte 32768.  Within the PVD:
#   byte  1– 5 : Standard Identifier "CD001"
#   byte 40–71 : Volume Identifier (32 a-characters)
_PVD_OFFSET = 16 * 2048  # 32768
_PVD_MAGIC_OFFSET = _PVD_OFFSET + 1  # where "CD001" lives
_PVD_MAGIC = b"CD001"
_PVD_LABEL_OFFSET = _PVD_OFFSET + 40
_PVD_LABEL_SIZE = 32


def _read_pvd_label(iso_path: str) -> str:
    """Return the stripped ISO 9660 Volume Identifier, or "" if unreadable / invalid."""
    try:
        with open(iso_path, "rb") as f:
            f.seek(_PVD_MAGIC_OFFSET)
            if f.read(5) != _PVD_MAGIC:
                log.debug("detect: PVD magic 'CD001' missing in %s — not a standard ISO 9660", iso_path)
                return ""
            f.seek(_PVD_LABEL_OFFSET)
            raw = f.read(_PVD_LABEL_SIZE)
        return raw.decode("ascii", errors="replace").strip()
    except OSError as e:
        log.error("detect: cannot read %s: %s", iso_path, e)
        return ""


def _read_iso_label(iso_path: str) -> str:
    """Read the ISO 9660 volume label at the fixed sector-16 offset.

    Unlike _read_pvd_label this does not check the CD001 magic, making it
    useful for unit tests that write a minimal label-only fixture.  Returns
    an empty string on OSError (e.g. missing file) or when the file is too
    small to contain a label.
    """
    try:
        with open(iso_path, "rb") as f:
            f.seek(_PVD_LABEL_OFFSET)
            raw = f.read(_PVD_LABEL_SIZE)
        if len(raw) < _PVD_LABEL_SIZE:
            return ""
        return raw.decode("ascii", errors="replace").strip()
    except OSError as e:
        log.error("detect: cannot read label from %s: %s", iso_path, e)
        return ""


def _label_is_windows(label: str) -> bool:
    """Return True if *label* matches a known Windows ISO volume identifier.

    Uses the pre-compiled _WIN_LABEL_RE regex.  Any label beginning with
    "WIN" already covers all "WINDOWS…" variants, so no redundant prefix
    check is needed.
    """
    return bool(_WIN_LABEL_RE.match(label))


# ---------------------------------------------------------------------------
# File-listing helpers — pure Python ISO 9660 reader
# ---------------------------------------------------------------------------


def _get_file_listing(iso_path: str) -> "list[str] | None":
    """Return a normalised lowercased file listing via pure Python ISO 9660 reader,
    or None if the file cannot be read / is not ISO 9660."""
    files = list_files(iso_path)
    if files is None:
        log.info("detect: pure Python ISO reader could not read %s", iso_path)
        files = _get_file_listing_via_archive_tools(iso_path)
    return files


def _normalise_archive_path(path: str) -> str:
    return path.strip().replace("\\", "/").lower().strip("/")


def _get_file_listing_via_archive_tools(iso_path: str) -> "list[str] | None":
    """List ISO/UDF contents with optional archive tools when available."""
    for tool in ("7z", "7zz", "7za"):
        exe = shutil.which(tool)
        if not exe:
            continue
        try:
            result = subprocess.run(
                [exe, "l", "-slt", iso_path],
                capture_output=True,
                text=True,
                timeout=_ARCHIVE_TOOL_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            log.debug("detect: %s listing failed for %s: %s", tool, iso_path, e)
            continue
        if result.returncode != 0:
            log.debug("detect: %s listing returned %s for %s", tool, result.returncode, iso_path)
            continue

        files = [
            _normalise_archive_path(line.removeprefix("Path = "))
            for line in result.stdout.splitlines()
            if line.startswith("Path = ")
        ]
        files = [path for path in files if path]
        if files:
            return files

    exe = shutil.which("bsdtar")
    if exe:
        try:
            result = subprocess.run(
                [exe, "-tf", iso_path],
                capture_output=True,
                text=True,
                timeout=_ARCHIVE_TOOL_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            log.debug("detect: bsdtar listing failed for %s: %s", iso_path, e)
        else:
            if result.returncode == 0:
                files = [_normalise_archive_path(line) for line in result.stdout.splitlines()]
                files = [path for path in files if path]
                if files:
                    return files
            else:
                log.debug("detect: bsdtar listing returned %s for %s", result.returncode, iso_path)

    return None


def _read_file_via_archive_tools(iso_path: str, wanted_path: str, max_bytes: int = 65536) -> bytes | None:
    """Read a small ISO/UDF file with optional archive tools when available."""
    wanted = wanted_path.strip("/")
    if not wanted:
        return None

    for tool in ("7z", "7zz", "7za"):
        exe = shutil.which(tool)
        if not exe:
            continue
        for candidate in _archive_read_candidates(wanted):
            try:
                result = subprocess.run(
                    [exe, "x", "-bd", "-so", iso_path, candidate],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=_ARCHIVE_TOOL_TIMEOUT,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                log.debug("detect: %s read failed for %s:%s: %s", tool, iso_path, candidate, e)
                continue
            if result.returncode == 0 and result.stdout:
                return result.stdout[:max_bytes]

    exe = shutil.which("bsdtar")
    if exe:
        for candidate in _archive_read_candidates(wanted):
            try:
                result = subprocess.run(
                    [exe, "-xOf", iso_path, candidate],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=_ARCHIVE_TOOL_TIMEOUT,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                log.debug("detect: bsdtar read failed for %s:%s: %s", iso_path, candidate, e)
            else:
                if result.returncode == 0 and result.stdout:
                    return result.stdout[:max_bytes]

    return None


def _archive_read_candidates(wanted_path: str) -> list[str]:
    candidates = [wanted_path]
    upper = wanted_path.upper()
    if upper != wanted_path:
        candidates.append(upper)

    parent, sep, filename = wanted_path.rpartition("/")
    if sep:
        upper_filename = f"{parent}/{filename.upper()}"
        if upper_filename not in candidates:
            candidates.append(upper_filename)

    return candidates


def _read_windows_metadata_file(iso_path: str, metadata_path: str) -> bytes | None:
    data = read_file(iso_path, metadata_path)
    if data:
        return data
    return _read_file_via_archive_tools(iso_path, metadata_path)


# ---------------------------------------------------------------------------
# Windows patterns
# ---------------------------------------------------------------------------

# Anchored at the start to avoid matching labels that merely *contain* "win".
# Covers: Windows 10/11 retail, ESD downloads, MSDN/volume-licence ISOs.
# Added support for Windows XP, Vista, 7, 8.
_WIN_LABEL_RE = re.compile(
    r"^(WIN|ESD-ISO|CC[A-Z0-9]+_[A-Z0-9]+FRE_|CCSDK[A-Z0-9]+|GRMSXP|WXPFRE|GSP1RM|7[0-9]{3}|MICROSOFT)",
    re.IGNORECASE,
)

# Files that exist ONLY in Windows installation media.
# NOTE: EFI directories are deliberately absent — Windows ISOs also have efi/boot/.
_WIN_FILE_MARKERS = [
    "sources/install.wim",  # Windows setup image (retail / OEM)
    "sources/install.esd",  # Windows setup image (ESD download)
    "sources/install.swm",  # Split setup image (multi-disc)
    "sources/boot.wim",  # Windows PE boot image
    "i386/txtsetup.sif",  # Windows XP / 2003
    "setup.exe",  # Windows setup root (Vista+)
    "bootmgr",  # Windows boot manager (Vista+)
    "ntldr",  # Windows XP bootloader
]


# ---------------------------------------------------------------------------
# Linux patterns
# ---------------------------------------------------------------------------

# Almost every major distro embeds its name in the ISO label.
# Catching the label early avoids the 7z subprocess entirely for common cases.
_LINUX_LABEL_RE = re.compile(
    r"ubuntu|debian|fedora|arch_[0-9]|archlinux|manjaro|linuxmint|mint|"
    r"opensuse|centos|rhel|red.hat|kali|pop.?os|elementary|endeavouros|"
    r"garuda|nixos|void|slackware|gentoo|alpine|tails|whonix|parrot|"
    r"mxlinux|mx.linux|zorin|lubuntu|kubuntu|xubuntu|raspbian|raspios|"
    r"mageia|pclinuxos|puppy|antix|bodhi|deepin|solus|"
    r"backbox|blackarch|bunsenlabs|calculate|devuan|dragora|exherbo|"
    r"funtoo|grml|guixsd|hyperbola|kwort|libreelec|lite|"
    r"peppermint|porteus|q4os|sabayon|siduction|sparky|trisquel|"
    r"turbolinux|vine|wifislax|artix|rebornos|biglinux|kaos|nobara",
    re.IGNORECASE,
)

# Files / directories present ONLY on Linux live and install media.
_LINUX_FILE_MARKERS = [
    "isolinux/isolinux.cfg",
    "syslinux/syslinux.cfg",
    "syslinux/ldlinux.c32",
    "boot/grub/grub.cfg",
    "boot/grub/i386-pc/",
    "grub/grub.cfg",
    "casper/filesystem.squashfs",
    "casper/filesystem.manifest",
    "casper/vmlinuz",
    "live/filesystem.squashfs",
    "live/filesystem.manifest",
    "live/vmlinuz",
    ".disk/info",
    "arch/pkglist.x86_64.txt",
    "arch/boot/x86_64/vmlinuz-linux",
    "images/pxeboot/vmlinuz",
    ".discinfo",
    "boot/vmlinuz",
    "boot/bzimage",
    "vmlinuz",
    "initrd.img",
    "boot/initrd",
    ".treeinfo",
    "images/install.img",
]

_LINUX_FILE_MARKER_PAIRS = [
    ("vmlinuz", "initrd.img"),
    ("vmlinuz", "initrd"),
    ("casper/vmlinuz", "casper/initrd"),
    ("live/vmlinuz", "live/initrd"),
    ("install.amd/vmlinuz", "install.amd/initrd.gz"),
    ("install.386/vmlinuz", "install.386/initrd.gz"),
    ("images/pxeboot/vmlinuz", "images/pxeboot/initrd.img"),
]

_WINDOWS_11_MIN_BUILD = 22000
_WINDOWS_METADATA_FILES = (
    "sources/idwbinfo.txt",
    "sources/cversion.ini",
)


# ---------------------------------------------------------------------------
# Main detection entry point
# ---------------------------------------------------------------------------


def _get_info_via_file_cmd(iso_path: str) -> str:
    """Run 'file' command on the ISO to get descriptive info."""
    try:
        # -b = brief (no filename), -L = follow symlinks
        result = subprocess.run(["file", "-bL", iso_path], capture_output=True, text=True, timeout=2)
        return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _normalise_listing(listing: list[str] | None) -> set[str]:
    return {f.lower().strip("/") for f in listing or []}


def _has_marker(paths: set[str], marker: str) -> bool:
    raw = marker.lower().strip()
    is_prefix = raw.endswith("/")
    marker = raw.strip("/")
    if is_prefix:
        prefix = marker + "/"
        return marker in paths or any(path.startswith(prefix) for path in paths)
    return marker in paths


def _looks_like_linux_from_listing(paths: set[str]) -> bool:
    for marker in _LINUX_FILE_MARKERS:
        if _has_marker(paths, marker):
            return True

    for left, right in _LINUX_FILE_MARKER_PAIRS:
        if _has_marker(paths, left) and _has_marker(paths, right):
            return True

    return False


def _file_info_says_bootable(info: str) -> bool:
    text = info.lower()
    if not text:
        return False
    if _file_info_says_not_bootable(info):
        return False
    return any(
        phrase in text
        for phrase in (
            "(bootable)",
            "boot image",
            "el torito",
            "dos/mbr boot sector",
            "boot sector",
            "efi application",
        )
    )


def _file_info_says_not_bootable(info: str) -> bool:
    text = info.lower()
    return "not bootable" in text or "non-bootable" in text or "non bootable" in text


def is_bootable(listing: list[str], iso_path: str = None) -> bool:
    """Check if the file listing suggests the image is bootable (BIOS or UEFI)."""

    if iso_path:
        info = _get_info_via_file_cmd(iso_path)
        if _file_info_says_not_bootable(info):
            log.info("is_bootable: 'file' command explicitly reported a non-bootable image")
            return False
        if _file_info_says_bootable(info):
            log.info("is_bootable: 'file' command detected bootable flag")
            return True
        if has_el_torito_boot_catalog(iso_path):
            log.info("is_bootable: El Torito boot catalog detected")
            return True
        if iso_path.lower().endswith(".iso"):
            return False

    # Fallback for callers that only have a file listing. These markers prove
    # bootloader files exist, but not that an ISO has boot catalog metadata.
    if listing:
        strict_markers = {
            "bootmgr",
            "bootmgr.efi",
            "ntldr",
            "efi/boot/bootx64.efi",
            "efi/boot/bootaa64.efi",
            "efi/boot/bootia32.efi",
            "efi/boot/bootarm.efi",
            "isolinux/isolinux.bin",
            "syslinux/ldlinux.c32",
            "i386/txtsetup.sif",
            "boot/grub/i386-pc/eltorito.img",
            "boot/grub/i386-pc/core.img",
            "boot/grub/efi.img",
        }

        lower_listing = _normalise_listing(listing)

        for marker in strict_markers:
            if marker in lower_listing:
                return True

        grub_configs = {"boot/grub/grub.cfg", "grub/grub.cfg"}
        grub_payloads = {
            "boot/grub/i386-pc/",
            "grub/i386-pc/",
            "boot/grub/x86_64-efi/",
            "grub/x86_64-efi/",
        }
        if any(config in lower_listing for config in grub_configs) and any(
            _has_marker(lower_listing, payload) for payload in grub_payloads
        ):
            return True

    return False


def detect_iso_type(iso_path: str) -> IsoType:
    """Detect the OS family of an ISO image using pure Python + 'file' fallback."""
    log.info("ISO detection: checking %s", iso_path)

    # 1. Read label
    label = _read_pvd_label(iso_path)

    # 2. Run 'file' command for hints (label and family)
    file_info = _get_info_via_file_cmd(iso_path)
    log.info("ISO detection: file info=%r", file_info)

    # 3. Get listing
    listing = _get_file_listing(iso_path)

    # 4. Logic hierarchy

    # Priority A: File markers (very reliable)
    if listing:
        lower_listing = _normalise_listing(listing)
        for marker in _WIN_FILE_MARKERS:
            if _has_marker(lower_listing, marker):
                return IsoType.WINDOWS
        if "i386/txtsetup.sif" in lower_listing or "setup.exe" in lower_listing:
            return IsoType.WINDOWS
        if _looks_like_linux_from_listing(lower_listing):
            return IsoType.LINUX

    # Priority B: 'file' command description
    info_l = file_info.lower()
    if "windows" in info_l or "microsoft" in info_l:
        return IsoType.WINDOWS
    # Many Linux ISOs have the name in the label reported by 'file'
    if _LINUX_LABEL_RE.search(file_info):
        return IsoType.LINUX

    # Priority C: PVD Label
    if label:
        if _WIN_LABEL_RE.match(label):
            return IsoType.WINDOWS
        if _LINUX_LABEL_RE.search(label):
            return IsoType.LINUX

    log.info("ISO detection: defaulting to Other")
    return IsoType.OTHER


# ---------------------------------------------------------------------------
# Backward-compatible wrappers
# ---------------------------------------------------------------------------


def is_windows_iso(iso_path: str) -> bool:
    """Return True if iso_path is a Windows installation image."""
    return detect_iso_type(iso_path) == IsoType.WINDOWS


def is_linux_iso(iso_path: str) -> bool:
    """Return True if iso_path is a Linux live or installer image."""
    return detect_iso_type(iso_path) == IsoType.LINUX


def _windows_build_from_text(text: str) -> int | None:
    for match in re.finditer(r"(?<!\d)(?:10\.0\.)?([1-3][0-9]{4})(?:\.\d+)?(?!\d)", text):
        build = int(match.group(1))
        if 10000 <= build < 40000:
            return build
    return None


def _windows_version_from_text(text: str) -> int | None:
    upper = text.upper()
    if re.search(r"\b(?:WIN(?:DOWS)?[_ -]?)?11\b|\bW11\b", upper):
        return 11

    if any(tag in upper for tag in ("CO_RELEASE", "NI_RELEASE", "GE_RELEASE")):
        return 11
    if any(tag in upper for tag in ("TH1", "TH2", "RS1", "RS2", "RS3", "RS4", "RS5", "19H1", "19H2")):
        return 10
    if any(tag in upper for tag in ("VB_RELEASE", "MN_RELEASE", "FE_RELEASE")):
        return 10

    build = _windows_build_from_text(upper)
    if build is not None:
        return 11 if build >= _WINDOWS_11_MIN_BUILD else 10

    if re.search(r"\b(?:WIN(?:DOWS)?[_ -]?)?10\b|\bW10\b", upper):
        return 10

    return None


def get_windows_version(iso_path: str) -> int | None:
    """Detect Windows version from the ISO. Returns 10, 11, or None.

    Attempts to find the version by:
    1. Checking the PVD label for 'W11', 'Win11', or modern build names.
    2. Checking for specific file markers if label is ambiguous.
    """
    label = _read_pvd_label(iso_path).upper()
    log.info("get_windows_version: label=%r", label)

    for source, text in (
        ("label", label),
        ("file", _get_info_via_file_cmd(iso_path)),
    ):
        version = _windows_version_from_text(text)
        if version is not None:
            log.info("get_windows_version: detected Windows %s from %s", version, source)
            return version

    for metadata_path in _WINDOWS_METADATA_FILES:
        data = _read_windows_metadata_file(iso_path, metadata_path)
        if not data:
            continue
        text = data.decode("utf-8", errors="replace")
        version = _windows_version_from_text(text)
        if version is not None:
            log.info("get_windows_version: detected Windows %s from %s", version, metadata_path)
            return version

    # Fallback: only return 11 when the label unequivocally says so.
    # The old DV9/DV8 heuristic was too fragile — many Windows 10 VLSC labels
    # accidentally matched and triggered the WinTweaks dialog.
    return None
