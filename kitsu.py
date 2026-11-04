#!/usr/bin/env python3
"""
Kitsu + AniList cover/banner downloader
=======================================

Reads every anime from anime.db (table `anime`) and looks it up on:

    1. Kitsu    (free API, no key)
    2. AniList  (free API, no key) - ONLY used for whatever Kitsu is
                                     missing (cover, banner, or both)

An anime is saved only when BOTH a cover and a banner were found:

    covers/<mal_id>.jpg
    banners/<mal_id>.jpg

If, after Kitsu AND AniList, one (or both) is still missing, NOTHING is
downloaded for that anime.

Result databases
    bover.db     table `bover`     fully OK anime (cover + banner saved)
    no_bover.db  table `no_bover`  partial / not found (nothing saved)

Resume
    progress.jsonl gets one line per finished mal_id. Press Ctrl+C (or
    lose power) and run again: finished IDs are skipped. Images are
    written to temp files and renamed only when the whole pair is ready.

Usage
    python kitsu_images.py
    python kitsu_images.py --db anime.db
    python kitsu_images.py --retry-incomplete   # retry partial/no_match
    python kitsu_images.py --names-only         # skip exact MAL ID lookups
    python kitsu_images.py --scroll             # one line per anime
"""

import argparse
import difflib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import namedtuple
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

# ============================================================
# COLORS
# ============================================================

IS_TTY = sys.stdout.isatty()

try:
    import colorama

    colorama.init()  # makes ANSI colors work on older Windows consoles
except ImportError:
    pass

if IS_TTY:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
else:
    GREEN = RED = YELLOW = BLUE = MAGENTA = ""
    CYAN = WHITE = DIM = BOLD = RESET = ""

SOURCE_COLORS = {"kitsu": MAGENTA, "anilist": BLUE, "disk": DIM}

# ============================================================
# CONFIGURATION
# ============================================================

KITSU_API = "https://kitsu.io/api/edge"
KITSU_HEADERS = {
    "Accept": "application/vnd.api+json",
    "User-Agent": "AnimeImageDownloader/2.0",
}
ANILIST_API = "https://graphql.anilist.co"
ANILIST_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "AnimeImageDownloader/2.0",
}

DEFAULT_DB = "anime.db"
DEFAULT_PROGRESS = "progress.jsonl"
BOVER_DB = "bover.db"
NO_BOVER_DB = "no_bover.db"
COVERS_DIR = "covers"
BANNERS_DIR = "banners"

DEFAULT_DELAY = 1  # seconds between Kitsu calls
ANILIST_MIN_DELAY = 0.7  # AniList allows ~90 requests / minute
MAX_RETRIES = 5  # per API request
MAX_CONSECUTIVE_ERRORS = 5  # stop if the network seems to be down
MATCH_THRESHOLD = 0.85  # minimum title similarity (0-1) for name matches
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

PROGRESS_VERSION = 2
FINAL_STATUSES = {"ok", "partial", "no_match"}

ANILIST_BY_MAL_ID = """
query ($idMal: Int) {
  Media(idMal: $idMal, type: ANIME) {
    id
    idMal
    coverImage { extraLarge large }
    bannerImage
  }
}
"""

ANILIST_SEARCH = """
query ($search: String) {
  Page(perPage: 5) {
    media(search: $search, type: ANIME) {
      id
      idMal
      title { romaji english native }
      synonyms
      startDate { year }
      coverImage { extraLarge large }
      bannerImage
    }
  }
}
"""

Anime = namedtuple("Anime", "mal_id title names year")


# ============================================================
# LIVE SINGLE-LINE DISPLAY
# ============================================================


def fit(segments, width):
    """[(text, color, elastic)] -> one colored string cut to `width`."""
    segments = list(segments)
    excess = sum(len(s[0]) for s in segments) - width

    if excess > 0:
        for i, (text, color, elastic) in enumerate(segments):
            if elastic:
                keep = max(len(text) - excess, 8)

                if keep < len(text):
                    segments[i] = (text[: keep - 3] + "...", color, elastic)

                break

    out = []
    room = width

    for text, color, _ in segments:
        if room <= 0:
            break

        out.append(f"{color}{text[:room]}{RESET}")
        room -= len(text)

    return "".join(out)


