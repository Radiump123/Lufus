import csv
import locale
import os
from pathlib import Path
from lufus.gui.constants import _find_resource_dir

LOCALE_TO_LANG = {
    "de_DE": "Deutsch",
    "de": "Deutsch",
    "es_ES": "Español",
    "es": "Español",
    "fr_FR": "Français",
    "fr": "Français",
    "pt_BR": "Português Brasileiro",
    "pt": "Português Brasileiro",
    "sv_SE": "Svenska",
    "sv": "Svenska",
    "ru_RU": "Русский",
    "ru": "Русский",
    "uk_UA": "українська",
    "uk": "українська",
    "ar_SA": "عربي",
    "ar": "عربي",
    "bn_BD": "বাংলা",
    "bn": "বাংলা",
}


def detect_system_language() -> str:
    """Detect the system locale and return the matching language name.

    Falls back to English if detection fails or no match is found.
    """
    try:
        # Ensure locale is initialized from the environment, then read current locale.
        locale.setlocale(locale.LC_CTYPE, "")
        loc = locale.getlocale()
    except Exception:
        loc = (None, None)

    lang_code = (loc[0] or "") or os.environ.get("LANG", "")

    if not lang_code:
        return "English"

    # Extract language and territory: e.g. "fr_FR.UTF-8" -> "fr_FR", "fr"
    lang_code = lang_code.split(".")[0]
    if lang_code in LOCALE_TO_LANG:
        return LOCALE_TO_LANG[lang_code]

    # Try just the language part: e.g. "fr_FR" -> "fr"
    parts = lang_code.split("_")
    if parts and parts[0] in LOCALE_TO_LANG:
        return LOCALE_TO_LANG[parts[0]]

    # Try lowercased
    if parts and parts[0].lower() in LOCALE_TO_LANG:
        return LOCALE_TO_LANG[parts[0].lower()]

    return "English"


def load_translations(language="English"):
    # load language csv files for localization
    lang_dir = _find_resource_dir("languages")
    t = {}
    if lang_dir is None:
        return t
    lang_file = lang_dir / f"{language}.csv"
    if lang_file.exists():
        # utf-8-sig strips BOM that some editors insert before the first byte.
        # Graceful row handling: skip malformed rows (no key column, empty key).
        with open(lang_file, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                key = row.get("key", "").strip()
                value = row.get("value", "")
                if key:
                    t[key] = value
    return t
