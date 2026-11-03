"""Run: cd ~/media-stack/realdebrid/service && python3 -m unittest -v   (a fake Real-Debrid, a temp library; no network, no real account)"""
import io
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

import engine
from engine import Engine, RDError, destination, guess, label

API = engine.API
CONTENT = {"https://dl.example/a": b"a" * 3000, "https://dl.example/b": b"b" * 2000}


class Response(io.BytesIO):
    def __init__(self, body=b"", status=200, headers=None, die_after=None, gate=None):
        super().__init__(body)
        self.status, self.headers, self.die_after, self.gate = status, headers or {}, die_after, gate

    def read(self, n=-1):
        if self.gate:
            self.gate.wait(5)
        if self.die_after is not None:
            if self.tell() >= self.die_after:
                raise OSError("connection lost")
            n = min(n if n > 0 else self.die_after, self.die_after - self.tell())
        return super().read(n)


def http_error(url, code, body=None):
    return urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(json.dumps(body or {}).encode()))


def torrent(id, status="downloaded", files=(), progress=100, filename=None, **extra):
    """A torrent in the shape the real /torrents/info returns (checked against the live API on 2026-11-02)."""
    files = [{"id": n + 1, "path": path, "bytes": size, "selected": int(sel)} for n, (path, size, sel) in enumerate(files)]
    links = [f"https://rd.example/{id}/{f['id']}" for f in files if f["selected"]] if status == "downloaded" else []
    return {"id": id, "filename": filename or (files[0]["path"].strip("/") if files else id), "hash": "abc" + id, "bytes": sum(f["bytes"] for f in files),
            "status": status, "progress": progress, "added": "2026-11-02T10:00:00.000Z", "host": "real-debrid.com", "split": 2000,
            "files": files, "links": links, **extra}


class FakeRD:
    """Stands in for urlopen: a small Real-Debrid and its file host."""

    def __init__(self):
        self.torrents, self.calls, self.downloads, self.next_magnet = {}, [], [], None
        self.points = 1600
        self.die_after, self.gate, self.ignore_range, self.short_once = None, None, False, False
        self.dead_links, self.archive_links = set(), set()
        self.folders = {}  # link -> the links inside it
        self.not_a_file = set()  # links /unrestrict/link refuses, as it does for a folder
        self.media = {}
        self.ranges = []  # what each download asked to resume from
        self.flaky = {}  # link -> error codes /unrestrict/link answers before it works

    def __call__(self, req, timeout=None):
        url, method = req.full_url, req.get_method()
        self.calls.append((method, url))
        if url in CONTENT:
            return self.serve(url, req)
        path = url.removeprefix(API)
        form = dict(urllib.parse.parse_qsl(req.data.decode())) if method == "POST" and req.data else {}
        out = self.route(method, path, form, req)
        if out == "missing":
            raise http_error(url, 404, {"error": "unknown_ressource", "error_code": 7})
        return Response(b"" if out is None else json.dumps(out).encode())

    def serve(self, url, req):
        body, die = CONTENT[url], self.die_after
        self.die_after = None  # only the first attempt breaks
        if self.short_once:
            self.short_once = False
            return Response(body[:100])
        rng = req.get_header("Range")
        self.ranges.append(rng)
        if rng and not self.ignore_range:
            return Response(body[int(rng[6:-1]):], 206, die_after=die, gate=self.gate)
        return Response(body, die_after=die, gate=self.gate)

    def route(self, method, path, form, req):
        parts = path.split("?")[0].strip("/").split("/")
        if (method, path) == ("GET", "/user"):
            return {"id": 1, "username": "tester", "email": "t@example.com", "points": self.points, "type": "premium",
                    "premium": 4258937, "expiration": "2026-12-22T06:00:57.000Z"}
        if (method, path) == ("GET", "/torrents/activeCount"):
            return {"nb": 2, "limit": 100}
        if method == "GET" and parts == ["torrents"]:
            return [{k: v for k, v in t.items() if k != "files"} for t in reversed(list(self.torrents.values()))]
        if method == "GET" and parts[:2] == ["torrents", "info"]:
            return self.torrents.get(parts[2], "missing")
        if (method, path) == ("POST", "/torrents/addMagnet"):
            t = self.next_magnet
            self.torrents[t["id"]] = t
            return {"id": t["id"], "uri": "x"}
        if (method, path) == ("PUT", "/torrents/addTorrent"):
            self.uploaded = req.data
            t = self.next_magnet
            self.torrents[t["id"]] = t
            return {"id": t["id"], "uri": "x"}
        if method == "POST" and parts[:2] == ["torrents", "selectFiles"]:
            t = self.torrents.get(parts[2])
            if not t:
                return "missing"
            ids = form["files"].split(",")
            for f in t["files"]:
                f["selected"] = int(str(f["id"]) in ids)
            t["status"] = "downloading"
            return None
        if (method, path) == ("POST", "/unrestrict/folder"):
            return self.folders.get(form["link"], "missing")
        if method == "GET" and parts[:2] == ["streaming", "mediaInfos"]:
            return self.media.get(parts[2], "missing")
        if method == "GET" and parts == ["downloads"]:
            return list(reversed(self.downloads))
        if method == "DELETE" and parts[:2] == ["downloads", "delete"]:
            found = [d for d in self.downloads if d["id"] == parts[2]]
            self.downloads = [d for d in self.downloads if d["id"] != parts[2]]
            return None if found else "missing"
        if (method, path) == ("GET", "/hosts/status"):
            return {"real-debrid.com": {"id": "real-debrid", "name": "Real-Debrid", "image": "i", "image_big": "b", "supported": 1, "status": "up", "check_time": "t", "competitors_status": {}},
                    "1fichier.com": {"id": "1f", "name": "1Fichier", "image": "i", "image_big": "b", "supported": 1, "status": "down", "check_time": "t", "competitors_status": {}}}
        if (method, path) == ("GET", "/traffic"):
            return {"1fichier.com": {"left": 500, "bytes": 100, "links": 2, "limit": 600, "type": "gigabytes", "extra": 0, "reset": "daily"}}
        if (method, path) == ("POST", "/settings/convertPoints"):
            if self.points < 1000:
                raise http_error(path, 503, {"error": "not_enough_points", "error_code": 26})
            self.points -= 1000
            return None
        if (method, path) == ("POST", "/unrestrict/link"):
            link = form["link"]
            if self.flaky.get(link):
                raise http_error(link, self.flaky[link].pop(0), {"error": "busy"})
            if link in self.not_a_file:
                raise http_error(link, 503, {"error": "hoster_unsupported", "error_code": 16})
            if link in self.dead_links:
                raise http_error(link, 503, {"error": "hoster_unavailable", "error_code": 19})
            url = "https://dl.example/" + ("a" if link.endswith("/1") else "b")
            return {"id": "D" + link[-1], "filename": link.rsplit("/", 1)[-1] + (".rar" if link in self.archive_links else ".mkv"), "filesize": len(CONTENT[url]), "link": link,
                    "host": "real-debrid.com", "chunks": 32, "download": url, "streamable": 1}
        if method == "DELETE" and parts[:2] == ["torrents", "delete"]:
            return None if self.torrents.pop(parts[2], None) else "missing"
        raise AssertionError(f"unexpected call {method} {path}")


def make(tmp, fake, **kw):
    kw.setdefault("spare", 0)
    return Engine({"movie": tmp / "Movies", "show": tmp / "Shows"}, tmp / "state.json", token="t", opener=fake, retry_delay=0, **kw)


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fake = FakeRD()
        self.rd = make(self.tmp, self.fake)