def format_eta(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)

    if hours:
        return f"{hours}h{minutes:02d}m"

    if minutes:
        return f"{minutes}m{secs:02d}s"

    return f"{secs}s"


class UI:
    """
    live=True  -> ONE line that is rewritten in place for every step.
    live=False -> one finished line per anime (for logs / pipes).
    """

    def __init__(self, live, total):
        self.live = live
        self.total = total
        self.index = 0
        self.mal_id = 0
        self.name = ""
        self.counts = {"ok": 0, "partial": 0, "no_match": 0, "error": 0}
        self.overall = {"ok": 0, "partial": 0, "no_match": 0}
        self.started = time.time()
        self.on_screen = False

    # ---- building the line -------------------------------------

    def _segments(self, parts):
        o = self.overall
        finished = self.index - 1
        eta = ""

        if finished >= 3:
            per_item = (time.time() - self.started) / finished
            eta = "ETA " + format_eta(per_item * (self.total - finished))

        segments = [
            (f"[{self.index}/{self.total}] ", CYAN, False),
        ]

        if eta:
            segments.append((eta + " ", DIM, False))

        segments += [
            ("| ", DIM, False),
            (f"#{self.mal_id} ", CYAN, False),
            (self.name, WHITE + BOLD, True),
            (" | ", DIM, False),
        ]
        segments += [(text, color, False) for text, color in parts]
        segments += [
            (" (", DIM, False),
            (f"A:{o['ok']}", GREEN, False),
            ("  ", DIM, False),
            (f"P:{o['partial']}", YELLOW, False),
            ("  ", DIM, False),
            (f"N:{o['no_match']}", RED, False),
            (")", DIM, False),
        ]
        return segments

    @staticmethod
    def _parts(parts, color=WHITE):
        return [(parts, color)] if isinstance(parts, str) else parts

    # ---- public API --------------------------------------------

    def begin(self, index, mal_id, name):
        self.index = index
        self.mal_id = mal_id
        self.name = name
        self.step("starting...", DIM)

    def step(self, parts, color=WHITE):
        """Rewrite the single status line with the current step."""
        if not self.live:
            return

        width = shutil.get_terminal_size((100, 20)).columns - 1
        segments = self._segments(self._parts(parts, color))
        sys.stdout.write("\r\033[K" + fit(segments, width))
        sys.stdout.flush()
        self.on_screen = True

    def countdown(self, seconds, text):
        """Waiting (rate limit / retry) with a live countdown."""
        if not self.live:
            print(f"  {YELLOW}{text} - waiting {seconds}s{RESET}")
            time.sleep(seconds)
            return

        for left in range(seconds, 0, -1):
            self.step(f"{text} - waiting {left}s", YELLOW)
            time.sleep(1)

    def finish(self, status, parts):
        """Show the final result of the current anime."""
        self.counts[status] += 1

        if self.live:
            self.step(parts)
        else:
            segments = self._segments(self._parts(parts))
            line = "".join(f"{c}{t}{RESET}" for t, c, _ in segments)
            print(line)

    def newline(self):
        if self.live and self.on_screen:
            sys.stdout.write("\n")
            sys.stdout.flush()

        self.on_screen = False


def source_parts(label, source):
    return [
        (f"{label}=", DIM),
        (source or "-", SOURCE_COLORS.get(source, RED)),
    ]


def result_parts(record):
    status = record["status"]
    cover, banner = record["has_cover"], record["has_banner"]

    if status == "ok":
        return (
            [("OK ", GREEN + BOLD)]
            + source_parts("cover", record["cover_source"])
            + [("  ", DIM)]
            + source_parts("banner", record["banner_source"])
            + [("  saved", DIM)]
        )

    if status == "partial":
        found = "cover" if cover else "banner"
        lacking = "banner" if cover else "cover"
        return [
            ("PARTIAL ", YELLOW + BOLD),
            (f"{found} only, no {lacking}", WHITE),
            (" - skipped", DIM),
        ]

    return [
        ("NO MATCH ", RED + BOLD),
        ("nothing found", WHITE),
        (" - skipped", DIM),
    ]


# ============================================================
# SMALL HELPERS
# ============================================================


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize(text):
    """Lowercase, strip punctuation: 'Fate/Zero!' -> 'fate zero'."""
    return re.sub(r"[\W_]+", " ", (text or "").lower()).strip()


def extension_from_url(url):
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return ext if ext in IMAGE_EXTENSIONS else ".jpg"


