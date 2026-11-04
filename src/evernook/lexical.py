"""Versioned lexical normalization; original text and code point coordinates stay authoritative."""
from collections.abc import Callable, Iterator
import unicodedata

from .models import SearchSpan


NORMALIZATION_VERSION = 1

# NFKD/casefold output depends on the interpreter's Unicode database. The index identity must pin
# it so an interpreter upgrade surfaces as `behind` (healed by a rebuild) instead of silently
# desynchronizing stored grams from freshly computed queries.
NORMALIZATION_IDENTITY = NORMALIZATION_VERSION * 1_000_000 + int(unicodedata.unidata_version.replace(".", ""))


def _normalized_piece(text: str, check: Callable[[], object] | None) -> str:
    if len(text) <= 1024:
        return unicodedata.normalize("NFKD", text).casefold()
    # Stable canonical ordering avoids an uninterruptible quadratic reorder of a long mark run.
    output: list[str] = []
    marks: dict[int, list[str]] = {}
    def flush() -> None:
        for key in sorted(marks):
            output.extend(marks[key])
        marks.clear()
    for position, char in enumerate(text):
        if check is not None and position % 1024 == 0:
            check()
        for decomposed in unicodedata.normalize("NFKD", char):
            combining = unicodedata.combining(decomposed)
            if combining:
                marks.setdefault(combining, []).append(decomposed)
            else:
                flush()
                output.append(decomposed)
    flush()
    if check is not None:
        check()
    return "".join(output).casefold()


def _pieces(text: str, check: Callable[[], object] | None = None) -> Iterator[tuple[str, int, int]]:
    start = 0
    for position, char in enumerate(text):
        if check is not None and position % 1024 == 0:
            check()
        if position and not unicodedata.combining(char):
            yield _normalized_piece(text[start:position], check), start, position
            start = position
    if text:
        yield _normalized_piece(text[start:], check), start, len(text)


def normalize(text: str) -> str:
    return "".join(piece for piece, _, _ in _pieces(text))


def matching_spans(text: str, words: tuple[str, ...], *,
                   check: Callable[[], object] | None = None) -> tuple[SearchSpan, ...]:
    pieces: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for piece, start, end in _pieces(text, check):
        pieces.append(piece)
        starts.extend([start] * len(piece))
        ends.extend([end] * len(piece))
    normalized = "".join(pieces)
    matches: dict[tuple[int, int], SearchSpan] = {}
    for word in words:
        if check is not None:
            check()
        position = normalized.find(word)
        while position >= 0:
            if check is not None:
                check()
            start, end = starts[position], ends[position + len(word) - 1]
            original = text[start:end]
            if normalize(original) == word:
                matches[start, end] = SearchSpan(start, end, original)
                break
            position = normalized.find(word, position + 1)
        if position < 0:
            return ()
    return tuple(matches[key] for key in sorted(matches))


def _token(text: str) -> str:
    return "x" + text.encode("utf-8").hex()


def index_terms(text: str) -> str:
    normalized = normalize(text)
    terms = {_token(normalized[position:position + size])
             for position in range(len(normalized)) for size in (1, 2, 3) if position + size <= len(normalized)}
    return " ".join(sorted(terms))


def query_expression(words: tuple[str, ...]) -> str:
    terms: set[str] = set()
    for word in words:
        size = min(3, len(word))
        terms.update(_token(word[position:position + size]) for position in range(len(word) - size + 1))
    return " AND ".join(f'"{term}"' for term in sorted(terms))
