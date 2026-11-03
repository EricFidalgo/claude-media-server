"""Real-Debrid -> Jellyfin: everything the app does with the account and the library folders.

Names only ever come in as a "pick" (kind, title, year or season/episode). destination() is the one place
a pick becomes a path, so nothing a client sends can point outside the library.
"""
import json
import os
import re
import shutil
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

API = os.environ.get("RD_API_BASE", "https://api.real-debrid.com/rest/1.0")
TOKEN_FILES = [Path("/run/secrets/realdebrid_token")]
MAX_TORRENTS = 2500  # the API allows 5000 per page; far more than one account needs on a phone
VIDEO = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".webm", ".mpg", ".mpeg", ".m2ts", ".wmv"}
MAX_FOLDER = 100
MAX_LINKS = 200
RETRIES = 3  # attempts in a row without getting a single new byte
PARTS = ".saving"  # a folder in each library for files on their way in. Jellyfin skips anything starting with a dot.
_EPISODE = re.compile(
    r"(?<![a-z0-9])s(\d{1,2})[ ._-]*e(\d{1,3})"  # S04E07
    r"|(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)"  # 4x07
    r"|(?<![a-z0-9])cap[ ._-]*(\d{1,2})(\d{2})(?!\d)",  # Cap.407
    re.I)
_EPISODE_END = re.compile(r"-e?(\d{1,3})(?![0-9a-z])", re.I)  # the "-12" of S02E01-12
_PACK = re.compile(r"(?<![a-z0-9])(?:s|seasons?[ ._-]*|temp(?:orada)?[ ._-]*)(\d{1,2})(?:[ ._-]*(?:-|to)[ ._-]*s?(\d{1,2}))?(?![a-z0-9])", re.I)
_YEAR = re.compile(r"(.*?)[\s._\[(-]+((?:19|20)\d\d)(?!\d)(?![\s._\])]+[(\[]?(?:19|20)\d\d(?![a-z0-9]))", re.I)  # a second year means the first is part of the title: Blade Runner 2049 (2017)
_SITE = re.compile(r"^\s*www\.\S+\s*-\s*", re.I)  # "www.UIndex.org - Michael.2026.mkv"
# Where a title ends. Plain "web", "dual" and "multi" stay out, or Charlotte's Web would lose half its name.
_JUNK = re.compile(
    r"[\s._\[(-]+(?:\d{3,4}[pi]|4k|uhd|blu[ ._-]?ray|bd-?remux|remux|br-?rip|bd-?rip|web[ ._-]?dl|web[ ._-]?rip|hdtv|hdrip|dvd-?rip"
    r"|microhd|hdts|x26[45]|h\.?26[45]|hevc|10bit|dual[ ._-]*audio|multi\d|dub|www)(?![a-z0-9])",
    re.I)
_SEASON_WORD = re.compile(r"[\s._-]*(?<![a-z0-9])(?:temp(?:orada)?|season)[\s._-]*\d{1,2}(?![a-z0-9]).*$", re.I)
_ENDING_COMPLETE = re.compile(r"[\s._-]+complete$", re.I)
_ENDING_YEAR = re.compile(r"[\s(\[-]+((?:19|20)\d\d)(?:[-–]\d{2,4})?[)\]]?$")
_SAMPLE = re.compile(r"(?i)(^|[/._ -])samples?\d*([/._ -]|$)")
_EXTRAS_FOLDER = re.compile(r"(?i)(^|[/._ -])(extras|featurettes)([/._ -][^/]*)?/")
_DISC_STREAM = re.compile(r"(?i)(^|/)BDMV/STREAM/")
_BARE_EPISODE = re.compile(r"(?:^|(?:^|[ ._-])(?:e|ep|episode)[ ._-]*)(\d{1,3})$", re.I)  # "05", "Ep1", "The.Wire.E05"


class RDError(Exception):
    """A failure a person can fix. The message is fit to show on the phone."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _tidy(text):
    text = re.sub(r"\s+", " ", re.sub(r"[._]+", " ", text)).strip(" -[]()")
    for opening, closing in ("()", "[]"):  # "Batman Collection (8 Films)" and "(500) Days of Summer" keep their brackets
        if text.count(opening) > text.count(closing):
            text += closing
        elif text.count(closing) > text.count(opening):
            text = opening + text
    return text


def _named(title):
    """'ted lasso' files with 'Ted Lasso' instead of becoming a second show. Any other capitals are the name's own."""
    return re.sub(r"(?:^|(?<=\s))\w", lambda m: m.group().upper(), title) if title == title.lower() else title