def existing_image(folder, mal_id):
    """Path of an already-saved image for this ID, or None."""
    for ext in IMAGE_EXTENSIONS:
        path = os.path.join(folder, f"{mal_id}{ext}")

        if os.path.exists(path):
            return path

    return None


def remove_file(path):
    try:
        os.remove(path)
    except OSError:
        pass


def clean_leftover_temp_files():
    """Remove .part files left behind by a sudden stop."""
    for folder in (COVERS_DIR, BANNERS_DIR):
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                if name.endswith(".part"):
                    remove_file(os.path.join(folder, name))


# ============================================================
# PROGRESS FILE (progress.jsonl)
# ============================================================


def load_progress(path, retry_incomplete):
    """Returns the set of mal_ids that are finished."""
    if not os.path.exists(path):
        return set()

    # A crash can leave the last line without a newline. Add one so the
    # next record does not get glued onto the broken line.
    with open(path, "rb+") as file:
        file.seek(0, os.SEEK_END)

        if file.tell() > 0:
            file.seek(-1, os.SEEK_END)

            if file.read(1) != b"\n":
                file.write(b"\n")

    last = {}

    with open(path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()

            if not line:
                continue

            try:
                entry = json.loads(line)

                # Lines from the old single-source version are ignored,
                # so those anime get the AniList second chance too.
                if entry.get("v") == PROGRESS_VERSION:
                    last[int(entry["mal_id"])] = entry["status"]
            except (ValueError, KeyError, TypeError):
                continue  # half-written / corrupt line

    done = set()

    for mal_id, status in last.items():
        if status == "ok" or (
            status in FINAL_STATUSES and not retry_incomplete
        ):
            done.add(mal_id)

    return done


def append_progress(path, record):
    line = json.dumps(record, ensure_ascii=False)

    with open(path, "a", encoding="utf-8") as file:
        file.write(line + "\n")
        file.flush()
        os.fsync(file.fileno())


# ============================================================
# DATABASES
# ============================================================


def load_anime(db_path):
    """Reads anime.db -> [Anime(mal_id, title, names, year), ...]."""
    if not os.path.exists(db_path):
        raise SystemExit(f"Database not found: {db_path}")

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row

    try:
        rows = connection.execute(
            "SELECT * FROM anime ORDER BY mal_id"
        ).fetchall()
    finally:
        connection.close()

    anime = []

    for row in rows:
        columns = row.keys()
        names = []

        for column in ("title", "title_english", "title_japanese"):
            if column in columns and row[column]:
                names.append(row[column].strip())

        if "synonyms" in columns and row["synonyms"]:
            names.extend(s.strip() for s in row["synonyms"].split(","))

        unique = []
        seen = set()

        for name in names:
            key = normalize(name)

            if key and key not in seen:
                seen.add(key)
                unique.append(name)

        year = None

        if "aired_from" in columns and row["aired_from"]:
            match = re.match(r"(\d{4})", row["aired_from"])
            year = int(match.group(1)) if match else None

        title = unique[0] if unique else f"(no name) {row['mal_id']}"
        anime.append(Anime(row["mal_id"], title, unique, year))

    return anime


def open_result_dbs():
    bover = sqlite3.connect(BOVER_DB)
    bover.execute("""
        CREATE TABLE IF NOT EXISTS bover (
            mal_id        INTEGER PRIMARY KEY,
            name          TEXT NOT NULL,
            kitsu_id      TEXT,
            anilist_id    INTEGER,
            cover_source  TEXT,
            banner_source TEXT,
            cover_file    TEXT,
            banner_file   TEXT,
            saved_at      TEXT NOT NULL
        )
        """)
    bover.commit()

    no_bover = sqlite3.connect(NO_BOVER_DB)
    no_bover.execute("""
        CREATE TABLE IF NOT EXISTS no_bover (
            mal_id        INTEGER PRIMARY KEY,
            name          TEXT NOT NULL,
            status        TEXT NOT NULL,   -- partial | no_match
            has_cover     INTEGER NOT NULL,
            has_banner    INTEGER NOT NULL,
            cover_source  TEXT,
            banner_source TEXT,
            kitsu_id      TEXT,
            anilist_id    INTEGER,
            checked_at    TEXT NOT NULL
        )
        """)
    no_bover.commit()
    return bover, no_bover


def count_totals(bover, no_bover):
    """All-time A / P / N numbers straight from the two databases."""
    return {
        "ok": bover.execute("SELECT COUNT(*) FROM bover").fetchone()[0],
        "partial": no_bover.execute(
            "SELECT COUNT(*) FROM no_bover WHERE status = 'partial'"
        ).fetchone()[0],
        "no_match": no_bover.execute(
            "SELECT COUNT(*) FROM no_bover WHERE status = 'no_match'"
        ).fetchone()[0],
    }


def save_result(bover, no_bover, record):
    """Writes the record to the right DB and removes it from the other."""
    mal_id = record["mal_id"]

    if record["status"] == "ok":
        bover.execute(
            "INSERT OR REPLACE INTO bover VALUES (?,?,?,?,?,?,?,?,?)",
            (
                mal_id,
                record["name"],
                record["kitsu_id"],
                record["anilist_id"],
                record["cover_source"],
                record["banner_source"],
                record["cover_file"],
                record["banner_file"],
                record["at"],
            ),
        )
        no_bover.execute("DELETE FROM no_bover WHERE mal_id = ?", (mal_id,))
    else:
        no_bover.execute(
            "INSERT OR REPLACE INTO no_bover VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                mal_id,
                record["name"],
                record["status"],
                int(record["has_cover"]),
                int(record["has_banner"]),
                record["cover_source"],
                record["banner_source"],
                record["kitsu_id"],
                record["anilist_id"],
                record["at"],
            ),
        )
        bover.execute("DELETE FROM bover WHERE mal_id = ?", (mal_id,))

    bover.commit()
    no_bover.commit()


