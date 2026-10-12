"""Turn an uploaded document into a list of sentences that sound good read aloud."""

import re
from collections import Counter
from pathlib import Path

from markitdown import MarkItDown

_markitdown = MarkItDown()

MAX_CHUNK_CHARS = 300  # longer sentences are split so audio starts quickly

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e",
    "fig", "no", "vol", "approx", "inc", "ltd", "co", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}

_PAGE_NUMBER = re.compile(r"(page\s*)?\d+(\s*(of|/)\s*\d+)?", re.IGNORECASE)
_TABLE_SEPARATOR = re.compile(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3})(?=\S)(.+?)(?<=\S)\1")
_LIST_MARKER = re.compile(r"^(\s*([-*+•]|\d+[.)])\s+)")
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[\"'(\[]?[A-Z0-9])")


def extract_markdown(path: Path) -> str:
    return _markitdown.convert(str(path)).text_content


def document_to_sentences(path: Path) -> list[str]:
    return split_sentences(clean_for_speech(extract_markdown(path)))


def clean_for_speech(markdown: str) -> str:
    """Strip markup and layout noise, returning paragraphs separated by blank lines."""
    repeated = _running_headers(markdown)
    lines = [line.strip() for line in markdown.splitlines()]

    paragraphs: list[str] = []
    current: list[str] = []
    in_code = False

    def flush() -> None:
        if current:
            paragraphs.append(" ".join(current))
            current.clear()

    for line in lines:
        if line.startswith("```"):
            if not in_code:
                flush()
                paragraphs.append("Code block omitted.")
            in_code = not in_code
            continue
        if in_code:
            continue
        if not line:
            if not (current and current[-1].endswith("-")):  # PDFs often break mid-word
                flush()
            continue
        if line in repeated or _PAGE_NUMBER.fullmatch(line) or _TABLE_SEPARATOR.fullmatch(line):
            continue

        standalone = False
        if line.startswith("#"):
            line = line.lstrip("#").strip()
            standalone = True
        elif line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            line = ", ".join(c for c in cells if c)
            standalone = True
        elif _LIST_MARKER.match(line):
            line = _LIST_MARKER.sub("", line)
            standalone = True
        line = line.lstrip("> ").strip()

        line = _IMAGE.sub(r"\1", line)
        line = _LINK.sub(r"\1", line)
        line = _URL.sub("", line)
        line = _EMPHASIS.sub(r"\2", line)
        line = line.replace("`", "")
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue

        if standalone:
            flush()
            if line[-1] not in ".!?:;":
                line += "."  # gives the voice a natural pause after headings/list items
            paragraphs.append(line)
        elif current and current[-1].endswith("-") and line[:1].islower():
            current[-1] = current[-1][:-1] + line  # re-join words hyphenated across line breaks
        else:
            current.append(line)

    flush()
    return "\n\n".join(paragraphs)


def _running_headers(markdown: str, edge_lines: int = 2) -> set[str]:
    """Lines repeated at the top/bottom of most pages (PDF pages are split by form feeds)."""
    pages = markdown.split("\f")
    if len(pages) < 3:
        return set()
    counts: Counter[str] = Counter()
    for page in pages:
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        edges = set(lines[:edge_lines] + lines[-edge_lines:])
        counts.update(line for line in edges if len(line) < 80 and not _PAGE_NUMBER.fullmatch(line))
    threshold = max(3, len(pages) // 2)
    return {line for line, n in counts.items() if n >= threshold}


def split_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for paragraph in text.split("\n\n"):
        pieces = _SENTENCE_END.split(paragraph.strip())
        merged: list[str] = []
        for piece in pieces:
            if merged and _ends_with_abbreviation(merged[-1]):
                merged[-1] = f"{merged[-1]} {piece}"
            else:
                merged.append(piece)
        for sentence in merged:
            sentences.extend(_chunk(sentence))
    return [s for s in sentences if re.search(r"\w", s)]


def _ends_with_abbreviation(text: str) -> bool:
    last_word = text.rsplit(" ", 1)[-1].rstrip(".").lower()
    return last_word in _ABBREVIATIONS or (len(last_word) == 1 and last_word.isalpha())


def _chunk(sentence: str) -> list[str]:
    if len(sentence) <= MAX_CHUNK_CHARS:
        return [sentence]
    chunks: list[str] = []
    current = ""
    for part in re.split(r"(?<=[,;:])\s+", sentence):
        if current and len(current) + len(part) + 1 > MAX_CHUNK_CHARS:
            chunks.append(current)
            current = part
        else:
            current = f"{current} {part}".strip()
    if current:
        chunks.append(current)
    # Fall back to hard word-boundary splits for run-ons with no punctuation.
    result: list[str] = []
    for chunk in chunks:
        while len(chunk) > MAX_CHUNK_CHARS:
            cut = chunk.rfind(" ", 0, MAX_CHUNK_CHARS)
            cut = cut if cut > 0 else MAX_CHUNK_CHARS
            result.append(chunk[:cut])
            chunk = chunk[cut:].strip()
        result.append(chunk)
    return result
