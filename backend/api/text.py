"""Text-formatting endpoint (module: 文本排版).

Reads a TXT, formats it with the ported engine, writes the result to the
workspace's ``01_input/`` under a *new* name (``<原基名>_排版<扩展名>`` — the
original upload is preserved alongside it) and returns stats + a preview (the
preview pane is a deliberate enhancement over the source tool, which only showed
stats).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.config import get_config
from ..core.paths import get_layout
from ..engines.text import format_text
from . import _common

router = APIRouter(prefix="/api/text", tags=["text"])


class FormatRequest(BaseModel):
    path: str
    config: dict = {}  # partial overrides for the 10 toggles


@router.post("/format")
def format_text_endpoint(req: FormatRequest) -> dict:
    _common.require_workspace()
    text, enc, src = _common.read_decoded_file(req.path)

    cfg = _common.partial_copy(get_config().text, req.config)
    result = format_text(text, cfg)

    # New name (``<基名>_排版<扩展名>``) so the original upload in 01_input/ is kept.
    out_path: Path = get_layout().input / f"{src.stem}_排版{src.suffix}"
    # write_bytes: keep the formatted text's line endings as-is (no CRLF translation).
    out_path.write_bytes(result["text"].encode("utf-8"))

    return {
        "source": str(src),
        "encoding": enc,
        "output_path": str(out_path),
        "stats": result["stats"],
        "preview": result["text"][:2000],
        "full_length": len(result["text"]),
    }
