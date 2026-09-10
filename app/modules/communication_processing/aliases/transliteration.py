"""Deterministic transliteration candidates for Devanagari and Gurmukhi tokens.

No ML transliteration, no fuzzy matching, no external corpora or APIs —
every mapping below is a fixed, fully-documented, fully-tested character
table. The algorithm is a small, explicit implementation of the
consonant-carries-an-inherent-vowel rule these two abugida scripts share
(a consonant is read with an implicit "a" *unless* immediately followed by
a vowel sign, which replaces it, or a virama/halant, which suppresses it
entirely) — not a flat per-character substitution, which would get this
wrong. The instant a character isn't covered by the table below, the whole
token is reported as `not_generated`: this module never invents a partial
or best-guess transliteration.

Phone numbers, email addresses, URLs, handles, and any token containing a
digit are never transliterated as names (`_looks_like_non_name`) — an
`exact_normalized` candidate is still produced for such tokens (safe,
non-interpretive), but never a `deterministic_transliteration` one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.modules.communication_processing.aliases.normalize import comparison_key, normalize_unicode
from app.modules.communication_processing.aliases.scripts import Script, detect_script
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.limits import MAX_ALIAS_TEXT_LENGTH, MAX_ALIAS_TOKENS

METHOD_NAME = "communication_processing.aliases.transliteration"
METHOD_VERSION = "1.0.0"

CATEGORY_EXACT_NORMALIZED = "exact_normalized"
CATEGORY_DETERMINISTIC_TRANSLITERATION = "deterministic_transliteration"
CATEGORY_NOT_GENERATED = "not_generated"

# --- Devanagari (U+0900-U+097F) ---
_DEVANAGARI_INDEPENDENT_VOWELS: dict[str, str] = {
    "अ": "a",  # अ A
    "आ": "aa",  # आ AA
    "इ": "i",  # इ I
    "ई": "ii",  # ई II
    "उ": "u",  # उ U
    "ऊ": "uu",  # ऊ UU
    "ए": "e",  # ए E
    "ऐ": "ai",  # ऐ AI
    "ओ": "o",  # ओ O
    "औ": "au",  # औ AU
}
_DEVANAGARI_CONSONANTS: dict[str, str] = {
    "क": "k",  # क KA
    "ख": "kh",  # ख KHA
    "ग": "g",  # ग GA
    "घ": "gh",  # घ GHA
    "ङ": "ng",  # ङ NGA
    "च": "ch",  # च CA
    "छ": "chh",  # छ CHA
    "ज": "j",  # ज JA
    "झ": "jh",  # झ JHA
    "ञ": "ny",  # ञ NYA
    "ट": "t",  # ट TTA
    "ठ": "th",  # ठ TTHA
    "ड": "d",  # ड DDA
    "ढ": "dh",  # ढ DDHA
    "ण": "n",  # ण NNA
    "त": "t",  # त TA
    "थ": "th",  # थ THA
    "द": "d",  # द DA
    "ध": "dh",  # ध DHA
    "न": "n",  # न NA
    "प": "p",  # प PA
    "फ": "ph",  # फ PHA
    "ब": "b",  # ब BA
    "भ": "bh",  # भ BHA
    "म": "m",  # म MA
    "य": "y",  # य YA
    "र": "r",  # र RA
    "ल": "l",  # ल LA
    "व": "v",  # व VA
    "श": "sh",  # श SHA
    "ष": "sh",  # ष SSA
    "स": "s",  # स SA
    "ह": "h",  # ह HA
}
_DEVANAGARI_MATRAS: dict[str, str] = {
    "ा": "aa",  # ा AA
    "ि": "i",  # ि I
    "ी": "ii",  # ी II
    "ु": "u",  # ु U
    "ू": "uu",  # ू UU
    "े": "e",  # े E
    "ै": "ai",  # ै AI
    "ो": "o",  # ो O
    "ौ": "au",  # ौ AU
}
_DEVANAGARI_VIRAMA = "्"  # ् (suppresses the inherent vowel)
_DEVANAGARI_NASALS = frozenset({"ं"})  # ं anusvara

# --- Gurmukhi (U+0A00-U+0A7F) ---
_GURMUKHI_INDEPENDENT_VOWELS: dict[str, str] = {
    "ਅ": "a",  # ਅ A
    "ਆ": "aa",  # ਆ AA
    "ਇ": "i",  # ਇ I
    "ਈ": "ii",  # ਈ II
    "ਉ": "u",  # ਉ U
    "ਊ": "uu",  # ਊ UU
    "ਏ": "e",  # ਏ EE
    "ਐ": "ai",  # ਐ AI
    "ਓ": "o",  # ਓ OO
    "ਔ": "au",  # ਔ AU
}
_GURMUKHI_CONSONANTS: dict[str, str] = {
    "ਕ": "k",  # ਕ KA
    "ਖ": "kh",  # ਖ KHA
    "ਗ": "g",  # ਗ GA
    "ਘ": "gh",  # ਘ GHA
    "ਙ": "ng",  # ਙ NGA
    "ਚ": "ch",  # ਚ CA
    "ਛ": "chh",  # ਛ CHA
    "ਜ": "j",  # ਜ JA
    "ਝ": "jh",  # ਝ JHA
    "ਞ": "ny",  # ਞ NYA
    "ਟ": "t",  # ਟ TTA
    "ਠ": "th",  # ਠ TTHA
    "ਡ": "d",  # ਡ DDA
    "ਢ": "dh",  # ਢ DDHA
    "ਣ": "n",  # ਣ NNA
    "ਤ": "t",  # ਤ TA
    "ਥ": "th",  # ਥ THA
    "ਦ": "d",  # ਦ DA
    "ਧ": "dh",  # ਧ DHA
    "ਨ": "n",  # ਨ NA
    "ਪ": "p",  # ਪ PA
    "ਫ": "ph",  # ਫ PHA
    "ਬ": "b",  # ਬ BA
    "ਭ": "bh",  # ਭ BHA
    "ਮ": "m",  # ਮ MA
    "ਯ": "y",  # ਯ YA
    "ਰ": "r",  # ਰ RA
    "ਲ": "l",  # ਲ LA
    "ਵ": "v",  # ਵ VA
    "ਸ": "s",  # ਸ SA
    "ਹ": "h",  # ਹ HA
}
_GURMUKHI_MATRAS: dict[str, str] = {
    "ਾ": "aa",  # ਾ AA
    "ਿ": "i",  # ਿ I
    "ੀ": "ii",  # ੀ II
    "ੁ": "u",  # ੁ U
    "ੂ": "uu",  # ੂ UU
    "ੇ": "e",  # ੇ EE
    "ੈ": "ai",  # ੈ AI
    "ੋ": "o",  # ੋ OO
    "ੌ": "au",  # ੌ AU
}
_GURMUKHI_VIRAMA = "੍"  # ੍ (halant, suppresses the inherent vowel)
_GURMUKHI_NASALS = frozenset({"ਂ", "ੰ"})  # ਂ bindi, ੰ tippi

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_PATTERN = re.compile(r"^(https?://|www\.)", re.IGNORECASE)
_HANDLE_PATTERN = re.compile(r"^[@#]")
_PHONE_LIKE_PATTERN = re.compile(r"^[+]?[\d][\d\-\s()]{5,}$")


@dataclass(frozen=True)
class TransliterationCandidate:
    """A deterministic, review-only alias candidate.

    Never proof that two records refer to one person — see `docs/
    architecture/multilingual-alias-candidates-v1.md`. `candidates` may be
    empty (nothing usable was generated); `category` explains why.
    """

    original_text: str
    script: Script
    candidates: tuple[str, ...]
    method: str
    method_version: str
    category: str
    reason: str | None = None


def _transliterate_indic(
    token: str,
    *,
    independent_vowels: dict[str, str],
    consonants: dict[str, str],
    matras: dict[str, str],
    virama: str,
    nasal_marks: frozenset[str],
) -> str | None:
    """Deterministically transliterate one token, or return `None`.

    Algorithm: an independent vowel maps directly; a consonant carries an
    inherent "a" unless the next character is a matra (which replaces it)
    or a virama (which suppresses it, consonant-cluster boundary); a nasal
    mark appends "n". The instant a character matches none of these, the
    whole token is unmappable and `None` is returned — never a partial or
    guessed result.
    """
    result: list[str] = []
    index = 0
    length = len(token)
    while index < length:
        ch = token[index]
        if ch in independent_vowels:
            result.append(independent_vowels[ch])
            index += 1
        elif ch in consonants:
            base = consonants[ch]
            next_ch = token[index + 1] if index + 1 < length else ""
            if next_ch == virama:
                result.append(base)
                index += 2
            elif next_ch in matras:
                result.append(base)
                result.append(matras[next_ch])
                index += 2
            else:
                result.append(base)
                result.append("a")
                index += 1
        elif ch in nasal_marks:
            result.append("n")
            index += 1
        else:
            return None
    return "".join(result)


def transliterate_devanagari_token(token: str) -> str | None:
    return _transliterate_indic(
        token,
        independent_vowels=_DEVANAGARI_INDEPENDENT_VOWELS,
        consonants=_DEVANAGARI_CONSONANTS,
        matras=_DEVANAGARI_MATRAS,
        virama=_DEVANAGARI_VIRAMA,
        nasal_marks=_DEVANAGARI_NASALS,
    )


def transliterate_gurmukhi_token(token: str) -> str | None:
    return _transliterate_indic(
        token,
        independent_vowels=_GURMUKHI_INDEPENDENT_VOWELS,
        consonants=_GURMUKHI_CONSONANTS,
        matras=_GURMUKHI_MATRAS,
        virama=_GURMUKHI_VIRAMA,
        nasal_marks=_GURMUKHI_NASALS,
    )


def _looks_like_non_name(token: str) -> bool:
    """Phone numbers, emails, URLs, handles, and digit-bearing IDs are never
    transliterated as names — a name token doesn't contain a digit."""
    if _EMAIL_PATTERN.match(token) or _URL_PATTERN.match(token) or _HANDLE_PATTERN.match(token):
        return True
    if _PHONE_LIKE_PATTERN.match(token):
        return True
    return any(ch.isdigit() for ch in token)