# ============================================================
# API CLIENTS
# ============================================================


class Api:
    name = "API"
    color = WHITE

    def __init__(self, delay, ui, headers):
        self.delay = delay
        self.ui = ui
        self.last_call = 0.0
        self.session = requests.Session()
        self.session.headers.update(headers)

    def say(self, text):
        self.ui.step([(self.name, self.color), (f": {text}", WHITE)])

    def call(self, method, url, **kwargs):
        """Request with retries. Returns parsed JSON, None on 404."""
        last_error = None

        for attempt in range(1, MAX_RETRIES + 1):
            wait = attempt * 3
            gap = time.time() - self.last_call

            if gap < self.delay:
                time.sleep(self.delay - gap)

            try:
                response = self.session.request(
                    method, url, timeout=20, **kwargs
                )
                self.last_call = time.time()

                if response.status_code == 200:
                    return response.json()

                if response.status_code == 404:
                    return None

                last_error = f"HTTP {response.status_code}"

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After", "")
                    wait = (
                        int(retry_after)
                        if retry_after.isdigit()
                        else 30 * attempt
                    )

            except (requests.RequestException, ValueError) as error:
                last_error = str(error)[:60]

            self.ui.countdown(
                wait,
                f"{self.name}: {last_error}, retry {attempt}/{MAX_RETRIES}",
            )

        raise RuntimeError(f"{self.name} failed: {last_error}")


def match_score(db_names, titles, candidate_year, db_year):
    """Best similarity (0-1) between any DB name and any candidate title."""
    best = 0.0
    wanted = [normalize(n) for n in db_names]

    for title in titles:
        candidate = normalize(title)

        for name in wanted:
            if not name or not candidate:
                continue

            if name == candidate:
                score = 1.0
            else:
                score = difflib.SequenceMatcher(None, name, candidate).ratio()

            best = max(best, score)

    # Same title but a different year is usually a different series.
    if db_year and candidate_year and abs(candidate_year - db_year) > 1:
        best -= 0.1

    return best


def pick_url(image):
    """Best available size from a Kitsu image object."""
    if not image:
        return None

    for size in ("original", "large", "medium", "small", "tiny"):
        if image.get(size):
            return image[size]

    return None


