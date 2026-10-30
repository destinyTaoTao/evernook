"""Versioned lexical normalization; original text and code point coordinates stay authoritative."""
from collections.abc import Iterator
import unicodedata

from .models import SearchSpan


NORMALIZATION_VERSION = 1


def _pieces(text: str) -> Iterator[tuple[str, int, int]]:
    start = 0
    for position, char in enumerate(text):
        if position and not unicodedata.combining(char):
            yield unicodedata.normalize("NFKD", text[start:position]).casefold(), start, position
            start = position
    if text:
        yield unicodedata.normalize("NFKD", text[start:]).casefold(), start, len(text)


def normalize(text: str) -> str:
    return "".join(piece for piece, _, _ in _pieces(text))


def matching_spans(text: str, words: tuple[str, ...]) -> tuple[SearchSpan, ...]:
    pieces: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for piece, start, end in _pieces(text):
        pieces.append(piece)
        starts.extend([start] * len(piece))
        ends.extend([end] * len(piece))
    normalized = "".join(pieces)
    matches: dict[tuple[int, int], SearchSpan] = {}
    for word in words:
        position = normalized.find(word)
        while position >= 0:
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
