"""Local, deterministic OpenAI HTTP fixture; never used by production startup."""
import json
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import argparse

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages = body['messages']
        system = messages[0]['content']
        user = messages[-1]['content']
        with self.server.lock:
            self.server.active += 1
            self.server.peak = max(self.server.peak, self.server.active)
        started=time.monotonic()
        time.sleep(self.server.delay)
        if 'FAKE_SCRIPT' in system:
            result = [{'type': 'NARRATOR', 'speaker': '旁白', 'text': user.strip(), 'instruct': '平静'}]
            kind = 'script.parse'
        elif 'FAKE_PERSONA' in system:
            result = {'description': '温暖清晰的成年男声', 'ref_text': '这是用于并发测试的参考语音。', 'gender': 'male'}
            kind = 'voices.foundation'
        elif 'start_segment' in system:
            result = [{'start_segment': 0, 'end_segment': 1, 'scene': '测试场景', 'mood': '平静',
                       'music_tags': {'scene': [], 'mood': [], 'emotion': [], 'custom': []}, 'intensity': 2}]
            kind = 'bgm.segment'
        else:
            result = {'scene': ['测试场景'], 'mood': [], 'emotion': [], 'custom': []}
            kind = 'music.suggest_tags'
        content = json.dumps(result, ensure_ascii=False)
        with self.server.lock:
            self.server.active -= 1
            self.server.calls.append({'kind': kind, 'chars': len(content), 'planned_seconds':self.server.delay, 'actual_wait_seconds':time.monotonic()-started})
            self.server.report.write_text(json.dumps({'peak_concurrent': self.server.peak, 'calls': self.server.calls}))
        response = json.dumps({'id': 'fixture', 'object': 'chat.completion', 'model': 'fixture',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': len(user), 'completion_tokens': len(content), 'total_tokens': len(user)+len(content)}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--delay', type=float, default=.2)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.lock = threading.Lock()
    server.active = server.peak = 0
    server.calls = []
    server.report = args.report
    server.delay = args.delay
    server.serve_forever()
