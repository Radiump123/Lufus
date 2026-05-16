#!/usr/bin/env bash
set -euo pipefail

VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating venv..."
    python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

pip install --upgrade pip
pip install -r requirements-python.txt
pip install pyinstaller

rm -rf build dist

# Collect system binaries needed at runtime (no Python equivalent)
BINARIES=()
for bin in \
    mkfs.vfat mkfs.ntfs mkfs.exfat mkfs.ext4 mkudffs \
    ntfslabel fatlabel e2label udflabel \
    badblocks grub-install wimlib-imagex chntpw \
    pkexec runuser xdg-open udevadm; do
    path=$(command -v "$bin" 2>/dev/null || true)
    if [ -n "$path" ]; then
        BINARIES+=(--add-binary "$path:.")
    fi
done

pyinstaller src/lufus/__main__.py \
    --name lufus \
    --onefile \
    --paths src \
    --hidden-import PySide6.QtCore \
    --hidden-import PySide6.QtGui \
    --hidden-import PySide6.QtWidgets \
    --hidden-import PySide6.QtSvg \
    --collect-all psutil \
    --hidden-import lufus.state \
    --hidden-import lufus.drives.autodetect_usb \
    --add-data "src/lufus/gui:lufus/gui" \
    "${BINARIES[@]}" \
    --noconfirm

echo "---"
echo "Binary: dist/lufus"
ls -lh dist/lufus
