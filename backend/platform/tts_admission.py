"""ASGI admission before authentication, thread-pool work or DB checkout."""
from __future__ import annotations
import asyncio
import threading
import time
from starlette.responses import JSONResponse


class TTSAdmissionMiddleware:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.active = self.waiting = 0

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') != 'POST' or scope['path'] not in {'/api/tts/batch', '/api/tts/batch-reset', '/api/v1/tasks'}:
            return await self.app(scope, receive, send)
        with self.lock:
            admitted = self.waiting < 32
            if admitted:
                self.waiting += 1
        if not admitted:
            return await JSONResponse({'detail': '提交请求过多，请稍后重试'}, 429, headers={'Retry-After': '10'})(scope, receive, send)
        entered = False
        try:
            started = time.monotonic()
            while not entered:
                with self.lock:
                    if self.active < 2:
                        self.active += 1; entered = True
                if entered:
                    break
                if time.monotonic() - started >= 10:
                    return await JSONResponse({'detail': '服务器繁忙，请稍后重试'}, 429, headers={'Retry-After': '10'})(scope, receive, send)
                await asyncio.sleep(0.05)
            # Enforce actual received bytes, including chunked requests.
            body = bytearray()
            deadline = time.monotonic() + 10
            while True:
                try:
                    message = await asyncio.wait_for(receive(), timeout=max(0, deadline - time.monotonic()))
                except TimeoutError:
                    return await JSONResponse({'detail': '读取提交请求超时'}, 408)(scope, receive, send)
                if message['type'] == 'http.disconnect':
                    return
                data = message.get('body', b'')
                if len(body) + len(data) > 1024 * 1024:
                    return await JSONResponse({'detail': '提交请求超过 1MiB'}, 413)(scope, receive, send)
                body.extend(data)
                if not message.get('more_body', False):
                    break
            received = False
            async def replay():
                nonlocal received
                if not received:
                    received = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()
            await self.app(scope, replay, send)
        finally:
            with self.lock:
                self.waiting -= 1
                if entered:
                    self.active -= 1
