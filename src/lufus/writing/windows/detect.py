"""ISO type detection — Windows, Linux, or Other.

Detection runs in this order for each call to detect_iso_type():

  1. PVD label  — pure Python, zero subprocesses, instant.
                  Most distros and all Microsoft ISOs brand the label clearly.

  2. File tree  — via 7z (p7zip-full) if available, else isoinfo (genisoimage).
                  Uses markers that are *exclusive* to each OS family so the
                  two sets never overlap and produce false positives.

is_windows_iso() and is_linux_iso() are thin wrappers kept for backward
compatibility.  Prefer calling detect_iso_type() directly when possible so
the file listing is only fetched once.
"""

import re
from enum import Enum

from lufus.lufus_logging import get_logger
from lufus.iso9660 import has_any_file, list_files

log = get_logger(__name__)


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
    return files


# ---------------------------------------------------------------------------
# Windows patterns
# ---------------------------------------------------------------------------

# Anchored at the start to avoid matching labels that merely *contain* "win".
# Covers: Windows 10/11 retail, ESD downloads, MSDN/volume-licence ISOs.
_WIN_LABEL_RE = re.compile(
    r"^(WIN|ESD-ISO|CC[A-Z0-9]+_[A-Z0-9]+FRE_|CCSDK[A-Z0-9]+)",
    re.IGNORECASE,
)

# Files that exist ONLY in Windows installation media.
# NOTE: EFI directories are deliberately absent — Windows ISOs also have efi/boot/.
_WIN_FILE_MARKERS = [
    "sources/install.wim",  # Windows setup image (retail / OEM)
    "sources/install.esd",  # Windows setup image (ESD download)
    "sources/install.swm",  # Split setup image (multi-disc)
    "sources/boot.wim",  # Windows PE boot image
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
    r"turbolinux|vine|wifislax",
    re.IGNORECASE,
)

# Files / directories present ONLY on Linux live and install media.
#
# Rules for adding a marker here:
#   YES — Must not exist in any standard Windows ISO
#   YES — Must be specific enough to avoid substring collisions
#   NO  — Do not add efi/boot/ or boot/efi/ — Windows ISOs have those too
#
_LINUX_FILE_MARKERS = [
    # ---- SysLinux / ISOLINUX (Linux-only bootloaders) ----
    "isolinux/isolinux.cfg",
    "syslinux/syslinux.cfg",
    "syslinux/ldlinux.c32",
    # ---- GRUB config files — Linux-only for optical/USB media ----
    # Windows uses BCD / bootmgr; it never ships grub.cfg on install media.
    "boot/grub/grub.cfg",
    "boot/grub/i386-pc/",
    "grub/grub.cfg",
    # ---- Ubuntu / Kubuntu / Xubuntu / Mint (Casper live system) ----
    "casper/filesystem.squashfs",
    "casper/filesystem.manifest",
    "casper/vmlinuz",
    # ---- Debian / Kali / Tails / Parrot (live-boot) ----
    "live/filesystem.squashfs",
    "live/filesystem.manifest",
    "live/vmlinuz",
    # ---- Ubuntu / Debian installer marker ----
    ".disk/info",
    # ---- Arch Linux (specific sub-paths, not the broad "arch/" directory) ----
    "arch/pkglist.x86_64.txt",
    "arch/boot/x86_64/vmlinuz-linux",
    # ---- Fedora / RHEL / CentOS installer (Anaconda) ----
    "images/pxeboot/vmlinuz",
    ".discinfo",
    # ---- Generic kernel presence under known Linux-only directories ----
    "boot/vmlinuz",
    "boot/bzimage",
]


# ---------------------------------------------------------------------------
# Main detection entry point
# ---------------------------------------------------------------------------


def detect_iso_type(iso_path: str) -> IsoType:
    """Detect the OS family of an ISO image using pure Python.

    Returns IsoType.WINDOWS, IsoType.LINUX, or IsoType.OTHER.

    Step 1 reads the ISO 9660 PVD label (instant, no subprocess).
    Step 2 walks the ISO 9660 directory tree (pure Python) to find
    OS-specific marker files.
    """
    log.info("ISO detection: checking %s", iso_path)

    # ------------------------------------------------------------------
    # Step 1 — PVD label (no subprocess, instant, works without any tools)
    # ------------------------------------------------------------------
    label = _read_pvd_label(iso_path)
    log.info("ISO detection: PVD label=%r", label)

    if label:
        if _WIN_LABEL_RE.match(label):
            log.info("ISO detection: Windows label match -> Windows")
            return IsoType.WINDOWS
        if _LINUX_LABEL_RE.search(label):
            log.info("ISO detection: Linux label match -> Linux")
            return IsoType.LINUX

    # ------------------------------------------------------------------
    # Step 2 — File listing (pure Python ISO 9660 reader)
    # ------------------------------------------------------------------
    listing = _get_file_listing(iso_path)

    if listing is None:
        log.warning("ISO detection: could not read ISO directory — defaulting to Other.")
        return IsoType.OTHER

    # Windows markers first
    lower_listing = [f.lower() for f in listing]
    for marker in _WIN_FILE_MARKERS:
        if marker.lower() in lower_listing:
            log.info("ISO detection: found Windows marker %r -> Windows", marker)
            return IsoType.WINDOWS

    # Linux markers second
    for marker in _LINUX_FILE_MARKERS:
        if marker.lower() in lower_listing:
            log.info("ISO detection: found Linux marker %r -> Linux", marker)
            return IsoType.LINUX

    log.info("ISO detection: no definitive markers found -> Other")
    return IsoType.OTHER


# ---------------------------------------------------------------------------
# Backward-compatible wrappers
# ---------------------------------------------------------------------------


def is_windows_iso(iso_path: str) -> bool:
    """Return True if iso_path is a Windows installation image."""
    return detect_iso_type(iso_path) == IsoType.WINDOWS


def is_linux_iso(iso_path: str) -> bool:
    """Return True if iso_path is a Linux distribution image."""
    return detect_iso_type(iso_path) == IsoType.LINUX
