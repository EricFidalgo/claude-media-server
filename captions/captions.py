"""Looks after the captions of the videos in the library. All the work on files happens here."""
import hashlib
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

WORK = ".captions"  # a folder in each library for videos being rewritten. On the same disk, so the swap is instant, and Jellyfin skips it.
MAX_JOBS = 50
SPARE = 1024**3  # room to leave free on the disk after the rewritten copy is made
CAN_ADD = {"srt": "srt", "vtt": "webvtt", "ass": "ass", "ssa": "ass"}  # file type -> how ffprobe must read it
FILE_TYPES = {f".{t}" for t in CAN_ADD} | {".sub", ".smi", ".sup"}  # other files Jellyfin also shows as captions
UNSURE = "Something went wrong. Check the list and try again."
VIDEO_TYPES = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".wmv", ".mpg", ".mpeg", ".m2ts"}
LANGUAGES = {  # first the code used for a new caption's file name, then the name people see
    "en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French", "de": "German", "it": "Italian", "nl": "Dutch",
    "ru": "Russian", "pl": "Polish", "tr": "Turkish", "ar": "Arabic", "hi": "Hindi", "ko": "Korean", "ja": "Japanese", "zh": "Chinese",
    "sv": "Swedish", "no": "Norwegian", "da": "Danish", "fi": "Finnish", "el": "Greek", "he": "Hebrew", "cs": "Czech", "hu": "Hungarian",
    "ro": "Romanian", "uk": "Ukrainian", "vi": "Vietnamese", "th": "Thai", "id": "Indonesian",
}
_LONG_CODES = {
    "eng": "en", "spa": "es", "por": "pt", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de", "ita": "it", "dut": "nl", "nld": "nl",
    "rus": "ru", "pol": "pl", "tur": "tr", "ara": "ar", "hin": "hi", "kor": "ko", "jpn": "ja", "chi": "zh", "zho": "zh",
    "swe": "sv", "nor": "no", "dan": "da", "fin": "fi", "gre": "el", "ell": "el", "heb": "he", "cze": "cs", "ces": "cs", "hun": "hu",
    "rum": "ro", "ron": "ro", "ukr": "uk", "vie": "vi", "tha": "th", "ind": "id",
}


def language_name(code):
    code = (code or "").lower()
    return LANGUAGES.get(_LONG_CODES.get(code, code), "Unknown language")


def _digest(*parts):
    return hashlib.sha256("/".join(str(p) for p in parts).encode(errors="surrogateescape")).hexdigest()[:20]


def _probe(path):
    """Every track in a file, as ffprobe sees it."""
    return _probe_all(path).get("streams", [])


def _probe_all(path):
    done = subprocess.run(
        ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, errors="replace",
    )
    if done.returncode:
        raise CaptionsError("Couldn't read that video.")
    return json.loads(done.stdout)


def _duration(probed):
    return float(probed.get("format", {}).get("duration") or 0)


