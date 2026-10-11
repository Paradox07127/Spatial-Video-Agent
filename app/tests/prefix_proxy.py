"""Strip-prefix reverse proxy for e2e (stands in for the site Ingress `/app(/|$)(.*)` -> `/$2` rewrite).

  python app/tests/prefix_proxy.py --listen 8301 --prefix /app --upstream 8300

Streams responses unbuffered (SSE) and passes Range headers through. Requests outside the prefix get 404.
"""
import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-connection"}


def handler(prefix: str, upstream: int):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _do(self):
            if not (self.path == prefix or self.path.startswith(prefix + "/")):
                self.send_error(404, "outside prefix")
                return
            path = self.path[len(prefix):] or "/"
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or None
            conn = http.client.HTTPConnection("127.0.0.1", upstream, timeout=3600)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP | {"host"}}
            headers["X-Forwarded-Prefix"] = prefix
            conn.request(self.command, path, body=body, headers=headers)
            r = conn.getresponse()
            self.send_response(r.status, r.reason)
            has_body = self.command != 'HEAD' and r.status >= 200 and r.status not in (204, 304)
            for k, v in r.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            length = r.getheader("Content-Length")
            if has_body and length is None:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            # 304/204/1xx and HEAD end at the header boundary. A chunk
            # terminator here would become the next keep-alive status line.
            if not has_body:
                conn.close()
                return
            while True:
                chunk = r.read1(65536) if length is None else r.read(65536)
                if not chunk:
                    break
                if length is None:
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if length is None:
                self.wfile.write(b"0\r\n\r\n")
            conn.close()

        do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _do

    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, default=8301)
    ap.add_argument("--prefix", default="/app")
    ap.add_argument("--upstream", type=int, default=8300)
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.listen), handler(a.prefix.rstrip("/"), a.upstream)).serve_forever()
