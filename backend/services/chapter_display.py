"""Original chapter titles for production rows, separate from file identities."""
from pathlib import Path
import re

from ..engines.book import detect_chapters


def chapter_source_path(stem: str, layout) -> Path | None:
    split_text = getattr(layout, "split_text", None)
    if split_text is None or not stem or Path(stem).name != stem:
        return None
    root = split_text.resolve()
    source = (root / f"{stem}.txt").resolve()
    return source if source.is_relative_to(root) else None


def chapter_display_name(stem: str, layout) -> str:
    """Restore source punctuation while retaining the repaired chapter number."""
    source = chapter_source_path(stem, layout)
    if source is None:
        return stem
    try:
        with source.open("r", encoding="utf-8-sig") as stream:
            opening = stream.read(4096)
        # The first split can include the book's introduction before its header.
        chapters = detect_chapters(opening)
    except (OSError, UnicodeError):
        return stem
    if not chapters or not chapters[0].get("title"):
        return stem
    prefix = re.match(r"^(第\s*\S+?\s*[章回节卷集部篇])", stem)
    header = opening[chapters[0]["start"]:].splitlines()[0].strip()
    return f"{prefix.group(1)} {chapters[0]['title']}" if prefix else header