def _cut(text):
    """Split at the first release word: 'Ted Lasso [HDTV][' -> ('Ted Lasso', ' [HDTV'...)."""
    junk = _JUNK.search(text)
    return (text[:junk.start()], text[junk.start():]) if junk else (text, "")


def _show_title(text):
    """A show's name, with any year the way Jellyfin reads it: 'Fallout.2024.' -> 'Fallout (2024)'."""
    text = _ENDING_COMPLETE.sub("", _tidy(_SEASON_WORD.sub("", text)))
    year = _ENDING_YEAR.search(text)
    if year and _tidy(text[:year.start()]):
        return f"{_tidy(text[:year.start()])} ({year.group(1)})"
    return text


def _stem(path):
    """The name without its video extension or a website at the front. 'Film.1080p' is a folder's name, so its '.1080p' stays."""
    path = Path(path)
    stem = _SITE.sub("", path.stem if is_video(path) else path.name)
    return stem.replace("+", " ") if " " not in stem else stem  # "Obsession+(1991)" uses + for spaces, "Romeo + Juliet" doesn't


def _parse(path):
    """(pick, details): what a name is, and the part of it that says how it was released."""
    stem = _stem(path)
    ep = _EPISODE.search(stem)
    if ep:
        season, episode = (int(g) for g in ep.groups() if g)
        before, details = _cut(stem[:ep.start()])
        title = _show_title(before)
        for parent in (p.name for p in Path(path).parents):  # "S03E07.mkv" takes its title from the folder above
            if title:
                break
            if parent and not re.match(r"(?i)(season|s)[ ._-]*\d+$", parent):
                title = _show_title(_cut(_PACK.split(_EPISODE.split(parent)[0])[0])[0])
        return {"kind": "show", "title": _named(title), "season": season, "episode": episode}, f"{details} {stem[ep.end():]}"
    year = _YEAR.match(stem)
    if year:
        before, details = _cut(year.group(1))
        if _tidy(before):
            return {"kind": "movie", "title": _named(_tidy(before)), "year": int(year.group(2))}, f"{details} {stem[year.end():]}"
    title, details = _cut(stem)
    return {"kind": "movie", "title": _named(_tidy(title)), "year": None}, details


def guess(path):
    """What a file probably is, as a pick: 'Ted.Lasso.s04e03.WEB.mkv' -> show, Ted Lasso, season 4, episode 3."""
    return _parse(path)[0]


def _season_part(name):
    """The 'S03' or 'Seasons 1 to 18' in a name. A group called '-S4' at the far end isn't one."""
    pack = _PACK.search(name)
    return pack if pack and not _cut(name[:pack.start()])[1] else None


def _guess_within(path, torrent, alone):
    """Where a file goes, helped by its torrent's name when its own says too little, because the page has nowhere to type one."""
    pick = guess(path)
    if pick["kind"] == "movie" and not pick["year"]:
        season, episode = _season_part(torrent), _BARE_EPISODE.search(_stem(path))
        if season and not season.group(2) and episode:
            return guess(f"{torrent}/S{int(season.group(1))}E{int(episode.group(1))}.mkv")
        if alone and guess(torrent)["title"]:
            return guess(torrent)
    if pick["title"]:
        return pick
    return {**pick, "title": guess(f"{torrent}/{Path(path).name}")["title"] or guess(torrent)["title"]}


def _word(words):
    return re.compile(rf"(?<![a-z0-9])(?:{words})(?![a-z0-9])", re.I)


_RESOLUTION = _word(r"\d{3,4}[pi]")
_DIMENSIONS = re.compile(r"(?<!\d)\d{3,4}x(\d{3,4})(?!\d)", re.I)  # 1280x720
_BARE_RESOLUTION = _word(r"2160|1080|720")
_UHD = _word(r"4k|uhd")
_QUALITIES = [  # the first that matches wins
    ("BluRay REMUX", _word(r"(?:bd-?)?remux")),
    ("BluRay", _word(r"blu[ ._-]?ray|bd-?rip|br-?rip|bdmv")),
    ("WEBRip", _word(r"web[ ._-]?rip")),
    ("WEB-DL", _word(r"web[ ._-]?dl|web")),
    ("HDRip", _word(r"hd-?rip")),
    ("DVDRip", _word(r"dvd-?rip|dvd")),
    ("HDTV", _word(r"hdtv")),
    ("CAM", _word(r"cam|cam-?rip|hd-?cam")),
    ("TS", _word(r"ts|hdts|telesync")),
    ("TC", _word(r"tc|telecine")),
    ("SCR", _word(r"scr|screener")),
]
_EN_WORDS = r"eng|english|ingl[eé]s"
_ES_WORDS = r"esp|spa|spanish|espa[nñ]ol|castellano|latino|lat"
_OTHER_WORDS = (
    r"hindi|hin|tamil|tam|telugu|tel|french|fre|fra|vff|vfq|vf2|vostfr|truefrench|german|ger|deu|italian|ita|por|portuguese|dublado|dub"
    r"|rus|russian|jpn|jap|japanese|kor|korean|chi|zho|chinese|ara|arabic|tur|turkish|pol|polish|nld|dutch|swe|swedish|tha|thai|cze|cz|hun")
