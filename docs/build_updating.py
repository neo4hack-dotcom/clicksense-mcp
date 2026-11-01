"""Builds docs/ClickSense-Maintenance-Guide.pdf from docs/UPDATING.md.   .venv/bin/python docs/build_updating.py

UPDATING.md is the single source (readable on GitHub); this turns it into a PDF with the same look
as the user guide. It understands the Markdown that file uses: headings (#, ##, ###), paragraphs
with **bold**, `code` and [links](...), bullet and numbered lists (with indented code blocks),
fenced code, tables, > notes and --- rules.
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import CondPageBreak, PageBreak, Paragraph, Spacer
from reportlab.platypus.tableofcontents import TableOfContents

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_guide as g  # noqa: E402  (styles, helpers and page decorations of the user guide)

HERE = Path(__file__).resolve().parent
SRC = HERE / "UPDATING.md"
OUT = HERE / "ClickSense-Maintenance-Guide.pdf"


def inline(text: str) -> str:
    """Markdown inline -> reportlab mini-HTML. Code spans are set aside first, so `**bold `code`**`
    keeps its bold."""
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(f"<font face='Courier'>{html.escape(m.group(1))}</font>")
        return f"\x00{len(codes) - 1}\x00"

    s = re.sub(r"`([^`]+)`", stash, text)
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![*\w])\*(?!\s)(.+?)\*(?!\w)", r"<i>\1</i>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", s)
    return re.sub("\x00(\\d+)\x00", lambda m: codes[int(m.group(1))], s)


def render(md: str) -> list:
    story: list = []
    lines = md.splitlines()
    i = 0
    para: list[str] = []

    def flush():
        if para:
            story.append(Paragraph(inline(" ".join(para)), g.BODY))
            para.clear()

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):  # fenced code, possibly indented inside a list item
            flush()
            indent = len(line) - len(line.lstrip())
            i += 1
            block = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i][indent:] if len(lines[i]) >= indent else lines[i].lstrip())
                i += 1
            g.story.clear()
            g.code("\n".join(block))
            story.extend(g.story)
            i += 1
            continue
        if line.startswith("# "):
            flush()
            i += 1
            continue  # the document title is on the cover
        if line.startswith("## "):
            flush()
            title = line[3:].strip()
            story.append(CondPageBreak(60 * mm))
            story.append(Paragraph(inline(title), g.H1 if not title.startswith("Appendix") else g.H1))
            i += 1
            continue
        if line.startswith("### "):
            flush()
            story.append(Paragraph(inline(line[4:]), g.H2))
            i += 1
            continue
        if stripped == "---":
            flush()
            i += 1
            continue
        if stripped.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                    rows.append([inline(c) for c in cells])
                i += 1
            n = len(rows[0])
            widths = {2: [62, 108], 3: [45, 70, 55]}.get(n, [170 / n] * n)
            g.story.clear()
            g.table(rows, widths)
            story.extend(g.story)
            continue
        if stripped.startswith(">"):
            flush()
            note = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                note.append(lines[i].strip().lstrip(">").strip())
                i += 1
            text = inline(" ".join(note))
            title, _, body = text.partition("</b>")
            g.story.clear()
            if body:
                g.callout(title.replace("<b>", ""), body.strip())
            else:
                g.callout("Note", text)
            story.extend(g.story)
            continue
        m = re.match(r"^(\s*)(-|\d+\.)\s+(.*)$", line)
        if m:
            flush()
            bullet = "•" if m.group(2) == "-" else m.group(2)
            item = [m.group(3)]
            i += 1
            # continuation lines of the same item (indented, not a new item, not a code fence)
            while i < len(lines) and lines[i].startswith("   ") and not lines[i].strip().startswith("```") \
                    and not re.match(r"^\s*(-|\d+\.)\s+", lines[i]):
                item.append(lines[i].strip())
                i += 1
            indent = 12 + 10 * (len(m.group(1)) // 3)
            style = g.ParagraphStyle(f"li{indent}", parent=g.BULLET, leftIndent=indent, bulletIndent=indent - 10)
            story.append(Paragraph(inline(" ".join(item)), style, bulletText=bullet))
            continue
        if not stripped:
            flush()
            i += 1
            continue
        para.append(stripped)
        i += 1
    flush()
    return story


def on_first(canv, doc):
    canv.saveState()
    canv.setFillColor(g.BRAND)
    canv.rect(0, A4[1] - 95 * mm, A4[0], 95 * mm, fill=1, stroke=0)
    canv.setFillColor(g.colors.white)
    canv.setFont("Helvetica-Bold", 34)
    canv.drawString(20 * mm, A4[1] - 50 * mm, "ClickSense")
    canv.setFont("Helvetica", 15)
    canv.drawString(20 * mm, A4[1] - 62 * mm, "Maintaining ClickSense, step by step")
    canv.setFont("Helvetica", 11)
    canv.drawString(20 * mm, A4[1] - 74 * mm, f"Maintenance guide — version {g.VERSION}")
    canv.setFillColor(g.MUTED)
    canv.setFont("Helvetica", 8.5)
    canv.drawString(20 * mm, 18 * mm, "Generated from docs/UPDATING.md — rebuild with: .venv/bin/python docs/build_updating.py")
    canv.restoreState()


def on_page(canv, doc):
    canv.saveState()
    canv.setStrokeColor(g.RULE)
    canv.setLineWidth(0.4)
    canv.line(20 * mm, 14 * mm, 190 * mm, 14 * mm)
    canv.setFont("Helvetica", 7.6)
    canv.setFillColor(g.MUTED)
    canv.drawString(20 * mm, 9.5 * mm, f"ClickSense {g.VERSION} — Maintenance guide")
    canv.drawRightString(190 * mm, 9.5 * mm, f"Page {doc.page}")
    canv.restoreState()


def build() -> None:
    body = render(SRC.read_text(encoding="utf-8"))
    intro = SRC.read_text(encoding="utf-8").split("\n## ")[0].split("\n", 1)[1].strip().split("\n\n")
    story: list = [Spacer(1, 92 * mm)]
    story += [Paragraph(inline(p.replace("\n", " ")), g.BODY) for p in intro if p.strip() and p.strip() != "---"]
    story += [PageBreak(), Paragraph("Contents", g.ParagraphStyle("toctitle", parent=g.H1))]
    toc = TableOfContents()
    toc.levelStyles = [g.TOC1, g.TOC2]
    story += [toc, PageBreak()]
    # Chapters only after the intro (skip the intro paragraphs already on the cover).
    first_h1 = next(i for i, f in enumerate(body) if isinstance(f, Paragraph) and f.style is g.H1)
    story += body[first_h1:]
    doc = g.Guide(str(OUT), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
                  bottomMargin=20 * mm, title="ClickSense Maintenance Guide", author="ClickSense")
    doc.multiBuild(story, onFirstPage=on_first, onLaterPages=on_page)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    build()
