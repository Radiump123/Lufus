import csv
from pathlib import Path
from lufus.gui.constants import _find_resource_dir


def load_translations(language="English"):
    # load language csv files for localization
    lang_dir = _find_resource_dir("languages")
    t = {}
    if lang_dir is None:
        return t
    lang_file = lang_dir / f"{language}.csv"
    if lang_file.exists():
        # read translations from csv :3
        with open(lang_file, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                t[row["key"]] = row["value"]
    return t
