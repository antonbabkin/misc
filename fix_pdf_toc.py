"""Clean noisy PDF bookmark trees while preserving working destinations.

This script rewrites a PDF's outline to remove synthetic top-level wrapper
entries such as `680_c6424_c001`, along with redundant `Table of Contents`
and `References` bookmarks. It reads the existing outline with pypdf,
converts it into an in-memory tree, drops unwanted nodes, and then writes a
new PDF whose bookmarks point back to the original destination pages.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path
from collections import Counter
from typing import Iterable

from pypdf import PdfReader, PdfWriter
from pypdf.generic import Destination, Fit


WRAPPER_TITLE_RE = re.compile(r"^\d+_[A-Za-z0-9]+_c\d+$")
TABLE_OF_CONTENTS_TITLE = "Table of Contents"
REFERENCES_TITLE = "References"


@dataclass
class OutlineNode:
    title: str
    page_number: int | None
    fit: Fit
    color: tuple[float, float, float] | None = None
    bold: bool = False
    italic: bool = False
    is_open: bool = True
    children: list["OutlineNode"] = field(default_factory=list)


def destination_to_fit(destination: Destination) -> Fit:
    destination_type = getattr(destination, "typ", None)

    if destination_type == "/Fit":
        return Fit.fit()
    if destination_type == "/FitB":
        return Fit.fit_box()
    if destination_type == "/FitH":
        return Fit.fit_horizontally(getattr(destination, "top", None))
    if destination_type == "/FitBH":
        return Fit.fit_box_horizontally(getattr(destination, "top", None))
    if destination_type == "/FitV":
        return Fit.fit_vertically(getattr(destination, "left", None))
    if destination_type == "/FitBV":
        return Fit.fit_box_vertically(getattr(destination, "left", None))
    if destination_type == "/FitR":
        return Fit.fit_rectangle(
            getattr(destination, "left", None),
            getattr(destination, "bottom", None),
            getattr(destination, "right", None),
            getattr(destination, "top", None),
        )
    if destination_type == "/XYZ":
        return Fit.xyz(
            getattr(destination, "left", None),
            getattr(destination, "top", None),
            getattr(destination, "zoom", None),
        )

    return Fit.fit()


def build_outline_nodes(reader: PdfReader, outline: Iterable[object]) -> list[OutlineNode]:
    nodes: list[OutlineNode] = []

    for item in outline:
        if isinstance(item, list):
            if not nodes:
                continue
            nodes[-1].children.extend(build_outline_nodes(reader, item))
            continue

        title = getattr(item, "title", None)
        if not title:
            continue

        color_value = getattr(item, "color", None)
        color = tuple(color_value) if color_value else None
        font_format = int(getattr(item, "font_format", 0) or 0)

        nodes.append(
            OutlineNode(
                title=title,
                page_number=reader_page_number(reader, item),
                fit=destination_to_fit(item),
                color=color,
                bold=bool(font_format & 2),
                italic=bool(font_format & 1),
                is_open=bool(item.get("/%is_open%", True)) if hasattr(item, "get") else True,
            )
        )

    return nodes


def reader_page_number(reader: PdfReader, destination: Destination) -> int | None:
    try:
        return reader.get_destination_page_number(destination)
    except Exception:
        return None


def unwrap_noisy_top_level(nodes: list[OutlineNode], wrapper_re: re.Pattern[str]) -> list[OutlineNode]:
    cleaned: list[OutlineNode] = []

    for node in nodes:
        if wrapper_re.fullmatch(node.title) and node.children:
            cleaned.extend(node.children)
            continue
        cleaned.append(node)

    return cleaned


def count_matching_top_level(nodes: Iterable[OutlineNode], wrapper_re: re.Pattern[str]) -> int:
    return sum(1 for node in nodes if wrapper_re.fullmatch(node.title) and node.children)


def remove_nodes_by_title(
    nodes: list[OutlineNode], titles_to_remove: set[str]
) -> tuple[list[OutlineNode], Counter[str]]:
    cleaned: list[OutlineNode] = []
    removed_counts: Counter[str] = Counter()

    for node in nodes:
        node.children, child_counts = remove_nodes_by_title(node.children, titles_to_remove)
        removed_counts.update(child_counts)

        if node.title in titles_to_remove:
            removed_counts[node.title] += 1
            continue

        cleaned.append(node)

    return cleaned, removed_counts


def add_outline_nodes(writer: PdfWriter, nodes: Iterable[OutlineNode], parent: object | None = None) -> None:
    for node in nodes:
        bookmark = writer.add_outline_item(
            title=node.title,
            page_number=node.page_number,
            parent=parent,
            color=node.color,
            bold=node.bold,
            italic=node.italic,
            fit=node.fit,
            is_open=node.is_open,
        )
        if node.children:
            add_outline_nodes(writer, node.children, parent=bookmark)


def derive_output_path(input_path: Path) -> Path:
    return input_path.with_stem(f"{input_path.stem}.cleaned")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove noisy top-level wrapper outline items from a PDF bookmark tree."
    )
    parser.add_argument("input_pdf", type=Path, help="Path to the source PDF.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Path for the cleaned PDF. Defaults to '<input>.cleaned.pdf'.",
    )
    parser.add_argument(
        "--wrapper-regex",
        default=WRAPPER_TITLE_RE.pattern,
        help="Regex that identifies noisy top-level wrapper bookmark titles.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = args.input_pdf.expanduser().resolve()
    output_path = (args.output or derive_output_path(input_path)).expanduser().resolve()
    wrapper_re = re.compile(args.wrapper_regex)

    reader = PdfReader(str(input_path))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    if reader.metadata:
        writer.add_metadata(dict(reader.metadata))

    outline_nodes = build_outline_nodes(reader, reader.outline)
    removed_wrapper_count = count_matching_top_level(outline_nodes, wrapper_re)
    cleaned_nodes = unwrap_noisy_top_level(outline_nodes, wrapper_re)
    cleaned_nodes, removed_title_counts = remove_nodes_by_title(
        cleaned_nodes,
        {TABLE_OF_CONTENTS_TITLE, REFERENCES_TITLE},
    )

    add_outline_nodes(writer, cleaned_nodes)

    with output_path.open("wb") as output_file:
        writer.write(output_file)

    print(f"Wrote cleaned PDF to {output_path}")
    print(f"Removed {removed_wrapper_count} noisy wrapper outline entries")
    print(
        f"Removed {removed_title_counts[TABLE_OF_CONTENTS_TITLE]} redundant '{TABLE_OF_CONTENTS_TITLE}' outline entries"
    )
    print(
        f"Removed {removed_title_counts[REFERENCES_TITLE]} redundant '{REFERENCES_TITLE}' outline entries"
    )


if __name__ == "__main__":
    main()