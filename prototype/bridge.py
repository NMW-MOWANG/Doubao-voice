#!/usr/bin/env python3
"""voice-glow 原型的桥接器：假数据源 + 静态服务器（只用标准库）。

它干两件事：
  * 在 http://127.0.0.1:8765/ 上把 prototype/dist/ 的构建产物端出去；
  * 在 /events 上开一条 SSE，把状态帧推给页面：
        data: {"phase": "recording", "level": 0.42, "text": "正在录音"}

默认自带"假数据"：一直循环演一遍「待机 → 录音 → 识别 → 完成」，不跑真实程序也能看效果。
关掉它用 --no-sim，然后由真实程序推状态（见下面 publish()）。

真实程序怎么接（以后做浮标嵌入时）：
    在 ui.py 里给浮标发状态的那两个地方各加一行——
        overlay.show(state, text)      ->  publish(state, 0.0, text)
        overlay.set_level(level)       ->  publish("recording", level)
    phase 的名字和浮标的 state 是同一套：idle / recording / recognizing。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")

PHASE_TEXT = {
    "idle": "",
    "recording": "正在录音",
    "recognizing": "识别中…",
    "done": "已识别",
    "error": "识别出错",
}

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


class Broadcaster:
    """把一个状态帧发给所有打开着的 SSE 连接。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: set[queue.Queue] = set()
        self._last: str | None = None

    def register(self) -> queue.Queue:
        box: queue.Queue = queue.Queue(maxsize=8)
        with self._lock:
            self._clients.add(box)
            last = self._last
        if last is not None:
            box.put_nowait(last)  # 新连上先给一帧，页面不用干等
        return box

    def unregister(self, box: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(box)

    def publish(self, frame: dict) -> None:
        data = json.dumps(frame, ensure_ascii=False)
        with self._lock:
            self._last = data
            clients = list(self._clients)
        for box in clients:
            try:
                box.put_nowait(data)
            except queue.Full:
                try:
                    box.get_nowait()  # 客户端落后了，丢旧帧、只留最新
                    box.put_nowait(data)
                except queue.Empty:
                    pass


BROADCAST = Broadcaster()


def publish(phase: str, level: float = 0.0, text: str | None = None) -> None:
    """推一帧状态。真实程序接进来时调这个。"""
    if text is None:
        text = PHASE_TEXT.get(phase, "")
    BROADCAST.publish(
        {"phase": phase, "level": max(0.0, min(1.0, float(level))), "text": text}
    )


# ---------- 假数据 ----------

SCRIPT = [
    ("idle", 1.2),
    ("recording", 3.8),
    ("recognizing", 2.2),
    ("done", 1.4),
]


def _envelope(t: float) -> float:
    """把音节包络叠上去，让假音量看着像人声而不是纯正弦。"""
    syllable = 0.5 + 0.5 * math.sin(t * 11.0)
    phrase = (0.5 + 0.5 * math.sin(t * 1.7 + 0.4)) ** 1.6
    grain = 0.85 + 0.15 * math.sin(t * 37.0)
    return max(0.0, min(1.0, phrase * (0.22 + 0.78 * syllable) * grain))


def simulate() -> None:
    index = 0
    start = time.monotonic()
    while True:
        phase, duration = SCRIPT[index]
        elapsed = time.monotonic() - start
        if elapsed >= duration:
            index = (index + 1) % len(SCRIPT)
            start = time.monotonic()
            phase, duration = SCRIPT[index]
            elapsed = 0.0
        publish(phase, _envelope(elapsed) if phase == "recording" else 0.0)
        time.sleep(0.05)  # 20Hz，和真实浮标的音量更新频率接近


# ---------- HTTP ----------

HINT_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>voice-glow 原型</title>
<style>body{background:#0f1114;color:#e8eaed;font:14px/1.7 -apple-system,"Noto Sans CJK SC",sans-serif;
margin:0;display:grid;place-items:center;height:100vh}main{max-width:620px;padding:0 24px}
code{background:#1b1f26;border:1px solid #262a30;border-radius:5px;padding:1px 6px}
h1{font-size:18px}p{color:#9aa3ad}</style></head>
<body><main><h1>还没构建页面</h1>
<p>SSE 流已经在 <code>/events</code> 上了，但 <code>prototype/dist/</code> 还不存在。</p>
<p>开发模式：在本目录跑 <code>npm run dev</code>，然后开 vite 给的地址（它已把 <code>/events</code> 代理到这里）。</p>
<p>或者构建一次：<code>npm run build</code>，然后刷新这个页面即可由本服务直接托管。</p>
</main></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "voice-glow-prototype"

    def log_message(self, fmt, *args):  # 别把终端刷满
        pass

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/events":
            self._serve_events()
            return
        self._serve_static(path)

    # SSE：一条长连接，一直挂着推状态
    def _serve_events(self) -> None:
        box = BROADCAST.register()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 500\n\n")
            self.wfile.flush()
            while True:
                try:
                    data = box.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # 心跳，免得中间层掐连接
                else:
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BROADCAST.unregister(box)

    def _serve_static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        target = os.path.normpath(os.path.join(DIST, rel))
        if target != DIST and not target.startswith(DIST + os.sep):
            self._send(403, "text/plain; charset=utf-8", b"forbidden")
            return
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            if rel == "index.html":
                self._send(200, "text/html; charset=utf-8", HINT_HTML.encode("utf-8"))
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as handle:
            body = handle.read()
        self._send(200, MIME.get(ext, "application/octet-stream"), body)

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="voice-glow 原型的桥接/静态服务器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-sim", action="store_true", help="不要假数据，只等真实程序推状态")
    args = parser.parse_args()

    if not args.no_sim:
        threading.Thread(target=simulate, daemon=True, name="sim").start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"bridge 在 {url}")
    print(f"  SSE 流：{url}events")
    if not os.path.isdir(DIST):
        print("  dist/ 还没构建——页面会给出提示，或者改用 npm run dev")
    print("  Ctrl-C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
