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

from captions import Captions, CaptionsError

MAX_BODY = 6 * 1024**2  # the plugin stops anything bigger; a caption file is far smaller


def json_field(body, key):
    try:
        value = json.loads(body)[key]
    except (ValueError, KeyError, TypeError):
        raise CaptionsError("That request didn't make sense.")
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise CaptionsError("Pick at least one caption.")
    return value


def _routes(captions):
    return [
        ("GET", r"/health", lambda m, b: {"ok": True}),
        ("GET", r"/videos", lambda m, b: captions.videos()),
        ("GET", r"/videos/([^/]+)", lambda m, b: captions.video(m[1])),
        ("POST", r"/videos/([^/]+)/remove", lambda m, b: captions.remove(m[1], json_field(b, "captions"))),
        ("POST", r"/videos/([^/]+)/add/([^/]+)/([^/]+)", lambda m, b: captions.add(m[1], m[2], m[3], b) or {"ok": True}),
        ("GET", r"/jobs", lambda m, b: captions.jobs()),
        ("GET", r"/changes", lambda m, b: captions.changes()),
    ]


def make_server(captions, host, port):
    routes = [(method, re.compile(pattern), run) for method, pattern, run in _routes(captions)]

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

        def _answer(self):
            path = self.path.split("?")[0]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    self.close_connection = True
                    return self._send(413, {"error": "That's too big."})
                body = self.rfile.read(length) if length else b""
                for method, pattern, run in routes:
                    match = pattern.fullmatch(path)
                    if match and method == self.command:
                        return self._send(200, run(match, body))
                self._send(404, {"error": "Not found."})
            except CaptionsError as err:
                self._send(400, {"error": str(err)})
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True  # they left before the answer finished, which is fine
            except Exception:
                logging.exception("%s %s failed", self.command, path)
                self._send(500, {"error": "Something went wrong in the captions service."})

        do_GET = do_POST = _answer

    return ThreadingHTTPServer((host, port), Handler)


def main():
    captions = Captions({"movie": "/library/movies", "show": "/library/shows"})
    captions.start()
    server = make_server(captions, "0.0.0.0", 8098)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    captions.stop()


if __name__ == "__main__":
    main()
