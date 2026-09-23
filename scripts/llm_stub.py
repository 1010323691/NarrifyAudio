"""Deterministic OpenAI-compatible LLM stub for platform smoke tests."""
from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _completion(body: dict) -> str:
    messages = body.get("messages") or []
    user_message = next(
        (item.get("content", "") for item in reversed(messages) if item.get("role") == "user"),
        "",
    )
    marker = "SOURCE TEXT:\n"
    source = user_message.split(marker, 1)[1].strip() if marker in user_message else "stub smoke text。"
    return json.dumps(
        [{"speaker": "NARRATOR", "text": source, "instruct": "Neutral, even narration."}],
        ensure_ascii=False,
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "NarrifyLLMStub/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, {"ok": True, "service": "llm-stub"})
            return
        self._send(404, {"detail": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self._send(404, {"detail": "Not Found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": {"message": "invalid json"}})
            return
        time.sleep(float(os.getenv("NARRIFY_LLM_STUB_DELAY", "0")))
        payload = {
            "id": "smoke-completion",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": _completion(body)},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 8, "completion_tokens": 8},
        }
        self._send(200, payload)

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