class AccountTest(EngineCase):
    def test_overview(self):
        o = self.rd.overview()
        self.assertEqual((o["account"]["username"], o["account"]["points"], o["account"]["expiration"]),
                         ("tester", 1600, "2026-12-22T06:00:57.000Z"))
        self.assertNotIn("email", o["account"])
        self.assertEqual(o["active"], {"nb": 2, "limit": 100})
        self.assertGreater(o["disk"]["free"], 0)
        self.assertTrue(o["configured"])

    def test_no_token_says_so_and_never_touches_the_network(self):
        with mock.patch.dict("os.environ", clear=True), mock.patch.object(engine, "TOKEN_FILES", []):
            rd = Engine({"movie": self.tmp}, self.tmp / "s.json", opener=lambda *a, **k: self.fail("network"))
            self.assertFalse(rd.overview()["configured"])
            with self.assertRaises(RDError):
                rd.torrents()

    def test_saving_a_key_connects_and_is_used_for_later_calls(self):
        seen = []
        def opener(req, timeout=None):
            seen.append(req.get_header("Authorization"))
            return self.fake(req, timeout)
        with mock.patch.dict("os.environ", clear=True), mock.patch.object(engine, "TOKEN_FILES", []):
            rd = Engine({"movie": self.tmp}, self.tmp / "s.json", opener=opener)
            self.assertFalse(rd.overview()["configured"])
            rd.set_token("  my-own-key \n")
            self.assertTrue(rd.overview()["configured"])
            self.assertEqual(set(seen), {"Bearer my-own-key"})
            # a restart finds the saved key
            again = Engine({"movie": self.tmp}, self.tmp / "s.json", opener=opener)
            self.assertTrue(again.overview()["configured"])

    def test_bad_token(self):
        def opener(req, timeout=None):
            raise http_error(req.full_url, 401, {"error": "bad_token", "error_code": 8})
        rd = make(self.tmp, opener)
        with self.assertRaisesRegex(RDError, "rejected"):
            rd.torrents()

    def test_a_rejected_key_changes_nothing(self):
        def opener(req, timeout=None):
            if req.get_header("Authorization") == "Bearer typo":
                raise http_error(req.full_url, 401, {"error": "bad_token", "error_code": 8})
            return self.fake(req, timeout)
        rd = make(self.tmp, opener)
        with self.assertRaisesRegex(RDError, "rejected"):
            rd.set_token("typo")
        self.assertTrue(rd.overview()["configured"])
        self.assertEqual(rd.overview()["account"]["username"], "tester")
        self.assertFalse((self.tmp / "token").exists())
        with self.assertRaises(RDError):
            rd.set_token("two words")

    def test_network_failure_is_a_readable_error(self):
        def opener(req, timeout=None):
            raise OSError("down")
        with self.assertRaisesRegex(RDError, "reach"):
            make(self.tmp, opener).torrents()


class TorrentsTest(EngineCase):
    PACK = [("/Show/Ted.Lasso.s04e01.mkv", 3000, 1), ("/Show/Ted.Lasso.s04e02.mkv", 2000, 1), ("/Show/notes.nfo", 5, 0)]

    def test_lists_the_whole_account(self):
        self.fake.torrents["A"] = torrent("A", files=[("/Older.Film.2001.mkv", 10, 1)])
        self.fake.torrents["B"] = torrent("B", "error", files=[("/Bad.mkv", 10, 0)], progress=0)
        rows = self.rd.torrents()
        self.assertEqual([(r["id"], r["status"]) for r in rows], [("B", "error"), ("A", "downloaded")])
        self.assertEqual(rows[1]["saved"], 0)

    def test_the_list_names_each_torrent_in_two_lines(self):
        self.fake.torrents["A"] = torrent("A", files=[("/Movie.Title.2024.2160p.BluRay.mkv", 6 * 10**9, 1)])
        row = self.rd.torrents()[0]
        self.assertEqual(row["label"], {"name": "Movie Title (2024) BluRay [🇺🇸]", "description": "2160p [6GB]"})

    def test_each_file_in_a_torrent_is_named_in_two_lines(self):
        self.fake.torrents["P"] = torrent("P", files=[("/Show/Ted.Lasso.s04e01.1080p.mkv", 3 * 10**9, 1)])
        self.assertEqual(self.rd.torrent("P")["files"][0]["label"], {"name": "Ted Lasso S04E01 [🇺🇸]", "description": "1080p [3GB]"})

    def test_a_file_whose_name_says_nothing_borrows_the_torrents_name(self):
        self.fake.torrents["P"] = torrent("P", files=[("/S03E07.mkv", 3000, 1)], filename="Fallout.2024.S03.1080p")
        self.fake.torrents["M"] = torrent("M", files=[("/---.mkv", 3000, 1)], filename="Buddy.2026.1080p")
        self.assertEqual(self.rd.torrent("P")["files"][0]["guess"], {"kind": "show", "title": "Fallout (2024)", "season": 3, "episode": 7})
        self.assertEqual(self.rd.torrent("M")["files"][0]["guess"], {"kind": "movie", "title": "Buddy", "year": 2026})

    def test_a_torrents_only_video_is_filed_by_the_torrents_name_when_its_own_says_less(self):
        self.fake.torrents["S"] = torrent("S", files=[("/LQSA808 [www.newpct1.com].avi", 3000, 1)], filename="La Que Se Avecina - Temporada 8 [HDTV][Cap.808][Español Castellano]")
        self.fake.torrents["M"] = torrent("M", files=[("/Grp-mt1080.mkv", 3000, 1), ("/notes.nfo", 10, 0)], filename="Movie.Title.2024.1080p.BluRay")
        self.assertEqual(self.rd.torrent("S")["files"][0]["guess"], {"kind": "show", "title": "La Que Se Avecina", "season": 8, "episode": 8})
        self.assertEqual(self.rd.torrent("M")["files"][0]["guess"], {"kind": "movie", "title": "Movie Title", "year": 2024})

    def test_a_torrents_only_video_keeps_its_own_name_when_that_has_a_year_or_an_episode(self):
        self.fake.torrents["Y"] = torrent("Y", files=[("/Dude, Where's My Car (2000).mkv", 3000, 1)], filename="Comedy Night 1080p")
        self.fake.torrents["E"] = torrent("E", files=[("/Ted.Lasso.S01E02.mkv", 3000, 1)], filename="Some Pack")
        self.assertEqual(self.rd.torrent("Y")["files"][0]["guess"], {"kind": "movie", "title": "Dude, Where's My Car", "year": 2000})
        self.assertEqual(self.rd.torrent("E")["files"][0]["guess"], {"kind": "show", "title": "Ted Lasso", "season": 1, "episode": 2})

    def test_a_film_in_a_pack_keeps_its_own_name(self):
        self.fake.torrents["P"] = torrent("P", files=[("/Plain Name.mkv", 3000, 1), ("/Other Name.mkv", 3000, 1)], filename="Movie.Title.2024.1080p")
        self.assertEqual(self.rd.torrent("P")["files"][0]["guess"], {"kind": "movie", "title": "Plain Name", "year": None})

    def test_bare_episode_files_in_a_season_pack_are_filed_as_that_season(self):
        self.fake.torrents["N"] = torrent("N", files=[("/Ep1.mkv", 3000, 1), ("/Ep2.mkv", 3000, 1)], filename="Dear Killer Nannies [2026] s01 Dual YG")
        self.fake.torrents["W"] = torrent("W", files=[("/The.Wire.E05.mkv", 3000, 1), ("/06.mkv", 3000, 1)], filename="The.Wire.S02.1080p.BluRay")
        self.assertEqual(self.rd.torrent("N")["files"][1]["guess"], {"kind": "show", "title": "Dear Killer Nannies (2026)", "season": 1, "episode": 2})
        self.assertEqual([f["guess"] for f in self.rd.torrent("W")["files"]], [{"kind": "show", "title": "The Wire", "season": 2, "episode": 5},
                                                                               {"kind": "show", "title": "The Wire", "season": 2, "episode": 6}])

    def test_a_single_bare_episode_from_a_season_pack_is_still_that_episode(self):
        self.fake.torrents["O"] = torrent("O", files=[("/05.mkv", 3000, 1)], filename="The.Wire.S02.1080p.BluRay")
        self.assertEqual(self.rd.torrent("O")["files"][0]["guess"], {"kind": "show", "title": "The Wire", "season": 2, "episode": 5})

    def test_numbered_files_stay_films_outside_a_single_season_pack(self):
        self.fake.torrents["R"] = torrent("R", files=[("/05.mkv", 3000, 1), ("/06.mkv", 3000, 1)], filename="Family Guy Seasons 1 to 18 1080p")
        self.fake.torrents["F"] = torrent("F", files=[("/Rocky 2.mkv", 3000, 1), ("/Rocky 3.mkv", 3000, 1)], filename="Rocky Collection 1080p")
        self.fake.torrents["G"] = torrent("G", files=[("/05.mkv", 3000, 1), ("/06.mkv", 3000, 1)], filename="Some.Film.Pack.1080p-S4")
        self.assertEqual(self.rd.torrent("R")["files"][0]["guess"]["kind"], "movie")
        self.assertEqual(self.rd.torrent("F")["files"][0]["guess"], {"kind": "movie", "title": "Rocky 2", "year": None})
        self.assertEqual(self.rd.torrent("G")["files"][0]["guess"]["kind"], "movie")

    def test_magnet_with_one_video_starts_by_itself(self):
        self.fake.next_magnet = torrent("T", "waiting_files_selection", files=[("/Film.2001.mkv", 3000, 0), ("/readme.txt", 1, 0)], progress=0)
        self.assertEqual(self.rd.add("magnet:?xt=urn:btih:abc"), {"kind": "torrent", "id": "T", "choose": False})
        self.assertEqual([f["selected"] for f in self.fake.torrents["T"]["files"]], [1, 0])

    def test_magnet_with_several_videos_waits_for_a_choice(self):
        self.fake.next_magnet = torrent("T", "waiting_files_selection", files=[(p, s, 0) for p, s, _ in self.PACK], progress=0)
        self.assertEqual(self.rd.add("magnet:?xt=urn:btih:abc"), {"kind": "torrent", "id": "T", "choose": True})
        info = self.rd.torrent("T")
        self.assertEqual([(f["id"], f["video"], f["suggested"]) for f in info["files"]], [(1, True, True), (2, True, True), (3, False, False)])
        self.assertEqual(info["files"][0]["guess"], {"kind": "show", "title": "Ted Lasso", "season": 4, "episode": 1})
        self.rd.select("T", [2])
        self.assertEqual([f["selected"] for f in self.fake.torrents["T"]["files"]], [0, 1, 0])

    def test_sample_files_are_not_suggested(self):
        self.fake.torrents["T"] = torrent("T", "waiting_files_selection", files=[("/Film.2001.mkv", 3 * 10**9, 0), ("/Sample/Film.sample.mkv", 10**7, 0)], progress=0)
        self.assertEqual([f["suggested"] for f in self.rd.torrent("T")["files"]], [True, False])

    def test_samples_and_extras_in_folders_are_not_suggested(self):
        self.fake.torrents["T"] = torrent("T", files=[(p, 3000, 1) for p in (
            "/The.Film.2010.1080p.mkv", "/samples/sample2.mkv", "/Samples/Sample1.mkv", "/Extras/Making Of.mkv",
            "/Featurettes/Season 1 - Honest Trailer.mkv", "/DVD Extras Vol. 1 - Vol. 7/Vol. 1 Extras/Family Guy - Extra 2.mp4")])
        self.assertEqual([f["suggested"] for f in self.rd.torrent("T")["files"]], [True, False, False, False, False, False])

    def test_a_show_or_film_called_extras_is_still_suggested(self):
        self.fake.torrents["T"] = torrent("T", files=[("/Extras.S01/Extras.S01E01.mkv", 3000, 1), ("/The.Extra.Man.2010.mkv", 3000, 1), ("/Extras.2004.mkv", 3000, 1)])
        self.assertEqual([f["suggested"] for f in self.rd.torrent("T")["files"]], [True, True, True])

    def test_only_a_discs_main_stream_is_suggested_and_it_takes_the_torrents_name(self):
        self.fake.torrents["D"] = torrent("D", files=[("/BDMV/STREAM/00001.m2ts", 3000, 1), ("/BDMV/STREAM/00002.m2ts", 90000, 1), ("/BDMV/STREAM/00003.m2ts", 2000, 1), ("/BDMV/index.bdmv", 10, 1)],
                                          filename="The.Nun.2018.MULTi.COMPLETE.UHD.BLURAY")
        files = self.rd.torrent("D")["files"]
        self.assertEqual([f["suggested"] for f in files], [False, True, False, False])
        self.assertEqual(files[1]["guess"], {"kind": "movie", "title": "The Nun", "year": 2018})

    def test_torrent_file_upload(self):
        self.fake.next_magnet = torrent("T", "waiting_files_selection", files=[("/Film.2001.mkv", 3000, 0)], progress=0)
        self.assertEqual(self.rd.add_torrent_file(b"d8:announce")["id"], "T")
        self.assertEqual(self.fake.uploaded, b"d8:announce")

    def test_rejects_other_input(self):
        for bad in ("", "ftp://x", "just words", "file:///etc/passwd"):
            with self.assertRaises(RDError, msg=bad):
                self.rd.add(bad)

    def test_a_magic_id_cannot_reach_other_api_paths(self):
        for bad in ("../user", "a/b", "a?b", ""):
            with self.assertRaises(RDError, msg=bad):
                self.rd.torrent(bad)

    def test_delete(self):
        self.fake.torrents["A"] = torrent("A", files=[("/F.2001.mkv", 10, 1)])
        self.assertEqual(len(self.rd.torrents()), 1)
        self.rd.delete_torrent("A")
        self.assertEqual(self.rd.torrents(), [])
        self.rd.delete_torrent("A")  # already gone is fine


