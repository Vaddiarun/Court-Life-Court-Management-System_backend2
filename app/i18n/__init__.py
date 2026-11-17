"""Language registry (English + 22 Eighth Schedule languages) and message templates.

Translations here cover notification / call templates only; UI strings live in the
frontend. Non-English templates must be reviewed by native speakers before release
(PRD 11: translations are for accessibility, not certified legal translations).
Missing keys fall back to English.
"""

import json
from functools import lru_cache
from pathlib import Path
from string import Template

LANGUAGES: list[dict] = [
    {"code": "en", "name": "English", "native": "English", "script": "Latin", "rtl": False},
    {"code": "as", "name": "Assamese", "native": "অসমীয়া", "script": "Bengali-Assamese", "rtl": False},
    {"code": "bn", "name": "Bengali", "native": "বাংলা", "script": "Bengali", "rtl": False},
    {"code": "brx", "name": "Bodo", "native": "बड़ो", "script": "Devanagari", "rtl": False},
    {"code": "doi", "name": "Dogri", "native": "डोगरी", "script": "Devanagari", "rtl": False},
    {"code": "gu", "name": "Gujarati", "native": "ગુજરાતી", "script": "Gujarati", "rtl": False},
    {"code": "hi", "name": "Hindi", "native": "हिन्दी", "script": "Devanagari", "rtl": False},
    {"code": "kn", "name": "Kannada", "native": "ಕನ್ನಡ", "script": "Kannada", "rtl": False},
    {"code": "ks", "name": "Kashmiri", "native": "کٲشُر", "script": "Perso-Arabic", "rtl": True},
    {"code": "kok", "name": "Konkani", "native": "कोंकणी", "script": "Devanagari", "rtl": False},
    {"code": "mai", "name": "Maithili", "native": "मैथिली", "script": "Devanagari", "rtl": False},
    {"code": "ml", "name": "Malayalam", "native": "മലയാളം", "script": "Malayalam", "rtl": False},
    {"code": "mni", "name": "Manipuri", "native": "ꯃꯤꯇꯩꯂꯣꯟ", "script": "Meitei Mayek", "rtl": False},
    {"code": "mr", "name": "Marathi", "native": "मराठी", "script": "Devanagari", "rtl": False},
    {"code": "ne", "name": "Nepali", "native": "नेपाली", "script": "Devanagari", "rtl": False},
    {"code": "or", "name": "Odia", "native": "ଓଡ଼ିଆ", "script": "Odia", "rtl": False},
    {"code": "pa", "name": "Punjabi", "native": "ਪੰਜਾਬੀ", "script": "Gurmukhi", "rtl": False},
    {"code": "sa", "name": "Sanskrit", "native": "संस्कृतम्", "script": "Devanagari", "rtl": False},
    {"code": "sat", "name": "Santali", "native": "ᱥᱟᱱᱛᱟᱲᱤ", "script": "Ol Chiki", "rtl": False},
    {"code": "sd", "name": "Sindhi", "native": "سنڌي", "script": "Perso-Arabic", "rtl": True},
    {"code": "ta", "name": "Tamil", "native": "தமிழ்", "script": "Tamil", "rtl": False},
    {"code": "te", "name": "Telugu", "native": "తెలుగు", "script": "Telugu", "rtl": False},
    {"code": "ur", "name": "Urdu", "native": "اردو", "script": "Perso-Arabic", "rtl": True},
]

LANGUAGE_CODES = {lang["code"] for lang in LANGUAGES}
LOCALES_DIR = Path(__file__).parent / "locales"


@lru_cache
def _load(code: str) -> dict:
    path = LOCALES_DIR / f"{code}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def translations_available() -> set[str]:
    return {p.stem for p in LOCALES_DIR.glob("*.json")}


def t(key: str, lang: str = "en", **params) -> str:
    """Translate `key` into `lang`, falling back to English, then to the key itself."""
    text = _load(lang).get(key) or _load("en").get(key) or key
    return Template(text).safe_substitute(**{k: ("" if v is None else v) for k, v in params.items()})
