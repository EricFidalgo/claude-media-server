"""The service's web API, over real HTTP, on top of the fake Real-Debrid from test_engine."""
import json
import shutil
import socket
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from server import make_server
from test_engine import CONTENT, FakeRD, make, torrent


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fake = FakeRD()
        self.rd = make(self.tmp, self.fake)
        self.rd.start()
        self.addCleanup(self.rd.stop)
        self.server = make_server(self.rd, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_address[1]}{path}", method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                text = r.read()
                return r.status, json.loads(text) if text else None
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class ApiTest(ServerCase):
    def test_reports_healthy_without_touching_real_debrid(self):
        self.assertEqual(self.call("GET", "/health"), (200, {"ok": True}))
        self.assertEqual(self.fake.calls, [])

    def test_the_key_can_be_set_and_is_never_sent_back(self):
        status, body = self.call("POST", "/token", {"key": "my-own-key"})
        self.assertEqual(status, 200)
        self.assertNotIn("my-own-key", json.dumps(body))
        self.assertNotIn("my-own-key", json.dumps(self.call("GET", "/overview")[1]))
        self.assertEqual(self.call("POST", "/token", {"key": ""})[0], 400)

    def test_reads(self):
        self.fake.torrents["M"] = torrent("M", files=[("/Buddy.2026.mkv", 3000, 1)])
        self.assertEqual(self.call("GET", "/overview")[1]["account"]["username"], "tester")
        self.assertEqual(self.call("GET", "/torrents")[1][0]["id"], "M")
        self.assertEqual(self.call("GET", "/torrents/M")[1]["files"][0]["guess"]["title"], "Buddy")
        self.assertEqual(self.call("GET", "/queue"), (200, []))
        self.assertEqual(self.call("GET", "/links"), (200, []))
        self.assertEqual(self.call("GET", "/hosts")[1][0]["status"], "up")

    def test_a_person_can_fix_it_error_is_a_400_with_the_message(self):
        status, body = self.call("POST", "/add", {"link": "just words"})
        self.assertEqual(status, 400)
        self.assertIn("Paste", body["error"])

    def test_add_then_save_lands_in_the_library(self):
        self.fake.next_magnet = torrent("M", files=[("/Buddy.2026.mkv", 3000, 1)])
        self.assertEqual(self.call("POST", "/add", {"link": "magnet:?xt=urn:btih:abc"})[1]["id"], "M")
        status, jobs = self.call("POST", "/torrents/M/save", {"picks": [{"file": 1, "kind": "movie", "title": "Buddy", "year": 2026}]})
        self.assertEqual((status, jobs[0]["path"]), (200, "Buddy (2026)/Buddy (2026).mkv"))
        dest = self.tmp / "Movies" / "Buddy (2026)" / "Buddy (2026).mkv"
        for _ in range(200):
            if dest.exists():
                break
            threading.Event().wait(0.01)
        self.assertEqual(dest.read_bytes(), CONTENT["https://dl.example/a"])

    def test_a_torrent_file_goes_up_as_raw_bytes(self):
        self.fake.next_magnet = torrent("T", "waiting_files_selection", files=[("/a.mkv", 10, 0), ("/b.mkv", 10, 0)], progress=0)
        self.assertEqual(self.call("POST", "/add-torrent", raw=b"d8:announce")[1], {"kind": "torrent", "id": "T", "choose": True})
        self.assertEqual(self.fake.uploaded, b"d8:announce")

    def test_too_big_is_refused(self):
        self.assertEqual(self.call("POST", "/add-torrent", raw=b"x" * (5 * 1024**2 + 1))[0], 413)
        self.assertEqual(self.call("POST", "/add", raw=b"x" * (64 * 1024 + 1))[0], 413)

    def test_select_delete_and_queue_controls(self):
        self.fake.torrents["W"] = torrent("W", "waiting_files_selection", files=[("/a.mkv", 10, 0), ("/b.mkv", 10, 0)], progress=0)
        self.assertEqual(self.call("POST", "/torrents/W/select", {"files": [2]})[0], 200)
        self.assertEqual(self.fake.torrents["W"]["files"][1]["selected"], 1)
        self.assertEqual(self.call("DELETE", "/torrents/W")[0], 200)
        self.assertEqual(self.fake.torrents, {})
        self.assertEqual(self.call("POST", "/queue/nope/retry")[0], 400)
        self.assertEqual(self.call("POST", "/queue/clear")[0], 200)

    def test_converts_points(self):
        self.assertEqual(self.call("POST", "/points/convert")[0], 200)
        self.assertEqual(self.fake.points, 600)

    def test_unknown_routes_and_bad_bodies(self):
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("DELETE", "/overview")[0], 404)
        self.assertEqual(self.call("GET", "/torrents/..%2f..%2fuser")[0], 400)
        self.assertEqual(self.call("POST", "/add", raw=b"{not json")[0], 400)
        self.fake.torrents["M"] = torrent("M", files=[("/Buddy.2026.mkv", 3000, 1)])
        for picks in ("nope", [], ["nope"], [{"file": [1]}], [{"file": "1"}], None):
            self.assertEqual(self.call("POST", "/torrents/M/save", {"picks": picks})[0], 400, picks)
        self.assertEqual(self.call("POST", "/torrents/M/select", {"files": ["all"]})[0], 400)
        self.assertEqual(self.call("POST", "/add", ["not", "a", "dict"])[0], 400)

    def test_a_surprise_doesnt_leak_details(self):
        self.rd.queue = lambda: 1 / 0
        status, body = self.call("GET", "/queue")
        self.assertEqual(status, 500)
        self.assertEqual(body, {"error": "Something went wrong in the Real-Debrid service."})

    def test_a_visitor_who_leaves_early_isnt_an_error(self):
        self.rd.queue = lambda: [{"id": "x" * 10**7}]  # bigger than the socket's buffer, so the write is still going when they leave
        host, port = self.server.server_address[:2]
        with self.assertNoLogs(level="ERROR"):
            client = socket.create_connection((host, port))
            client.sendall(b"GET /queue HTTP/1.1\r\nHost: x\r\n\r\n")
            client.recv(1)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # hang up at once
            client.close()
            threading.Event().wait(0.3)


if __name__ == "__main__":
    unittest.main()