class Kitsu(Api):
    name = "Kitsu"
    color = MAGENTA

    def __init__(self, delay, ui):
        super().__init__(delay, ui, KITSU_HEADERS)

    def by_mapping(self, mal_id):
        """Exact match through Kitsu's MyAnimeList ID mapping."""
        data = self.call(
            "GET",
            f"{KITSU_API}/mappings",
            params={
                "filter[externalSite]": "myanimelist/anime",
                "filter[externalId]": mal_id,
                "include": "item",
                "page[limit]": 1,
            },
        )

        if not data or not data.get("data"):
            return None

        ref = (
            data["data"][0]
            .get("relationships", {})
            .get("item", {})
            .get("data")
        )

        if not ref or ref.get("type") != "anime":
            return None

        for included in data.get("included", []):
            if included.get("type") == "anime" and included.get(
                "id"
            ) == ref.get("id"):
                return included

        return None

    def find(self, anime, names_only):
        """Returns (item, method, score) or (None, None, score)."""
        if not names_only:
            self.say(f"looking up MAL ID {anime.mal_id}")
            item = self.by_mapping(anime.mal_id)

            if item:
                return item, "mapping", 1.0

        best_item, best_score = None, 0.0

        for name in anime.names[:3]:  # title, english, japanese
            self.say(f"searching '{name}'")
            data = self.call(
                "GET",
                f"{KITSU_API}/anime",
                params={"filter[text]": name, "page[limit]": 10},
            )

            for item in (data or {}).get("data", []):
                attrs = item.get("attributes", {})
                titles = [attrs.get("canonicalTitle")]
                titles += (attrs.get("titles") or {}).values()
                titles += attrs.get("abbreviatedTitles") or []
                start = attrs.get("startDate") or ""
                year = int(start[:4]) if start[:4].isdigit() else None
                score = match_score(
                    anime.names, [t for t in titles if t], year, anime.year
                )

                if score > best_score:
                    best_item, best_score = item, score

            if best_score >= 0.95:
                break

        if best_score >= MATCH_THRESHOLD:
            return best_item, "name", best_score

        return None, None, best_score


class AniList(Api):
    name = "AniList"
    color = BLUE

    def __init__(self, delay, ui):
        super().__init__(max(delay, ANILIST_MIN_DELAY), ui, ANILIST_HEADERS)

    def graphql(self, query, variables):
        data = self.call(
            "POST",
            ANILIST_API,
            json={"query": query, "variables": variables},
        )
        return (data or {}).get("data") or {}

    def find(self, anime, names_only):
        """Returns (media, method, score) or (None, None, score)."""
        if not names_only:
            self.say(f"looking up MAL ID {anime.mal_id}")
            media = self.graphql(
                ANILIST_BY_MAL_ID, {"idMal": anime.mal_id}
            ).get("Media")

            if media:
                return media, "mapping", 1.0

        best_media, best_score = None, 0.0

        for name in anime.names[:2]:  # title, english
            self.say(f"searching '{name}'")
            page = self.graphql(ANILIST_SEARCH, {"search": name}).get("Page")

            for media in (page or {}).get("media") or []:
                # Same name but another MAL ID = a different season/entry.
                if media.get("idMal") and media["idMal"] != anime.mal_id:
                    continue

                title = media.get("title") or {}
                titles = [title.get("romaji"), title.get("english")]
                titles += [title.get("native")] + (media.get("synonyms") or [])
                year = (media.get("startDate") or {}).get("year")
                score = match_score(
                    anime.names, [t for t in titles if t], year, anime.year
                )

                if score > best_score:
                    best_media, best_score = media, score

            if best_score >= 0.95:
                break

        if best_score >= MATCH_THRESHOLD:
            return best_media, "name", best_score

        return None, None, best_score


# ============================================================
# IMAGE DOWNLOAD (all-or-nothing)
# ============================================================


def fetch_to(session, url, temp_path):
    """Streams a URL into temp_path. False if the image is gone (404)."""
    last_error = None

    for attempt in range(1, 4):
        try:
            with session.get(url, stream=True, timeout=30) as response:
                if response.status_code == 404:
                    return False

                response.raise_for_status()

                with open(temp_path, "wb") as file:
                    for chunk in response.iter_content(chunk_size=65536):
                        file.write(chunk)

            return True

        except requests.RequestException as error:
            last_error = error
        except BaseException:
            remove_file(temp_path)  # Ctrl+C etc.: leave no half file
            raise

        remove_file(temp_path)
        time.sleep(attempt * 3)

    raise RuntimeError(f"Image download failed: {str(last_error)[:60]}")