class GuessTest(unittest.TestCase):
    def test_movies(self):
        self.assertEqual(guess("Buddy.2026.1080p.WEB.h264-EDITH.mkv"), {"kind": "movie", "title": "Buddy", "year": 2026})
        self.assertEqual(guess("/It Follows 2014 BluRay 1080p.mkv"), {"kind": "movie", "title": "It Follows", "year": 2014})
        self.assertEqual(guess("El final de Oak Street (2026) [Bluray 1080p][Esp].mkv"),
                         {"kind": "movie", "title": "El final de Oak Street", "year": 2026})
        self.assertEqual(guess("2012.2009.1080p.mkv"), {"kind": "movie", "title": "2012", "year": 2009})

    def test_a_year_in_the_title_isnt_the_films_year(self):
        self.assertEqual(guess("Blade Runner 2049 (2017) 2160p.mkv"), {"kind": "movie", "title": "Blade Runner 2049", "year": 2017})
        self.assertEqual(guess("Blade.Runner.2049.2017.1080p.mkv"), {"kind": "movie", "title": "Blade Runner 2049", "year": 2017})

    def test_a_range_of_years_or_a_screen_size_doesnt_change_the_year(self):
        self.assertEqual(guess("The Batman Collection (1989-2018) 1080p")["year"], 1989)
        self.assertEqual(guess("Star Trek 2009 - 2016 1080p")["year"], 2009)
        self.assertEqual(guess("Some Film 2014 1920x1080.mkv"), {"kind": "movie", "title": "Some Film", "year": 2014})

    def test_movie_without_a_year_loses_the_release_junk(self):
        self.assertEqual(guess("Plain Name.mp4"), {"kind": "movie", "title": "Plain Name", "year": None})
        self.assertEqual(guess("Some.Film.1080p.WEB.h264-GRP.mkv"), {"kind": "movie", "title": "Some Film", "year": None})

    def test_shows(self):
        self.assertEqual(guess("South.Park.S29E02.Billionaire.Weenietown.2160p.ATV.WEB.DL.mkv"),
                         {"kind": "show", "title": "South Park", "season": 29, "episode": 2})
        self.assertEqual(guess("South Park - S29E01 - South American Biker Gangs - 2160p.mkv"),
                         {"kind": "show", "title": "South Park", "season": 29, "episode": 1})
        self.assertEqual(guess("/Ted.Lasso.s04e03.TVHUB.WEB.1080p.mkv"),
                         {"kind": "show", "title": "Ted Lasso", "season": 4, "episode": 3})
        self.assertEqual(guess("Top Gear 12x05 HDTV.mkv"), {"kind": "show", "title": "Top Gear", "season": 12, "episode": 5})

    def test_a_website_at_the_front_never_reaches_a_folder_name(self):
        self.assertEqual(guess("www.1TamilMV.lease - Toy Story 5 (2026) BluRay - 720p.mkv"), {"kind": "movie", "title": "Toy Story 5", "year": 2026})
        self.assertEqual(guess("www.UIndex.org    -    Michael.2026.1080p.WEB.mkv"), {"kind": "movie", "title": "Michael", "year": 2026})
        self.assertEqual(guess("Mr.Robot - S01E01.mkv"), {"kind": "show", "title": "Mr Robot", "season": 1, "episode": 1})

    def test_a_spanish_style_episode_number(self):
        self.assertEqual(guess("Ted Lasso [HDTV][Cap.407].avi"), {"kind": "show", "title": "Ted Lasso", "season": 4, "episode": 7})
        self.assertEqual(guess("La Que Se Avecina - Temp.6 [HDTV][Cap.612][Español Castellano][www.newpct.com].avi"),
                         {"kind": "show", "title": "La Que Se Avecina", "season": 6, "episode": 12})
        self.assertEqual(guess("Serie [Cap.1012].mkv"), {"kind": "show", "title": "Serie", "season": 10, "episode": 12})

    def test_a_show_keeps_its_year_the_way_jellyfin_reads_it(self):
        self.assertEqual(guess("Fallout.2024.S02E01.720p.mkv"), {"kind": "show", "title": "Fallout (2024)", "season": 2, "episode": 1})
        self.assertEqual(destination(guess("Fallout.2024.S02E01.720p.mkv"), ".mkv"), ("show", "Fallout (2024)/Season 02/Fallout (2024) S02E01.mkv"))

    def test_a_folder_style_name_keeps_everything_after_its_last_dot(self):
        self.assertEqual(guess("Some.Film.1080p"), {"kind": "movie", "title": "Some Film", "year": None})
        self.assertEqual(guess("Ted.Lasso.S01E02.ViruseProject"), {"kind": "show", "title": "Ted Lasso", "season": 1, "episode": 2})

    def test_an_all_lowercase_name_files_with_the_rest_of_its_show(self):
        self.assertEqual(guess("ted.lasso.s03e12.hdr.2160p.web.h265-ggwp.mkv"), {"kind": "show", "title": "Ted Lasso", "season": 3, "episode": 12})

    def test_a_title_keeps_words_that_only_look_like_release_details(self):
        self.assertEqual(guess("Charlotte's.Web.2006.1080p.mkv"), {"kind": "movie", "title": "Charlotte's Web", "year": 2006})
        self.assertEqual(guess("Charlotte's Web.mkv"), {"kind": "movie", "title": "Charlotte's Web", "year": None})

    def test_a_season_folder_names_its_files(self):
        self.assertEqual(guess("/Ted.Lasso.S04.1080p/S04E01.mkv"), {"kind": "show", "title": "Ted Lasso", "season": 4, "episode": 1})

    def test_older_video_files_can_be_saved(self):
        for ext in (".mpg", ".mpeg", ".m2ts", ".wmv"):
            self.assertEqual(destination({"kind": "movie", "title": "Film", "year": 2000}, ext), ("movie", f"Film (2000)/Film (2000){ext}"))

    def test_show_title_comes_from_the_folder_when_the_file_has_none(self):
        self.assertEqual(guess("/Rick and Morty/Season 3/S03E07.mkv"),
                         {"kind": "show", "title": "Rick and Morty", "season": 3, "episode": 7})