def generate_candidates(text: str) -> TransliterationCandidate:
    """Generate alias candidates for one alias/token.

    Always includes an `exact_normalized` candidate (the NFC + whitespace
    + case-fold comparison key) when there's any text at all. Adds a
    `deterministic_transliteration` candidate only for a Devanagari- or
    Gurmukhi-script token that the fixed character table fully covers, and
    never for a token that looks like a phone/email/handle/URL/ID.
    """
    if len(text) > MAX_ALIAS_TEXT_LENGTH:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED, f"alias text exceeds {MAX_ALIAS_TEXT_LENGTH} characters"
        )

    normalized = normalize_unicode(text)
    stripped = normalized.strip()
    script = detect_script(normalized)
    key = comparison_key(normalized)
    exact = (key,) if key else ()

    if not stripped:
        return TransliterationCandidate(
            original_text=text,
            script=script,
            candidates=(),
            method=METHOD_NAME,
            method_version=METHOD_VERSION,
            category=CATEGORY_NOT_GENERATED,
            reason="empty after normalization",
        )

    if _looks_like_non_name(stripped):
        return TransliterationCandidate(
            original_text=text,
            script=script,
            candidates=exact,
            method=METHOD_NAME,
            method_version=METHOD_VERSION,
            category=CATEGORY_EXACT_NORMALIZED,
            reason="excluded from transliteration: looks like a phone/email/handle/URL/ID",
        )

    transliterated: str | None = None
    if script is Script.DEVANAGARI:
        transliterated = transliterate_devanagari_token(stripped)
    elif script is Script.GURMUKHI:
        transliterated = transliterate_gurmukhi_token(stripped)

    if transliterated:
        candidates = tuple(dict.fromkeys((*exact, transliterated)))
        return TransliterationCandidate(
            original_text=text,
            script=script,
            candidates=candidates,
            method=METHOD_NAME,
            method_version=METHOD_VERSION,
            category=CATEGORY_DETERMINISTIC_TRANSLITERATION,
        )

    if script is Script.LATIN:
        return TransliterationCandidate(
            original_text=text,
            script=script,
            candidates=exact,
            method=METHOD_NAME,
            method_version=METHOD_VERSION,
            category=CATEGORY_EXACT_NORMALIZED,
        )

    return TransliterationCandidate(
        original_text=text,
        script=script,
        candidates=exact,
        method=METHOD_NAME,
        method_version=METHOD_VERSION,
        category=CATEGORY_NOT_GENERATED,
        reason=f"no deterministic transliteration available for script={script.value}",
    )


def generate_candidates_for_tokens(tokens: list[str]) -> list[TransliterationCandidate]:
    """Batch wrapper over `generate_candidates`, bounded by `MAX_ALIAS_TOKENS`."""
    if len(tokens) > MAX_ALIAS_TOKENS:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED, f"more than {MAX_ALIAS_TOKENS} tokens supplied"
        )
    return [generate_candidates(token) for token in tokens]
