"""A small stand-in for Real-Debrid and its file host, for the throwaway Jellyfin run. Shapes copied from the live API."""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl

TOKEN = "e2e-fake-token"
BASE = "http://fake-rd:9000"
MB = 1024 * 1024
SIZES = {"film": 6 * MB, "ep1": 4 * MB, "ep2": 4 * MB, "link": 3 * MB, "new": 2 * MB, "span": 5 * MB}


def torrent(id, status, files, progress=100, **extra):
    files = [{"id": n + 1, "path": p, "bytes": size, "selected": sel} for n, (p, size, sel) in enumerate(files)]
    links = [f"https://rd.example/{id}/{f['id']}" for f in files if f["selected"]] if status == "downloaded" else []
    return {"id": id, "filename": extra.pop("filename", files[0]["path"].strip("/") if files else id), "hash": "aa" + id, "bytes": sum(f["bytes"] for f in files),
            "host": "real-debrid.com", "split": 2000, "progress": progress, "status": status, "added": "2026-11-02T10:00:00.000Z", "ended": "2026-11-02T10:05:00.000Z",
            "files": files, "links": links, **extra}


TORRENTS = {t["id"]: t for t in [
    torrent("OLDEST", "downloaded", [("/The.Oldest.Film.1990.1080p.mkv", MB, 1)]),  # far enough down to need Show more
    *[torrent(f"OLD{n}", "downloaded", [(f"/Old.Film.{n}.1999.1080p.mkv", MB, 1)]) for n in range(59)],
    torrent("FAIL1", "magnet_error", [], 0, filename="Broken <img src=x onerror=window.__xss=1>"),
    torrent("CACHE1", "downloading", [("/Caching.Film.2020.1080p.mkv", 9 * MB, 1)], 40, speed=3 * MB, seeders=12),
    torrent("PICK1", "waiting_files_selection", [("/Pack/One.2001.mkv", 2 * MB, 0), ("/Pack/Two.2002.mkv", 2 * MB, 0), ("/Pack/sample.mkv", MB, 0)], 0, filename="Pack of films"),
    torrent("PACK1", "downloaded", [("/Ted.Lasso.S04/Ted.Lasso.s04e01.1080p.mkv", SIZES["ep1"], 1), ("/Ted.Lasso.S04/notes.nfo", 100, 0), ("/Ted.Lasso.S04/Ted.Lasso.s04e02.1080p.mkv", SIZES["ep2"], 1)], filename="Ted.Lasso.S04.1080p"),
    torrent("FILM1", "downloaded", [("/Buddy.2026.1080p.WEB.mkv", SIZES["film"], 1)]),
    torrent("SPAN1", "downloaded", [("/Pelicula.Buena.2021.1080p.BluRay.Spanish.mkv", SIZES["span"], 1)]),
    torrent("RETRY1", "downloaded", [("/Retry.Film.2022.1080p.mkv", SIZES["new"], 1)]),
    torrent("EVIL1", "downloaded", [("/<img src=x onerror=xss=1>.2020.mkv", SIZES["new"], 1)]),
]}
DOWNLOADS = [{"id": "LINK1", "filename": "Some.Link.Film.1999.mkv", "mimeType": "video/x-matroska", "filesize": SIZES["link"], "link": "https://host.example/link",
              "host": "host.example", "host_icon": "i", "chunks": 16, "download": f"{BASE}/dl/link", "streamable": 1, "generated": "2026-11-02T09:00:00.000Z"}]
POINTS = [1600]
REFUSALS = {"RETRY1": 1, "EVIL1": 99}  # how many saves Real-Debrid turns down, so the page's Try again and Cancel get used


