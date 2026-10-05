from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _content_disposition(filename: str, inline: bool = False) -> str:
    return f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(filename)}"


def file_response(request: Request, path: Path, *, media_type: str, filename: str, inline: bool = False):
    size = path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        response = FileResponse(path, media_type=media_type, filename=filename, content_disposition_type="inline" if inline else "attachment")
        response.headers["Accept-Ranges"] = "bytes"
        return response

    match = _RANGE_RE.fullmatch(range_header.strip())
    if match is None or (not match.group(1) and not match.group(2)):
        raise HTTPException(416, "无效的 Range 请求", headers={"Content-Range": f"bytes */{size}"})
    raw_start, raw_end = match.groups()
    try:
        if raw_start:
            start = int(raw_start)
            end = int(raw_end) if raw_end else size - 1
        else:
            suffix = int(raw_end)
    except ValueError:
        raise HTTPException(416, "无效的 Range 请求", headers={"Content-Range": f"bytes */{size}"}) from None
    if not raw_start:
        if suffix <= 0:
            raise HTTPException(416, "无效的 Range 请求", headers={"Content-Range": f"bytes */{size}"})
        start = max(0, size - suffix)
        end = size - 1
    if size == 0 or start >= size or start > end:
        raise HTTPException(416, "Range 超出文件范围", headers={"Content-Range": f"bytes */{size}"})
    end = min(end, size - 1)
    length = end - start + 1

    def body():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        body(),
        status_code=206,
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
            "Content-Disposition": _content_disposition(filename, inline),
        },
    )