class LabelTest(unittest.TestCase):
    """The owner's AIOStreams format. Expected lines were written by reading each real name, not by running the code."""

    def lines(self, name, size=None):
        got = label(name, size)
        return got["name"], got["description"]

    def test_the_format_the_owner_asked_for(self):
        self.assertEqual(self.lines("Movie.Title.2024.2160p.BluRay.x265.English.mkv", 6_000_000_000),
                         ("Movie Title (2024) BluRay [🇺🇸]", "2160p [6GB]"))

    def test_sizes_are_rounded_whole_and_base_1000(self):
        for size, shown in ((999_227_023, "999MB"), (999_600_000, "1GB"), (6_400_000_000, "6GB"), (162_000_000_000, "162GB"), (1_500_000_000_000, "2TB"), (500, "500B")):
            self.assertEqual(label("Film.2020.1080p.mkv", size)["description"], f"1080p [{shown}]")

    def test_parts_that_are_not_known_are_left_out(self):
        self.assertEqual(self.lines("Plain Name.mp4"), ("Plain Name [🇺🇸]", ""))
        self.assertEqual(self.lines("Plain Name.mp4", 5_000_000), ("Plain Name [🇺🇸]", "[5MB]"))
        self.assertEqual(self.lines("The Odyssey 2026", 0), ("The Odyssey (2026) [🇺🇸]", ""))

    def test_resolution(self):
        for name, shown in (("Backrooms (2026) 1080p.mp4", "1080p"),
                            ("Resident Evil - Noche Cero (2026)1080p Latino [CLYON].mp4", "1080p"),
                            ("Ted.Lasso.S04.400p.NewComers", "400p"),
                            ("Rick and Morty S01E01 Pilot (1280x720) [Phr0stY].mkv", "720p"),
                            ("Family.Guy.S24.1080.WEBDL", "1080p"),
                            ("The.Nun.2018.MULTi.COMPLETE.UHD.BLURAY", "2160p"),
                            ("Get Hard (2015) [2160p] [4K] [WEB] [5.1] [YTS.MX]", "2160p"),
                            ("10 Cloverfield Lane 2016 PROPER UHD BluRay 1080p DD Atmos 5 1 DoVi HDR10 x265-SM737.mkv", "1080p"),
                            ("Buddy.2026.2160p.4K.WEB.x265.10bit.AAC5.1-[YTS.GG - YTS.BZ].mkv", "2160p"),
                            ("A.Film.2020.AAC5.1.x265.mkv", "")):
            self.assertEqual(label(name)["description"], shown, name)

    def test_quality(self):
        for name, first in (("10.Cloverfield.Lane.2016.1080p.BluRay.REMUX.AVC.DTS-HD.MA.TrueHD.7.1.Atmos-RARBG.mkv", "10 Cloverfield Lane (2016) BluRay REMUX [🇺🇸]"),
                            ("10.Cloverfield.Lane.2016.4K.HDR.DV.2160p BDRemux Ita Eng x265-NAHOM", "10 Cloverfield Lane (2016) BluRay REMUX [🇺🇸]"),
                            ("Airplane! 1980 1080p Blu-ray Remux AVC DTS-HD MA 5.1 - KRaLiMaRKo.mkv", "Airplane! (1980) BluRay REMUX [🇺🇸]"),
                            ("El final de Oak Street (2026) [Bluray 1080p][Esp].mkv", "El final de Oak Street (2026) BluRay [🇪🇸]"),
                            ("The Notebook 2004 Dual Audio [Hindi+English] 720p BRRip ESub - Team  MoviesBay", "The Notebook (2004) BluRay [🇺🇸]"),
                            ("Animals (2026) iTA-ENG.WEBDL.1080p.x264-Dr4gon.mkv", "Animals (2026) WEB-DL [🇺🇸]"),
                            ("Resident Evil (2026) WEBDL-1080p.mp4", "Resident Evil (2026) WEB-DL [🇺🇸]"),
                            ("Backrooms.2026.1080p.WEB-DL.H.264.Dual YG.mkv", "Backrooms (2026) WEB-DL [🇺🇸]"),
                            ("South.Park.S29E02.Billionaire.Weenietown.2160p.ATV.WEB.DL.DDP5.1.H.265-NTb.mkv", "South Park S29E02 WEB-DL [🇺🇸]"),
                            ("Buddy.2026.1080p.WEB.h264-EDITH.mkv", "Buddy (2026) WEB-DL [🇺🇸]"),
                            ("A.Film.2020.1080p.WEBRip.x264.mkv", "A Film (2020) WEBRip [🇺🇸]"),
                            ("A.Film.2020.1080p.WEB-Rip.x264.mkv", "A Film (2020) WEBRip [🇺🇸]"),
                            ("Titanic.1997.HDTV.1080p.x264.[ExYu-Subs].mp4", "Titanic (1997) HDTV [🇺🇸]"),
                            ("Other.Mommy.2026.TS.mp4", "Other Mommy (2026) TS [🇺🇸]"),
                            ("The Odyssey (2026) [1080p - HQ HDTS - x265 - AAC - [Tam+Hin+Tel+Eng] - HC-Sub] - MAZE.mkv", "The Odyssey (2026) TS [🇺🇸]"),
                            ("The.Odyssey.2026.ts.MULTi2.mkv", "The Odyssey (2026) TS [🇺🇸]"),
                            ("Backrooms (2026) 1080p.mp4", "Backrooms (2026) [🇺🇸]"),
                            ("A Film (2020).ts", "A Film (2020) [🇺🇸]")):  # a .ts file isn't a TS rip
            self.assertEqual(label(name)["name"], first, name)

    def test_flags_follow_the_languages_named(self):
        us, es, both, none = "🇺🇸", "🇪🇸", "🇺🇸 🇪🇸", None
        for name, flags in (("The Notebook 2004 Dual Audio [Hindi+English] 720p BRRip ESub - Team  MoviesBay", us),
                            ("El final de Oak Street (2026) [Bluray 1080p][Esp].mkv", es),
                            ("Toy Story 5 (2026) 720p BluRay - x264 - [Tel + Tam + Hin + Eng].mkv", us),
                            ("Animals (2026) iTA-ENG.WEBDL.1080p.x264-Dr4gon.mkv", us),
                            ("When.Harry.Met.Sally.1989.1080p.BluRay.ENG.LATINO.DTS.5.1.H264-BEN.THE.MEN.mkv", both),
                            ("The.Grand.Budapest.Hotel.2014.bluray-latino-e-ingles-subt.mp4", both),
                            ("El Secreto De Una Obsesion [MicroHD 1080p][AC3 5.1-Castellano-AC3 5.1 Ingles+Subs][ES-EN]", both),
                            ("Resident Evil - Noche Cero (2026)1080p Latino [CLYON].mp4", es),
                            ("Backrooms.Sin.salida.2026.1080p-Dual-Lat.mkv", both),
                            ("Airplane.1980.1080p.BluRay.AV1.Opus.Multi.en.ja.es.fr.de.it.mkv", both),
                            ("TheOdyssey_IMAX-TLR-2_4K-1.90_HEVC_10bit_EN-XX_TrueHD_51_TCQ_20260531.mkv", us),
                            ("Top.Gear.S12.VFF.1080p.HDTV.AAC.2.0.x264-NOTAG", none),
                            ("A Odisseia Dub 2026 - BAIXANDOVIATORRENT.COM.mp4", none),
                            ("Toy Story 5 (2026) 720p BluRay - x264 - [Tel + Tam + Hin].mkv", none)):
            shown = f" [{flags}]" if flags else ""
            self.assertTrue(label(name)["name"].endswith(shown) and ("[" in label(name)["name"]) == bool(flags), f"{name}: {label(name)['name']}")

    def test_subtitles_are_not_audio(self):
        self.assertEqual(label("Ted.Lasso.S04E07.720p.Subtitulado.Esp.SC.mp4")["name"], "Ted Lasso S04E07 [🇺🇸]")
        self.assertEqual(label("Cena Con Delitto - Knives Out (2019) 1080p H265 ita eng AC3 5.1 sub ita eng Licdom.mkv")["name"], "Cena Con Delitto - Knives Out (2019) [🇺🇸]")
        self.assertEqual(label("Film (2020) 1080p Hindi Eng Subs.mkv")["name"], "Film (2020)")

    def test_multi_means_the_original_english_is_there(self):
        self.assertEqual(label("Ted.Lasso.S04E05.MULTi.VFF.AD.DV.HDR.2160p.WEB.EAC3.5.1.Atmos.H265-FRQC.mkv")["name"], "Ted Lasso S04E05 WEB-DL [🇺🇸]")

    def test_an_untagged_name_counts_as_english_only_in_the_latin_alphabet(self):
        self.assertEqual(label("Plain Name.mp4")["name"], "Plain Name [🇺🇸]")
        self.assertEqual(label("Майкл.avi")["name"], "Майкл")
        self.assertEqual(label("Тед Лассо_4_сезон_1080p_RHS")["name"], "Тед Лассо 4 сезон")

    def test_a_website_or_the_film_title_is_not_a_language(self):
        self.assertEqual(label("La que se avecina Temporada 2 [www.cantabriatorrent.es]")["name"], "La que se avecina S02 [🇺🇸]")
        self.assertEqual(label("Spanish Apartment (2002) 1080p.mkv")["name"], "Spanish Apartment (2002) [🇺🇸]")
        self.assertEqual(label("La.Vida.S01E03.Vuelve.En.Casa.1080p.mkv")["name"], "La Vida S01E03 [🇺🇸]")

    def test_names_from_your_account_that_needed_more_care(self):
        for name, first in (("LQSA812 [www.newpct1.com].avi", "LQSA812 [🇺🇸]"),
                            ("ObsesionM1080.www.newpct1.com.mkv", "ObsesionM1080 [🇺🇸]"),
                            ("Одиссея [The Odyssey] (2026)", "Одиссея [The Odyssey] (2026)"),
                            ("Obsession+(1991).mp4", "Obsession (1991) [🇺🇸]"),
                            ("Obsession+-+Posedlost+2026+CZ+TITULKY+kinorip.avi", "Obsession - Posedlost (2026)"),
                            ("Romeo + Juliet (1996) 1080p.mkv", "Romeo + Juliet (1996) [🇺🇸]"),
                            ("ted.lasso.s03e12.hdr.2160p.web.h265-ggwp.mkv", "Ted Lasso S03E12 WEB-DL [🇺🇸]"),
                            ("the.dink.2026.1080p.web.hevc.x265.rmteam.mkv", "The Dink (2026) WEB-DL [🇺🇸]")):
            self.assertEqual(label(name)["name"], first, name)

    def test_shows_carry_their_episode_or_season(self):
        for name, first in (("Fallout.2024.S02E01.720p.HEVC.x265-MeGusta[EZTVx.to].mkv", "Fallout (2024) S02E01 [🇺🇸]"),
                            ("/Rick and Morty/Season 3/S03E07.mkv", "Rick and Morty S03E07 [🇺🇸]"),
                            ("Ted Lasso [HDTV][Cap.407].avi", "Ted Lasso S04E07 HDTV [🇺🇸]"),
                            ("La Que Se Avecina - Temporada 8 [HDTV][Cap.808][Español Castellano]", "La Que Se Avecina S08E08 HDTV [🇪🇸]"),
                            ("La Que Se Avecina - Temp.6 [HDTV][Cap.612][Español Castellano][www.newpct.com].avi", "La Que Se Avecina S06E12 HDTV [🇪🇸]"),
                            ("Ted Lasso S02e01-12 (720p Ita Eng Spa 10bit SubS) byMe7alh", "Ted Lasso S02E01-12 [🇺🇸 🇪🇸]"),
                            ("Ted Lasso (2020) S03 (1080p BluRay x265 10bit EAC3 5.1 Ghost)", "Ted Lasso (2020) S03 BluRay [🇺🇸]"),
                            ("Ted.Lasso.s04.TVHUB.WEB.1080p", "Ted Lasso S04 WEB-DL [🇺🇸]"),
                            ("Top.Gear.S12.VFF.1080p.HDTV.AAC.2.0.x264-NOTAG", "Top Gear S12 HDTV"),
                            ("Pluribus Season 1 Mp4 1080p", "Pluribus S01 [🇺🇸]"),
                            ("Shark Tank - Season 1-9 (480p)", "Shark Tank S01-09 [🇺🇸]"),
                            ("Family Guy Seasons 1 to 18 Complete with The Movie and Extras [NVEnc H265][MP3 2Ch]", "Family Guy S01-18 [🇺🇸]"),
                            ("Family Guy Complete Season 19 (2020–21)", "Family Guy S19 [🇺🇸]"),
                            ("The Simpsons (1989-2018) Seasons 01-29 & Movie [1080p] [Ultimate Batch] [HEVC] [x265] [pseudo]", "The Simpsons (1989) S01-29 [🇺🇸]")):
            self.assertEqual(label(name)["name"], first, name)

    def test_titles_lose_what_is_not_the_title(self):
        for name, first in (("www.1TamilMV.lease - Toy Story 5 (2026) BluRay - 720p - x264 - [Tam + Tel + Hin + Eng] - AAC - 1.2GB - ESub.mkv", "Toy Story 5 (2026) BluRay [🇺🇸]"),
                            ("www.UIndex.org    -    Michael.2026.1080p.WEB.mkv", "Michael (2026) WEB-DL [🇺🇸]"),
                            ("Grand Theft Auto VI An Extended Look 10bit 4KDS 1080p SVT-AV1-PSYEX.mkv", "Grand Theft Auto VI An Extended Look [🇺🇸]"),
                            ("El Secreto De Una Obsesion [MicroHD 1080p][AC3 5.1-Castellano-AC3 5.1 Ingles+Subs][ES-EN]", "El Secreto De Una Obsesion [🇺🇸 🇪🇸]"),
                            ("Batman Collection (8 Films) 1989-2016 720p BluRay x264 Ganool", "Batman Collection (8 Films) (1989) BluRay [🇺🇸]"),
                            ("A Odisseia Dub 2026 - BAIXANDOVIATORRENT.COM.mp4", "A Odisseia (2026)"),
                            ("(500) Days of Summer (2009) 1080p.mkv", "(500) Days of Summer (2009) [🇺🇸]"),
                            ("Charlotte's.Web.2006.1080p.BluRay.mkv", "Charlotte's Web (2006) BluRay [🇺🇸]")):
            self.assertEqual(label(name)["name"], first, name)


