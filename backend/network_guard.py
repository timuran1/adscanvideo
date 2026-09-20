"""Download proxy: resolve, reject private addresses, and connect to validated IPs.

Every redirect creates another proxied request. DNS is never resolved a second time
between validation and connection, preventing DNS rebinding through the proxy.
"""
import ipaddress
import select
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def public_addresses(host, port):
    if port not in (80, 443):
        raise ValueError('Only HTTP and HTTPS video links are supported.')
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not answers or any(not ipaddress.ip_address(a[4][0]).is_global for a in answers):
        raise ValueError('Private or local network URLs are not allowed.')
    return answers


def validate_url(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or len(url) > 2048:
            raise ValueError('Enter a public HTTP or HTTPS video URL.')
        public_addresses(p.hostname, p.port or (443 if p.scheme == 'https' else 80))
    except (OSError, ValueError) as e:
        raise ValueError('Enter a reachable public HTTP or HTTPS video URL.') from e
    return url


def connect_public(host, port):
    addresses = public_addresses(host, port)
    for family, kind, proto, _, addr in addresses:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(15)
        try:
            sock.connect(addr)
            return sock
        except OSError:
            sock.close()
    raise OSError('Cannot reach video host')


class Proxy(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_CONNECT(self):
        try:
            p = urlsplit('//' + self.path)
            with connect_public(p.hostname, p.port or 443) as upstream:
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                self.relay(upstream)
        except (OSError, ValueError):
            self.close_connection = True
            # Do not expose resolved addresses to clients.
            try:
                self.send_error(403, 'Video host unavailable or blocked')
            except OSError:
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            p = urlsplit(self.path)
            if p.scheme != 'http' or p.username or p.password:
                raise ValueError()
            with connect_public(p.hostname, p.port or 80) as upstream:
                path = p.path or '/'
                if p.query:
                    path += '?' + p.query
                headers = [f'{self.command} {path} HTTP/1.1', f'Host: {p.netloc}', 'Connection: close']
                for key, value in self.headers.items():
                    if key.lower() not in ('host', 'connection', 'proxy-connection', 'proxy-authorization', 'transfer-encoding', 'content-length'):
                        headers.append(f'{key}: {value}')
                upstream.sendall(('\r\n'.join(headers) + '\r\n\r\n').encode('latin-1'))
                self.relay(upstream)
        except (OSError, ValueError):
            self.send_error(403, 'Video host unavailable or blocked')
        self.close_connection = True

    def relay(self, upstream):
        while True:
            ready, _, _ = select.select([self.connection, upstream], [], [], 20)
            if not ready:
                return
            for src in ready:
                chunk = src.recv(65536)
                if not chunk:
                    return
                (upstream if src is self.connection else self.connection).sendall(chunk)


def start_proxy():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{server.server_port}'
