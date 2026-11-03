"""The service's web API: JSON over HTTP on the private Docker network.

No login here on purpose: nothing publishes this port, and the Jellyfin plugin is the only way in from
outside, for admins only. Run: python3 server.py
"""
import json
import logging
import re
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from engine import Engine, RDError

MAX_JSON = 64 * 1024
MAX_TORRENT = 5 * 1024**2
MAX_DRAIN = 64 * 1024**2


class TooBig(Exception):
    pass


def _routes(rd):
    json_field = lambda body, key: (body or {}).get(key) if isinstance(body, dict) else None

    def file_number(text):
        if not text.isdigit():
            raise RDError("That file isn't in this torrent.")
        return int(text)

    return [
        ("GET", r"/health", lambda m, b: {"ok": True}),
        ("GET", r"/overview", lambda m, b: rd.overview()),
        ("GET", r"/torrents", lambda m, b: rd.torrents()),
        ("GET", r"/torrents/([^/]+)", lambda m, b: rd.torrent(m[1])),
        ("GET", r"/torrents/([^/]+)/files/([^/]+)", lambda m, b: rd.file_info(m[1], file_number(m[2]))),
        ("GET", r"/queue", lambda m, b: rd.queue()),
        ("GET", r"/links", lambda m, b: rd.links()),
        ("GET", r"/links/([^/]+)", lambda m, b: rd.link_info(m[1])),
        ("GET", r"/hosts", lambda m, b: rd.hosts()),
        ("POST", r"/add", lambda m, b: rd.add(json_field(b, "link") or "")),
        ("POST", r"/torrents/([^/]+)/select", lambda m, b: rd.select(m[1], json_field(b, "files") or [])),
        ("POST", r"/torrents/([^/]+)/save", lambda m, b: rd.save(m[1], json_field(b, "picks"))),
        ("POST", r"/links/([^/]+)/save", lambda m, b: rd.save_link(m[1], json_field(b, "pick"))),
        ("POST", r"/queue/clear", lambda m, b: rd.clear()),
        ("POST", r"/queue/([^/]+)/retry", lambda m, b: rd.retry(m[1])),
        ("POST", r"/points/convert", lambda m, b: rd.convert_points()),
        ("DELETE", r"/torrents/([^/]+)", lambda m, b: rd.delete_torrent(m[1])),
        ("DELETE", r"/links/([^/]+)", lambda m, b: rd.delete_link(m[1])),
        ("DELETE", r"/queue/([^/]+)", lambda m, b: rd.cancel(m[1])),
    ]


def make_server(rd, host, port):
    routes = [(method, re.compile(pattern), run) for method, pattern, run in _routes(rd)]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _raw_body(self, limit):
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                self.close_connection = True
                left = min(length, MAX_DRAIN)  # let the sender finish, so it can read the answer
                while left > 0:
                    got = self.rfile.read(min(left, 1 << 20))
                    if not got:
                        break
                    left -= len(got)
                raise TooBig()
            return self.rfile.read(length) if length else b""

        def _json_body(self):
            raw = self._raw_body(MAX_JSON)
            if not raw:
                return None
            try:
                return json.loads(raw)
            except ValueError:
                raise RDError("That request didn't make sense.")

        def _answer(self):
            path = self.path.split("?")[0]
            try:
                if (self.command, path) == ("POST", "/add-torrent"):
                    return self._send(200, rd.add_torrent_file(self._raw_body(MAX_TORRENT)))
                for method, pattern, run in routes:
                    match = pattern.fullmatch(path)
                    if match and method == self.command:
                        body = self._json_body() if method == "POST" else None
                        out = run(match, body)
                        return self._send(200, {"ok": True} if out is None else out)
                self._send(404, {"error": "Not found."})
            except RDError as err:
                self._send(400, {"error": str(err)})
            except TooBig:
                self._send(413, {"error": "That's too big."})
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True  # they left before the answer finished, which is fine
            except Exception:
                logging.exception("%s %s failed", self.command, path)
                self._send(500, {"error": "Something went wrong in the Real-Debrid service."})

        do_GET = do_POST = do_DELETE = _answer

    return ThreadingHTTPServer((host, port), Handler)


def main():
    rd = Engine({"movie": "/library/movies", "show": "/library/shows"}, "/state/state.json")
    rd.start()
    server = make_server(rd, "0.0.0.0", 8097)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    rd.stop()


if __name__ == "__main__":
    main()