class DestinationTest(unittest.TestCase):
    def test_movie_and_show_layouts_are_what_jellyfin_matches(self):
        self.assertEqual(destination({"kind": "movie", "title": "Buddy", "year": 2026}, ".MKV"),
                         ("movie", "Buddy (2026)/Buddy (2026).mkv"))
        self.assertEqual(destination({"kind": "movie", "title": "Plain Name"}, ".mp4"),
                         ("movie", "Plain Name/Plain Name.mp4"))
        self.assertEqual(destination({"kind": "show", "title": "South Park", "season": 29, "episode": 2}, ".mkv"),
                         ("show", "South Park/Season 29/South Park S29E02.mkv"))

    def test_names_can_never_leave_the_library(self):
        for title in ("", "  ", "../x", "a/b", "a\\b", ".hidden", "..", "x" * 200, "a\x00b", None, 5, "é" * 150, "a\ud800b"):
            with self.assertRaises(RDError, msg=repr(title)):
                destination({"kind": "movie", "title": title}, ".mkv")

    def test_rejects_nonsense(self):
        for pick, ext in (({"kind": "other", "title": "x"}, ".mkv"),
                          ({"kind": "show", "title": "x", "season": 1}, ".mkv"),
                          ({"kind": "show", "title": "x", "season": -1, "episode": 1}, ".mkv"),
                          ({"kind": "show", "title": "x", "season": "1/..", "episode": 1}, ".mkv"),
                          ({"kind": "movie", "title": "x", "year": "../.."}, ".mkv"),
                          ({"kind": "movie", "title": "x"}, ".exe")):
            with self.assertRaises(RDError, msg=repr(pick)):
                destination(pick, ext)