# "en" and "es" count only all lower or all upper, so an episode called "Vuelve En Casa" isn't a language.
_ENGLISH = re.compile(rf"(?<![A-Za-z0-9])(?:(?i:{_EN_WORDS})|EN|en)(?![A-Za-z0-9])")
_SPANISH = re.compile(rf"(?<![A-Za-z0-9])(?:(?i:{_ES_WORDS})|ES|es)(?![A-Za-z0-9])")
_OTHER_LANGUAGE = _word(_OTHER_WORDS)
_ORIGINAL_TOO = _word(r"multi\d?|dual")  # two or more tracks: the original, nearly always English, is among them
_SITE_NAME = re.compile(r"\[?www\.[^\s\]]+\]?", re.I)  # or [www.cantabriatorrent.es] would read as Spanish
_LANGUAGE = rf"(?:(?i:{_EN_WORDS}|{_ES_WORDS}|{_OTHER_WORDS})|EN|en|ES|es)"
_SUBTITLES = re.compile(  # "Eng Subs", "sub ita eng", "Subtitulado Esp" are subtitles, not what's spoken
    rf"(?<![A-Za-z0-9]){_LANGUAGE}[ ._-]+(?i:e?subs?)(?![A-Za-z0-9])"
    rf"|(?<![A-Za-z0-9])(?i:subt(?:itulado|itulos|itles?)?|e?subs?)(?![A-Za-z0-9])(?:[ ._-]+{_LANGUAGE}(?![A-Za-z0-9]))*")


def _size(n):
    """Whole numbers, base 1000, no space, as the owner's other app shows them: 999227023 -> 999MB."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 999.5:
            return f"{round(n)}{unit}"
        n /= 1000
    return f"{round(n)}TB"


def _resolution(details):
    found = _RESOLUTION.search(details)
    if found:
        return found.group(0).lower()
    found = _DIMENSIONS.search(details)
    if found:
        return f"{found.group(1)}p"
    found = _BARE_RESOLUTION.search(details)
    if found:
        return f"{found.group(0)}p"
    return "2160p" if _UHD.search(details) else ""


def _latin(title):
    return all(unicodedata.name(c, "").startswith("LATIN") for c in title if c.isalpha())


def _flags(details, title):
    """English shows when it's named or when nothing says otherwise. Spanish shows only when it's named."""
    text = _SUBTITLES.sub(" ", _SITE_NAME.sub(" ", details))
    spanish = _SPANISH.search(text)
    english = _ENGLISH.search(text) or _ORIGINAL_TOO.search(text) or not (spanish or _OTHER_LANGUAGE.search(text) or not _latin(title))
    flags = [flag for flag, shown in (("🇺🇸", english), ("🇪🇸", spanish)) if shown]
    return f"[{' '.join(flags)}]" if flags else ""


def label(name, size=None):
    """Two lines for a person: 'Movie Title (2024) BluRay [🇺🇸]' over '2160p [6GB]'. Only for showing; the library's paths come from guess()."""
    pick, details = _parse(name)
    stem = _stem(name)
    title, number = pick["title"], ""
    if pick["kind"] == "show":
        last = _EPISODE_END.match(stem, _EPISODE.search(stem).end())
        number = f"S{pick['season']:02d}E{pick['episode']:02d}" + (f"-{int(last.group(1)):02d}" if last else "")
    else:
        pack = _season_part(stem)
        if pack:
            title, details = _named(_show_title(stem[:pack.start()])), stem[pack.end():]
            number = f"S{int(pack.group(1)):02d}" + (f"-{int(pack.group(2)):02d}" if pack.group(2) else "")
        elif pick["year"]:
            number = f"({pick['year']})"
    quality = next((shown for shown, pattern in _QUALITIES if pattern.search(details)), "")
    first = (title, number, quality, _flags(details, title))
    second = (_resolution(details), f"[{_size(size)}]" if size else "")
    return {"name": " ".join(filter(None, first)), "description": " ".join(filter(None, second))}


def _clean_title(title):
    title = " ".join(title.split()) if isinstance(title, str) else ""
    if not title or title.startswith(".") or re.search(r"[/\\\x00-\x1f\ud800-\udfff]", title):
        raise RDError("Give it a title without slashes.")
    if len(title.encode()) > 150:  # counted in bytes, as the disk counts file names
        raise RDError("That title is too long.")
    return title


