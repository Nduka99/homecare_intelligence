"""Find a model's quote in the note it came from, or report that it is not there.

Adapted from PetroKnowledge `verify.py` (`folded`, `quote_pattern`, `find_quote`; MIT, same
author). Its table-cell handling is left out, as notes have no tables. A quote matches when the
note holds the same words in the same order: case, typographic forms (curly quotes, dashes) and
Unicode compatibility forms may differ, and so may line breaks and the spacing around
punctuation. A comma, semicolon, colon or full stop may be added or dropped between words or at
the quote's end. Between letters, or digits, spacing must be the quote's own ("therapist" is not
"the rapist"), and a quote must start and end at word edges ("3 to 6" does not match "3 to 60").
"""

import re
import unicodedata
from collections.abc import Sequence

NUMBER_GAP = r"\s+"
WORD_GAP = r"[,;:.]?\s+"
# Where a digit meets a non-digit, or at punctuation, spacing is optional; a mark the quote
# leaves out may stand there too, when spacing follows it.
OPTIONAL_GAP = r"\s*(?:[,;:.](?=\s)\s*)?"
SOFT_MARKS = {",", ";", ":", "."}
TYPOGRAPHY = str.maketrans(
    dict.fromkeys("‘’‚′", "'") | dict.fromkeys("“”„″", '"') | dict.fromkeys("–—−‐‑", "-")
)
# A number with its decimal point or comma is one unit ("1.5" never matches "1. 5"); any other
# character is its own unit.
UNIT = re.compile(r"\d+(?:[.,]\d+)*|\S")


def folded(text: str) -> tuple[str, tuple[int, ...]]:
    """The text with typographic and compatibility forms folded (NFKC), and each folded
    character's position in the original. Invisible format characters are left out."""
    chars = []
    where = []
    for index, char in enumerate(text):
        if unicodedata.category(char) == "Cf":
            continue
        for piece in unicodedata.normalize("NFKC", char.translate(TYPOGRAPHY)).translate(
            TYPOGRAPHY
        ):
            chars.append(piece)
            where.append(index)
    return "".join(chars), tuple(where)


def same_kind(before: str, after: str) -> bool:
    letters = before[-1].isalpha() and after[0].isalpha()
    return letters or (before[-1].isdigit() and after[0].isdigit())


def unit_pattern(units: Sequence[re.Match[str]], index: int) -> str:
    """One quoted unit: itself, or for a soft mark not between two digits, the mark or nothing."""
    text = units[index].group()
    inside = 0 < index < len(units) - 1
    last = 0 < index == len(units) - 1
    if text not in SOFT_MARKS or not (inside or last):
        return re.escape(text)
    if inside:
        before, after = units[index - 1].group(), units[index + 1].group()
        if before[-1].isdigit() and after[0].isdigit():
            return re.escape(text)
    return f"(?:{re.escape(text)})?"


def quote_pattern(quote: str) -> str:
    units = list(UNIT.finditer(folded(quote)[0]))
    parts = []
    for index, unit in enumerate(units):
        if index:
            previous = units[index - 1].group()
            spaced = unit.start() > units[index - 1].end()
            if previous[-1].isdigit() and unit.group()[0].isdigit():
                parts.append(NUMBER_GAP if spaced else "")
            elif same_kind(previous, unit.group()):
                parts.append(WORD_GAP if spaced else "")
            else:
                parts.append(OPTIONAL_GAP)
        parts.append(unit_pattern(units, index))
    return r"(?<!\w)" + "".join(parts) + r"(?!\w)"


def locate(quote: str, text: str) -> tuple[int, int] | None:
    """Where the text holds `quote` (start, end), or None if it does not."""
    if not quote.split():
        return None
    text_folded, where = folded(text)
    match = re.search(quote_pattern(quote), text_folded, re.IGNORECASE)
    if match is None:
        return None
    return where[match.start()], where[match.end() - 1] + 1


def find_quote(quote: str, text: str) -> str | None:
    """The text's own wording of `quote`, or None if the text does not contain it."""
    span = locate(quote, text)
    return None if span is None else text[span[0] : span[1]]


# The spelling-tolerant match, for a quote whose model silently corrected a typo or joined two
# list lines. It is stricter than fuzzy alignment (Google's LangExtract accepts a span when 75%
# of the quote's tokens match): every word must be there, in order, with nothing skipped.
WORD_TOKEN = re.compile(r"[^\W_]+")
LOOSE_MIN_LETTERS = 5  # shorter words carry meaning in one letter: "not", "now", "need", "feed"
WORDS_PER_LOOSE = 4  # at most one loosely matched word in every four quoted words


def one_edit_apart(a: str, b: str) -> bool:
    """Whether `b` is `a` with one letter changed, added or dropped, or two neighbours swapped."""
    if abs(len(a) - len(b)) > 1:
        return False
    first = 0
    while first < min(len(a), len(b)) and a[first] == b[first]:
        first += 1
    if len(a) == len(b):
        changed = a[first + 1 :] == b[first + 1 :]
        swapped = a[first : first + 2] == b[first : first + 2][::-1]
        return changed or (swapped and a[first + 2 :] == b[first + 2 :])
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    return longer[first + 1 :] == shorter[first:]


def loosely_equal(wanted: str, got: str) -> bool:
    """Whether two different words may count as one: letters only, both long, one edit apart."""
    long_enough = min(len(wanted), len(got)) >= LOOSE_MIN_LETTERS
    letters = wanted.isalpha() and got.isalpha()
    return long_enough and letters and one_edit_apart(wanted, got)


def find_close_quote(quote: str, text: str) -> str | None:
    """The text's own wording of `quote` when the text holds the same words in the same order,
    side by side, with spacing, punctuation and list markers between them ignored, and at most
    one word in four spelled one edit differently (words of five or more letters only; numbers
    and shorter words must be exact). None otherwise. Use it only after `find_quote` fails, and
    mark what it finds: it is evidence a person should read, not proof of the exact words."""
    wanted = [word.lower() for word in WORD_TOKEN.findall(folded(quote)[0])]
    if not wanted:
        return None
    allowed = len(wanted) // WORDS_PER_LOOSE
    text_folded, where = folded(text)
    words = list(WORD_TOKEN.finditer(text_folded))
    lowered = [word.group().lower() for word in words]
    for start in range(len(words) - len(wanted) + 1):
        loose = 0
        for offset, word in enumerate(wanted):
            got = lowered[start + offset]
            if got == word:
                continue
            if not loosely_equal(word, got) or loose == allowed:
                break
            loose += 1
        else:
            first, last = words[start], words[start + len(wanted) - 1]
            return text[where[first.start()] : where[last.end() - 1] + 1]
    return None
