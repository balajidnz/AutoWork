"""Render a tailored resume from markdown to PDF.

`/tailor` produces markdown, and no ATS accepts a markdown file — so without
this the tailoring loop stops one step short of something you can actually
upload. This closes it.

Layout is hand-built rather than driven by CSS. WeasyPrint would allow CSS but
needs pango and cairo as system libraries, which do not install cleanly on
macOS; fpdf2 is pure Python and works anywhere. The tradeoff is manual
positioning, which is fine — a resume has one fixed structure, and the
right-aligned dates on a shared line with the employer are easier to place
directly than to coax out of a layout engine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from fpdf import FPDF

# fpdf2's core fonts are latin-1. Every one of these appears in the resumes and
# raises rather than degrading, so they are mapped rather than risked.
_TRANSLITERATE = {
    # No padding around the replacement: the source already spaces its dashes,
    # and " - " turns "Northwind — Engineer" into "Northwind  -  Engineer".
    "—": "-", "–": "-", "‒": "-", "−": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "…": "...", "•": "-", "₹": "INR ", "≤": "<=",
    "≥": ">=", " ": " ", "​": "", "→": "->",
}
_TRANSLATION = str.maketrans({k: v for k, v in _TRANSLITERATE.items()})

# "Northwind — Full-Stack Engineer (SDE-1), May 2025 – present"
_TRAILING_DATE = re.compile(
    r"^(?P<left>.+?)[,–—-]\s*(?P<date>"
    r"(?:[A-Z][a-z]{2,8}\s+\d{4}|\d{4})\s*[–—-]\s*"
    r"(?:[A-Z][a-z]{2,8}\s+\d{4}|\d{4}|[Pp]resent|[Nn]ow))\s*$"
)


def latin1(text: str) -> str:
    """Make a string safe for fpdf2's core fonts."""
    return text.translate(_TRANSLATION).encode("latin-1", "replace").decode("latin-1")


@dataclass
class Block:
    kind: str          # name | contact | section | role | sub | bullet | text
    text: str
    right: str = ""    # right-aligned companion, e.g. a date range


def parse(markdown: str) -> list[Block]:
    """Structure a resume markdown file.

    Deliberately forgiving: `/tailor` writes prose, and a renderer that only
    accepts one exact shape would reject perfectly good output.
    """
    blocks: list[Block] = []
    seen_heading = False

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        # Guidance for the tailoring model, not resume content.
        if line.lstrip().startswith(">"):
            continue

        if line.startswith("# "):
            blocks.append(Block("name", line[2:].split("—")[0].split(" - ")[0].strip()))
            seen_heading = True
        elif line.startswith("### "):
            blocks.append(Block("sub", line[4:].strip()))
        elif line.startswith("## "):
            body = line[3:].strip()
            if match := _TRAILING_DATE.match(body):
                blocks.append(Block("role", match["left"].strip(), match["date"].strip()))
            elif len(body) <= 34 and not any(c in body for c in "—,("):
                blocks.append(Block("section", body))
            else:
                blocks.append(Block("role", body))
        elif line.lstrip().startswith(("- ", "* ")):
            blocks.append(Block("bullet", line.lstrip()[2:].strip()))
        elif blocks and blocks[-1].kind in {"bullet", "text"}:
            # Markdown lazy continuation. The resumes are hard-wrapped at ~80
            # columns, so a bullet spans three source lines; treating each as
            # its own block rendered the continuations as separate paragraphs
            # at the margin, losing both the hanging indent and the wrap.
            blocks[-1].text += " " + line.strip()
        elif blocks and blocks[-1].kind == "name" and not seen_heading:
            blocks.append(Block("contact", line.strip()))
        elif len(blocks) == 1 and blocks[0].kind == "name":
            blocks.append(Block("contact", line.strip()))
        else:
            blocks.append(Block("text", line.strip()))
    return blocks


class Resume(FPDF):
    def footer(self) -> None:  # no page numbers — a resume is not a report
        pass


_BOLD_RUN = re.compile(r"\*\*(.+?)\*\*")


def runs(text: str) -> list[tuple[str, bool]]:
    """Split `a **b** c` into [(a, False), (b, True), (c, False)]."""
    out: list[tuple[str, bool]] = []
    cursor = 0
    for match in _BOLD_RUN.finditer(text):
        if match.start() > cursor:
            out.append((text[cursor:match.start()], False))
        out.append((match.group(1), True))
        cursor = match.end()
    if cursor < len(text):
        out.append((text[cursor:], False))
    return out or [(text, False)]


def _wrap(pdf: FPDF, text: str, width: float, size: float) -> list[list[tuple[str, bool]]]:
    """Greedy wrap that measures bold and regular runs at their own widths.

    Hand-rolled because fpdf2's `multi_cell` wraps every continuation line back
    to the page margin regardless of `set_left_margin` or the starting x, which
    makes a hanging indent impossible. Wrapping here also means the inline bold
    survives the line break instead of being re-parsed per line.
    """
    lines: list[list[tuple[str, bool]]] = [[]]
    used = 0.0
    for chunk, bold in runs(text):
        pdf.set_font("helvetica", "B" if bold else "", size)
        for word in re.split(r"(\s+)", chunk):
            if not word:
                continue
            advance = pdf.get_string_width(word)
            if word.isspace():
                if used and used + advance <= width:
                    lines[-1].append((word, bold))
                    used += advance
                continue
            if used + advance > width and used:
                lines.append([])
                used = 0.0
            lines[-1].append((word, bold))
            used += advance
    return [line for line in lines if line]