def _number(value, top, what):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= top:
        raise RDError(f"That {what} doesn't look right.")
    return value


def destination(pick, ext):
    """(library, path inside it) for a pick, in the layout Jellyfin matches best."""
    ext = str(ext).lower()
    if ext not in VIDEO:
        raise RDError("That isn't a video file.")
    title = _clean_title(pick.get("title", ""))
    kind = pick.get("kind")
    if kind == "movie":
        if pick.get("year") is not None:
            title = f"{title} ({_number(pick['year'], 2100, 'year')})"
        return "movie", f"{title}/{title}{ext}"
    if kind == "show":
        season, episode = _number(pick.get("season"), 200, "season"), _number(pick.get("episode"), 5000, "episode")
        return "show", f"{title}/Season {season:02d}/{title} S{season:02d}E{episode:02d}{ext}"
    raise RDError("Choose Movie or Show.")


def is_video(path):
    return Path(path).suffix.lower() in VIDEO


def _suggested(files):
    """The ids of the files to tick by default: videos, without samples, extras, or the many short streams on a disc."""
    videos = [f for f in files if is_video(f["path"]) and not _SAMPLE.search(f["path"])
              and (not _EXTRAS_FOLDER.search(f["path"]) or _EPISODE.search(Path(f["path"]).name))]  # a show can be called Extras
    streams = [f for f in videos if _DISC_STREAM.search(f["path"])]
    main = max(streams, key=lambda f: f["bytes"], default=None)
    return {f["id"] for f in videos if f is main or f not in streams}


