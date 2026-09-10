# Multilingual Alias and Transliteration Candidates (Sarthak Phase 1)

`app/modules/communication_processing/aliases/` provides conservative, deterministic utilities for comparing and transliterating name-like text across scripts. Every function is pure and total — no ML model, no external corpus, no network call, no fuzzy/edit-distance matching. Nothing here merges or asserts identity; see the policy section below and `docs/decisions/ADR-005-provenance-first-communication-processing.md`.

## Unicode normalization form

**NFC (Canonical Composition)** is the sole normalization form used throughout this module (`aliases/normalize.py`, `UNICODE_NORMALIZATION_FORM = "NFC"`). Chosen over NFD/NFKC/NFKD: NFC preserves the canonical precomposed form (matching how most real-world text — and most Devanagari/Gurmukhi input methods — already produce text) without the *compatibility* folding NFKC/NFKD apply, which can collapse meaningfully distinct characters (ligatures, width variants) into a shared target — too aggressive for a conservative comparison utility. `normalize_unicode` is idempotent: normalizing already-normalized text is a no-op.

## Whitespace normalization and comparison key

`normalize_whitespace` collapses internal whitespace runs to a single space and strips the ends. `comparison_key` composes NFC normalization, whitespace normalization, and Unicode case-folding into one stable key, used only for **exact** comparison after normalization (the `exact_normalized` candidate category) — never a fuzzy or similarity match.

## Safe tokenization

`safe_tokenize` NFC-normalizes then splits on `str.split()` with no argument, which splits on any Unicode whitespace run and discards empty tokens. No locale-specific word segmentation, no external tokenizer library.

## Script detection

`aliases/scripts.py`'s `detect_script` classifies a string's *letter* characters only (digits, punctuation, and whitespace carry no script information and are ignored) by fixed Unicode code-point range:

| Script | Range |
|---|---|
| `LATIN` | Basic Latin (`U+0041–005A`, `U+0061–007A`) + Latin-1 Supplement + Latin Extended-A/B (`U+00C0–024F`) |
| `DEVANAGARI` | `U+0900–097F` |
| `GURMUKHI` | `U+0A00–0A7F` |

`UNKNOWN` is returned when there are no recognized-script letters at all (pure digits/punctuation, or a script this module doesn't support, e.g. Cyrillic/Han/Arabic). `MIXED` is returned when letters from more than one script (recognized, unrecognized, or both) are present — this is a deliberately conservative signal: mixed-script text is not silently attributed to whichever script happens to dominate.

## Transliteration algorithm

Devanagari and Gurmukhi are abugidas: a consonant carries an inherent "a" vowel unless a following vowel sign (matra) replaces it, or a virama/halant suppresses it entirely (consonant-cluster boundary). `aliases/transliteration._transliterate_indic` implements exactly this rule with three small, fully-documented, fully-tested character tables per script (independent vowels, consonants, dependent vowel signs) plus a virama and nasalization marks — not a flat per-character substitution, which would get consonant+vowel-sign combinations wrong.

The instant a character in the token isn't in one of these tables, the **whole token** is reported unmappable and the function returns `None` — this module never produces a partial or best-guess transliteration. The tables are intentionally small (covering common consonants, independent vowels, and vowel signs sufficient for typical names) and are exactly what `tests/unit/communication_processing/test_aliases_transliteration.py` exercises — every mapping in the table is tested, and nothing is claimed to work beyond what's tested.

Worked examples (also unit-tested):

- Devanagari राम (RA + AA-matra + MA) → `"raama"`
- Devanagari सिंह (SA + I-matra + anusvara + HA) → `"sinha"`
- Gurmukhi ਰਾਮ (RA + AA-matra + MA) → `"raama"`

## Candidate categories

Every call to `generate_candidates(text)` returns exactly one `TransliterationCandidate` with a `category`:

- **`exact_normalized`** — only the NFC + whitespace + case-fold comparison key is offered. Used for Latin-script text, and for any text excluded from transliteration (see below).
- **`deterministic_transliteration`** — the text's script is fully covered by the Devanagari or Gurmukhi character table; both the exact-normalized key and the transliterated form are offered.
- **`not_generated`** — nothing usable could be produced (empty text, an unsupported/mixed script, or a Devanagari/Gurmukhi token containing a character outside the small documented table). `reason` explains why.

Every candidate also carries `method`/`method_version` (`"communication_processing.aliases.transliteration"`, `"1.0.0"`), so a consumer always knows exactly which deterministic logic (and version of it) produced a given candidate.

## Never transliterated as names

`_looks_like_non_name` excludes a token from transliteration (though it may still get an `exact_normalized` candidate) when it:

- matches an email pattern (`local@domain.tld`);
- starts with `http://`, `https://`, or `www.`;
- starts with `@` or `#` (a handle/hashtag);
- looks like a phone number (mostly digits, 6+ digits, optionally with `+`/`-`/spaces/parens);
- contains **any** digit at all — a name token doesn't contain a digit, which conservatively also excludes account numbers and other alphanumeric IDs.

## Identity-safety policy

- A `TransliterationCandidate` is never proof that two records refer to one person — it is a normalized/transliterated *spelling* only.
- This module never auto-merges or creates an `EntityV1` from a candidate (statically verified: no file under `aliases/` imports `EntityV1`, see `tests/unit/communication_processing/test_module_safety.py`).
- Candidates are meant to be surfaced to a human reviewer alongside their `method`/`category`/`reason` — never applied automatically.
- No ML transliteration, no fuzzy/edit-distance matching, no external transliteration API or corpus is used anywhere in this module.