class SavingCase(EngineCase):
    def setUp(self):
        super().setUp()
        self.fake.torrents["P"] = torrent("P", files=[("/Ted.Lasso.s04e01.mkv", 3000, 1), ("/notes.nfo", 5, 0), ("/Ted.Lasso.s04e02.mkv", 2000, 1)])
        self.fake.torrents["M"] = torrent("M", files=[("/Buddy.2026.1080p.mkv", 3000, 1)])

    def pick_ep(self, file, episode):
        return {"file": file, "kind": "show", "title": "Ted Lasso", "season": 4, "episode": episode}

    MOVIE = {"file": 1, "kind": "movie", "title": "Buddy", "year": 2026}

    def settled(self, rd=None):
        rd = rd or self.rd
        for _ in range(200):
            q = rd.queue()
            if all(j["status"] in ("done", "failed") for j in q):
                return q
            time.sleep(0.01)
        self.fail(f"queue never settled: {rd.queue()}")

    def start(self, rd=None):
        rd = rd or self.rd
        rd.start()
        self.addCleanup(rd.stop)
        return rd

    @property
    def buddy(self):
        return self.tmp / "Movies" / "Buddy (2026)" / "Buddy (2026).mkv"


class SaveTest(SavingCase):
    def test_a_movie_lands_where_jellyfin_looks(self):
        self.start()
        self.rd.save("M", [self.MOVIE])
        q = self.settled()
        self.assertEqual(self.buddy.read_bytes(), CONTENT["https://dl.example/a"])
        self.assertEqual(list(self.buddy.parent.iterdir()), [self.buddy])  # no .part left behind
        self.assertEqual([(j["status"], j["library"], j["path"]) for j in q], [("done", "movie", "Buddy (2026)/Buddy (2026).mkv")])

    def test_each_episode_of_a_pack_gets_its_own_file(self):
        """Links line up with the *selected* files only, so file 3 must take the second link."""
        self.start()
        self.rd.save("P", [self.pick_ep(1, 1), self.pick_ep(3, 2)])
        self.settled()
        season = self.tmp / "Shows" / "Ted Lasso" / "Season 04"
        self.assertEqual((season / "Ted Lasso S04E01.mkv").read_bytes(), CONTENT["https://dl.example/a"])
        self.assertEqual((season / "Ted Lasso S04E02.mkv").read_bytes(), CONTENT["https://dl.example/b"])

    def test_the_list_shows_what_is_already_saved(self):
        self.start()
        self.rd.save("P", [self.pick_ep(1, 1)])
        self.settled()
        self.assertEqual({r["id"]: r["saved"] for r in self.rd.torrents()}, {"M": 0, "P": 1})
        files = {f["id"]: f["job"] for f in self.rd.torrent("P")["files"]}
        self.assertEqual(files[1]["status"], "done")
        self.assertIsNone(files[3])

    def test_the_list_says_how_many_more_videos_a_part_saved_torrent_has(self):
        self.start()
        self.rd.save("P", [self.pick_ep(1, 1)])
        self.settled()
        self.assertEqual({r["id"]: r["more"] for r in self.rd.torrents()}["P"], 1)
        self.rd.save("P", [self.pick_ep(3, 2)])
        self.settled()
        self.assertEqual({r["id"]: r["more"] for r in self.rd.torrents()}["P"], 0)

    def test_a_saved_film_with_a_text_file_beside_it_has_nothing_more_to_save(self):
        self.fake.torrents["F"] = torrent("F", files=[("/Film/Film.2001.mkv", 1, 1), ("/Film/about.nfo", 1, 1)])
        self.fake.torrents["F"]["links"] = ["https://rd.example/M/1", "https://rd.example/F/2"]
        self.start()
        self.rd.save("F", [{"file": 1, "kind": "movie", "title": "Film", "year": 2001}])
        self.settled()
        row = {r["id"]: r for r in self.rd.torrents()}["F"]
        self.assertEqual((row["saved"], row["more"]), (1, 0))

    def test_a_saved_film_with_a_sample_beside_it_has_nothing_more_to_save(self):
        self.fake.torrents["F"] = torrent("F", files=[("/Film/Film.2001.mkv", 1, 1), ("/Film/Sample/film-sample.mkv", 1, 1)])
        self.fake.torrents["F"]["links"] = ["https://rd.example/M/1", "https://rd.example/F/2"]
        self.start()
        self.rd.save("F", [{"file": 1, "kind": "movie", "title": "Film", "year": 2001}])
        self.settled()
        row = {r["id"]: r for r in self.rd.torrents()}["F"]
        self.assertEqual((row["saved"], row["more"]), (1, 0))

    def test_a_saved_discs_short_streams_arent_more_to_save(self):
        self.fake.torrents["D"] = torrent("D", files=[("/BDMV/STREAM/00001.m2ts", 1, 1), ("/BDMV/STREAM/00002.m2ts", 9, 1)], filename="The.Nun.2018.UHD.BLURAY")
        self.fake.torrents["D"]["links"] = ["https://rd.example/F/1", "https://rd.example/M/2"]
        self.start()
        self.rd.save("D", [{"file": 2, "kind": "movie", "title": "The Nun", "year": 2018}])
        self.settled()
        row = {r["id"]: r for r in self.rd.torrents()}["D"]
        self.assertEqual((row["saved"], row["more"]), (1, 0))

    def test_a_long_list_of_saved_films_costs_no_extra_calls(self):
        self.start()
        self.rd.save("M", [{"file": 1, "kind": "movie", "title": "Buddy", "year": 2026}])
        self.settled()
        self.fake.calls.clear()
        self.rd._forget("torrents")
        self.rd.torrents()
        self.assertEqual([url for _, url in self.fake.calls if "/torrents/info/M" in url], [])

    def test_the_list_still_works_when_a_part_saved_torrent_cant_be_read(self):
        self.start()
        self.rd.save("P", [self.pick_ep(1, 1)])
        self.settled()
        real = self.fake.route
        self.fake.route = lambda method, path, form, req: "missing" if "/torrents/info/P" in path else real(method, path, form, req)
        self.rd._forget("torrents", ("info", "P"))
        self.assertEqual({r["id"]: r["more"] for r in self.rd.torrents()}["P"], 0)

    def test_never_overwrites_and_adds_nothing_when_one_pick_is_bad(self):
        mine = self.tmp / "Shows" / "Ted Lasso" / "Season 04" / "Ted Lasso S04E02.mkv"
        mine.parent.mkdir(parents=True)
        mine.write_bytes(b"mine")
        self.start()
        with self.assertRaisesRegex(RDError, "already"):
            self.rd.save("P", [self.pick_ep(1, 1), self.pick_ep(3, 2)])
        self.assertEqual(self.rd.queue(), [])
        self.assertEqual(mine.read_bytes(), b"mine")

    def test_two_picks_cannot_share_a_destination(self):
        with self.assertRaisesRegex(RDError, "already"):
            self.rd.save("P", [self.pick_ep(1, 1), self.pick_ep(3, 1)])
        self.rd.save("P", [self.pick_ep(1, 1)])
        with self.assertRaisesRegex(RDError, "already"):
            self.rd.save("P", [self.pick_ep(1, 1)])

    def test_a_file_appearing_during_the_download_is_not_overwritten(self):
        self.fake.gate = gate = threading.Event()
        self.start()
        self.rd.save("M", [self.MOVIE])
        self.buddy.parent.mkdir(parents=True, exist_ok=True)
        self.buddy.write_bytes(b"mine")
        gate.set()
        q = self.settled()
        self.assertEqual(q[0]["status"], "failed")
        self.assertEqual(self.buddy.read_bytes(), b"mine")

    def test_refuses_what_cannot_be_saved(self):
        self.fake.torrents["W"] = torrent("W", "downloading", files=[("/F.2001.mkv", 10, 1)], progress=40)
        self.fake.torrents["R"] = torrent("R", files=[("/A.2001.mkv", 10, 1), ("/B.2002.mkv", 10, 1)])
        self.fake.torrents["R"]["links"] = ["https://rd.example/R/rar"]  # Real-Debrid packed it into one archive
        cases = [("W", self.MOVIE, "caching"), ("R", self.MOVIE, "archive"),
                 ("P", {**self.pick_ep(2, 1)}, "video"), ("P", {**self.pick_ep(9, 1)}, "file"),
                 ("M", {**self.MOVIE, "title": "../../etc"}, "title")]
        for id, pick, why in cases:
            with self.assertRaisesRegex(RDError, why, msg=id):
                self.rd.save(id, [pick])

    def test_an_unselected_file_cannot_be_saved(self):
        self.fake.torrents["U"] = torrent("U", files=[("/A.2001.mkv", 10, 1), ("/B.2002.mkv", 10, 0)])
        with self.assertRaisesRegex(RDError, "wasn't picked"):
            self.rd.save("U", [{"file": 2, "kind": "movie", "title": "B", "year": 2002}])

    def test_not_enough_space_counts_what_is_already_queued(self):
        with mock.patch("engine.shutil.disk_usage", return_value=mock.Mock(free=4000, total=10**6)):  # room for one, not two
            self.rd.save("M", [self.MOVIE])  # nothing running yet, so it stays queued
            with self.assertRaisesRegex(RDError, "space"):
                self.rd.save("P", [self.pick_ep(1, 1)])