def download_images(session, jobs, mal_id, ui):
    """
    jobs: [(kind, url, folder)]. Every file goes to a temp name first and
    is renamed only when ALL of them arrived. Returns failed kinds.
    """
    staged = []
    failed = []

    try:
        for kind, url, folder in jobs:
            ui.step([("downloading ", DIM), (kind, CYAN)])
            os.makedirs(folder, exist_ok=True)
            final = os.path.join(folder, f"{mal_id}{extension_from_url(url)}")
            temp = final + ".part"

            if fetch_to(session, url, temp):
                staged.append((temp, final))
            else:
                failed.append(kind)

        if failed:
            return failed

        for temp, final in staged:
            os.replace(temp, final)

        staged = []
        return []

    finally:
        for temp, _ in staged:
            remove_file(temp)


# ============================================================
# MAIN LOGIC
# ============================================================


def new_record(anime):
    return {
        "v": PROGRESS_VERSION,
        "mal_id": anime.mal_id,
        "name": anime.title,
        "status": "no_match",  # ok | partial | no_match
        "has_cover": False,  # found (and saved only when status == ok)
        "has_banner": False,
        "cover_source": None,  # kitsu | anilist | disk
        "banner_source": None,
        "cover_file": None,
        "banner_file": None,
        "kitsu_id": None,
        "kitsu_match": None,
        "anilist_id": None,
        "anilist_match": None,
        "at": now_iso(),
    }


def process_one(anime, kitsu, anilist, img_session, names_only, ui):
    mal_id = anime.mal_id
    record = new_record(anime)
    folders = {"cover": COVERS_DIR, "banner": BANNERS_DIR}

    urls = {"cover": None, "banner": None}
    source = {
        kind: "disk" if existing_image(folder, mal_id) else None
        for kind, folder in folders.items()
    }

    def missing():
        return [kind for kind in folders if not source[kind]]

    # ---- 1. Kitsu ---------------------------------------------
    if missing():
        item, method, _ = kitsu.find(anime, names_only)

        if item:
            record["kitsu_id"] = item.get("id")
            record["kitsu_match"] = method
            attrs = item.get("attributes", {})
            found = {
                "cover": pick_url(attrs.get("posterImage")),
                "banner": pick_url(attrs.get("coverImage")),
            }

            for kind in missing():
                if found[kind]:
                    urls[kind], source[kind] = found[kind], "kitsu"

    # ---- 2. AniList, only for what Kitsu did not give ---------
    if missing():
        ui.step(
            [
                ("AniList", BLUE),
                (": filling missing ", WHITE),
                ("+".join(missing()), YELLOW),
            ]
        )
        media, method, _ = anilist.find(anime, names_only)

        if media:
            record["anilist_id"] = media.get("id")
            record["anilist_match"] = method
            cover = media.get("coverImage") or {}
            found = {
                "cover": cover.get("extraLarge") or cover.get("large"),
                "banner": media.get("bannerImage"),
            }

            for kind in missing():
                if found[kind]:
                    urls[kind], source[kind] = found[kind], "anilist"

    # ---- 3. Download only when the pair is complete -----------
    if not missing():
        jobs = [
            (kind, urls[kind], folders[kind])
            for kind in folders
            if urls[kind]  # anything already on disk needs no download
        ]
        failed = download_images(img_session, jobs, mal_id, ui) if jobs else []

        for kind in failed:  # image URL turned out to be dead
            source[kind] = None

    record["has_cover"] = bool(source["cover"])
    record["has_banner"] = bool(source["banner"])
    record["cover_source"] = source["cover"]
    record["banner_source"] = source["banner"]

    if record["has_cover"] and record["has_banner"]:
        record["status"] = "ok"
        record["cover_file"] = os.path.basename(
            existing_image(COVERS_DIR, mal_id)
        )
        record["banner_file"] = os.path.basename(
            existing_image(BANNERS_DIR, mal_id)
        )
    elif record["has_cover"] or record["has_banner"]:
        record["status"] = "partial"

    return record


