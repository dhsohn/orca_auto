#!/usr/bin/env python3
"""Fail when a bilingual Markdown pair (``X.md`` / ``X.ko.md``) drifts structurally.

Prose is free to differ between the English and Korean editions; structure is
not.  For every pair the gate compares, in document order:

* the heading levels (ATX ``#``..``######`` and setext underlines),
* the Markdown tables and the row count of each (delimiter row excluded),
* the fenced code blocks: fence language, line count and the code text with
  ``#`` comments removed (a translated comment is fine, a changed command is
  not); ``mermaid`` blocks additionally ignore quoted node labels,

and, order-independent, the set of relative link targets (``X.ko.md`` and
``X.md`` targets are treated as the same document) plus the number of in-page
``#anchor`` links (heading slugs differ per language, so anchors are counted,
not compared).

Standard library only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

KO_SUFFIX = ".ko.md"
EN_SUFFIX = ".md"
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)

FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*#*[ \t]*$")
SETEXT_UNDERLINE_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
TABLE_DELIMITER_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
BLOCK_PREFIX_RE = re.compile(r"^ {0,3}(?:[-+*>]|\d{1,9}[.)])(?:\s|$)")
INLINE_CODE_RE = re.compile(r"`+[^`]*`+")
LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^\s()<>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
HASH_COMMENT_RE = re.compile(r"(?:^|(?<=\s))#.*$")
MERMAID_LABEL_RE = re.compile(r'"[^"]*"')


@dataclass(frozen=True)
class Element:
    """One structural element; ``key`` is what must match across languages."""

    kind: str
    line: int
    key: tuple[object, ...]
    label: str

    def describe(self) -> str:
        return f"{self.kind} {self.label}".rstrip()


@dataclass
class Structure:
    path: Path
    elements: list[Element] = field(default_factory=list)
    relative_links: set[str] = field(default_factory=set)
    anchor_link_count: int = 0


def _normalize_link_target(target: str) -> str:
    return re.sub(r"\.ko\.md(?=$|#)", ".md", target)


def _heading_element(line_no: int, level: int, text: str) -> Element:
    shown = text.strip()
    if len(shown) > 60:
        shown = shown[:57] + "..."
    return Element("heading", line_no, (level,), f"level {level} {shown!r}")


def normalize_code_line(language: str, line: str) -> str:
    """Return the command part of one code line: comments (and mermaid labels) removed."""

    normalized = HASH_COMMENT_RE.sub("", line).rstrip()
    if language == "mermaid":
        normalized = MERMAID_LABEL_RE.sub('""', normalized)
    return normalized


def _code_element(line_no: int, language: str, code_lines: list[str]) -> Element:
    text = "\n".join(normalize_code_line(language, line) for line in code_lines)
    return Element(
        "code block",
        line_no,
        (language, len(code_lines), text),
        f"[{language or '-'}] ({len(code_lines)} lines)",
    )


def _table_element(line_no: int, row_lines: list[str]) -> Element:
    rows = sum(1 for row in row_lines if not TABLE_DELIMITER_RE.match(row))
    return Element("table", line_no, (rows,), f"({rows} rows)")


def parse_markdown(path: Path) -> Structure:
    """Return the structural elements and relative links of one Markdown file."""

    structure = Structure(path)
    lines = path.read_text(encoding="utf-8").splitlines()

    fence: tuple[str, int, str, int] | None = None  # char, length, language, start line
    code_lines: list[str] = []
    table_start = 0
    table_rows: list[str] = []
    previous_paragraph_line: int | None = None

    def flush_table() -> None:
        if table_rows:
            structure.elements.append(_table_element(table_start, list(table_rows)))
            table_rows.clear()

    for index, line in enumerate(lines, start=1):
        if fence is not None:
            fence_char, fence_length, language, start = fence
            match = FENCE_RE.match(line)
            if (
                match is not None
                and match.group(1)[0] == fence_char
                and len(match.group(1)) >= fence_length
                and not match.group(2).strip()
            ):
                structure.elements.append(_code_element(start, language, code_lines))
                fence = None
                code_lines = []
            else:
                code_lines.append(line)
            continue

        fence_match = FENCE_RE.match(line)
        if fence_match is not None and not (
            fence_match.group(1)[0] == "`" and "`" in fence_match.group(2)
        ):
            flush_table()
            info = fence_match.group(2).strip()
            language = info.split()[0] if info else ""
            fence = (fence_match.group(1)[0], len(fence_match.group(1)), language, index)
            previous_paragraph_line = None
            continue

        if line.lstrip().startswith("|"):
            if not table_rows:
                table_start = index
            table_rows.append(line)
            previous_paragraph_line = None
            continue
        flush_table()

        heading = ATX_HEADING_RE.match(line)
        if heading is not None:
            structure.elements.append(
                _heading_element(index, len(heading.group(1)), heading.group(2) or "")
            )
            previous_paragraph_line = None
            continue

        underline = SETEXT_UNDERLINE_RE.match(line)
        if underline is not None and previous_paragraph_line == index - 1:
            level = 1 if underline.group(1)[0] == "=" else 2
            structure.elements.append(_heading_element(index - 1, level, lines[index - 2]))
            previous_paragraph_line = None
            continue

        prose = INLINE_CODE_RE.sub("", line)
        for link in LINK_RE.finditer(prose):
            target = link.group(1)
            if target.startswith("#"):
                structure.anchor_link_count += 1
            elif not URL_SCHEME_RE.match(target) and not target.startswith("//"):
                structure.relative_links.add(_normalize_link_target(target))

        is_paragraph = bool(line.strip()) and BLOCK_PREFIX_RE.match(line) is None
        previous_paragraph_line = index if is_paragraph else None

    flush_table()
    if fence is not None:
        structure.elements.append(_code_element(fence[3], fence[2], code_lines))
    return structure


def _first_code_difference(left: Element, right: Element) -> str:
    left_lines = str(left.key[2]).split("\n")
    right_lines = str(right.key[2]).split("\n")
    for offset, (a, b) in enumerate(zip(left_lines, right_lines, strict=False), start=1):
        if a != b:
            return f"code line {offset} differs (comments ignored): {a!r} vs {b!r}"
    return f"code length differs: {len(left_lines)} vs {len(right_lines)} lines"


def compare_structures(left: Structure, right: Structure) -> list[str]:
    """Return human-readable mismatches between two parsed documents (empty = aligned)."""

    problems: list[str] = []
    left_name = left.path.name
    right_name = right.path.name

    for position, (a, b) in enumerate(zip(left.elements, right.elements, strict=False), start=1):
        if a.kind == b.kind and a.key == b.key:
            continue
        detail = ""
        if a.kind == b.kind == "code block" and a.key[0] == b.key[0]:
            detail = "; " + _first_code_difference(a, b)
        problems.append(
            f"element {position}: {left_name}:{a.line} {a.describe()} "
            f"vs {right_name}:{b.line} {b.describe()}{detail}"
        )
        break
    else:
        if len(left.elements) != len(right.elements):
            longer, longer_name = (
                (left, left_name)
                if len(left.elements) > len(right.elements)
                else (right, right_name)
            )
            shorter_count = min(len(left.elements), len(right.elements))
            extra = longer.elements[shorter_count]
            problems.append(
                f"element {shorter_count + 1}: {longer_name}:{extra.line} {extra.describe()} "
                f"has no counterpart ({len(left.elements)} vs {len(right.elements)} elements)"
            )

    missing = sorted(left.relative_links - right.relative_links)
    extra_links = sorted(right.relative_links - left.relative_links)
    if missing:
        problems.append(f"links only in {left_name}: {', '.join(missing)}")
    if extra_links:
        problems.append(f"links only in {right_name}: {', '.join(extra_links)}")
    if left.anchor_link_count != right.anchor_link_count:
        problems.append(
            f"in-page anchor links: {left_name} has {left.anchor_link_count}, "
            f"{right_name} has {right.anchor_link_count}"
        )
    return problems


def korean_sibling(path: Path) -> Path:
    return path.with_name(path.name[: -len(EN_SUFFIX)] + KO_SUFFIX)


def english_sibling(path: Path) -> Path:
    return path.with_name(path.name[: -len(KO_SUFFIX)] + EN_SUFFIX)


def _iter_korean_files(directory: Path) -> Iterator[Path]:
    for candidate in sorted(directory.rglob(f"*{KO_SUFFIX}")):
        relative_parts = candidate.relative_to(directory).parts[:-1]
        if any(part in EXCLUDED_DIR_NAMES or part.startswith(".") for part in relative_parts):
            continue
        if candidate.is_file():
            yield candidate


def discover_pairs(paths: Iterable[Path]) -> list[tuple[Path, Path]]:
    """Return ``(english, korean)`` pairs found under ``paths`` by naming convention."""

    pairs: dict[Path, Path] = {}
    for path in paths:
        if path.is_dir():
            for korean in _iter_korean_files(path):
                english = english_sibling(korean)
                if english.is_file():
                    pairs[english.resolve()] = korean.resolve()
            continue
        if not path.is_file():
            raise FileNotFoundError(f"not a file or directory: {path}")
        if path.name.endswith(KO_SUFFIX):
            english, korean = english_sibling(path), path
        elif path.name.endswith(EN_SUFFIX):
            english, korean = path, korean_sibling(path)
        else:
            raise ValueError(f"not a Markdown file: {path}")
        if not english.is_file() or not korean.is_file():
            raise FileNotFoundError(f"no bilingual sibling for {path} ({english} / {korean})")
        pairs[english.resolve()] = korean.resolve()
    return sorted(pairs.items())


def _display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _describe_listing(structure: Structure) -> list[str]:
    lines = [f"    {element.line:>4}: {element.describe()}" for element in structure.elements]
    if structure.relative_links:
        lines.append(f"    links: {', '.join(sorted(structure.relative_links))}")
    if structure.anchor_link_count:
        lines.append(f"    in-page anchors: {structure.anchor_link_count}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Markdown files or directories to check (default: the repository root).",
    )
    parser.add_argument(
        "--pairs",
        action="store_true",
        help="List the discovered bilingual pairs and exit without checking them.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print the structural listing of every checked file.",
    )
    args = parser.parse_args(argv)

    search_paths = [path.resolve() for path in args.paths] or [repo_root]
    root = repo_root if not args.paths else Path.cwd().resolve()
    try:
        pairs = discover_pairs(search_paths)
    except (FileNotFoundError, ValueError) as error:
        print(f"[docs-parity] ERROR: {error}", file=sys.stderr)
        return 2
    if not pairs:
        print("[docs-parity] ERROR: no X.md / X.ko.md pairs found", file=sys.stderr)
        return 2

    if args.pairs:
        for english, korean in pairs:
            print(f"{_display(english, root)} <-> {_display(korean, root)}")
        return 0

    failures = 0
    for english, korean in pairs:
        label = f"{_display(english, root)} <-> {_display(korean, root)}"
        left = parse_markdown(english)
        right = parse_markdown(korean)
        problems = compare_structures(left, right)
        if args.verbose:
            print(f"[docs-parity] {label}")
            print(f"  {left.path.name}:")
            print("\n".join(_describe_listing(left)))
            print(f"  {right.path.name}:")
            print("\n".join(_describe_listing(right)))
        if problems:
            failures += 1
            print(f"[docs-parity] MISMATCH {label}")
            for problem in problems:
                print(f"  {problem}")
        elif args.verbose:
            print("  OK")

    checked = len(pairs)
    if failures:
        print(f"[docs-parity] {failures} of {checked} pairs out of sync")
        return 1
    print(f"[docs-parity] {checked} pairs aligned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