def _paragraph(
    pdf: FPDF, text: str, size: float, lead: float, width: float, *, hang: float = 0.0
) -> None:
    """Emit already-wrapped text at absolute positions.

    Uses `text()` rather than `cell()` on purpose. `cell()` performs its own
    line break when a token would cross the right margin, and that break resets
    x to the page margin — which silently overrode the hanging indent and
    re-wrapped lines this function had already measured. Placing each run
    absolutely means the wrap computed in `_wrap` is the wrap that renders.
    """
    base = pdf.l_margin
    bottom = pdf.h - pdf.b_margin

    for index, line in enumerate(_wrap(pdf, text, width - hang, size)):
        if pdf.get_y() + lead > bottom:
            pdf.add_page()
        y = pdf.get_y()
        baseline = y + lead * 0.76      # text() positions the baseline, not the top
        x = base

        if hang:
            if index == 0:
                pdf.set_font("helvetica", "", size)
                pdf.text(x, baseline, chr(149))   # latin-1 bullet
            x = base + hang

        for chunk, bold in line:
            pdf.set_font("helvetica", "B" if bold else "", size)
            pdf.text(x, baseline, chunk)
            x += pdf.get_string_width(chunk)
        pdf.set_y(y + lead)


def build(blocks: list[Block], *, body_pt: float = 9.0) -> Resume:
    """Lay the document out and return it unsaved, so the caller can inspect
    the page count before committing to a size."""
    pdf = Resume(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=9)
    pdf.set_margins(13, 9, 13)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin
    # Leading tracks the point size so shrinking to fit stays proportional.
    lead = body_pt * 0.42
    indent = 3.0

    for block in blocks:
        text = latin1(block.text)

        if block.kind == "name":
            pdf.set_font("helvetica", "B", 17)
            pdf.cell(0, 8, " ".join(text.upper()), align="C",
                     new_x="LMARGIN", new_y="NEXT")
        elif block.kind == "contact":
            pdf.set_font("helvetica", "", 8.5)
            pdf.set_text_color(60, 60, 60)
            pdf.cell(0, lead + 0.4, text, align="C", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0, 0, 0)
            pdf.ln(1.5)
        elif block.kind == "section":
            pdf.ln(2.2)
            pdf.set_font("helvetica", "B", 9.5)
            pdf.cell(0, lead + 0.6, " ".join(text.upper()), new_x="LMARGIN", new_y="NEXT")
            y = pdf.get_y()
            pdf.set_line_width(0.3)
            pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
            pdf.ln(1.6)
        elif block.kind == "role":
            pdf.ln(1.2)
            pdf.set_font("helvetica", "B", 9.8)
            if block.right:
                right = latin1(block.right)
                pdf.cell(width - pdf.get_string_width(right) - 1, lead + 0.9, text)
                pdf.set_font("helvetica", "", 9)
                pdf.cell(0, lead + 0.9, right, align="R", new_x="LMARGIN", new_y="NEXT")
            else:
                pdf.cell(0, lead + 0.9, text, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(0.6)
        elif block.kind == "sub":
            pdf.ln(0.8)
            pdf.set_font("helvetica", "BI", body_pt)
            pdf.cell(0, lead + 0.3, text, new_x="LMARGIN", new_y="NEXT")
        elif block.kind == "bullet":
            _paragraph(pdf, text, body_pt, lead, width, hang=indent)
        else:
            _paragraph(pdf, text, body_pt, lead, width)
            pdf.ln(0.5)

    return pdf


def pages(blocks: list[Block], *, body_pt: float = 9.0) -> int:
    return len(build(blocks, body_pt=body_pt).pages)


def render(blocks: list[Block], path: Path, *, body_pt: float = 9.0) -> Path:
    pdf = build(blocks, body_pt=body_pt)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))
    return path


def render_file(
    source: Path, out: Path | None = None, *, body_pt: float = 9.0, fit: bool = True
) -> Path:
    """Render, shrinking the body size until it fits on one page.

    A tailored resume varies in length with the role, and two pages at SDE-1
    reads worse than one tight page. Stops at 7pt — below that it is unreadable
    and the honest answer is to cut content rather than shrink it further.
    """
    blocks = parse(source.read_text(encoding="utf-8"))
    target = out or source.with_suffix(".pdf")
    size = body_pt
    if fit:
        # Page count comes from the laid-out document, not from sniffing the
        # saved bytes — fpdf2 compresses, so /Type /Page markers are not
        # reliably present in the output file.
        while size > 7.0 and len(build(blocks, body_pt=size).pages) > 1:
            size = round(size - 0.25, 2)
    render(blocks, target, body_pt=size)
    return target
