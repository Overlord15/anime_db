#!/usr/bin/env python3
"""
main_curl.py - MyAnimeList anime database scraper (curl edition).

Same database, resume registry (done.jsonl), missing-ID database,
parsers, CSV export and statistics as main_clean.py - but the pages are
fetched with curl_cffi (browser TLS fingerprint, no browser, no JS), which
is much lighter and lets many IDs load in parallel.

.env keys (same as main_clean.py, plus the optional ones marked *):

    MAL_CLIENT_ID, API_BASE, ANIME_URL, CHARACTERS_URL
    START_ID, MAX_ID_OVERRIDE, REQUEST_DELAY, PAGE_TIMEOUT (ms)
    PAGE_ERROR_MAX_RETRIES, API_MAX_RETRIES, MISSING_CONFIRM_LOADS
    MAIN_DB, MISSING_DB, DONE_FILE, EXPORT_DIR

  * FETCH_CONCURRENCY   IDs fetched at the same time (default: CONCURRENCY,
                        else 5)
  * CURL_IMPERSONATE    browser profile curl_cffi imitates (default chrome)
  * USER_AGENT          override the User-Agent (default: the profile's own)
  * STAGGER_DELAY       seconds between the starts of parallel IDs (0.4)
  * HTML_DIR            where menu option [3] saves raw HTML (curl_html)

Install:  pip install curl_cffi beautifulsoup4 python-dotenv colorama requests
"""

import os
import re
import csv
import sys
import json
import shutil
import time
import html as html_lib
import asyncio
import signal
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup, NavigableString, Comment
from dotenv import load_dotenv
from colorama import init, Fore, Style

try:
    from curl_cffi.requests import AsyncSession
except ImportError:
    raise SystemExit("curl_cffi is missing. Install it: pip install curl_cffi")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ============================================================
# CONFIGURATION
# ============================================================

# Load variables into environment
load_dotenv()
init(autoreset=True)

# Credentials & Endpoints
MAL_CLIENT_ID = os.environ["MAL_CLIENT_ID"].strip()
API_BASE = os.environ["API_BASE"]
ANIME_URL = os.environ["ANIME_URL"]
CHARACTERS_URL = os.environ["CHARACTERS_URL"]

# Scraper Settings
START_ID = int(os.environ["START_ID"])
MAX_ID_OVERRIDE = int(os.environ["MAX_ID_OVERRIDE"])
REQUEST_DELAY = float(os.environ["REQUEST_DELAY"])
PAGE_TIMEOUT = int(os.environ["PAGE_TIMEOUT"])  # milliseconds

# Parallel fetching: how many anime IDs are fetched at the same time.
# Each ID loads its details page, then its characters page.
FETCH_CONCURRENCY = int(os.environ["FETCH_CONCURRENCY"])

# curl_cffi settings
CURL_IMPERSONATE = (
    os.environ.get("CURL_IMPERSONATE", "chrome").strip() or "chrome"
)
USER_AGENT = os.environ.get("USER_AGENT", "").strip()
STAGGER_DELAY = float(os.environ.get("STAGGER_DELAY", "0.4"))
HTML_DIR = os.environ.get("HTML_DIR", "curl_html")

# Refresh option [4]: rows scraped more recently than this are skipped, so a
# refresh can be stopped and resumed. Raise these if you do not want rows that
# never get a "premiered" season (old movies, OVAs...) re-fetched every run.
REFRESH_NOT_AIRED_MIN_HOURS = float(
    os.environ.get("REFRESH_NOT_AIRED_MIN_HOURS", "12")
)
REFRESH_NO_PREMIERED_MIN_HOURS = float(
    os.environ.get("REFRESH_NO_PREMIERED_MIN_HOURS", "12")
)

# Retry & Error Handling
PAGE_ERROR_MAX_RETRIES = int(os.environ["PAGE_ERROR_MAX_RETRIES"])
API_MAX_RETRIES = int(os.environ["API_MAX_RETRIES"])
MISSING_CONFIRM_LOADS = int(os.environ["MISSING_CONFIRM_LOADS"])

# Storage paths
MAIN_DB = os.environ["MAIN_DB"]
MISSING_DB = os.environ["MISSING_DB"]
DONE_FILE = os.environ["DONE_FILE"]
EXPORT_DIR = os.environ["EXPORT_DIR"]

# Legacy Migrations
OLD_FILE_NAMES = {
    "anime_database_v2.db": MAIN_DB,
    "missing_anime_ids.db": MISSING_DB,
}


# ============================================================
# CUSTOM ERRORS
# ============================================================


class PageLoadError(RuntimeError):
    """The site denied the request / page did not load properly."""

    def __init__(self, message, wait=None):
        super().__init__(message)
        self.wait = wait


class StopScraper(Exception):
    """Raised to shut the whole scraper off after repeated denials."""


# Set by the first Ctrl+C: finish the running batch, then stop.
STOP_REQUESTED = False


def _graceful_sigint(signum, frame):
    """1st Ctrl+C = finish the current batch. 2nd Ctrl+C = quit now."""
    global STOP_REQUESTED

    if STOP_REQUESTED:
        raise KeyboardInterrupt

    STOP_REQUESTED = True
    print(
        f"\n{YELLOW}[Ctrl+C] Finishing the current batch, saving it, "
        f"then closing... (press Ctrl+C again to quit immediately){RESET}"
    )


# ============================================================
# COLORS / LOGGING
# ============================================================

GREEN = Fore.GREEN
RED = Fore.RED
YELLOW = Fore.YELLOW
CYAN = Fore.CYAN
RESET = Style.RESET_ALL