class ResilienceTest(SavingCase):
    def test_a_dropped_connection_resumes_where_it_stopped(self):
        self.fake.die_after = 1000
        self.start()
        self.rd.save("M", [self.MOVIE])
        self.settled()
        self.assertEqual(self.buddy.read_bytes(), CONTENT["https://dl.example/a"])
        ranges = [c for c in self.fake.calls if c[1] == "https://dl.example/a"]
        self.assertEqual(len(ranges), 2)

    def test_a_short_download_is_not_passed_off_as_finished(self):
        self.fake.short_once = True
        self.start()
        self.rd.save("M", [self.MOVIE])
        self.settled()
        self.assertEqual(self.buddy.read_bytes(), CONTENT["https://dl.example/a"])

    def test_a_host_that_ignores_resume_starts_over_cleanly(self):
        self.fake.die_after, self.fake.ignore_range = 1000, True
        self.start()
        self.rd.save("M", [self.MOVIE])
        self.settled()
        self.assertEqual(self.buddy.read_bytes(), CONTENT["https://dl.example/a"])

    def test_gives_up_with_a_reason_and_can_be_retried(self):
        self.fake.dead_links.add("https://rd.example/M/1")
        self.start()
        self.rd.save("M", [self.MOVIE])
        q = self.settled()
        self.assertEqual(q[0]["status"], "failed")
        self.assertIn("hoster unavailable", q[0]["error"])
        self.fake.dead_links.clear()
        self.rd.retry(q[0]["id"])
        self.settled()
        self.assertEqual(self.buddy.read_bytes(), CONTENT["https://dl.example/a"])

    def stop_partway(self):
        """The service stops with 1000 bytes of the Buddy file saved."""
        self.fake.die_after, self.rd.retry_delay = 1000, 60
        self.rd.start()
        self.rd.save("M", [self.MOVIE])
        for _ in range(200):
            if self.rd.queue()[0]["done"] == 1000:
                break
            time.sleep(0.01)
        self.rd.stop()
        self.assertFalse(self.buddy.exists())

    def test_a_restart_carries_on_from_the_partial_file(self):
        self.stop_partway()
        job = self.rd.queue()[0]

        again = self.start(make(self.tmp, self.fake))
        self.assertEqual(self.settled(again)[0]["id"], job["id"])
        self.assertEqual(self.buddy.read_bytes(), CONTENT["https://dl.example/a"])
        self.assertEqual(self.fake.ranges[-1], "bytes=1000-")

    def test_a_new_save_never_continues_a_partial_file_left_by_another(self):
        self.stop_partway()
        self.rd.state_path.write_text("[]")  # cancelled just as the service stopped, before it could tidy up

        again = self.start(make(self.tmp, self.fake))
        again.save("P", [{"file": 3, "kind": "movie", "title": "Buddy", "year": 2026}])  # same name, different file
        self.assertEqual(self.settled(again)[0]["status"], "done")
        self.assertEqual(self.buddy.read_bytes(), CONTENT["https://dl.example/b"])
        self.assertEqual([p for p in (self.tmp / "Movies").rglob("*") if p.is_file()], [self.buddy])  # the leftover is gone

    def test_no_folders_appear_in_the_library_until_the_file_is_ready(self):
        self.fake.gate = gate = threading.Event()
        self.start()
        self.rd.save("P", [self.pick_ep(1, 1)])
        for _ in range(100):
            if self.rd.queue()[0]["status"] == "saving":
                break
            time.sleep(0.01)
        self.assertFalse((self.tmp / "Shows" / "Ted Lasso").exists())
        gate.set()
        self.settled()
        self.assertTrue((self.tmp / "Shows" / "Ted Lasso" / "Season 04" / "Ted Lasso S04E01.mkv").exists())

    def test_saving_carries_on_when_the_queue_file_cannot_be_written(self):
        self.start()
        with mock.patch("engine.os.replace", side_effect=OSError("No space left on device")):
            self.rd.save("M", [self.MOVIE])
            self.rd.save("P", [self.pick_ep(1, 1)])
            q = self.settled()
        self.assertEqual([j["status"] for j in q], ["done", "done"])
        self.assertEqual(self.buddy.read_bytes(), CONTENT["https://dl.example/a"])

    def test_a_busy_real_debrid_is_tried_again_by_itself(self):
        for code in (429, 503):
            with self.subTest(code=code):
                self.fake.flaky["https://rd.example/M/1"] = [code]
                self.buddy.unlink(missing_ok=True)
                self.rd.clear()
                if not self.rd._threads:
                    self.start()
                self.rd.save("M", [self.MOVIE])
                self.settled()
                self.assertEqual(self.buddy.read_bytes(), CONTENT["https://dl.example/a"])

    def test_a_flat_refusal_is_not_tried_again(self):
        self.fake.flaky["https://rd.example/M/1"] = [403]
        self.start()
        self.rd.save("M", [self.MOVIE])
        self.assertEqual(self.settled()[0]["status"], "failed")
        self.assertEqual(self.fake.calls.count(("POST", API + "/unrestrict/link")), 1)

    def test_a_torrent_being_saved_cannot_be_deleted_until_the_save_is_cancelled(self):
        self.rd.save("M", [self.MOVIE])  # nothing running yet, so it stays queued
        with self.assertRaisesRegex(RDError, "being saved"):
            self.rd.delete_torrent("M")
        self.assertIn("M", self.fake.torrents)
        self.rd.cancel(self.rd.queue()[0]["id"])
        self.rd.delete_torrent("M")
        self.assertNotIn("M", self.fake.torrents)

    def test_cancel_removes_the_partial_file_and_nothing_else(self):
        keep = self.tmp / "Shows" / "Ted Lasso" / "Season 03" / "x.mkv"
        keep.parent.mkdir(parents=True)
        keep.write_bytes(b"mine")
        self.fake.gate = gate = threading.Event()
        self.start()
        self.rd.save("P", [self.pick_ep(1, 1)])
        for _ in range(100):
            if self.rd.queue()[0]["status"] == "saving":
                break
            time.sleep(0.01)
        self.rd.cancel(self.rd.queue()[0]["id"])
        gate.set()
        for _ in range(100):
            if not (self.tmp / "Shows" / "Ted Lasso" / "Season 04").exists():
                break
            time.sleep(0.01)
        self.assertFalse((self.tmp / "Shows" / "Ted Lasso" / "Season 04").exists())
        self.assertEqual(keep.read_bytes(), b"mine")
        self.assertEqual(self.rd.queue(), [])

    def test_clearing_finished_rows_leaves_files_alone(self):
        self.start()
        self.rd.save("M", [self.MOVIE])
        self.settled()
        self.rd.clear()
        self.assertEqual(self.rd.queue(), [])
        self.assertTrue(self.buddy.exists())
        self.assertEqual({r["id"]: r["saved"] for r in self.rd.torrents()}["M"], 1)  # still known to be saved

    def test_a_file_deleted_from_the_library_can_be_saved_again(self):
        self.start()
        self.rd.save("M", [self.MOVIE])
        self.settled()
        self.buddy.unlink()
        self.assertEqual({r["id"]: r["saved"] for r in self.rd.torrents()}["M"], 0)
        self.rd.save("M", [self.MOVIE])
        self.settled()
        self.assertEqual(self.buddy.read_bytes(), CONTENT["https://dl.example/a"])
        self.assertEqual(len(self.rd.queue()), 1)

    def test_an_archive_behind_a_single_link_fails_rather_than_saving_junk(self):
        self.fake.archive_links.add("https://rd.example/M/1")
        self.start()
        self.rd.save("M", [self.MOVIE])
        q = self.settled()
        self.assertEqual(q[0]["status"], "failed")
        self.assertIn("archive", q[0]["error"])
        self.assertFalse(self.buddy.exists())

    def test_deleting_a_torrent_never_touches_the_library(self):
        self.start()
        self.rd.save("M", [self.MOVIE])
        self.settled()
        self.rd.delete_torrent("M")
        self.assertTrue(self.buddy.exists())