def direct(link):
    if link.startswith("https://rd.example/FILM1"):
        return "film"
    if link.startswith("https://rd.example/PACK1"):
        return "ep1" if link.endswith("/1") else "ep2"
    if link.startswith("https://rd.example/SPAN1"):
        return "span"
    if link.startswith(("https://rd.example/NEW", "https://rd.example/RETRY1", "https://rd.example/EVIL1")):
        return "new"
    return "link"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send(self, status, payload=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, name):
        size = SIZES.get(name)
        if size is None:
            return self.send(404, {"error": "no"})
        start = int(re.search(r"\d+", self.headers.get("Range", "0")).group())
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(size - start))
        self.end_headers()
        self.wfile.write(b"r" * (size - start))

    def handle_any(self):
        path = self.path.split("?")[0]
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if path.startswith("/dl/"):
            return self.serve_file(path[4:])
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self.send(401, {"error": "bad_token", "error_code": 8})
        form = dict(parse_qsl(raw.decode(errors="replace")))
        parts = path.strip("/").split("/")
        key = (self.command, path)
        if key == ("GET", "/user"):
            return self.send(200, {"id": 1, "username": "e2e", "email": "e@example.com", "points": POINTS[0], "locale": "en", "avatar": "", "type": "premium",
                                   "premium": 4258937, "expiration": "2026-12-22T06:00:57.000Z"})
        if key == ("GET", "/torrents/activeCount"):
            return self.send(200, {"nb": 2, "limit": 100})
        if key == ("GET", "/torrents"):
            return self.send(200, [{k: v for k, v in t.items() if k != "files"} for t in reversed(list(TORRENTS.values()))])
        if self.command == "GET" and parts[:2] == ["torrents", "info"]:
            t = TORRENTS.get(parts[2])
            return self.send(200, t) if t else self.send(404, {"error": "unknown_ressource", "error_code": 7})
        if key == ("POST", "/torrents/addMagnet") or key == ("PUT", "/torrents/addTorrent"):
            t = torrent("NEW1", "waiting_files_selection", [("/New.Film.2024.1080p.mkv", SIZES["new"], 0)], 0)
            TORRENTS[t["id"]] = t
            return self.send(201, {"id": t["id"], "uri": "x"})
        if self.command == "POST" and parts[:2] == ["torrents", "selectFiles"]:
            t = TORRENTS[parts[2]]
            ids = form["files"].split(",")
            for f in t["files"]:
                f["selected"] = int(str(f["id"]) in ids)
            t.update(status="downloaded", progress=100, links=[f"https://rd.example/{t['id']}/{f['id']}" for f in t["files"] if f["selected"]])
            return self.send(204)
        if self.command == "DELETE" and parts[:2] == ["torrents", "delete"]:
            return self.send(204) if TORRENTS.pop(parts[2], None) else self.send(404, {"error": "unknown_ressource", "error_code": 7})
        if key == ("POST", "/unrestrict/link"):
            torrent_id = form["link"].removeprefix("https://rd.example/").split("/")[0]
            if REFUSALS.get(torrent_id):
                REFUSALS[torrent_id] -= 1
                return self.send(403, {"error": "permission_denied", "error_code": 9})
            name = direct(form["link"])
            return self.send(200, {"id": "D" + name, "filename": name + ".mkv", "mimeType": "video/x-matroska", "filesize": SIZES[name], "link": form["link"],
                                   "host": "real-debrid.com", "chunks": 32, "crc": 1, "download": f"{BASE}/dl/{name}", "streamable": 1})
        if key == ("POST", "/unrestrict/folder"):
            return self.send(404, {"error": "unknown_ressource", "error_code": 7})
        if key == ("GET", "/downloads"):
            return self.send(200, DOWNLOADS)
        if self.command == "DELETE" and parts[:2] == ["downloads", "delete"]:
            DOWNLOADS[:] = [d for d in DOWNLOADS if d["id"] != parts[2]]
            return self.send(204)
        if self.command == "GET" and parts[:2] == ["streaming", "mediaInfos"]:
            return self.send(200, {"filename": "Ted Lasso", "hoster": "x", "link": "l", "type": "show", "season": "4", "episode": "1", "year": None, "duration": 1800.5,
                                   "bitrate": 8000000, "size": 100, "details": {"video": {"und1": {"codec": "hevc", "width": 3840, "height": 2160}},
                                   "audio": {"eng1": {"lang": "English", "codec": "eac3", "channels": 5.1}}, "subtitles": {"spa1": {"lang": "Spanish"}}}})
        if key == ("GET", "/hosts/status"):
            return self.send(200, {"1fichier.com": {"id": "1f", "name": "1Fichier", "supported": 1, "status": "down"}, "real-debrid.com": {"id": "rd", "name": "Real-Debrid", "supported": 1, "status": "up"}})
        if key == ("GET", "/traffic"):
            return self.send(200, {"1fichier.com": {"left": 500 * MB, "bytes": 100, "links": 2, "limit": 600, "type": "gigabytes", "extra": 0, "reset": "daily"}})
        if key == ("POST", "/settings/convertPoints"):
            POINTS[0] -= 1000
            return self.send(204)
        return self.send(404, {"error": "unknown_ressource", "error_code": 7})

    do_GET = do_POST = do_PUT = do_DELETE = handle_any


ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
