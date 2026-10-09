"""The e2e /app proxy strips the prefix, passes Range, and streams SSE chunks before the upstream finishes."""
import http.client
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .conftest import SVA


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Up(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(2):
                data = f"event: step\ndata: {i}\n\n".encode()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
                time.sleep(0.6)
            self.wfile.write(b"0\r\n\r\n")
        else:
            body = f"{self.path}|{self.headers.get('Range')}".encode()
            self.send_response(206 if self.headers.get("Range") else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def test_prefix_proxy():
    up, px = free_port(), free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", up), Up)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc = subprocess.Popen([sys.executable, str(SVA / "app/tests/prefix_proxy.py"), "--listen", str(px), "--upstream", str(up)])
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", px), timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.1)
        c = http.client.HTTPConnection("127.0.0.1", px)
        c.request("GET", "/app/api/assets/x/points.bin", headers={"Range": "bytes=0-31"})
        r = c.getresponse()
        assert (r.status, r.read()) == (206, b"/api/assets/x/points.bin|bytes=0-31")
        c.request("GET", "/other")
        r = c.getresponse()
        r.read()
        assert r.status == 404
        t0 = time.monotonic()
        c.request("GET", "/app/sse")
        r = c.getresponse()
        first = r.read1(100)
        assert b"data: 0" in first and time.monotonic() - t0 < 0.5, "first SSE chunk must not wait for the stream to end"
        r.read()
    finally:
        proc.kill()
        srv.shutdown()
