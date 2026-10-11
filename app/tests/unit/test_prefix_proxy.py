"""Bodyless responses must not poison the prefix proxy's persistent connection."""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from app.tests.prefix_proxy import handler


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/not-modified':
            self.send_response(304)
        elif self.path == '/empty':
            self.send_response(204)
        else:
            self.send_response(200)
            self.send_header('Content-Length', '7')
        self.end_headers()
        if self.path == '/script':
            self.wfile.write(b'let x=1')

    def do_HEAD(self):
        self.send_response(200)
        if self.path == '/script-length':
            self.send_header('Content-Length', '7')
        self.end_headers()


@contextmanager
def serving(request_handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), request_handler)
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('method,path,status', [
    ('GET', '/not-modified', 304),
    ('GET', '/empty', 204),
    ('HEAD', '/script', 200),
    ('HEAD', '/script-length', 200),
])
def test_bodyless_response_preserves_next_response_on_same_connection(method, path, status):
    with serving(Upstream) as upstream, serving(handler('/app', upstream)) as proxy:
        connection = http.client.HTTPConnection('127.0.0.1', proxy, timeout=3)
        try:
            connection.request(method, '/app' + path)
            first = connection.getresponse()
            assert first.status == status
            assert first.getheader('Transfer-Encoding') is None
            if path == '/script-length':
                assert first.getheader('Content-Length') == '7'
            assert first.read() == b''
            connection.request('GET', '/app/script')
            following = connection.getresponse()
            assert following.status == 200
            assert following.read() == b'let x=1'
        finally:
            connection.close()
