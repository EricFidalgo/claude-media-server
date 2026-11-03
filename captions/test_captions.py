"""The captions service's web API, over real HTTP, with real ffmpeg on tiny real videos in a temp library."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

from server import make_server
from captions import Captions

SRT = "1\n00:00:00,000 --> 00:00:01,000\nHello\n"


def make_video(path, *subs):
    """A 1-second video with one audio track, and one built-in caption per (language, title) in subs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    source = path.parent / "source.srt"
    source.write_text(SRT)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64:r=5", "-f", "lavfi", "-i", "sine=d=1"]
    for _ in subs:
        cmd += ["-i", str(source)]
    cmd += ["-map", "0", "-map", "1"]
    for n, (language, title) in enumerate(subs):
        cmd += ["-map", str(n + 2), f"-metadata:s:s:{n}", f"language={language}", f"-metadata:s:s:{n}", f"title={title}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-c:s", "srt", str(path)]
    subprocess.run(cmd, check=True)
    source.unlink()
    return path


class CaptionsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.movies, self.shows = self.tmp / "movies", self.tmp / "shows"
        self.movies.mkdir()
        self.shows.mkdir()
        self.serve()

    def serve(self, **options):
        self.captions = Captions({"movie": str(self.movies), "show": str(self.shows)}, **options)
        self.captions.start()
        self.addCleanup(self.captions.stop)
        self.server = make_server(self.captions, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_address[1]}{path}", method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def video(self, name):
        return next(v for v in self.call("GET", "/videos")[1] if v["name"] == name)

    def test_lists_the_videos_in_both_libraries_and_skips_hidden_folders(self):
        make_video(self.movies / "The Notebook (2004)" / "The Notebook (2004).mkv")
        make_video(self.shows / "South Park" / "Season 29" / "South Park S29E02.mkv")
        make_video(self.movies / ".saving" / "half-finished.mkv")
        (self.movies / "The Notebook (2004)" / "poster.jpg").write_text("not a video")

        status, videos = self.call("GET", "/videos")

        self.assertEqual(status, 200)
        self.assertEqual([(v["name"], v["where"]) for v in videos], [
            ("South Park S29E02", "South Park · Season 29"),
            ("The Notebook (2004)", "Movies"),
        ])


    def test_shows_a_videos_built_in_captions_in_plain_words(self):
        make_video(self.movies / "Film" / "Film.mkv", ("eng", "SDH"), ("spa", ""))

        status, answer = self.call("GET", "/videos/" + self.video("Film")["id"])

        self.assertEqual(status, 200)
        self.assertEqual([(t["name"], t["detail"]) for t in answer["captions"]], [
            ("English", "Built in · SDH"),
            ("Spanish", "Built in"),
        ])
        self.assertEqual(answer["languages"]["pt"], "Portuguese")

    def test_an_unknown_video_is_refused(self):
        self.assertEqual(self.call("GET", "/videos/nothing")[0], 400)


    def wait_for_job(self, id):
        for _ in range(200):
            job = next(j for j in self.call("GET", "/jobs")[1] if j["id"] == id)
            if job["status"] in ("done", "failed"):
                return job
            time.sleep(0.05)
        self.fail("the job never finished")

    def remove(self, video, *names):
        """Asks to remove the named captions, and waits for the job to finish."""
        captions = self.call("GET", "/videos/" + video["id"])[1]["captions"]
        tracks = [c["id"] for c in captions if c["name"] in names]
        status, job = self.call("POST", f"/videos/{video['id']}/remove", {"captions": tracks})
        self.assertEqual(status, 200, job)
        return self.wait_for_job(job["id"])

    def test_removing_a_built_in_caption_leaves_everything_else_alone(self):
        path = make_video(self.movies / "Film" / "Film.mkv", ("eng", "SDH"), ("spa", ""), ("fre", ""))
        video = self.video("Film")

        job = self.remove(video, "Spanish")

        self.assertEqual(job["status"], "done", job)
        self.assertEqual([c["name"] for c in self.call("GET", "/videos/" + video["id"])[1]["captions"]], ["English", "French"])
        streams = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, check=True).stdout)
        self.assertEqual([s["codec_type"] for s in streams["streams"]], ["video", "audio", "subtitle", "subtitle"])
        self.assertEqual(streams["streams"][0]["codec_name"], "h264")
        self.assertEqual(streams["streams"][1]["codec_name"], "aac")
        self.assertAlmostEqual(float(streams["format"]["duration"]), 1.0, delta=0.2)
        self.assertEqual(os.listdir(path.parent), ["Film.mkv"], "no leftovers beside the video")


    def test_a_page_gone_out_of_date_cannot_remove_the_wrong_caption(self):
        make_video(self.movies / "Film" / "Film.mkv", ("eng", ""), ("spa", ""), ("fre", ""))
        video = self.video("Film")
        captions = self.call("GET", "/videos/" + video["id"])[1]["captions"]
        french = next(c["id"] for c in captions if c["name"] == "French")
        self.remove(video, "English")  # French has moved up a place since the page was drawn

        status, job = self.call("POST", f"/videos/{video['id']}/remove", {"captions": [french]})
        job = self.wait_for_job(job["id"])

        self.assertEqual(job["status"], "failed")
        self.assertIn("changed", job["error"])
        names = [c["name"] for c in self.call("GET", "/videos/" + video["id"])[1]["captions"]]
        self.assertEqual(names, ["Spanish", "French"])

    def test_a_video_ffmpeg_cannot_rewrite_stays_exactly_as_it_was(self):
        path = make_video(self.movies / "Film" / "Film.mkv", ("eng", ""), ("spa", ""))
        video = self.video("Film")
        captions = self.call("GET", "/videos/" + video["id"])[1]["captions"]
        before = path.read_bytes()
        fakes = self.tmp / "fakes"
        fakes.mkdir()
        (fakes / "ffmpeg").write_text("#!/bin/sh\nexit 1\n")
        (fakes / "ffmpeg").chmod(0o755)
        os.environ["PATH"] = f"{fakes}:{os.environ['PATH']}"
        self.addCleanup(os.environ.__setitem__, "PATH", os.environ["PATH"].split(":", 1)[1])

        status, job = self.call("POST", f"/videos/{video['id']}/remove", {"captions": [captions[0]["id"]]})
        job = self.wait_for_job(job["id"])

        self.assertEqual(job["status"], "failed")
        self.assertIn("nothing was changed", job["error"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list((self.movies / ".captions").glob("*")), [], "the half-made copy is cleaned up")

    def test_refuses_to_rewrite_a_video_without_room_for_a_second_copy(self):
        path = make_video(self.movies / "Film" / "Film.mkv", ("eng", ""), ("spa", ""))
        before = path.read_bytes()
        self.serve(spare=10**18)
        video = self.video("Film")

        job = self.remove(video, "Spanish")

        self.assertEqual(job["status"], "failed")
        self.assertIn("room", job["error"])
        self.assertEqual(path.read_bytes(), before)

    def test_a_copy_left_behind_by_a_crash_is_cleaned_up_at_start(self):
        make_video(self.movies / "Film" / "Film.mkv")
        (self.movies / ".captions").mkdir()
        (self.movies / ".captions" / "left-behind.mkv").write_text("half a video")

        self.serve()

        self.assertEqual(list((self.movies / ".captions").glob("*")), [])


    def test_adding_a_caption_file_puts_it_where_jellyfin_looks_and_it_shows_in_the_list(self):
        path = make_video(self.movies / "Film" / "Film.mkv", ("eng", ""))
        video = self.video("Film")

        status, _ = self.call("POST", f"/videos/{video['id']}/add/pt/srt", raw=SRT.encode())

        self.assertEqual(status, 200)
        self.assertEqual((path.parent / "Film.pt.srt").read_text(), SRT)
        captions = self.call("GET", "/videos/" + video["id"])[1]["captions"]
        self.assertEqual([(c["name"], c["detail"]) for c in captions], [("English", "Built in"), ("Portuguese", "Added")])

    def test_adding_a_second_caption_in_the_same_language_keeps_the_first(self):
        path = make_video(self.movies / "Film" / "Film.mkv")
        video = self.video("Film")

        self.call("POST", f"/videos/{video['id']}/add/en/srt", raw=SRT.encode())
        self.call("POST", f"/videos/{video['id']}/add/en/srt", raw=SRT.replace("Hello", "Other").encode())

        self.assertEqual((path.parent / "Film.en.srt").read_text(), SRT)
        self.assertIn("Other", (path.parent / "Film.en.2.srt").read_text())

    def test_a_file_that_is_not_captions_is_refused(self):
        path = make_video(self.movies / "Film" / "Film.mkv")
        video = self.video("Film")

        status, answer = self.call("POST", f"/videos/{video['id']}/add/en/srt", raw=b"this is my shopping list")

        self.assertEqual(status, 400)
        self.assertIn("caption file", answer["error"])
        self.assertEqual(sorted(os.listdir(path.parent)), ["Film.mkv"])
        self.assertEqual(list((self.movies / ".captions").glob("*")), [])

    def test_only_known_languages_and_caption_types_can_be_added(self):
        make_video(self.movies / "Film" / "Film.mkv")
        video = self.video("Film")

        self.assertEqual(self.call("POST", f"/videos/{video['id']}/add/xx/srt", raw=SRT.encode())[0], 400)
        self.assertEqual(self.call("POST", f"/videos/{video['id']}/add/en/exe", raw=SRT.encode())[0], 400)

    def test_removing_an_added_caption_deletes_its_file_and_leaves_the_video_alone(self):
        path = make_video(self.movies / "Film" / "Film.mkv", ("eng", ""))
        before = path.read_bytes()
        video = self.video("Film")
        self.call("POST", f"/videos/{video['id']}/add/pt/srt", raw=SRT.encode())

        job = self.remove(video, "Portuguese")

        self.assertEqual(job["status"], "done", job)
        self.assertFalse((path.parent / "Film.pt.srt").exists())
        self.assertEqual(path.read_bytes(), before, "no need to rewrite the video")

    def test_a_caption_file_beside_another_video_is_not_this_videos(self):
        make_video(self.shows / "Show" / "Season 01" / "Show S01E01.mkv")
        make_video(self.shows / "Show" / "Season 01" / "Show S01E010.mkv")
        (self.shows / "Show" / "Season 01" / "Show S01E010.en.srt").write_text(SRT)

        answer = self.call("GET", "/videos/" + self.video("Show S01E01")["id"])[1]

        self.assertEqual(answer["captions"], [])

    def test_marks_each_change_so_jellyfin_knows_when_to_look_again(self):
        make_video(self.movies / "Film" / "Film.mkv", ("eng", ""))
        video = self.video("Film")
        self.assertEqual(self.call("GET", "/changes")[1], {"latest": None})

        self.call("POST", f"/videos/{video['id']}/add/pt/srt", raw=SRT.encode())
        first = self.call("GET", "/changes")[1]["latest"]
        self.assertTrue(first)

        self.remove(video, "Portuguese")
        self.assertNotIn(self.call("GET", "/changes")[1]["latest"], (None, first))

    def test_removing_several_at_once_covers_added_and_built_in_together(self):
        path = make_video(self.movies / "Film" / "Film.mkv", ("eng", ""), ("spa", ""), ("fre", ""))
        video = self.video("Film")
        self.call("POST", f"/videos/{video['id']}/add/pt/srt", raw=SRT.encode())

        job = self.remove(video, "Spanish", "French", "Portuguese")

        self.assertEqual(job["status"], "done", job)
        self.assertEqual([c["name"] for c in self.call("GET", "/videos/" + video["id"])[1]["captions"]], ["English"])
        self.assertEqual(os.listdir(path.parent), ["Film.mkv"])

    def test_a_longer_named_videos_caption_file_is_not_touched_when_clearing_a_shorter_named_one(self):
        folder = self.movies / "Film"
        make_video(folder / "Film.mkv")
        make_video(folder / "Film.Extended.mkv")
        make_video(folder / "Film 2.mkv")
        own = folder / "Film.en.srt"
        extended = folder / "Film.Extended.en.srt"
        second = folder / "Film 2.fr.srt"
        for file in (own, extended, second):
            file.write_text(SRT)
        video = self.video("Film")

        self.assertEqual([c["name"] for c in self.call("GET", "/videos/" + video["id"])[1]["captions"]], ["English"])
        self.assertEqual(self.remove(video, "English")["status"], "done")

        self.assertFalse(own.exists())
        self.assertTrue(extended.exists())
        self.assertTrue(second.exists())

    def test_asking_twice_cannot_remove_a_second_identical_caption(self):
        make_video(self.movies / "Film" / "Film.mkv", ("eng", ""), ("eng", ""), ("eng", ""))
        video = self.video("Film")
        first = self.call("GET", "/videos/" + video["id"])[1]["captions"][0]["id"]

        _, one = self.call("POST", f"/videos/{video['id']}/remove", {"captions": [first]})
        _, two = self.call("POST", f"/videos/{video['id']}/remove", {"captions": [first]})

        self.assertEqual(self.wait_for_job(one["id"])["status"], "done")
        self.assertEqual(self.wait_for_job(two["id"])["status"], "failed")
        self.assertEqual(len(self.call("GET", "/videos/" + video["id"])[1]["captions"]), 2)

    def test_a_long_queue_still_gets_worked_through(self):
        make_video(self.movies / "Film" / "Film.mkv", ("eng", ""))
        video = self.video("Film")
        for _ in range(70):
            self.call("POST", f"/videos/{video['id']}/remove", {"captions": ["gone"]})

        self.assertEqual(self.remove(video, "English")["status"], "done")

    def test_a_file_name_that_is_not_valid_text_does_not_break_the_page(self):
        make_video(self.movies / "Film" / "Film.mkv", ("eng", ""))
        folder = os.fsencode(self.movies / "Film")
        os.rename(os.path.join(folder, b"Film.mkv"), os.path.join(folder, b"Caf\xe9.mkv"))
        make_video(self.movies / "Other" / "Other.mkv")

        status, videos = self.call("GET", "/videos")

        self.assertEqual(status, 200, videos)
        self.assertEqual(len(videos), 2)
        odd = next(v for v in videos if v["name"].startswith("Caf"))
        status, shown = self.call("GET", "/videos/" + odd["id"])
        self.assertEqual(status, 200, shown)
        self.assertEqual([c["name"] for c in shown["captions"]], ["English"])

    def test_every_video_type_the_downloader_saves_is_listed(self):
        make_video(self.movies / "Film" / "Film.m2ts")

        self.assertEqual(self.video("Film")["where"], "Movies")

    def test_a_caption_that_cannot_be_deleted_says_so_without_claiming_nothing_changed(self):
        path = make_video(self.movies / "Film" / "Film.mkv")
        video = self.video("Film")
        self.call("POST", f"/videos/{video['id']}/add/pt/srt", raw=SRT.encode())
        os.chmod(path.parent, 0o555)
        self.addCleanup(os.chmod, path.parent, 0o755)

        job = self.remove(video, "Portuguese")

        self.assertEqual(job["status"], "failed")
        self.assertIn("Check the list", job["error"])
        self.assertTrue((path.parent / "Film.pt.srt").exists())

    def test_the_change_is_reported_even_if_the_second_half_of_a_removal_fails(self):
        path = make_video(self.movies / "Film" / "Film.mkv", ("eng", ""))
        video = self.video("Film")
        self.call("POST", f"/videos/{video['id']}/add/pt/srt", raw=SRT.encode())
        before = self.call("GET", "/changes")[1]["latest"]
        sidecar = path.parent / "Film.pt.srt"
        real_unlink = Path.unlink

        def stuck(file, *args, **kwargs):
            if file == sidecar:
                raise PermissionError("stuck")
            return real_unlink(file, *args, **kwargs)

        with unittest.mock.patch.object(Path, "unlink", stuck):
            job = self.remove(video, "English", "Portuguese")

        self.assertEqual(job["status"], "failed")
        self.assertIn("Check the list", job["error"])
        self.assertNotIn("nothing", job["error"])
        self.assertNotEqual(self.call("GET", "/changes")[1]["latest"], before)


if __name__ == "__main__":
    unittest.main()