def _flush(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rewrite_without(path, tracks, work):
    """Copies the video, leaving out the given built-in tracks, checks the copy, and only then puts it in place."""
    os.makedirs(work, exist_ok=True)
    before = _probe_all(path)
    temp = Path(work) / f"{uuid.uuid4().hex}{path.suffix}"
    gone = {t["index"] for t in tracks}
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file", "-i", str(path), "-map", "0"]
    cmd += [arg for index in sorted(gone) for arg in ("-map", f"-0:{index}")]
    cmd += ["-c", "copy", "-map_metadata", "0", "-map_chapters", "0", str(temp)]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if done.returncode:
            logging.error("ffmpeg failed on %s: %s", path, done.stderr[-2000:])
            raise CaptionsError("The video couldn't be rewritten, so nothing was changed.")
        after = _probe_all(temp)
        kept = lambda streams: [(s.get("codec_type"), s.get("codec_name")) for s in streams]
        wanted = kept(s for s in before["streams"] if s["index"] not in gone)
        if kept(after["streams"]) != wanted or abs(_duration(after) - _duration(before)) > 1:
            logging.error("the copy of %s didn't match: %s, wanted %s", path, kept(after["streams"]), wanted)
            raise CaptionsError("The rewritten video didn't match the original, so nothing was changed.")
        shutil.copymode(path, temp)
        _flush(temp)  # safely on the disk first: a power cut after the swap mustn't lose the only copy
        os.replace(temp, path)
        _flush(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _built_in(stream, version):
    tags = stream.get("tags", {})
    marks = ["Built in"]
    if stream.get("disposition", {}).get("forced"):
        marks.append("Forced")
    if tags.get("title"):
        marks.append(tags["title"])
    elif stream.get("disposition", {}).get("hearing_impaired"):
        marks.append("Hearing impaired")
    # The id says which track of which version of the file, so an out-of-date page can't remove the wrong one,
    # even when two tracks look identical: any rewrite makes every old id stop matching.
    id = "b" + _digest(version, stream["index"], stream.get("codec_name"), tags.get("language"), tags.get("title"))
    return {"id": id, "index": stream["index"], "name": language_name(tags.get("language")), "detail": " · ".join(marks)}


def _claims(stem, file):
    """Jellyfin's rule: a caption file's name starts with the video's name, and then a separator or nothing."""
    rest = file.name[len(stem):] if file.name.startswith(stem) else None
    return bool(rest) and rest[0] in ".-_ "


def _caption_files(video):
    """The caption files beside a video. One that fits two videos, like Film.Extended.en.srt, goes with the closer name."""
    beside = [f for f in sorted(video.parent.iterdir()) if f.is_file()]
    longer = [f.stem for f in beside if f.suffix.lower() in VIDEO_TYPES and len(f.stem) > len(video.stem)]
    return [f for f in beside if f.suffix.lower() in FILE_TYPES and _claims(video.stem, f) and not any(_claims(s, f) for s in longer)]


def _added(video, path):
    tokens = path.stem[len(video.stem):].replace("-", ".").replace("_", ".").replace(" ", ".").split(".")
    code = next((t for t in tokens if language_name(t) != "Unknown language"), None)
    return {"id": "f" + _digest(path.name), "path": path, "name": language_name(code), "detail": "Added"}


def _is_caption_file(path, type):
    done = subprocess.run(
        ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-f", CAN_ADD[type], "-count_packets", "-select_streams", "s",
         "-show_entries", "stream=nb_read_packets", "-of", "json", str(path)],
        capture_output=True, text=True, errors="replace",
    )
    if done.returncode:
        return False
    streams = json.loads(done.stdout).get("streams", [])
    return bool(streams) and int(streams[0].get("nb_read_packets") or 0) > 0


class CaptionsError(Exception):
    """A problem to show the person, in their words."""


class Captions:
    def __init__(self, roots, spare=SPARE):
        self.roots = {kind: Path(path) for kind, path in roots.items()}
        self.spare = spare
        self._latest_change = None
        self._lock = threading.Lock()
        self._jobs = []
        self._waiting = queue.Queue()
        self._worker = threading.Thread(target=self._work, daemon=True)

    def start(self):
        for root in self.roots.values():  # the service stopped mid-rewrite: the original is untouched, the copy is junk
            for left in (root / WORK).glob("*"):
                left.unlink(missing_ok=True)
        self._worker.start()

    def stop(self):
        self._waiting.put(None)
        self._worker.join()

    def _walk(self):
        """Every video as (id, kind, path). The id is what the page uses, so no path ever comes in from outside."""
        for kind, root in self.roots.items():
            for folder, subfolders, files in os.walk(root):
                subfolders[:] = [s for s in subfolders if not s.startswith(".")]  # .saving and friends
                for name in files:
                    path = Path(folder) / name
                    if not name.startswith(".") and path.suffix.lower() in VIDEO_TYPES:
                        id = _digest(kind, path.relative_to(root))
                        yield id, kind, path

    def _where(self, kind, path):
        return "Movies" if kind == "movie" else " · ".join(path.relative_to(self.roots[kind]).parts[:-1])

    def videos(self):
        found = [{"id": id, "name": path.stem, "where": self._where(kind, path)} for id, kind, path in self._walk()]
        return sorted(found, key=lambda v: v["name"].casefold())

    def _find(self, id):
        for found, kind, path in self._walk():
            if found == id:
                return kind, path
        raise CaptionsError("That video isn't in the library any more.")

    def _tracks(self, path):
        stat = path.stat()
        version = (stat.st_size, stat.st_mtime_ns)
        built_in = [_built_in(s, version) for s in _probe(path) if s.get("codec_type") == "subtitle"]
        return built_in + [_added(path, f) for f in _caption_files(path)]

    def video(self, id):
        kind, path = self._find(id)
        captions = [{key: t[key] for key in ("id", "name", "detail")} for t in self._tracks(path)]
        return {"id": id, "name": path.stem, "where": self._where(kind, path), "captions": captions, "languages": LANGUAGES}

    def jobs(self):
        with self._lock:
            return [dict(j) for j in self._jobs]

    def remove(self, id, wanted):
        """Starts removing captions from a video. Rewriting a big video takes minutes, so this only queues it."""
        kind, path = self._find(id)
        job = {"id": uuid.uuid4().hex, "video": id, "name": path.stem, "status": "waiting", "error": None}
        with self._lock:  # forget the oldest finished ones. A job still to do has to stay, as the worker comes back to it
            spare = max(len(self._jobs) + 1 - MAX_JOBS, 0)
            forget = [j["id"] for j in self._jobs if j["status"] in ("done", "failed")][:spare]
            self._jobs = [*(j for j in self._jobs if j["id"] not in forget), job]
        self._waiting.put((job["id"], kind, path, list(wanted)))
        return dict(job)

    def _set(self, id, **changes):
        with self._lock:
            next(j for j in self._jobs if j["id"] == id).update(changes)

    def _work(self):
        while (item := self._waiting.get()) is not None:
            id, kind, path, wanted = item
            self._set(id, status="working")
            try:
                self._remove_now(kind, path, wanted)
                self._set(id, status="done")
            except CaptionsError as err:
                self._set(id, status="failed", error=str(err))
            except Exception:
                logging.exception("removing captions from %s failed", path)
                self._set(id, status="failed", error=UNSURE)

    def _remove_now(self, kind, path, wanted):
        tracks = [t for t in self._tracks(path) if t["id"] in wanted]
        if len(tracks) != len(set(wanted)):
            raise CaptionsError("This video's captions have changed since the page loaded, so nothing was removed. Check the list and try again.")
        built_in = [t for t in tracks if "index" in t]
        changed = False
        try:
            if built_in:  # first, because it's the part most likely to fail, and then nothing has changed yet
                if shutil.disk_usage(self.roots[kind]).free < path.stat().st_size + self.spare:
                    raise CaptionsError("There isn't room on the disk to rewrite this video, so nothing was changed.")
                _rewrite_without(path, built_in, self.roots[kind] / WORK)
                changed = True
            for track in tracks:
                if "path" in track:
                    track["path"].unlink(missing_ok=True)
                    changed = True
                    if track["path"].suffix.lower() == ".sub":
                        track["path"].with_suffix(".idx").unlink(missing_ok=True)  # the other half of a .sub
        except OSError:
            logging.exception("removing captions from %s failed", path)
            raise CaptionsError(UNSURE)
        finally:
            if changed:  # even a part-finished removal has to reach Jellyfin
                with self._lock:
                    self._latest_change = uuid.uuid4().hex

    def changes(self):
        """Something new here after each change, which is how the plugin knows to have Jellyfin look again."""
        with self._lock:
            return {"latest": self._latest_change}

    def add(self, id, language, type, content):
        """Saves a caption file beside the video, named the way Jellyfin expects, after checking it really is one."""
        if language not in LANGUAGES or type not in CAN_ADD:
            raise CaptionsError("That kind of caption can't be added.")
        kind, path = self._find(id)
        work = self.roots[kind] / WORK
        os.makedirs(work, exist_ok=True)
        temp = work / f"{uuid.uuid4().hex}.{type}"
        try:
            temp.write_bytes(content)
            if not _is_caption_file(temp, type):
                raise CaptionsError("That doesn't look like a caption file. It should be a .srt, .vtt, .ass or .ssa file.")
            with self._lock:
                os.replace(temp, self._free_name(path, language, type))
                self._latest_change = uuid.uuid4().hex
        finally:
            temp.unlink(missing_ok=True)

    def _free_name(self, video, language, type):
        name = video.with_name(f"{video.stem}.{language}.{type}")
        number = 2
        while name.exists():
            name = video.with_name(f"{video.stem}.{language}.{number}.{type}")
            number += 1
        return name