def download_row(id, filename, link="https://host.example/1", size=3000):
    """A row in the shape the real /downloads returns."""
    return {"id": id, "filename": filename, "mimeType": "video/x-matroska", "filesize": size, "link": link, "host": "host.example",
            "host_icon": "i", "chunks": 16, "download": "https://dl.example/a", "streamable": 1, "generated": "2026-11-02T10:00:00.000Z"}


class LinksTest(SavingCase):
    def test_an_http_link_is_unrestricted(self):
        self.assertEqual(self.rd.add("https://host.example/1"), {"kind": "link", "ids": ["D1"]})

    def test_a_folder_link_brings_each_file_in_it(self):
        self.fake.not_a_file.add("https://host.example/folder")
        self.fake.folders["https://host.example/folder"] = ["https://host.example/1", "https://host.example/2"]
        self.assertEqual(self.rd.add("https://host.example/folder"), {"kind": "link", "ids": ["D1", "D2"]})

    def test_a_link_that_is_neither_explains_the_first_problem(self):
        self.fake.not_a_file.add("https://host.example/nope")
        with self.assertRaisesRegex(RDError, "hoster unsupported"):
            self.rd.add("https://host.example/nope")

    def test_other_kinds_of_address_are_refused(self):
        for bad in ("", "ftp://x", "just words", "file:///etc/passwd", "https://a b"):
            with self.assertRaisesRegex(RDError, "Paste", msg=bad):
                self.rd.add(bad)
        self.assertEqual(self.fake.calls, [])

    def test_the_list_guesses_what_each_link_is(self):
        self.fake.downloads = [download_row("L1", "Buddy.2026.1080p.mkv")]
        row = self.rd.links()[0]
        self.assertEqual((row["id"], row["name"], row["bytes"], row["download"]), ("L1", "Buddy.2026.1080p.mkv", 3000, "https://dl.example/a"))
        self.assertEqual(row["guess"], {"kind": "movie", "title": "Buddy", "year": 2026})
        self.assertIsNone(row["job"])

    def test_a_link_saves_to_the_library(self):
        self.fake.downloads = [download_row("L1", "Buddy.2026.1080p.mkv")]
        self.start()
        self.rd.save_link("L1", {"kind": "movie", "title": "Buddy", "year": 2026})
        self.settled()
        self.assertEqual(self.buddy.read_bytes(), CONTENT["https://dl.example/a"])
        self.assertEqual(self.rd.links()[0]["job"]["status"], "done")

    def test_a_link_that_isnt_a_video_cant_be_saved(self):
        self.fake.downloads = [download_row("L1", "notes.zip")]
        with self.assertRaisesRegex(RDError, "video"):
            self.rd.save_link("L1", {"kind": "movie", "title": "Buddy", "year": 2026})

    def test_a_link_that_is_gone_cant_be_saved(self):
        with self.assertRaisesRegex(RDError, "no longer"):
            self.rd.save_link("L1", {"kind": "movie", "title": "Buddy", "year": 2026})

    def test_deleting_a_link_leaves_the_saved_file(self):
        self.fake.downloads = [download_row("L1", "Buddy.2026.1080p.mkv")]
        self.start()
        self.rd.save_link("L1", {"kind": "movie", "title": "Buddy", "year": 2026})
        self.settled()
        self.rd.delete_link("L1")
        self.assertEqual(self.rd.links(), [])
        self.assertTrue(self.buddy.exists())
        self.rd.delete_link("L1")  # already gone is fine


class AccountControlsTest(SavingCase):
    def test_hosts_show_status_and_any_traffic_limit(self):
        hosts = {h["host"]: h for h in self.rd.hosts()}
        self.assertEqual((hosts["real-debrid.com"]["status"], hosts["real-debrid.com"]["left"]), ("up", None))
        self.assertEqual((hosts["1fichier.com"]["status"], hosts["1fichier.com"]["left"], hosts["1fichier.com"]["limit"], hosts["1fichier.com"]["reset"]),
                         ("down", 500, 600, "daily"))

    def test_converting_points_shows_the_new_balance_straight_away(self):
        self.assertEqual(self.rd.overview()["account"]["points"], 1600)
        self.rd.convert_points()
        self.assertEqual(self.rd.overview()["account"]["points"], 600)
        with self.assertRaisesRegex(RDError, "not enough points"):
            self.rd.convert_points()


class FileDetailsTest(SavingCase):
    INFO = {"filename": "Ted Lasso", "hoster": "x", "link": "l", "type": "show", "season": "4", "episode": "1", "year": None, "duration": 1800.5,
            "bitrate": 8000000, "size": 3000, "baseUrl": "b", "availableFormats": {}, "availableQualities": {},
            "details": {"video": {"und1": {"stream": "0:0", "lang": "Unknown", "lang_iso": "und", "codec": "hevc", "colorspace": "yuv420p10le", "width": 3840, "height": 2160}},
                        "audio": {"eng1": {"stream": "0:1", "lang": "English", "lang_iso": "eng", "codec": "eac3", "sampling": 48000, "channels": 5.1}},
                        "subtitles": {"spa1": {"stream": "0:2", "lang": "Spanish", "lang_iso": "spa", "type": "subrip"}}}}

    def test_reports_quality_and_a_better_guess(self):
        self.fake.media["D1"] = self.INFO
        d = self.rd.file_info("P", 1)
        self.assertEqual((d["width"], d["height"], d["codec"], d["duration"]), (3840, 2160, "hevc", 1800.5))
        self.assertEqual(d["audio"], [{"lang": "English", "codec": "eac3", "channels": 5.1}])
        self.assertEqual(d["subtitles"], ["Spanish"])
        self.assertEqual(d["guess"], {"kind": "show", "title": "Ted Lasso", "season": 4, "episode": 1})

    def test_a_movie_keeps_the_year_it_was_given(self):
        self.fake.media["D1"] = {**self.INFO, "filename": "Buddy", "type": "movie", "season": None, "episode": None, "year": "2026"}
        self.assertEqual(self.rd.file_info("M", 1)["guess"], {"kind": "movie", "title": "Buddy", "year": 2026})

    def test_only_a_selected_file_has_details(self):
        with self.assertRaisesRegex(RDError, "wasn't picked"):
            self.rd.file_info("P", 2)


if __name__ == "__main__":
    unittest.main()