logging.basicConfig(
    filename="scraper.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ============================================================
# DATABASE
# ============================================================


def migrate_old_files():
    for old, new in OLD_FILE_NAMES.items():
        if os.path.exists(old) and not os.path.exists(new):
            shutil.copy2(old, new)
            print(
                f"{YELLOW}Copied {old} -> {new} (old file left untouched)"
                f"{RESET}"
            )


migrate_old_files()

db = sqlite3.connect(MAIN_DB)
db.row_factory = sqlite3.Row

missing_db = sqlite3.connect(MISSING_DB)

CHILD_TABLES = [
    "anime_genre",
    "anime_company",
    "anime_song",
    "anime_relation",
    "anime_recommendation",
    "anime_character",
    "anime_character_va",
    "anime_staff",
]
ALL_DATA_TABLES = ["anime", "character", "person"] + CHILD_TABLES

# Same structure as the existing anime_database_v2.db (unchanged).
SCHEMA = """
CREATE TABLE IF NOT EXISTS anime (
    mal_id            INTEGER PRIMARY KEY,
    url               TEXT,
    title             TEXT NOT NULL,
    title_english     TEXT,
    title_japanese    TEXT,
    synonyms          TEXT,
    type              TEXT,
    source            TEXT,
    episodes          INTEGER,
    status            TEXT,
    aired_text        TEXT,
    aired_from        TEXT,
    aired_to          TEXT,
    premiered         TEXT,
    broadcast         TEXT,
    duration_text     TEXT,
    duration_minutes  INTEGER,
    rating            TEXT,
    score             REAL,
    scored_by         INTEGER,
    rank              INTEGER,
    popularity        INTEGER,
    members           INTEGER,
    favorites         INTEGER,
    synopsis          TEXT,
    background        TEXT,
    poster_url        TEXT,
    page_title        TEXT,
    meta_description  TEXT,
    scraped_at        TEXT NOT NULL
);

-- kind: genre | theme | demographic | explicit_genre
CREATE TABLE IF NOT EXISTS anime_genre (
    mal_id    INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    genre_id  INTEGER,
    name      TEXT NOT NULL,
    PRIMARY KEY (mal_id, kind, name)
);

-- role: studio | producer | licensor
CREATE TABLE IF NOT EXISTS anime_company (
    mal_id      INTEGER NOT NULL,
    role        TEXT NOT NULL,
    company_id  INTEGER,
    name        TEXT NOT NULL,
    PRIMARY KEY (mal_id, role, name)
);

-- kind: opening | ending
CREATE TABLE IF NOT EXISTS anime_song (
    mal_id    INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    position  INTEGER NOT NULL,
    title     TEXT,
    artist    TEXT,
    episodes  TEXT,
    PRIMARY KEY (mal_id, kind, position)
);

CREATE TABLE IF NOT EXISTS anime_relation (
    mal_id          INTEGER NOT NULL,
    relation_type   TEXT,
    related_kind    TEXT,
    related_format  TEXT,
    related_id      INTEGER NOT NULL,
    related_title   TEXT,
    PRIMARY KEY (mal_id, relation_type, related_kind, related_id)
);

CREATE TABLE IF NOT EXISTS anime_recommendation (
    mal_id            INTEGER NOT NULL,
    recommended_id    INTEGER NOT NULL,
    recommended_title TEXT,
    votes             TEXT,
    PRIMARY KEY (mal_id, recommended_id)
);

CREATE TABLE IF NOT EXISTS character (
    character_id  INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    url           TEXT
);

-- Voice actors and staff members share this table.
CREATE TABLE IF NOT EXISTS person (
    person_id  INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    url        TEXT
);

CREATE TABLE IF NOT EXISTS anime_character (
    mal_id        INTEGER NOT NULL,
    character_id  INTEGER NOT NULL,
    role          TEXT,
    PRIMARY KEY (mal_id, character_id)
);

CREATE TABLE IF NOT EXISTS anime_character_va (
    mal_id        INTEGER NOT NULL,
    character_id  INTEGER NOT NULL,
    person_id     INTEGER NOT NULL,
    language      TEXT NOT NULL,
    PRIMARY KEY (mal_id, character_id, person_id, language)
);

CREATE TABLE IF NOT EXISTS anime_staff (
    mal_id     INTEGER NOT NULL,
    person_id  INTEGER NOT NULL,
    role       TEXT NOT NULL,
    PRIMARY KEY (mal_id, person_id, role)
);

CREATE TABLE IF NOT EXISTS failed (
    mal_id        INTEGER PRIMARY KEY,
    attempts      INTEGER DEFAULT 0,
    error         TEXT,
    last_attempt  TEXT
);

CREATE TABLE IF NOT EXISTS scraper_state (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""


def initialize_databases():
    db.executescript(SCHEMA)
    missing_db.execute("""
        CREATE TABLE IF NOT EXISTS missing_ids (
            mal_id INTEGER PRIMARY KEY,
            detected_at TEXT NOT NULL
        )
    """)
    db.commit()
    missing_db.commit()
    bootstrap_registry()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# DONE REGISTRY (done.jsonl)  - one JSON line per finished ID
#   {"id": 1, "status": "ok", "title": "...", "at": "..."}
#   {"id": 7, "status": "missing", "title": null, "at": "..."}
# Resume reads this file to know which IDs to skip.
# ============================================================


def register_done(anime_id, status, title=None):
    line = json.dumps(
        {"id": anime_id, "status": status, "title": title, "at": now_iso()},
        ensure_ascii=False,
    )

    with open(DONE_FILE, "a", encoding="utf-8") as file:
        file.write(line + "\n")
        file.flush()
        os.fsync(file.fileno())


def load_done_ids():
    """IDs registered as finished (scraped or confirmed missing)."""
    done = set()

    if not os.path.exists(DONE_FILE):
        return done

    with open(DONE_FILE, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()

            if not line:
                continue

            try:
                entry = json.loads(line)
            except ValueError:
                continue  # e.g. half-written last line after a crash

            if entry.get("status") in ("ok", "missing"):
                done.add(int(entry["id"]))

    return done


def bootstrap_registry():
    """First run with this file: build it from the databases."""
    if os.path.exists(DONE_FILE):
        return

    count = 0

    with open(DONE_FILE, "w", encoding="utf-8") as file:
        for row in db.execute(
            "SELECT mal_id, title FROM anime ORDER BY mal_id"
        ):
            file.write(
                json.dumps(
                    {
                        "id": row[0],
                        "status": "ok",
                        "title": row[1],
                        "at": now_iso(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            count += 1

        for row in missing_db.execute(
            "SELECT mal_id FROM missing_ids ORDER BY mal_id"
        ):
            file.write(
                json.dumps(
                    {
                        "id": row[0],
                        "status": "missing",
                        "title": None,
                        "at": now_iso(),
                    }
                )
                + "\n"
            )
            count += 1

    if count:
        print(
            f"{CYAN}Created {DONE_FILE} from existing data "
            f"({count} IDs).{RESET}"
        )


# ============================================================
# SCRAPER STATE
# ============================================================


def get_state(key, default=None):
    row = db.execute(
        "SELECT value FROM scraper_state WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def save_state(key, value):
    db.execute(
        "INSERT OR REPLACE INTO scraper_state (key, value) VALUES (?, ?)",
        (key, str(value)),
    )
    db.commit()


# ============================================================
# DATABASE HELPERS
# ============================================================


def scraped_ids():
    return {r[0] for r in db.execute("SELECT mal_id FROM anime")}


def missing_ids():
    return {r[0] for r in missing_db.execute("SELECT mal_id FROM missing_ids")}


def failed_ids():
    return {r[0] for r in db.execute("SELECT mal_id FROM failed")}


def save_missing_id(anime_id):
    missing_db.execute(
        "INSERT OR IGNORE INTO missing_ids (mal_id, detected_at) VALUES (?, ?)",
        (anime_id, now_iso()),
    )
    missing_db.commit()
    db.execute("DELETE FROM failed WHERE mal_id = ?", (anime_id,))
    db.commit()


def save_failed(anime_id, error):
    db.execute(
        """
        INSERT INTO failed (mal_id, attempts, error, last_attempt)
        VALUES (?, 1, ?, ?)
        ON CONFLICT(mal_id) DO UPDATE SET
            attempts = attempts + 1,
            error = excluded.error,
            last_attempt = excluded.last_attempt
    """,
        (anime_id, str(error), now_iso()),
    )
    db.commit()


def legacy_row_ids():
    """
    Anime saved by the OLD scraper. They contain the excess data this
    version no longer produces:
      * background holding the anime title (ad block leaked in)
      * recommendation votes = 'AutoRec' / wrong recommended_id
      * meta_description with the 'Looking for information...' boilerplate
      * page_title with the ' - MyAnimeList.net' suffix
    """
    rows = db.execute("""
        SELECT mal_id FROM anime
        WHERE meta_description LIKE 'Looking for information on the anime%'
           OR page_title LIKE '% - MyAnimeList.net'
           OR (background IS NOT NULL
               AND (background = title OR background = title_english))
        UNION
        SELECT mal_id FROM anime_recommendation
        WHERE votes = 'AutoRec' OR mal_id = recommended_id
    """).fetchall()
    return {r[0] for r in rows}


def refresh_candidates(not_aired=True, no_premiered=True):
    """
    Already-scraped anime that should be fetched again:
      * status is 'Not yet aired'   (release info changes over time)
      * premiered season is empty   (filled in once the season is known)
    A row matching EITHER condition is returned. Rows scraped within the
    refresh age window are skipped (see REFRESH_* settings).
    """
    now = datetime.now(timezone.utc)
    cut_na = (now - timedelta(hours=REFRESH_NOT_AIRED_MIN_HOURS)).isoformat()
    cut_pm = (now - timedelta(hours=REFRESH_NO_PREMIERED_MIN_HOURS)).isoformat()

    conditions = []
    params = []

    if not_aired:
        conditions.append(
            "(LOWER(TRIM(status)) = 'not yet aired' AND scraped_at < ?)"
        )
        params.append(cut_na)

    if no_premiered:
        conditions.append(
            "((premiered IS NULL OR TRIM(premiered) = '') AND scraped_at < ?)"
        )
        params.append(cut_pm)

    if not conditions:
        return []

    rows = db.execute(
        f"SELECT mal_id FROM anime WHERE {' OR '.join(conditions)} "
        "ORDER BY mal_id",
        params,
    ).fetchall()
    return [r[0] for r in rows]


def diff_anime_row(anime_id, new_row):
    """
    Compares the stored anime row with the freshly scraped one (ignoring
    scraped_at). Returns {column: (old, new)} for every changed column.
    """
    old = db.execute(
        "SELECT * FROM anime WHERE mal_id = ?", (anime_id,)
    ).fetchone()

    if old is None:
        return {}

    changes = {}

    for column in new_row.keys():
        if column == "scraped_at":
            continue

        if old[column] != new_row[column]:
            changes[column] = (old[column], new_row[column])

    return changes


def save_anime(anime_id, details, cast):
    """Stores one anime across all tables in a single transaction."""
    row = details["anime"]

    with db:
        for table in CHILD_TABLES:
            db.execute(f"DELETE FROM {table} WHERE mal_id = ?", (anime_id,))

        columns = list(row.keys())
        db.execute(
            f"INSERT OR REPLACE INTO anime ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            [row[column] for column in columns],
        )

        db.executemany(
            "INSERT OR IGNORE INTO anime_genre "
            "(mal_id, kind, genre_id, name) VALUES (?, ?, ?, ?)",
            [
                (anime_id, g["kind"], g["id"], g["name"])
                for g in details["genres"]
            ],
        )

        db.executemany(
            "INSERT OR IGNORE INTO anime_company "
            "(mal_id, role, company_id, name) VALUES (?, ?, ?, ?)",
            [
                (anime_id, c["role"], c["id"], c["name"])
                for c in details["companies"]
            ],
        )

        db.executemany(
            "INSERT OR REPLACE INTO anime_song "
            "(mal_id, kind, position, title, artist, episodes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    anime_id,
                    s["kind"],
                    s["position"],
                    s["title"],
                    s["artist"],
                    s["episodes"],
                )
                for s in details["songs"]
            ],
        )

        db.executemany(
            "INSERT OR IGNORE INTO anime_relation "
            "(mal_id, relation_type, related_kind, related_format, "
            "related_id, related_title) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    anime_id,
                    r["relation_type"],
                    r["kind"],
                    r["format"],
                    r["id"],
                    r["title"],
                )
                for r in details["relations"]
            ],
        )

        db.executemany(
            "INSERT OR IGNORE INTO anime_recommendation "
            "(mal_id, recommended_id, recommended_title, votes) "
            "VALUES (?, ?, ?, ?)",
            [
                (anime_id, r["id"], r["title"], r["votes"])
                for r in details["recommendations"]
            ],
        )

        for ch in cast["characters"]:
            db.execute(
                "INSERT OR REPLACE INTO character "
                "(character_id, name, url) VALUES (?, ?, ?)",
                (ch["id"], ch["name"], ch["url"]),
            )
            db.execute(
                "INSERT OR REPLACE INTO anime_character "
                "(mal_id, character_id, role) VALUES (?, ?, ?)",
                (anime_id, ch["id"], ch["role"]),
            )

            for va in ch["voice_actors"]:
                db.execute(
                    "INSERT OR REPLACE INTO person "
                    "(person_id, name, url) VALUES (?, ?, ?)",
                    (va["id"], va["name"], va["url"]),
                )
                db.execute(
                    "INSERT OR IGNORE INTO anime_character_va "
                    "(mal_id, character_id, person_id, language) "
                    "VALUES (?, ?, ?, ?)",
                    (anime_id, ch["id"], va["id"], va["language"]),
                )

        for member in cast["staff"]:
            db.execute(
                "INSERT OR REPLACE INTO person "
                "(person_id, name, url) VALUES (?, ?, ?)",
                (member["id"], member["name"], member["url"]),
            )
            for role in member["roles"]:
                db.execute(
                    "INSERT OR IGNORE INTO anime_staff "
                    "(mal_id, person_id, role) VALUES (?, ?, ?)",
                    (anime_id, member["id"], role),
                )

        db.execute("DELETE FROM failed WHERE mal_id = ?", (anime_id,))


# ============================================================
# CSV EXPORT (one file per table)
# ============================================================


def export_csv():
    print(f"{CYAN}Exporting tables to CSV...{RESET}")
    os.makedirs(EXPORT_DIR, exist_ok=True)

    for table in ALL_DATA_TABLES:
        cursor = db.execute(f"SELECT * FROM {table}")
        headers = [column[0] for column in cursor.description]

        with open(
            os.path.join(EXPORT_DIR, f"{table}.csv"),
            "w",
            newline="",
            encoding="utf-8-sig",
        ) as file:
            writer = csv.writer(file)
            writer.writerow(headers)
            writer.writerows(cursor.fetchall())

    print(f"{GREEN}CSV export completed ({EXPORT_DIR}/).{RESET}")


# ============================================================
# MAL API  (used ONLY to find the highest anime ID)
# ============================================================

SEASONS = ["winter", "spring", "summer", "fall"]


def upcoming_seasons(count=6):
    """Current season followed by the next ones: [(2026, 'fall'), ...]"""
    now = datetime.now()
    index = (now.month - 1) // 3
    year = now.year
    result = []

    for _ in range(count):
        result.append((year, SEASONS[index]))
        index += 1

        if index == 4:
            index = 0
            year += 1

    return result


def api_get(session, url, params):
    """GET with retries. Returns parsed JSON, or None for HTTP 404."""
    last_error = None

    for attempt in range(1, API_MAX_RETRIES + 1):
        wait = attempt * 3

        try:
            response = session.get(url, params=params, timeout=20)

            if response.status_code == 200:
                return response.json()

            if response.status_code == 404:
                return None

            if response.status_code in (401, 403):
                raise RuntimeError(
                    "MAL API authorization failed. Check MAL_CLIENT_ID."
                )

            if response.status_code == 400 and "nsfw" in params:
                # Endpoint does not accept the optional nsfw flag.
                params = {k: v for k, v in params.items() if k != "nsfw"}
                continue

            last_error = f"HTTP {response.status_code}"

            if response.status_code == 429:
                wait = min(30 * attempt, 120)

        except requests.RequestException as error:
            last_error = str(error)

        print(
            f"{YELLOW}[API] {last_error}. Retry "
            f"{attempt}/{API_MAX_RETRIES} in {wait}s...{RESET}"
        )
        time.sleep(wait)

    raise RuntimeError(
        f"MAL API failed after {API_MAX_RETRIES} tries: {last_error}"
    )


def find_highest_id():
    """
    Highest anime ID currently known to MAL, taken from the newest
    seasons and the 'upcoming' ranking (newest IDs live there).
    """
    if MAX_ID_OVERRIDE:
        print(f"{YELLOW}Using MAX_ID override: {MAX_ID_OVERRIDE}{RESET}")
        return MAX_ID_OVERRIDE

    if not MAL_CLIENT_ID:
        raise RuntimeError(
            "MAL_CLIENT_ID is missing in .env (needed once, to find the "
            "highest anime ID). Or set MAX_ID=<number> to skip the API."
        )

    session = requests.Session()
    session.headers.update(
        {
            "X-MAL-CLIENT-ID": MAL_CLIENT_ID,
            "User-Agent": "AnimeDatabaseScraper/2.0",
        }
    )

    sources = [
        (f"{API_BASE}/anime/season/{year}/{season}", {"limit": 500})
        for year, season in upcoming_seasons()
    ]
    sources.append(
        (
            f"{API_BASE}/anime/ranking",
            {"ranking_type": "upcoming", "limit": 500},
        )
    )

    highest = 0

    try:
        for url, base_params in sources:
            offset = 0

            for _ in range(10):  # hard cap on pages per source
                data = api_get(
                    session,
                    url,
                    {**base_params, "offset": offset, "nsfw": "true"},
                )

                if not data:
                    break

                items = data.get("data", [])

                for item in items:
                    highest = max(highest, int(item["node"]["id"]))

                if not items or not data.get("paging", {}).get("next"):
                    break

                offset += len(items)
    finally:
        session.close()

    if not highest:
        raise RuntimeError(
            "MAL API returned no anime. Set MAX_ID=<number> in .env instead."
        )

    return highest


def resolve_highest_id():
    """
    Runs at the start of EVERY scrape. Finds the highest anime ID (MAL API,
    or the MAX_ID_OVERRIDE) and compares it with the value stored in the
    database. When it grew, the stored value is updated.
    """
    stored = to_int(get_state("highest_id")) or 0
    manual = bool(MAX_ID_OVERRIDE)

    try:
        found = find_highest_id()
    except RuntimeError as error:
        if not stored:
            raise

        print(f"{YELLOW}Highest-ID check failed: {error}{RESET}")
        print(f"{YELLOW}Using the stored highest ID: {stored}{RESET}")
        return stored

    save_state("highest_id_checked_at", now_iso())

    if found > stored:
        if stored:
            print(
                f"{GREEN}Highest anime ID increased: {stored} -> {found} "
                f"(+{found - stored} new IDs). Database updated.{RESET}"
            )
        else:
            print(f"{GREEN}Highest anime ID: {found} (first check){RESET}")

        save_state("highest_id", found)
        save_state("highest_id_changed_at", now_iso())
        return found

    if found == stored:
        print(f"{GREEN}Highest anime ID: {found} (unchanged){RESET}")
        return found

    if manual:
        print(
            f"{YELLOW}MAX_ID override ({found}) is lower than the stored "
            f"highest ID ({stored}). Using the override for this run only."
            f"{RESET}"
        )
        return found

    print(
        f"{YELLOW}API reports {found}, lower than the stored {stored}. "
        f"Keeping {stored}.{RESET}"
    )
    return stored


# ============================================================
# TEXT / NUMBER HELPERS
# ============================================================


def clean_text(value):
    if value is None:
        return None

    text = html_lib.unescape(str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def get_text(element):
    if not element:
        return None

    return clean_text(element.get_text(" ", strip=True))


def multiline_text(element):
    """Keeps paragraph breaks (<br>) but not breaks at inline tags."""
    if not element:
        return None

    copy = BeautifulSoup(str(element), "html.parser")

    for junk in copy.select("script, style, iframe, ins, noscript"):
        junk.decompose()

    # Raw newlines inside the HTML source are NOT line breaks; only <br> is.
    for node in copy.find_all(string=True):
        if not isinstance(node, Comment):
            node.replace_with(re.sub(r"[ \t\r\n\xa0]+", " ", str(node)))

    for br in copy.find_all("br"):
        br.replace_with("\n")

    text = copy.get_text("")
    text = html_lib.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def to_int(value):
    if value is None:
        return None

    match = re.search(r"\d[\d,]*", str(value))
    return int(match.group().replace(",", "")) if match else None


def to_float(value):
    if value is None:
        return None

    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group()) if match else None


def id_from_url(url):
    match = re.search(
        r"/(?:anime|manga|character|people|producer|genre)/(\d+)",
        url or "",
    )
    return int(match.group(1)) if match else None


def parse_date(text):
    text = (text or "").strip()

    if not text or text == "?":
        return None

    for fmt, out in (
        ("%b %d, %Y", "%Y-%m-%d"),
        ("%b %Y", "%Y-%m"),
        ("%Y", "%Y"),
    ):
        try:
            return datetime.strptime(text, fmt).strftime(out)
        except ValueError:
            continue

    return None


def parse_aired(text):
    """'Oct 8, 2026 to ?' -> ('2026-10-08', None)"""
    if not text:
        return None, None

    parts = [p.strip() for p in text.split(" to ")]
    start = parse_date(parts[0])
    end = parse_date(parts[1]) if len(parts) > 1 else start
    return start, end


def parse_duration_minutes(text):
    if not text:
        return None

    hours = re.search(r"(\d+)\s*hr", text)
    minutes = re.search(r"(\d+)\s*min", text)

    if not hours and not minutes:
        return None

    return (int(hours.group(1)) * 60 if hours else 0) + (
        int(minutes.group(1)) if minutes else 0
    )


# ---- cleaning of values that used to carry excess data ----------------

_SITE_SUFFIX = re.compile(r"\s*[-|]\s*MyAnimeList(?:\.net)?\s*$", re.I)

_META_PREFIX = re.compile(
    r"^Looking for information on the anime\b.*?\?\s*"
    r"Find out more with MyAnimeList,.*?online anime and manga community "
    r"and database\.?\s*",
    re.I | re.S,
)

# '(Source: ANN)', '[Written by MAL Rewrite]' on the last line(s).
_CREDIT_LINE = re.compile(
    r"(?:\n|^)\s*[\[(]\s*(?:Source|Written by)\b[^\n]*[\])]\s*$",
    re.I,
)

_NO_CONTENT = re.compile(r"^no (?:synopsis|background) information", re.I)


def clean_page_title(title):
    """'Cowboy Bebop (TV 1998) - MyAnimeList.net' -> 'Cowboy Bebop (TV 1998)'"""
    return clean_text(_SITE_SUFFIX.sub("", title or ""))


def clean_meta_description(text):
    """Removes MAL's fixed 'Looking for information on the anime...' lead."""
    text = clean_text(text)

    if not text:
        return None

    return clean_text(_META_PREFIX.sub("", text))


def clean_synopsis(text):
    """Removes trailing credit lines: '(Source: ANN)', '[Written by ...]'."""
    if not text:
        return None

    previous = None

    while previous != text:
        previous = text
        text = _CREDIT_LINE.sub("", text).strip()

    if not text or _NO_CONTENT.match(text):
        return None

    return text


# ============================================================
# DETAILS PAGE PARSING
# ============================================================


def find_info_row(soup, label):
    """Finds a sidebar row such as 'Studios:' / 'Genres:'."""
    wanted = label.lower().rstrip(":")

    for row in soup.select(".leftside .spaceit_pad"):
        dark = row.select_one(".dark_text")

        if dark and (get_text(dark) or "").lower().rstrip(":") == wanted:
            return row

    return None


def info_value(soup, label):
    """Plain text value of a sidebar row without the label."""
    row = find_info_row(soup, label)

    if not row:
        return None

    copy = BeautifulSoup(str(row), "html.parser")

    for junk in copy.select(
        ".dark_text, sup, script, style, .statistics-info, meta, "
        'span[style*="display: none"]'
    ):
        junk.decompose()

    value = get_text(copy)

    if value and value.lower().startswith("none found"):
        return None

    if value in (None, "?", "Unknown", "N/A"):
        return None

    return value


def info_links(soup, labels):
    """Returns [{'id', 'name'}] for the links of a sidebar row."""
    results = []

    for label in labels:
        row = find_info_row(soup, label)

        if not row:
            continue

        for link in row.find_all("a"):
            name = clean_text(link.get("title")) or get_text(link)

            if not name or name.lower() == "add some":
                continue

            entry = {"id": id_from_url(link.get("href")), "name": name}

            if entry not in results:
                results.append(entry)

    return results


def parse_songs(soup):
    songs = []

    # MAL really spells the opening class "opnening".
    for kind, selector in (
        ("opening", ".theme-songs.opnening, .theme-songs.opening"),
        ("ending", ".theme-songs.ending"),
    ):
        position = 0

        for block in soup.select(selector):
            for row in block.select("tr"):
                artist_span = row.select_one(".theme-song-artist")

                if not artist_span:
                    continue

                cell = artist_span.find_parent("td")
                copy = BeautifulSoup(str(cell), "html.parser")

                artist_copy = copy.select_one(".theme-song-artist")
                artist = re.sub(
                    r"^by\s+", "", get_text(artist_copy) or ""
                ).strip()
                artist_copy.decompose()

                episodes = None
                episode_copy = copy.select_one(".theme-song-episode")

                if episode_copy:
                    episodes = (get_text(episode_copy) or "").strip("() ")
                    episode_copy.decompose()

                for junk in copy.select("input, script, style, a.js-"):
                    junk.decompose()

                title = get_text(copy) or ""
                title = re.sub(r"^#?\d+\s*:\s*", "", title)
                title = title.strip().strip('"').strip()

                position += 1
                songs.append(
                    {
                        "kind": kind,
                        "position": position,
                        "title": title or None,
                        "artist": artist or None,
                        "episodes": episodes or None,
                    }
                )

    return songs


def parse_relations(soup):
    relations = []

    def add(relation_text, link, title=None):
        href = link.get("href") or ""
        kind_match = re.search(r"myanimelist\.net/(anime|manga)/", href)
        related_id = id_from_url(href)

        if not related_id:
            return

        relation_text = clean_text(relation_text) or ""
        relation_text = relation_text.rstrip(":")
        fmt_match = re.match(r"(.*?)\s*\((.*?)\)\s*$", relation_text)

        relations.append(
            {
                "relation_type": (
                    fmt_match.group(1) if fmt_match else relation_text
                )
                or None,
                "format": fmt_match.group(2) if fmt_match else None,
                "kind": kind_match.group(1) if kind_match else None,
                "id": related_id,
                "title": title or get_text(link),
            }
        )

    # Tile layout
    for entry in soup.select(".related-entries .entry"):
        relation = entry.select_one(".relation")
        link = entry.select_one(".title a")

        if relation and link:
            add(get_text(relation), link)

    # Table layout ("Sequel: <links>")
    for row in soup.select("table.anime_detail_related_anime tr"):
        cells = row.find_all("td")

        if len(cells) >= 2:
            for link in cells[1].find_all("a"):
                add(get_text(cells[0]), link)

    return relations


def recommended_anime_id(item, anime_id):
    """
    The recommended anime's ID.

    User recommendations can be linked as '/recommendations/anime/<a>-<b>'
    (the pair of both anime IDs). The old parser took the first number,
    which was often the anime being scraped itself. Here the ID that is
    NOT the current anime is always chosen.
    """
    direct = []
    pairs = []

    for link in item.select("a[href]"):
        href = link.get("href") or ""

        pair = re.search(r"/recommendations/anime/(\d+)-(\d+)", href)

        if pair:
            pairs.append((int(pair.group(1)), int(pair.group(2))))
            continue

        match = re.search(r"/anime/(\d+)", href)

        if match:
            direct.append(int(match.group(1)))

    for candidate in direct:
        if candidate != anime_id:
            return candidate

    for first, second in pairs:
        for candidate in (first, second):
            if candidate != anime_id:
                return candidate

    return None


def parse_recommendations(soup, anime_id):
    recommendations = []

    for item in soup.select("#anime_recommendation li.btn-anime"):
        rec_id = recommended_anime_id(item, anime_id)

        if not rec_id:
            continue

        users_text = get_text(item.select_one(".users"))

        # 'AutoRec' is a label for system suggestions, not a vote count.
        votes = (
            to_int(users_text)
            if users_text and re.search(r"\d", users_text)
            else None
        )

        recommendations.append(
            {
                "id": rec_id,
                "title": clean_text(item.get("title"))
                or get_text(item.select_one(".title")),
                "votes": votes,
            }
        )

    return recommendations


_INLINE_TAGS = {
    "a",
    "i",
    "b",
    "u",
    "em",
    "strong",
    "span",
    "small",
    "sup",
    "sub",
    "font",
    "wbr",
    "s",
    "strike",
    "q",
    "cite",
    "abbr",
    "mark",
}


def parse_background(soup, known_titles=()):
    """
    Background text sits directly after the 'Background' header as plain
    text / <br> / inline tags, and is followed by an advertisement block.
    The old parser took the next *element* (the ad block) instead.
    """
    heading = soup.find("h2", id="background")

    if not heading:
        return None

    header_block = heading.find_parent("div")

    if not header_block:
        return None

    parts = []

    for node in header_block.next_siblings:
        if isinstance(node, Comment):
            continue

        if isinstance(node, NavigableString):
            parts.append(re.sub(r"\s+", " ", str(node)))
            continue

        name = (node.name or "").lower()

        if name == "br":
            parts.append("\n")
        elif name in _INLINE_TAGS:
            parts.append(multiline_text(node) or "")
        else:
            break  # ad block / next section: background is over

    text = multiline_text(BeautifulSoup("".join(parts), "html.parser"))

    if not text or _NO_CONTENT.match(text):
        return None

    if text.lower().startswith("no background"):
        return None

    # Safety net: never store a title as the background.
    if text.casefold() in {t.casefold() for t in known_titles if t}:
        return None

    return text


def parse_details(soup, anime_id, url):
    title = get_text(soup.select_one("h1.title-name strong")) or get_text(
        soup.select_one("h1.title-name")
    )

    if not title:
        raise PageLoadError(
            "Anime title not found. Page may be blocked or incomplete."
        )

    english = get_text(soup.select_one(".title-english")) or info_value(
        soup, "English"
    )
    japanese = info_value(soup, "Japanese")

    aired_text = info_value(soup, "Aired")
    aired_from, aired_to = parse_aired(aired_text)

    duration_text = info_value(soup, "Duration")

    score_element = soup.select_one('[itemprop="ratingValue"]')
    count_element = soup.select_one('[itemprop="ratingCount"]')

    poster = soup.select_one('img[itemprop="image"]') or soup.select_one(
        ".leftside img"
    )
    poster_url = None

    if poster:
        poster_url = poster.get("data-src") or poster.get("src")

    meta = soup.select_one('meta[name="description"]')
    synopsis = clean_synopsis(
        multiline_text(soup.select_one('[itemprop="description"]'))
    )

    anime = {
        "mal_id": anime_id,
        "url": url,
        "title": title,
        "title_english": english,
        "title_japanese": japanese,
        "synonyms": info_value(soup, "Synonyms"),
        "type": info_value(soup, "Type"),
        "source": info_value(soup, "Source"),
        "episodes": to_int(info_value(soup, "Episodes")),
        "status": info_value(soup, "Status"),
        "aired_text": aired_text,
        "aired_from": aired_from,
        "aired_to": aired_to,
        "premiered": info_value(soup, "Premiered"),
        "broadcast": info_value(soup, "Broadcast"),
        "duration_text": duration_text,
        "duration_minutes": parse_duration_minutes(duration_text),
        "rating": info_value(soup, "Rating"),
        "score": to_float(get_text(score_element)),
        "scored_by": to_int(get_text(count_element)),
        "rank": to_int(info_value(soup, "Ranked")),
        "popularity": to_int(info_value(soup, "Popularity")),
        "members": to_int(info_value(soup, "Members")),
        "favorites": to_int(info_value(soup, "Favorites")),
        "synopsis": synopsis,
        "background": parse_background(
            soup, known_titles=(title, english, japanese)
        ),
        "poster_url": poster_url,
        "page_title": clean_page_title(
            soup.title.get_text() if soup.title else None
        ),
        "meta_description": clean_meta_description(
            meta.get("content") if meta else None
        ),
        "scraped_at": now_iso(),
    }

    genres = []

    for kind, labels in (
        ("genre", ["Genre", "Genres"]),
        ("theme", ["Theme", "Themes"]),
        ("demographic", ["Demographic", "Demographics"]),
        ("explicit_genre", ["Explicit Genre", "Explicit Genres"]),
    ):
        for entry in info_links(soup, labels):
            genres.append({"kind": kind, **entry})

    companies = []

    for role, labels in (
        ("studio", ["Studio", "Studios"]),
        ("producer", ["Producers", "Producer"]),
        ("licensor", ["Licensors", "Licensor"]),
    ):
        for entry in info_links(soup, labels):
            companies.append({"role": role, **entry})

    return {
        "anime": anime,
        "genres": genres,
        "companies": companies,
        "songs": parse_songs(soup),
        "relations": parse_relations(soup),
        "recommendations": parse_recommendations(soup, anime_id),
    }


# ============================================================
# CHARACTER / VOICE ACTOR / STAFF PARSING
# ============================================================


def named_people_link(container):
    """First /people/ link that actually has text (not the image link)."""
    for link in container.select('a[href*="/people/"]'):
        name = get_text(link)

        if name:
            return link, name

    return None, None


def parse_characters(soup):
    characters = {}

    for heading in soup.select('h3[class*="h3_character"]'):
        link = heading.select_one('a[href*="/character/"]')

        if not link:
            continue

        character_id = id_from_url(link.get("href"))

        if not character_id:
            continue

        row = heading.find_parent("tr")
        cell = heading.find_parent("td")
        role_element = cell.select_one("small") if cell else None

        entry = characters.setdefault(
            character_id,
            {
                "id": character_id,
                "name": get_text(link),
                "url": link.get("href"),
                "role": get_text(role_element),
                "voice_actors": [],
            },
        )

        if not row:
            continue

        for va_cell in row.select("td.va-t"):
            va_link, va_name = named_people_link(va_cell)

            if not va_link:
                continue

            language = get_text(va_cell.select_one("small")) or "Unknown"
            person_id = id_from_url(va_link.get("href"))

            va = {
                "id": person_id,
                "name": va_name,
                "url": va_link.get("href"),
                "language": language,
            }

            if person_id and va not in entry["voice_actors"]:
                entry["voice_actors"].append(va)

    return list(characters.values())


def parse_staff(soup):
    heading = None

    for h2 in soup.find_all("h2"):
        if (get_text(h2) or "").lower().startswith("staff"):
            heading = h2

    if heading is None:
        return []

    staff = {}

    for row in heading.find_all_next("tr"):
        if row.find_previous("h2") is not heading:
            break

        link, name = named_people_link(row)
        small = row.select_one("small")

        if not link or not small:
            continue

        person_id = id_from_url(link.get("href"))

        if not person_id:
            continue

        roles = [
            r.strip() for r in (get_text(small) or "").split(",") if r.strip()
        ]

        entry = staff.setdefault(
            person_id,
            {
                "id": person_id,
                "name": name,
                "url": link.get("href"),
                "roles": [],
            },
        )

        for role in roles:
            if role not in entry["roles"]:
                entry["roles"].append(role)

    return list(staff.values())


def parse_cast(soup):
    return {
        "characters": parse_characters(soup),
        "staff": parse_staff(soup),
    }


# ============================================================
# FETCHING  (curl_cffi: browser TLS fingerprint, no browser, no JS)
# ============================================================

_BLOCK_TITLES = (
    "access denied",
    "just a moment",
    "attention required",
    "too many requests",
    "forbidden",
    "captcha",
    "service unavailable",
    "bad gateway",
    "temporarily unavailable",
)


def new_session():
    """One shared session (connection pool + cookies) for all parallel IDs."""
    headers = {"Accept-Language": "en-US,en;q=0.9"}

    if USER_AGENT:
        headers["User-Agent"] = USER_AGENT

    return AsyncSession(
        impersonate=CURL_IMPERSONATE,
        headers=headers,
        max_clients=FETCH_CONCURRENCY + 2,
        allow_redirects=True,
    )


def run_async(coroutine):
    """asyncio.run() that also works for curl_cffi on Windows."""
    if sys.platform == "win32":
        if sys.version_info >= (3, 12):
            return asyncio.run(
                coroutine, loop_factory=asyncio.SelectorEventLoop
            )

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    return asyncio.run(coroutine)


async def fetch_html(session, url):
    """
    Plain GET. Returns (status, html_text, retry_after_seconds_or_None).
    Raises PageLoadError only when the request itself failed.
    """
    try:
        response = await session.get(url, timeout=max(PAGE_TIMEOUT / 1000, 5))
    except Exception as error:
        raise PageLoadError(f"Page failed to load ({url}): {error}") from error

    retry_after = response.headers.get("Retry-After")
    retry_after = (
        int(retry_after) if retry_after and retry_after.isdigit() else None
    )

    return response.status_code, response.text, retry_after


def check_status(status, url, retry_after=None):
    """Raises PageLoadError when the site denies the request."""
    if status in (403, 429):
        raise PageLoadError(
            f"Request denied: HTTP {status} ({url})",
            wait=max(30, retry_after or 0),
        )

    if status >= 500:
        raise PageLoadError(f"Server error: HTTP {status} ({url})", wait=10)


def inspect_page(status, soup, url):
    """
    Returns the status, or 404 when the page is MAL's "404 Not Found" page.
    Raises PageLoadError when the page is a block page / incomplete.
    """
    title = (soup.title.get_text() if soup.title else "").strip().lower()

    # MAL's own 404 page (see 404_Not_Found sample).
    if (
        status == 404
        or soup.select_one(".error404")
        or title.startswith("404 not found")
    ):
        return 404

    # A real MAL page always has a heading inside #content. Only treat the
    # title as a block page when that is missing, otherwise anime titles
    # such as "Forbidden Love" are flagged by mistake. The title must also
    # START with the block text (optionally after an HTTP code like "403").
    has_real_content = bool(soup.select_one("#content h1"))
    looks_blocked = bool(
        re.match(
            r"^(?:\d{3}\s*[-:]?\s*)?(?:"
            + "|".join(re.escape(word) for word in _BLOCK_TITLES)
            + r")",
            title,
        )
    )

    if looks_blocked and not has_real_content:
        raise PageLoadError(f"Blocked page: '{title}' ({url})", wait=30)

    if status >= 400:
        raise PageLoadError(f"HTTP {status} ({url})", wait=10)

    if not soup.select_one("#content"):
        raise PageLoadError(f"Page content did not load ({url})", wait=5)

    return status


async def load_html(session, url, anime_id):
    """
    Returns (status, soup).
        status == 404 -> the page is MAL's "404 Not Found" page.
    Raises PageLoadError when the site denies / blocks / times out.
    """
    status, text, retry_after = await fetch_html(session, url)
    check_status(status, url, retry_after)

    soup = BeautifulSoup(text, "html.parser")
    return inspect_page(status, soup, url), soup


@dataclass
class Result:
    anime_id: int
    status: str  # ok | missing | denied | error | cancelled
    details: dict = None
    cast: dict = None
    error: str = None


async def scrape_attempt(session, anime_id):
    url = ANIME_URL.format(anime_id)

    status, soup = await load_html(session, url, anime_id)

    # Confirm a 404 before trusting it.
    for _ in range(MISSING_CONFIRM_LOADS - 1):
        if status != 404:
            break

        await asyncio.sleep(REQUEST_DELAY)
        status, soup = await load_html(session, url, anime_id)

    if status == 404:
        return Result(anime_id, "missing")

    details = parse_details(soup, anime_id, url)

    await asyncio.sleep(REQUEST_DELAY)

    cast_status, cast_soup = await load_html(
        session, CHARACTERS_URL.format(anime_id), anime_id
    )

    cast = (
        parse_cast(cast_soup)
        if cast_status != 404
        else {"characters": [], "staff": []}
    )

    # Safety net: use the preview on the details page.
    if not cast["characters"]:
        cast["characters"] = parse_characters(soup)

    if not cast["staff"]:
        cast["staff"] = parse_staff(soup)

    return Result(anime_id, "ok", details=details, cast=cast)


async def scrape_tab(session, anime_id, stop_event, delay):
    """One slot = one anime ID. Retries denied requests, then flags a stop."""
    await asyncio.sleep(delay)  # stagger the starts a little
    last_error = None

    for attempt in range(1, PAGE_ERROR_MAX_RETRIES + 1):
        if stop_event.is_set():
            return Result(anime_id, "cancelled")

        try:
            return await scrape_attempt(session, anime_id)

        except PageLoadError as error:
            last_error = error
            logging.warning(
                "ID %s attempt %s/%s: %s",
                anime_id,
                attempt,
                PAGE_ERROR_MAX_RETRIES,
                error,
            )
            print(
                f"{YELLOW}[RETRY {attempt}/{PAGE_ERROR_MAX_RETRIES}] "
                f"ID {anime_id}: {error}{RESET}"
            )

            if attempt < PAGE_ERROR_MAX_RETRIES:
                await asyncio.sleep(max(error.wait or 0, attempt * 5))

        except Exception as error:
            logging.exception("Unexpected error for ID %s", anime_id)
            return Result(anime_id, "error", error=repr(error))

    stop_event.set()
    return Result(anime_id, "denied", error=str(last_error))


# ============================================================
# COMPONENT CHECK  (does the plain curl HTML hold the data we scrape?)
# ============================================================


def is_loaded(value):
    if value is None:
        return False

    if isinstance(value, (list, tuple, set, dict, str)):
        return len(value) > 0

    return True


def preview(value, width=58):
    if isinstance(value, list):
        if not value:
            return ""

        text = f"{len(value)} item(s), e.g. {value[0]}"
    else:
        text = str(value)

    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "\u2026"


def details_components(soup, anime_id):
    """[(name, value)] for everything parse_details() reads."""

    def links(*labels):
        return info_links(soup, list(labels))

    def names(*labels):
        return [entry["name"] for entry in links(*labels)]

    def companies(*labels):
        # An entry only counts when it has a company ID.
        return [
            f"{entry['name']} (id {entry['id']})"
            for entry in links(*labels)
            if entry["id"]
        ]

    title = get_text(soup.select_one("h1.title-name strong")) or get_text(
        soup.select_one("h1.title-name")
    )
    english = get_text(soup.select_one(".title-english")) or info_value(
        soup, "English"
    )

    poster = soup.select_one('img[itemprop="image"]') or soup.select_one(
        ".leftside img"
    )
    poster_url = (
        (poster.get("data-src") or poster.get("src")) if poster else None
    )

    meta = soup.select_one('meta[name="description"]')
    songs = parse_songs(soup)

    return [
        ("Title", title),
        ("English title", english),
        ("Japanese title", info_value(soup, "Japanese")),
        ("Synonyms", info_value(soup, "Synonyms")),
        ("Type", info_value(soup, "Type")),
        ("Source", info_value(soup, "Source")),
        ("Episodes", info_value(soup, "Episodes")),
        ("Status", info_value(soup, "Status")),
        ("Aired", info_value(soup, "Aired")),
        ("Premiered", info_value(soup, "Premiered")),
        ("Broadcast", info_value(soup, "Broadcast")),
        ("Duration", info_value(soup, "Duration")),
        ("Rating", info_value(soup, "Rating")),
        (
            "Score",
            to_float(get_text(soup.select_one('[itemprop="ratingValue"]'))),
        ),
        (
            "Scored by",
            to_int(get_text(soup.select_one('[itemprop="ratingCount"]'))),
        ),
        ("Rank", info_value(soup, "Ranked")),
        ("Popularity", info_value(soup, "Popularity")),
        ("Members", info_value(soup, "Members")),
        ("Favorites", info_value(soup, "Favorites")),
        (
            "Synopsis",
            clean_synopsis(
                multiline_text(soup.select_one('[itemprop="description"]'))
            ),
        ),
        (
            "Background",
            parse_background(soup, known_titles=(title, english)),
        ),
        ("Poster URL", poster_url),
        (
            "Page title",
            clean_page_title(soup.title.get_text() if soup.title else None),
        ),
        (
            "Meta description",
            clean_meta_description(meta.get("content") if meta else None),
        ),
        ("Genres", names("Genre", "Genres")),
        ("Themes", names("Theme", "Themes")),
        ("Demographics", names("Demographic", "Demographics")),
        ("Explicit genres", names("Explicit Genre", "Explicit Genres")),
        ("Studios", companies("Studio", "Studios")),
        ("Producers", companies("Producers", "Producer")),
        ("Licensors", companies("Licensors", "Licensor")),
        ("Openings", [s["title"] for s in songs if s["kind"] == "opening"]),
        ("Endings", [s["title"] for s in songs if s["kind"] == "ending"]),
        ("Relations", [r["title"] for r in parse_relations(soup)]),
        (
            "Recommendations",
            [r["title"] for r in parse_recommendations(soup, anime_id)],
        ),
        (
            "Cast preview (details page)",
            [c["name"] for c in parse_characters(soup)],
        ),
        (
            "Staff preview (details page)",
            [s["name"] for s in parse_staff(soup)],
        ),
    ]


def cast_components(soup):
    """[(name, value)] for everything parse_cast() reads."""
    characters = parse_characters(soup)
    voice_actors = [
        f"{va['name']} ({va['language']})"
        for character in characters
        for va in character["voice_actors"]
    ]

    return [
        ("Characters", [c["name"] for c in characters]),
        ("Voice actors", voice_actors),
        ("Staff", [s["name"] for s in parse_staff(soup)]),
    ]


def print_checklist(components):
    loaded = 0

    for name, value in components:
        ok = is_loaded(value)
        loaded += ok
        mark, color = ("\u2713", GREEN) if ok else ("\u2717", RED)
        detail = preview(value) if ok else "not found in HTML"
        print(f"  {color}[{mark}]{RESET} {name:<30} {detail}")

    print(f"  {CYAN}{loaded}/{len(components)} components loaded{RESET}")
    return loaded, len(components)


async def check_components(ids):
    os.makedirs(HTML_DIR, exist_ok=True)
    totals = [0, 0]

    async with new_session() as session:
        for anime_id in ids:
            for kind, template in (
                ("details", ANIME_URL),
                ("characters", CHARACTERS_URL),
            ):
                url = template.format(anime_id)
                print("\n" + "=" * 62)
                print(f"{CYAN}ID {anime_id} - {kind} page{RESET}  {url}")

                try:
                    status, text, retry_after = await fetch_html(session, url)
                    path = os.path.join(HTML_DIR, f"{anime_id}_{kind}.html")

                    with open(path, "w", encoding="utf-8") as file:
                        file.write(text)

                    check_status(status, url, retry_after)
                    soup = BeautifulSoup(text, "html.parser")
                    status = inspect_page(status, soup, url)

                except PageLoadError as error:
                    print(f"{RED}  {error}{RESET}")
                    await asyncio.sleep(REQUEST_DELAY)
                    continue

                print(f"  HTTP {status}   raw HTML saved -> {path}")

                if status == 404:
                    print(f"{RED}  This ID has no page.{RESET}")
                    await asyncio.sleep(REQUEST_DELAY)
                    continue

                components = (
                    details_components(soup, anime_id)
                    if kind == "details"
                    else cast_components(soup)
                )
                loaded, total = print_checklist(components)
                totals[0] += loaded
                totals[1] += total

                await asyncio.sleep(REQUEST_DELAY)

    print("\n" + "=" * 62)
    print(
        f"{GREEN}TOTAL: {totals[0]}/{totals[1]} components present "
        f"in the curl HTML{RESET}"
    )


def check_components_action():
    raw = input("Anime IDs to check (space/comma separated) [1]: ")
    ids = [int(number) for number in re.findall(r"\d+", raw)] or [1]

    try:
        run_async(check_components(ids))
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrupted by user.{RESET}")


# ============================================================
# STATISTICS
# ============================================================


def ranges(numbers):
    """[1,2,3,7,9,10] -> '1-3, 7, 9-10'"""
    numbers = sorted(numbers)
    parts = []
    start = previous = None

    for number in numbers:
        if start is None:
            start = previous = number
        elif number == previous + 1:
            previous = number
        else:
            parts.append(
                f"{start}-{previous}" if start != previous else f"{start}"
            )
            start = previous = number

    if start is not None:
        parts.append(
            f"{start}-{previous}" if start != previous else f"{start}"
        )

    return ", ".join(parts)


def print_statistics():
    def count(connection, table):
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[
            0
        ]

    done = scraped_ids()
    missing = missing_ids()
    failed = failed_ids() - done
    highest = get_state("highest_id")
    legacy = legacy_row_ids()

    print("\n" + "=" * 62)
    print("STATISTICS")
    print("=" * 62)
    print(f"{GREEN}Anime scraped:           {len(done)}{RESET}")
    print(f"{GREEN}Characters:              {count(db, 'character')}{RESET}")
    print(f"{GREEN}People (VA + staff):     {count(db, 'person')}{RESET}")
    print(f"{GREEN}Songs:                   {count(db, 'anime_song')}{RESET}")
    print(
        f"{GREEN}Relations:               {count(db, 'anime_relation')}{RESET}"
    )
    print(
        f"{GREEN}Recommendations:         "
        f"{count(db, 'anime_recommendation')}{RESET}"
    )
    print(f"{RED}IDs with no anime page:  {len(missing)}{RESET}")
    print(f"{YELLOW}Failed (will retry):     {len(failed)}{RESET}")
    print(f"{YELLOW}Old-format rows:         {len(legacy)}{RESET}")
    print(f"{CYAN}Registered in {DONE_FILE}: {len(load_done_ids())}{RESET}")

    if highest:
        highest = int(highest)
        registered = load_done_ids()
        remaining = [
            i for i in range(START_ID, highest + 1) if i not in registered
        ]
        print(f"{CYAN}Highest ID (stored):     {highest}{RESET}")

        checked = get_state("highest_id_checked_at")

        if checked:
            print(f"{CYAN}Highest ID checked at:   {checked}{RESET}")

        print(f"{CYAN}Remaining to scrape:     {len(remaining)}{RESET}")
    else:
        print(f"{CYAN}Highest ID (stored):     not checked yet{RESET}")

    print("-" * 62)

    if failed:
        print(f"{YELLOW}Failed IDs: {ranges(failed)}{RESET}")

    print("=" * 62)


# ============================================================
# MAIN SCRAPER
# ============================================================


async def run_scrape(highest_id, repair_ids, refresh_ids=None):
    # refresh_ids (menu option [4]): scrape exactly these already-stored IDs
    # again and overwrite their rows. Otherwise normal Scrape / Resume.
    refresh_mode = refresh_ids is not None

    # Resume: everything registered in done.jsonl is skipped.
    # Old-format rows are re-scraped only when the user chose to repair.
    registered = load_done_ids()
    skip = (registered - repair_ids) if repair_ids else registered

    if refresh_mode:
        todo = sorted(refresh_ids)
    else:
        todo = [i for i in range(START_ID, highest_id + 1) if i not in skip]

    print("=" * 62)
    if refresh_mode:
        print(f"{CYAN}REFRESH MODE: re-scraping stored anime{RESET}")
    print(f"Highest ID:        {highest_id}")
    print(f"In {DONE_FILE}:".ljust(19) + f"{len(registered)}")
    print(f"Missing pages:     {len(missing_ids())}")
    print(
        f"To scrape now:     {len(todo)}"
        + (f"  (incl. {len(repair_ids)} re-scrapes)" if repair_ids else "")
    )
    print(
        f"Parallel fetches:  {FETCH_CONCURRENCY} "
        f"(curl_cffi, impersonating {CURL_IMPERSONATE})"
    )
    print("=" * 62)

    if not todo:
        print(f"{GREEN}Nothing to do. Everything is up to date.{RESET}")
        return

    stop_error = None
    tally = {"ok": 0, "missing": 0, "denied": 0, "error": 0}
    refresh_stats = {
        "updated": 0,
        "unchanged": 0,
        "status_changed": 0,
        "premiered_filled": 0,
    }
    started = time.monotonic()

    async with new_session() as session:
        print(f"{GREEN}curl session started.{RESET}")

        try:
            for start in range(0, len(todo), FETCH_CONCURRENCY):
                if STOP_REQUESTED:
                    break

                batch = todo[start : start + FETCH_CONCURRENCY]
                stop_event = asyncio.Event()

                print(
                    f"\n{CYAN}[BATCH {start // FETCH_CONCURRENCY + 1}] "
                    f"IDs {', '.join(map(str, batch))}{RESET}"
                )

                # All IDs of the batch run together; wait until all finish.
                results = await asyncio.gather(
                    *(
                        scrape_tab(
                            session,
                            anime_id,
                            stop_event,
                            index * STAGGER_DELAY,
                        )
                        for index, anime_id in enumerate(batch)
                    )
                )

                # Insert the batch's results one after another (serially).
                for result in sorted(results, key=lambda r: r.anime_id):
                    anime_id = result.anime_id
                    tally[result.status] = tally.get(result.status, 0) + 1

                    if result.status == "ok":
                        changes = (
                            diff_anime_row(anime_id, result.details["anime"])
                            if refresh_mode
                            else {}
                        )
                        save_anime(anime_id, result.details, result.cast)

                        if refresh_mode:
                            if changes:
                                refresh_stats["updated"] += 1
                                parts = []

                                for column in ("status", "premiered"):
                                    if column in changes:
                                        old_v, new_v = changes[column]
                                        parts.append(
                                            f"{column}: {old_v} -> {new_v}"
                                        )

                                if "status" in changes:
                                    refresh_stats["status_changed"] += 1

                                if "premiered" in changes and not changes[
                                    "premiered"
                                ][0]:
                                    refresh_stats["premiered_filled"] += 1

                                other = [
                                    c
                                    for c in changes
                                    if c not in ("status", "premiered")
                                ]

                                if other:
                                    parts.append(
                                        f"{len(other)} other field(s)"
                                    )

                                print(
                                    f"{GREEN}[UPDATED] {anime_id} - "
                                    f"{', '.join(parts)}{RESET}"
                                )
                            else:
                                refresh_stats["unchanged"] += 1
                                print(
                                    f"{CYAN}[UNCHANGED] {anime_id} - "
                                    f"{result.details['anime']['title']}"
                                    f"{RESET}"
                                )

                            register_done(
                                anime_id,
                                "ok",
                                result.details["anime"]["title"],
                            )
                            continue

                        register_done(
                            anime_id,
                            "ok",
                            result.details["anime"]["title"],
                        )
                        print(
                            f"{GREEN}[SUCCESS] {anime_id} - "
                            f"{result.details['anime']['title']} "
                            f"({len(result.cast['characters'])} characters, "
                            f"{len(result.cast['staff'])} staff, "
                            f"{len(result.details['songs'])} songs){RESET}"
                        )
                        logging.info("Scraped ID %s", anime_id)

                    elif result.status == "missing" and refresh_mode:
                        # Keep the stored row; do not mark it as missing.
                        print(
                            f"{YELLOW}[NO PAGE] ID {anime_id} no longer "
                            f"loads. Stored row left unchanged.{RESET}"
                        )

                    elif result.status == "missing":
                        save_missing_id(anime_id)
                        register_done(anime_id, "missing")
                        print(
                            f"{RED}[NO PAGE] ID {anime_id} does not load "
                            f"an anime page.{RESET}"
                        )
                        logging.info("No anime page for ID %s", anime_id)

                    elif result.status == "denied":
                        save_failed(anime_id, result.error)
                        stop_error = (
                            f"ID {anime_id}: request denied "
                            f"{PAGE_ERROR_MAX_RETRIES} times. "
                            f"Last error: {result.error}"
                        )
                        print(f"{RED}[DENIED] ID {anime_id}{RESET}")

                    elif result.status == "error":
                        save_failed(anime_id, result.error)
                        print(
                            f"{RED}[FAILED] ID {anime_id}: "
                            f"{result.error}{RESET}"
                        )

                    # "cancelled" -> untouched, picked up again on Resume.

                if stop_error:
                    raise StopScraper(stop_error)

                if STOP_REQUESTED:
                    print(
                        f"{GREEN}Batch saved. Stopping as requested."
                        f"{RESET}"
                    )
                    break

                await asyncio.sleep(REQUEST_DELAY)

        finally:
            elapsed = max(time.monotonic() - started, 0.001)
            done_now = sum(tally.values())
            print(
                f"\n{CYAN}This run: {tally['ok']} scraped, "
                f"{tally['missing']} no page, "
                f"{tally['error'] + tally['denied']} failed "
                f"in {elapsed:.0f}s ({done_now / elapsed:.2f} IDs/s){RESET}"
            )

            if refresh_mode:
                print(
                    f"{GREEN}Refresh result: {refresh_stats['updated']} "
                    f"updated, {refresh_stats['unchanged']} unchanged "
                    f"(of {tally['ok']} re-scraped){RESET}"
                )
                print(
                    f"{GREEN}  status changed:   "
                    f"{refresh_stats['status_changed']}{RESET}"
                )
                print(
                    f"{GREEN}  premiered filled: "
                    f"{refresh_stats['premiered_filled']}{RESET}"
                )


def scrape_menu_action():
    initialize_databases()

    # Every run: check the highest ID and update the database if it grew.
    try:
        highest_id = resolve_highest_id()
    except RuntimeError as error:
        print(f"{RED}{error}{RESET}")
        return

    repair_ids = set()
    legacy = legacy_row_ids()

    if legacy:
        print(
            f"\n{YELLOW}{len(legacy)} anime in the database were saved by "
            f"the old scraper and contain excess data\n"
            f"(background = title, 'AutoRec' votes, wrong recommended IDs, "
            f"meta/page-title boilerplate).{RESET}"
        )
        answer = input("Re-scrape them with the clean parser? [y/N]: ")

        if answer.strip().lower() in ("y", "yes"):
            repair_ids = legacy

    global STOP_REQUESTED
    STOP_REQUESTED = False
    previous_handler = signal.signal(signal.SIGINT, _graceful_sigint)

    try:
        run_async(run_scrape(highest_id, repair_ids))

    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrupted by user.{RESET}")

    except StopScraper as error:
        logging.error("Scraper stopped: %s", error)
        print(f"\n{RED}[SHUT OFF] {error}{RESET}")
        print(
            f"{YELLOW}Progress is saved. Wait a while, then choose "
            f"Scrape / Resume again.{RESET}"
        )

    finally:
        signal.signal(signal.SIGINT, previous_handler)
        print(f"\n{CYAN}Saving final data...{RESET}")

        try:
            export_csv()
            print_statistics()
        except Exception as error:
            print(f"{RED}Export error: {error}{RESET}")


def refresh_menu_action():
    """Re-scrape 'Not yet aired' anime and anime with no premiered season."""
    initialize_databases()

    all_na = len(refresh_candidates(True, False))
    all_pm = len(refresh_candidates(False, True))
    both = len(refresh_candidates(True, True))

    print("\n" + "=" * 62)
    print("REFRESH STORED ANIME")
    print("=" * 62)
    print(f"  Status 'Not yet aired':       {all_na}")
    print(f"  No premiered season:          {all_pm}")
    print(f"  Either (will be scraped):     {both}")
    print(
        f"{CYAN}  (rows already scraped within the last "
        f"{REFRESH_NOT_AIRED_MIN_HOURS:g}h are skipped, so a refresh can "
        f"be resumed){RESET}"
    )
    print("\n  [1] Both  (not yet aired OR no premiered season)  [default]")
    print("  [2] Only 'Not yet aired'")
    print("  [3] Only 'no premiered season'")
    print("  [b] Back")

    mode = input("\nSelect: ").strip().lower() or "1"

    if mode not in ("1", "2", "3"):
        print("Cancelled.")
        return

    ids = refresh_candidates(
        not_aired=mode in ("1", "2"),
        no_premiered=mode in ("1", "3"),
    )

    if not ids:
        print(f"{GREEN}Nothing to refresh.{RESET}")
        return

    answer = input(f"Re-scrape {len(ids)} anime now? [Y/n]: ")

    if answer.strip().lower() in ("n", "no"):
        print("Cancelled.")
        return

    global STOP_REQUESTED
    STOP_REQUESTED = False
    previous_handler = signal.signal(signal.SIGINT, _graceful_sigint)

    try:
        highest = to_int(get_state("highest_id")) or max(ids)
        run_async(run_scrape(highest, set(), refresh_ids=ids))

    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrupted by user.{RESET}")

    except StopScraper as error:
        logging.error("Scraper stopped: %s", error)
        print(f"\n{RED}[SHUT OFF] {error}{RESET}")
        print(
            f"{YELLOW}Progress is saved. Wait a while, then run Refresh "
            f"again (already refreshed rows are skipped).{RESET}"
        )

    finally:
        signal.signal(signal.SIGINT, previous_handler)
        print(f"\n{CYAN}Saving final data...{RESET}")

        try:
            export_csv()
            print_statistics()
        except Exception as error:
            print(f"{RED}Export error: {error}{RESET}")


def main():
    initialize_databases()

    print("=" * 62)
    print("MYANIMELIST ANIME DATABASE SCRAPER  (curl)")
    print("=" * 62)
    print("  [1] Scrape / Resume")
    print("  [2] Show stats")
    print("  [3] Check page components (what does curl receive?)")
    print("  [4] Refresh: re-scrape 'Not yet aired' / no premiered season")
    print("  [q] Quit")

    try:
        choice = input("\nSelect: ").strip().lower()

        if choice == "1":
            scrape_menu_action()
        elif choice == "2":
            print_statistics()
        elif choice == "3":
            check_components_action()
        elif choice == "4":
            refresh_menu_action()
        else:
            print("Bye.")

    except (KeyboardInterrupt, EOFError):
        print(f"\n{YELLOW}Cancelled.{RESET}")

    finally:
        db.close()
        missing_db.close()


if __name__ == "__main__":
    main()