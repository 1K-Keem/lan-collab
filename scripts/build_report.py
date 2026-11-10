"""Render the technical-report Markdown draft to PDF (optional reportlab dependency)."""

import argparse
from html import escape
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("docs/technical-report.md"))
    parser.add_argument("--output", type=Path, default=Path("docs/technical-report.pdf"))
    args = parser.parse_args()
    try:
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_LEFT
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Preformatted, PageBreak, Spacer
    except ImportError:
        parser.error(
            "Install the optional PDF tool: python -m pip install -r requirements-report.txt"
        )
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            "BodyReport",
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            spaceAfter=9,
            alignment=TA_LEFT,
        )
    )
    styles.add(
        ParagraphStyle(
            "CodeReport",
            fontName="Courier",
            fontSize=7.6,
            leading=11,
            textColor=colors.HexColor("#18344a"),
            spaceAfter=12,
        )
    )
    styles.add(
        ParagraphStyle("ListReport", fontName="Helvetica", fontSize=9, leading=12, spaceAfter=4)
    )
    flow = []
    paragraph, code = [], None

    def flush():
        if paragraph:
            flow.append(Paragraph(escape(" ".join(paragraph)), styles["BodyReport"]))
            paragraph.clear()

    for line in args.source.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            flush()
            if code is None:
                code = []
            else:
                flow.append(Preformatted("\n".join(code), styles["CodeReport"]))
                code = None
        elif code is not None:
            code.append(line)
        elif line == "<!-- pagebreak -->":
            flush()
            flow.append(PageBreak())
        elif line.startswith("# ") or line.startswith("## "):
            flush()
            heading = "Heading2" if line.startswith("## ") else "Heading1"
            flow.append(Paragraph(escape(line.lstrip("# ")), styles[heading]))
            flow.append(Spacer(1, 5))
        elif line.partition(". ")[0].isdigit():
            flush()
            flow.append(Paragraph(escape(line), styles["ListReport"]))
        elif not line.strip():
            flush()
        else:
            paragraph.append(line.strip())
    flush()

    def footer(canvas, document):
        canvas.setFont("Helvetica", 8)
        canvas.drawString(42, 25, "LAN Collab | Implementation draft — manual acceptance pending")
        canvas.drawRightString(553, 25, str(document.page))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(args.output),
        pagesize=(595, 842),
        rightMargin=42,
        leftMargin=42,
        topMargin=35,
        bottomMargin=40,
    ).build(flow, onFirstPage=footer, onLaterPages=footer)
    print(args.output)


if __name__ == "__main__":
    main()
