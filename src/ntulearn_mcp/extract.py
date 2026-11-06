"""Pull readable text out of downloaded course files so Claude can read them."""

from __future__ import annotations

from pathlib import Path

TEXT_EXT = {".txt", ".md", ".csv", ".py", ".java", ".c", ".cpp", ".h", ".js", ".ts", ".json", ".m", ".r", ".sql", ".tex"}


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        parts = []
        chars = 0
        for i, page in enumerate(reader.pages, 1):
            t = (page.extract_text() or "").strip()
            chars += len(t)
            parts.append(f"--- page {i} ---\n{t}")
        text = "\n\n".join(parts)
        if chars < 20 * max(1, len(reader.pages)):
            text += "\n\n[Little text found: this PDF may be scanned images or mostly diagrams.]"
        return text
    if ext == ".pptx":
        from pptx import Presentation

        prs = Presentation(str(path))
        parts = []
        for i, slide in enumerate(prs.slides, 1):
            lines = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    t = shape.text_frame.text.strip()
                    if t:
                        lines.append(t)
                if getattr(shape, "has_table", False) and shape.has_table:
                    for row in shape.table.rows:
                        lines.append(" | ".join(c.text.strip() for c in row.cells))
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    lines.append(f"[notes] {notes}")
            parts.append(f"--- slide {i} ---\n" + "\n".join(lines))
        return "\n\n".join(parts)
    if ext == ".docx":
        import docx

        d = docx.Document(str(path))
        lines = [p.text for p in d.paragraphs if p.text.strip()]
        for table in d.tables:
            for row in table.rows:
                lines.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(lines)
    if ext in (".html", ".htm"):
        from .content import html_to_text

        return html_to_text(path.read_text(encoding="utf-8", errors="replace"))
    if ext in TEXT_EXT:
        return path.read_text(encoding="utf-8", errors="replace")
    raise ValueError(
        f"Can't extract text from '{ext}' files. Supported: .pdf .pptx .docx .html and plain-text/code files. "
        "(Old .ppt/.doc formats need converting first.)"
    )
