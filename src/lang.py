"""Language code normalization.

Handles ISO 639-1 (2-char), ISO 639-2/T (3-char, lower), ISO 639-2/B (e.g. "ger" vs "deu"),
and BCP 47 tags ("pt-BR"). Keeps the module dependency-free — no external lookup.
"""
from __future__ import annotations

# 639-1 -> 639-2/T
_LANG_2_TO_3 = {
    "pt": "por", "en": "eng", "es": "spa", "fr": "fra", "de": "deu",
    "it": "ita", "ja": "jpn", "zh": "zho", "ko": "kor", "ru": "rus",
    "ar": "ara", "hi": "hin", "nl": "nld", "sv": "swe", "no": "nor",
    "da": "dan", "fi": "fin", "pl": "pol", "tr": "tur", "uk": "ukr",
    "th": "tha", "vi": "vie", "id": "ind",
}

# 639-2/B (bibliographic) -> 639-2/T (terminological).
# ffprobe / mkvmerge sometimes emit /B variants.
_LANG_B_TO_T = {
    "fre": "fra", "ger": "deu", "dut": "nld", "rum": "ron",
    "scc": "srp", "scr": "hrv", "wel": "cym", "ice": "isl",
    "alb": "sqi", "arm": "hye", "geo": "kat", "per": "fas",
    "may": "msa", "bur": "mya", "tib": "bod", "cze": "ces",
    "slo": "slk", "mac": "mkd", "chi": "zho", "baq": "eus",
    "gre": "ell",
}


def normalize(code: str | None) -> str:
    """Return canonical ISO 639-2/T form (lowercase, 3 chars when possible).

    Empty input -> empty string.
    """
    if not code:
        return ""
    s = str(code).strip().lower()
    # strip BCP 47 region: "pt-br" -> "pt"
    base = s.split("-", 1)[0]
    if len(base) == 2:
        return _LANG_2_TO_3.get(base, base)
    if len(base) == 3:
        return _LANG_B_TO_T.get(base, base)
    return base


def lang_matches(a: str | None, b: str | None) -> bool:
    """True when two codes denote the same language across 639-1/2 forms."""
    return normalize(a) == normalize(b) and normalize(a) != ""
