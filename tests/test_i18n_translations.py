from __future__ import annotations

import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LANG_DIR = ROOT / "src" / "lufus" / "gui" / "languages"


def test_non_bootable_confirm_popup_is_translated_in_all_languages():
    for path in LANG_DIR.glob("*.csv"):
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = {row["key"]: row["value"] for row in csv.DictReader(f)}

        assert "msgbox_not_bootable_body_confirm" in rows, path.name
        assert "\n\n" in rows["msgbox_not_bootable_body_confirm"], path.name
