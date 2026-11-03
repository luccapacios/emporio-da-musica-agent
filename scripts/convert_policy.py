"""Convert the store policy PDF into sectioned markdown.

The agent reads the manual by section (see emporio.policy), so this script keeps the
numbered headings and rebuilds tables and bullet lists from pypdf's layout extraction.
The output is committed, so the conversion can be reviewed like any other file.

Usage: python scripts/convert_policy.py [pdf] [out.md]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PDF = ROOT / "data" / "raw" / "politicas_da_loja.pdf"
DEFAULT_OUT = ROOT / "data" / "processed" / "policies.md"

PAGE_HEADER = "Empório da Música Manual de Políticas e Procedimentos"
SECTION = re.compile(r"^(\d{1,2})\. (\S.*)$")
SUBSECTION = re.compile(r"^(\d{1,2}\.\d) (\S.*)$")
NUMBERED_ITEM = re.compile(r"^(\d)\.\s+(\S.*)$")
LABEL = re.compile(r"^[A-ZÀ-Ú][\wÀ-ú ]{2,30}: ")


def extract_lines(pdf: Path) -> list[str]:
    reader = PdfReader(pdf)
    lines: list[str] = []
    # Page 1 is the cover; its contact data is repeated in section 1.2.
    for page in reader.pages[1:]:
        for raw in page.extract_text(extraction_mode="layout").splitlines():
            line = raw.rstrip()
            if line.strip() == PAGE_HEADER or re.fullmatch(r"\s*Página \d+", line):
                continue
            # Blank lines are kept (collapsed): they mark paragraph breaks.
            if line.strip() or (lines and lines[-1]):
                lines.append(line)
    return lines


def cells(line: str) -> list[str]:
    return re.split(r"\s{2,}", line.strip())


def to_markdown(lines: list[str]) -> str:
    out: list[str] = []
    table: list[list[str]] = []
    paragraph: list[str] = []
    # A line that stops well short of the right margin ends its paragraph.
    full_width = max(len(line.strip()) for line in lines if len(cells(line)) == 1)
    previous_width = full_width

    def flush_paragraph() -> None:
        if paragraph:
            out.append(" ".join(paragraph))
            out.append("")
            paragraph.clear()

    def flush_table() -> None:
        if table:
            width = max(len(row) for row in table)
            rows = [row + [""] * (width - len(row)) for row in table]
            out.append("| " + " | ".join(rows[0]) + " |")
            out.append("|" + "---|" * width)
            out.extend("| " + " | ".join(row) + " |" for row in rows[1:])
            out.append("")
            table.clear()

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        ended_short = previous_width < 0.9 * full_width
        previous_width = len(stripped)

        if not stripped:
            flush_paragraph(), flush_table()
            continue
        if indent == 0 and (m := SUBSECTION.match(stripped)):
            flush_paragraph(), flush_table()
            out += [f"### {m.group(1)} {m.group(2)}", ""]
            continue
        if indent == 0 and (m := SECTION.match(stripped)):
            flush_paragraph(), flush_table()
            out += [f"## {m.group(1)}. {m.group(2)}", ""]
            continue

        parts = cells(line)
        if len(parts) >= 2 and not stripped.startswith("•") and not NUMBERED_ITEM.match(stripped):
            flush_paragraph()
            table.append(parts)
            continue
        if table and indent > 20 and len(parts) == 1:
            # Wrapped text of the last cell of the previous row.
            table[-1][-1] += " " + stripped
            continue
        flush_table()

        if stripped.startswith("•"):
            flush_paragraph()
            paragraph.append("- " + stripped.lstrip("• ").strip())
        elif m := NUMBERED_ITEM.match(stripped):
            flush_paragraph()
            paragraph.append(f"{m.group(1)}. {m.group(2)}")
        elif LABEL.match(stripped) and paragraph and ended_short:
            flush_paragraph()
            paragraph.append(stripped)
        else:
            paragraph.append(stripped)

    flush_paragraph(), flush_table()
    text = "\n".join(out)
    # Bullet lists were emitted as one paragraph per item; tighten them into lists.
    text = re.sub(r"\n\n(?=- |\d\. )", "\n", text)
    return text.strip() + "\n"


def main() -> None:
    pdf = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    header = (
        "# Empório da Música: Manual de Políticas e Procedimentos\n\n"
        f"Converted from `{pdf.name}` (versão 2.1, junho de 2025) by "
        "`scripts/convert_policy.py`. Text is kept as in the source.\n\n"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + to_markdown(extract_lines(pdf)), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