def _better_guess(base, media):
    """Real-Debrid's read of a file beats a guess from its name, wherever it actually gives an answer."""
    def whole(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    title = media.get("filename") or base["title"]
    if media.get("type") == "show" and whole(media.get("season")) is not None and whole(media.get("episode")) is not None:
        return {"kind": "show", "title": title, "season": whole(media["season"]), "episode": whole(media["episode"])}
    if media.get("type") == "movie":
        return {"kind": "movie", "title": title, "year": whole(media.get("year")) or base.get("year")}
    return base


class _Stop(Exception):
    """This download should end here: cancelled, or the service is shutting down."""


def _safe_id(id):
    id = str(id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", id):
        raise RDError("Unknown item.")
    return id


class Engine:
    def __init__(self, roots, state_path, token=None, opener=urllib.request.urlopen, spare=5 * 1024**3, retry_delay=20, workers=2):
        self.roots = {k: Path(v) for k, v in roots.items()}
        self.state_path = Path(state_path)
        self.spare, self.retry_delay, self.workers = spare, retry_delay, workers
        self._token, self._open = token, opener
        self._lock = threading.RLock()
        self._cache = {}  # key -> (fetched at, value)
        self._wake = threading.Condition(self._lock)
        self._stopping = threading.Event()
        self._threads, self._cancelled, self._speed = [], set(), {}
        self._jobs = self._load()

    # ---- Real-Debrid API ---------------------------------------------------------------

    def token(self):
        t = self._token or os.environ.get("REALDEBRID_API_TOKEN")
        for f in TOKEN_FILES if not t else []:
            try:
                t = f.read_text().strip()
            except OSError:
                pass
        return t or None

    def _call(self, method, path, form=None, raw=None):
        tok = self.token()
        if not tok:
            raise RDError("Real-Debrid isn't connected: no token.")
        data = raw if raw is not None else urllib.parse.urlencode(form).encode() if form is not None else None
        req = urllib.request.Request(API + path, method=method, headers={"Authorization": f"Bearer {tok}"}, data=data)
        try:
            with self._open(req, timeout=20) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RDError("Real-Debrid rejected the token.", 401)
            detail = ""
            try:
                detail = json.loads(e.read()).get("error", "")
            except Exception:
                pass
            raise RDError(f"Real-Debrid: {str(detail).replace('_', ' ') or e.code}", e.code)
        except OSError:
            raise RDError("Can't reach Real-Debrid.")
        return json.loads(body) if body else None

    def _cached(self, key, ttl, fetch):
        got = self._cache.get(key)
        if got and time.time() - got[0] < ttl:
            return got[1]
        value = fetch()
        self._cache[key] = (time.time(), value)
        return value

    def _forget(self, *keys):
        for k in keys:
            self._cache.pop(k, None)

    # ---- account -----------------------------------------------------------------------

    def overview(self):
        if not self.token():
            return {"configured": False, "account": None, "active": None, "disk": None}
        keep = ("username", "type", "points", "premium", "expiration")
        user = self._cached("user", 60, lambda: self._call("GET", "/user"))
        u = shutil.disk_usage(self._existing_root())
        return {"configured": True, "account": {k: user.get(k) for k in keep},
                "active": self._cached("active", 10, lambda: self._call("GET", "/torrents/activeCount")),
                "disk": {"free": u.free, "total": u.total}}

    def _existing_root(self):
        for root in self.roots.values():
            for p in (root, *root.parents):
                if p.exists():
                    return p
        return Path("/")

    # ---- torrents ----------------------------------------------------------------------

    def torrents(self):
        keep = ("id", "filename", "bytes", "status", "progress", "speed", "seeders", "added", "ended")
        rows = self._cached("torrents", 5, lambda: self._call("GET", f"/torrents?limit={MAX_TORRENTS}") or [])
        saved, saving = {}, {}
        with self._lock:
            for job in self._jobs:
                if job.get("torrent") and self._is_saved(job):
                    saved.setdefault(job["torrent"], set()).add(job["file"])
                elif job.get("torrent") and job["status"] != "done":
                    saving.setdefault(job["torrent"], set()).add(job["file"])
        return [{**{k: r.get(k) for k in keep}, "label": label(r["filename"], r.get("bytes")), "more": self._more(r, saved.get(r["id"], set())),
                 "saved": len(saved.get(r["id"], ())), "saving": len(saving.get(r["id"], ()))} for r in rows]

    def _wanted_videos(self, id):
        files = self._info(id)["files"]
        return {f["id"] for f in files if f["selected"]} & _suggested(files)

    def _more(self, row, saved):
        """How many more videos a part-saved torrent has. Counting its links costs nothing, so most torrents never need the extra call."""
        if not saved or len(saved) >= len(row.get("links") or []):
            return 0
        try:
            videos = self._cached(("videos", row["id"]), 3600, lambda: self._wanted_videos(row["id"]))
        except RDError:
            return 0
        return len(videos - saved)

    def _info(self, id):
        id = _safe_id(id)
        return self._cached(("info", id), 4, lambda: self._call("GET", f"/torrents/info/{id}"))

    def torrent(self, id):
        info = self._info(id)
        wanted = _suggested(info.get("files", []))
        alone = len({f["id"] for f in info.get("files", []) if f["selected"]} & wanted or wanted) == 1
        files = [{"id": f["id"], "path": f["path"], "name": Path(f["path"]).name, "bytes": f["bytes"], "selected": bool(f["selected"]),
                  "video": is_video(f["path"]), "suggested": f["id"] in wanted, "guess": _guess_within(f["path"], info.get("filename", ""), alone),
                  "label": label(f["path"], f["bytes"]),
                  "job": self._file_job(info["id"], f["id"])} for f in info.get("files", [])]
        keep = ("id", "filename", "hash", "bytes", "status", "progress", "speed", "seeders", "added", "ended")
        return {**{k: info.get(k) for k in keep}, "files": files}

    def select(self, id, file_ids):
        id = _safe_id(id)
        if not isinstance(file_ids, list) or not file_ids or not all(type(f) is int for f in file_ids):
            raise RDError("Pick at least one file.")
        ids = [str(f) for f in file_ids]
        self._call("POST", f"/torrents/selectFiles/{id}", {"files": ",".join(ids)})
        self._forget(("info", id), "torrents")

    def _started(self, id):
        """A new torrent: starts by itself when there's only one video to choose, otherwise asks."""
        self._forget("torrents", "active")
        choose = True
        try:
            info = self._info(id)
            if info["status"] == "waiting_files_selection":
                wanted = sorted(_suggested(info["files"]))
                if len(wanted) == 1:
                    self.select(id, wanted)
                    choose = False
            elif info["status"] != "magnet_conversion":
                choose = False
        except RDError:
            pass  # still shows in the list, and the choice can be made from there
        return {"kind": "torrent", "id": id, "choose": choose}

    def add(self, link):
        link = str(link).strip()
        if link.startswith("magnet:?") and len(link) < 8000:
            return self._started(self._call("POST", "/torrents/addMagnet", {"magnet": link})["id"])
        if re.fullmatch(r"https?://\S+", link) and len(link) < 8000:
            return {"kind": "link", "ids": self._add_link(link)}
        raise RDError("Paste a magnet link or an http(s) link.")

    def _add_link(self, link):
        """A file link gives one download; a folder link gives one for each file inside it."""
        self._forget("links")
        try:
            return [self._call("POST", "/unrestrict/link", {"link": link})["id"]]
        except RDError as first:
            if first.status in (None, 401):
                raise
            try:
                inside = self._call("POST", "/unrestrict/folder", {"link": link})
            except RDError:
                raise first
            if not inside:
                raise first
        return [self._call("POST", "/unrestrict/link", {"link": each})["id"] for each in inside[:MAX_FOLDER]]

    def add_torrent_file(self, data):
        if not data:
            raise RDError("That file is empty.")
        return self._started(self._call("PUT", "/torrents/addTorrent", raw=data)["id"])

    def delete_torrent(self, id):
        id = _safe_id(id)
        with self._lock:
            if any(j.get("torrent") == id and j["status"] in ("queued", "saving") for j in self._jobs):
                raise RDError("This is still being saved. Cancel the save first.")
        try:
            self._call("DELETE", f"/torrents/delete/{id}")
        except RDError as err:
            if err.status != 404:
                raise
        self._forget("torrents", "active", ("info", id))

    # ---- saving to the library ---------------------------------------------------------
    #
    # A job is one file on its way into a library folder. Jobs live in state.json so they survive a restart.
    # queued -> saving -> done | failed. A job downloads to its own file in the library's PARTS folder, so it can
    # only ever resume its own bytes. The finished file appears in one step and can never replace something there.

    def _load(self):
        try:
            jobs = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return []
        for job in jobs:
            if job["status"] == "saving":  # the service stopped mid-download; its partial file is still there
                job["status"] = "queued"
        return jobs

    def _persist(self):
        """A full disk here mustn't stop the downloads: the queue is still right in memory, and the next change retries."""
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(self._jobs))
            os.replace(tmp, self.state_path)
        except OSError as err:
            print(f"Couldn't write the queue: {err}", flush=True)

    def _dest(self, job):
        return self.roots[job["library"]] / job["path"]

    def _part(self, job):
        return self.roots[job["library"]] / PARTS / f"{job['id']}.part"

    def _is_saved(self, job):
        return job["status"] == "done" and self._dest(job).exists()

    def _public(self, job):
        keep = ("id", "torrent", "file", "library", "path", "status", "done", "total", "error")
        return {**{k: job.get(k) for k in keep}, "speed": self._speed.get(job["id"], 0)}

    def _file_job(self, torrent, file):
        with self._lock:
            for job in reversed(self._jobs):
                if job.get("torrent") == torrent and job["file"] == file and (job["status"] != "done" or self._is_saved(job)):
                    return self._public(job)
        return None

    def queue(self):
        with self._lock:
            return [self._public(j) for j in self._jobs if not j.get("hidden")]

    def _job(self, id):
        for job in self._jobs:
            if job["id"] == id:
                return job
        raise RDError("That's no longer in the queue.")

    def _enqueue(self, items):
        """items: [(pick, extension, link, bytes, source fields)]. All of them go in, or none do."""
        jobs = []
        for pick, ext, link, size, source in items:
            library, path = destination(pick, ext)
            jobs.append({"id": uuid.uuid4().hex[:12], **source, "link": link, "library": library, "path": path,
                         "status": "queued", "done": 0, "total": size, "error": None})
        with self._lock:
            taken = {(j["library"], j["path"]) for j in self._jobs if j["status"] != "done"}
            for job in jobs:
                where = (job["library"], job["path"])
                if where in taken:
                    raise RDError(f"{Path(job['path']).name} is already being saved.")
                if self._dest(job).exists():
                    raise RDError(f"{Path(job['path']).name} is already in the library.")
                taken.add(where)
            waiting = sum(max(j["total"] - j["done"], 0) for j in self._jobs if j["status"] in ("queued", "saving"))
            if shutil.disk_usage(self._existing_root()).free < waiting + sum(j["total"] for j in jobs) + self.spare:
                raise RDError("Not enough space on the disk.")
            wanted = {(j["library"], j["path"]) for j in jobs}
            self._jobs = [j for j in self._jobs if (j["library"], j["path"]) not in wanted] + jobs  # drops rows for files since deleted
            self._persist()
            self._wake.notify_all()
            return [self._public(j) for j in jobs]

    @staticmethod
    def _links_by_file(info):
        """file id -> Real-Debrid link. Links line up with the selected files only."""
        chosen = [f["id"] for f in info["files"] if f["selected"]]
        links = info.get("links") or []
        if len(links) != len(chosen):
            raise RDError("Real-Debrid packed this one into an archive, so its files can't be saved one by one.")
        return dict(zip(chosen, links))

    def save(self, id, picks):
        info = self._info(id)
        if info["status"] != "downloaded":
            raise RDError("Still caching on Real-Debrid. It can be saved once it's ready.")
        files = {f["id"]: f for f in info["files"]}
        by_file = self._links_by_file(info)
        if not picks or not isinstance(picks, list) or not all(isinstance(p, dict) for p in picks):
            raise RDError("Pick at least one file.")
        items = []
        for pick in picks:
            f = files.get(pick.get("file")) if type(pick.get("file")) is int else None
            if not f:
                raise RDError("That file isn't in this torrent.")
            ext = Path(f["path"]).suffix
            destination(pick, ext)
            if f["id"] not in by_file:
                raise RDError(f"{Path(f['path']).name} wasn't picked when this was added, so Real-Debrid doesn't have it.")
            items.append((pick, ext, by_file[f["id"]], f["bytes"], {"torrent": info["id"], "file": f["id"]}))
        return self._enqueue(items)

    def _sweep(self):
        """Partial files whose job is gone, as when a cancel and a shutdown happened together."""
        with self._lock:
            wanted = {self._part(j) for j in self._jobs}
        for root in self.roots.values():
            for part in (root / PARTS).glob("*.part"):
                if part not in wanted:
                    part.unlink(missing_ok=True)

    def start(self):
        self._sweep()
        self._stopping.clear()
        self._threads = [threading.Thread(target=self._worker, daemon=True) for _ in range(self.workers)]
        for t in self._threads:
            t.start()

    def stop(self):
        self._stopping.set()
        with self._lock:
            self._wake.notify_all()
        for t in self._threads:
            t.join(10)
        self._threads = []

    def _worker(self):
        while not self._stopping.is_set():
            with self._lock:
                job = next((j for j in self._jobs if j["status"] == "queued"), None)
                if not job:
                    self._wake.wait(5)
                    continue
                job.update(status="saving", error=None)
                self._persist()
            self._run(job)

    def _run(self, job):
        tries = 0
        while True:
            before = job["done"]
            try:
                self._fetch(job)
                return
            except _Stop:
                return
            except (RDError, OSError) as err:
                failure = err
                # A refusal from Real-Debrid won't change by trying again; a dropped connection or a busy server often will.
                final = isinstance(err, RDError) and err.status is not None and err.status != 429 and err.status < 500
            except Exception as err:  # never let a surprise take the worker down with it
                failure, final = err, True
            tries = 1 if job["done"] > before else tries + 1
            if final or tries >= RETRIES:
                return self._finish_failed(job, failure)
            if self._stopping.wait(self.retry_delay):
                return

    def _finish_failed(self, job, err):
        with self._lock:
            self._speed.pop(job["id"], None)
            if job["id"] in self._cancelled:
                self._cancelled.discard(job["id"])
                self._part(job).unlink(missing_ok=True)
                return
            job.update(status="failed", error=str(err) or "The download failed.")
            self._persist()

    def _check(self, job):
        if job["id"] in self._cancelled:
            with self._lock:
                self._cancelled.discard(job["id"])
                self._speed.pop(job["id"], None)
                self._part(job).unlink(missing_ok=True)
            raise _Stop()
        if self._stopping.is_set():
            raise _Stop()

    def _fetch(self, job):
        dest = self._dest(job)
        part = self._part(job)
        if dest.exists():
            raise RDError(f"{dest.name} is already in the library.", 409)
        u = self._call("POST", "/unrestrict/link", {"link": job["link"]})  # direct links expire, so get a fresh one every time
        if u.get("filename") and not is_video(u["filename"]):
            raise RDError("Real-Debrid packed this one into an archive, so it can't be saved.", 409)
        total = job["total"] = u.get("filesize") or job["total"]
        part.parent.mkdir(parents=True, exist_ok=True)
        have = part.stat().st_size if part.exists() else 0
        if have > total:
            part.unlink()
            have = 0
        job["done"] = have
        if have < total:
            self._download(job, u["download"], part, have)
        self._check(job)
        if part.stat().st_size != total:
            raise OSError(f"The download stopped early, at {part.stat().st_size} of {total} bytes.")
        with self._lock:
            self._check(job)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(part, dest)  # unlike a rename, this refuses to replace a file that appeared meanwhile
            except FileExistsError:
                raise RDError(f"{dest.name} is already in the library.", 409)
            part.unlink()
            self._speed.pop(job["id"], None)
            job.update(status="done", done=total, error=None)
            self._persist()

    def _download(self, job, url, part, have):
        req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        with self._open(req, timeout=60) as r:
            resumed = r.status == 206
            job["done"] = have if resumed else 0
            mark, marked = time.time(), job["done"]
            with open(part, "ab" if resumed else "wb") as f:
                while True:
                    self._check(job)
                    chunk = r.read(1 << 20)
                    if not chunk:
                        return
                    f.write(chunk)
                    job["done"] += len(chunk)
                    now = time.time()
                    if now - mark >= 1:
                        self._speed[job["id"]] = int((job["done"] - marked) / (now - mark))
                        mark, marked = now, job["done"]

    def cancel(self, id):
        with self._lock:
            job = self._job(id)
            if job["status"] == "done":
                job["hidden"] = True  # the row goes; the file stays, and so does knowing it was saved
            else:
                self._jobs.remove(job)
                if job["status"] == "saving":
                    self._cancelled.add(id)  # its worker tidies up on its next pass
                else:
                    self._part(job).unlink(missing_ok=True)
            self._persist()

    def retry(self, id):
        with self._lock:
            job = self._job(id)
            if job["status"] != "failed":
                raise RDError("Only a failed save can be tried again.")
            job.update(status="queued", error=None)
            self._persist()
            self._wake.notify_all()

    def clear(self):
        with self._lock:
            for job in self._jobs:
                if job["status"] == "done":
                    job["hidden"] = True
            self._persist()

    # ---- links -------------------------------------------------------------------------

    def _link_rows(self):
        return self._cached("links", 5, lambda: self._call("GET", f"/downloads?limit={MAX_LINKS}") or [])

    def links(self):
        with self._lock:
            jobs = {}
            for job in self._jobs:
                if job.get("download") and (job["status"] != "done" or self._is_saved(job)):
                    jobs[job["download"]] = self._public(job)
        return [{"id": r["id"], "name": r["filename"], "bytes": r["filesize"], "host": r.get("host"), "generated": r.get("generated"),
                 "download": r.get("download"), "video": is_video(r["filename"]), "guess": guess(r["filename"]), "job": jobs.get(r["id"])}
                for r in self._link_rows()]

    def _link_row(self, id):
        id = _safe_id(id)
        row = next((r for r in self._link_rows() if r["id"] == id), None)
        if not row:
            raise RDError("That link is no longer on Real-Debrid.")
        return row

    def save_link(self, id, pick):
        row = self._link_row(id)
        if not isinstance(pick, dict):
            raise RDError("Choose Movie or Show.")
        return self._enqueue([(pick, Path(row["filename"]).suffix, row["link"], row["filesize"], {"download": row["id"], "file": None})])

    def delete_link(self, id):
        id = _safe_id(id)
        try:
            self._call("DELETE", f"/downloads/delete/{id}")
        except RDError as err:
            if err.status != 404:
                raise
        self._forget("links")

    # ---- account controls --------------------------------------------------------------

    def hosts(self):
        def fetch():
            status, traffic = self._call("GET", "/hosts/status"), self._call("GET", "/traffic") or {}
            rows = []
            for host, h in status.items():
                t = traffic.get(host) or {}
                rows.append({"host": host, "name": h.get("name") or host, "status": h.get("status"), "supported": bool(h.get("supported")),
                             "left": t.get("left"), "limit": t.get("limit"), "used": t.get("bytes"), "type": t.get("type"), "reset": t.get("reset")})
            return sorted(rows, key=lambda r: (r["status"] != "up", r["name"].lower()))
        return self._cached("hosts", 300, fetch)

    def convert_points(self):
        self._call("POST", "/settings/convertPoints", {})
        self._forget("user")

    # ---- what's inside a file ----------------------------------------------------------

    def _details(self, link, path):
        media = self._call("GET", f"/streaming/mediaInfos/{_safe_id(self._call('POST', '/unrestrict/link', {'link': link})['id'])}")
        d = media.get("details") or {}
        video = next(iter((d.get("video") or {}).values()), {})
        return {"duration": media.get("duration"), "bitrate": media.get("bitrate"), "width": video.get("width"), "height": video.get("height"),
                "codec": video.get("codec"), "audio": [{"lang": a.get("lang"), "codec": a.get("codec"), "channels": a.get("channels")} for a in (d.get("audio") or {}).values()],
                "subtitles": [s.get("lang") for s in (d.get("subtitles") or {}).values()], "guess": _better_guess(guess(path), media)}

    def file_info(self, id, file):
        info = self._info(id)
        f = next((f for f in info["files"] if f["id"] == file), None)
        if not f:
            raise RDError("That file isn't in this torrent.")
        link = self._links_by_file(info).get(file) if info["status"] == "downloaded" else None
        if not link:
            raise RDError(f"{Path(f['path']).name} wasn't picked or isn't ready, so Real-Debrid can't describe it.")
        return self._cached(("media", id, file), 3600, lambda: self._details(link, f["path"]))

    def link_info(self, id):
        row = self._link_row(id)
        return self._cached(("media", row["id"]), 3600, lambda: self._details(row["link"], row["filename"]))
