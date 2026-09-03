r"""
renpy_hd_i18n.py - localisation de l'interface RenPyHD.

Les chaînes vivent dans app\i18n\<code>.json (clé -> texte, à plat). Le français (fr.json) est la référence : toute clé
absente d'une langue retombe sur le français, puis sur la clé elle-même. `t(key, **fmt)` remplace uniquement les
repères `{nom}` fournis (les accolades littérales des textes sont laissées telles quelles).

Ordre de choix de la langue : option --lang, puis `ui_lang` de renpy_hd_config.json, puis langue d'affichage de Windows,
sinon anglais.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
I18N_DIR = APP_DIR / "i18n"
DEFAULT_LANG = "fr"

# code -> (drapeau, nom natif)
LANGUAGES: dict[str, tuple[str, str]] = {
    "fr": ("\U0001F1EB\U0001F1F7", "Français"),
    "en": ("\U0001F1EC\U0001F1E7", "English"),
    "es": ("\U0001F1EA\U0001F1F8", "Español"),
    "de": ("\U0001F1E9\U0001F1EA", "Deutsch"),
    "ru": ("\U0001F1F7\U0001F1FA", "Русский"),
    "pt-BR": ("\U0001F1E7\U0001F1F7", "Português (Brasil)"),
}

_STRINGS: dict[str, dict[str, str]] = {}
_LANG = DEFAULT_LANG
_FMT_RE = re.compile(r"\{(\w+)\}")


def _load(code: str) -> dict[str, str]:
    if code in _STRINGS:
        return _STRINGS[code]
    path = I18N_DIR / f"{code}.json"
    data: dict[str, str] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # fichier abîmé : on continue avec le français
            print(f"i18n: {path.name} illisible ({exc})", flush=True)
    _STRINGS[code] = data
    return data


def available() -> list[str]:
    """Codes pour lesquels un fichier json existe (dans l'ordre de LANGUAGES)."""
    return [c for c in LANGUAGES if (I18N_DIR / f"{c}.json").is_file()]


def normalize(code: str | None) -> str | None:
    if not code:
        return None
    code = code.strip().replace("_", "-")
    if code in LANGUAGES:
        return code
    low = code.lower()
    for c in LANGUAGES:
        if c.lower() == low:
            return c
    base = low.split("-")[0]
    if base == "pt":
        return "pt-BR"
    for c in LANGUAGES:
        if c.lower().split("-")[0] == base:
            return c
    return None


def detect_system_language() -> str:
    """Langue d'affichage de Windows (GetUserDefaultUILanguage), sinon locale Python, sinon anglais."""
    try:
        import ctypes
        lang_id = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())  # type: ignore[attr-defined]
        primary = lang_id & 0x3FF
        table = {0x0C: "fr", 0x09: "en", 0x0A: "es", 0x07: "de", 0x19: "ru", 0x16: "pt-BR"}
        if primary in table:
            return table[primary]
    except Exception:
        pass
    try:
        import locale
        loc = locale.getlocale()[0] or ""
        found = normalize(loc.replace("_", "-")) if loc else None
        if found:
            return found
    except Exception:
        pass
    return "en"


def set_language(code: str | None) -> str:
    global _LANG
    _LANG = normalize(code) or DEFAULT_LANG
    _load(DEFAULT_LANG)
    _load(_LANG)
    return _LANG


def current() -> str:
    return _LANG


def t(key: str, **fmt) -> str:
    """Texte localisé ; `{nom}` remplacé par fmt[nom] (seulement pour les noms fournis)."""
    s = _load(_LANG).get(key)
    if s is None:
        s = _load(DEFAULT_LANG).get(key, key)
    if fmt:
        s = _FMT_RE.sub(lambda m: str(fmt[m.group(1)]) if m.group(1) in fmt else m.group(0), s)
    return s


def missing_keys(code: str) -> list[str]:
    """Clés présentes dans fr.json mais absentes de <code>.json (pour le contrôle de couverture)."""
    ref = _load(DEFAULT_LANG)
    other = _load(code)
    return [k for k in ref if k not in other]


_load(DEFAULT_LANG)