def print_summary(ui):
    try:
        bover = sqlite3.connect(BOVER_DB)
        no_bover = sqlite3.connect(NO_BOVER_DB)
        ok = bover.execute("SELECT COUNT(*) FROM bover").fetchone()[0]
        partial = no_bover.execute(
            "SELECT COUNT(*) FROM no_bover WHERE status = 'partial'"
        ).fetchone()[0]
        none = no_bover.execute(
            "SELECT COUNT(*) FROM no_bover WHERE status = 'no_match'"
        ).fetchone()[0]
        bover.close()
        no_bover.close()
    except sqlite3.Error:
        return

    c = ui.counts
    print(f"{DIM}{'-' * 60}{RESET}")
    print(f"{CYAN}{BOLD}This run{RESET}")
    print(f"  {GREEN}ok       {c['ok']}{RESET}")
    print(f"  {YELLOW}partial  {c['partial']}{RESET}")
    print(f"  {RED}no match {c['no_match']}{RESET}")
    print(
        f"  {MAGENTA}errors   {c['error']}{RESET} "
        f"{DIM}(retried next run){RESET}"
    )
    print(f"{CYAN}{BOLD}All time{RESET}")
    print(f"  {GREEN}{BOVER_DB:<12}{ok}  (cover + banner saved){RESET}")
    print(
        f"  {YELLOW}{NO_BOVER_DB:<12}{partial} partial{RESET}, "
        f"{RED}{none} not found{RESET}"
    )
    print(f"{DIM}{'-' * 60}{RESET}")


def main():
    parser = argparse.ArgumentParser(
        description="Download Kitsu/AniList covers + banners for anime.db"
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database")
    parser.add_argument(
        "--progress", default=DEFAULT_PROGRESS, help="JSONL progress file"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help="seconds between API calls (default: %(default)s)",
    )
    parser.add_argument(
        "--retry-incomplete",
        action="store_true",
        help="try the partial / no_match IDs again",
    )
    parser.add_argument(
        "--names-only",
        action="store_true",
        help="match by name only (skip exact MAL ID lookups)",
    )
    parser.add_argument(
        "--scroll",
        action="store_true",
        help="print one line per anime instead of one refreshing line",
    )
    args = parser.parse_args()

    clean_leftover_temp_files()

    anime_list = load_anime(args.db)
    done = load_progress(args.progress, args.retry_incomplete)
    todo = [a for a in anime_list if a.mal_id not in done]

    print(f"{CYAN}{'=' * 60}{RESET}")
    print(f"{CYAN}{BOLD}KITSU + ANILIST COVER / BANNER DOWNLOADER{RESET}")
    print(f"{CYAN}{'=' * 60}{RESET}")
    print(f"  Anime in {args.db}: {WHITE}{len(anime_list)}{RESET}")
    print(f"  Already finished:  {GREEN}{len(done)}{RESET}")
    print(f"  To do now:         {YELLOW}{len(todo)}{RESET}")
    print(f"{CYAN}{'=' * 60}{RESET}")

    if not todo:
        print(f"{GREEN}Nothing to do. Everything is up to date.{RESET}")
        return

    ui = UI(live=IS_TTY and not args.scroll, total=len(todo))
    kitsu = Kitsu(args.delay, ui)
    anilist = AniList(args.delay, ui)
    img_session = requests.Session()
    img_session.headers["User-Agent"] = KITSU_HEADERS["User-Agent"]
    bover, no_bover = open_result_dbs()
    ui.overall = count_totals(bover, no_bover)
    consecutive_errors = 0

    try:
        for index, anime in enumerate(todo, 1):
            ui.begin(index, anime.mal_id, anime.title)

            try:
                record = process_one(
                    anime, kitsu, anilist, img_session, args.names_only, ui
                )
                consecutive_errors = 0
            except (RuntimeError, requests.RequestException) as error:
                consecutive_errors += 1
                ui.finish(
                    "error",
                    [
                        ("ERROR ", RED + BOLD),
                        (str(error)[:70], WHITE),
                        (" - will retry next run", DIM),
                    ],
                )

                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    ui.newline()
                    print(
                        f"{RED}{MAX_CONSECUTIVE_ERRORS} errors in a row. "
                        f"Stopping. Run again later to continue.{RESET}"
                    )
                    break

                continue

            # DBs first, progress line last: a crash in between only
            # means this ID is processed once more (the writes repeat).
            save_result(bover, no_bover, record)
            append_progress(args.progress, record)
            ui.overall = count_totals(bover, no_bover)
            ui.finish(record["status"], result_parts(record))

    except KeyboardInterrupt:
        ui.newline()
        print(
            f"{YELLOW}Stopped by user. Progress is saved; "
            f"run again to resume.{RESET}"
        )
    finally:
        ui.newline()
        bover.close()
        no_bover.close()

    print_summary(ui)


if __name__ == "__main__":
    main()