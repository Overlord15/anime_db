#!/usr/bin/env python3
"""
Download poster images (anime.poster_url) ONLY for the mal_ids listed in the
no_bover table.

Works whether no_bover lives in the same DB as `anime` or in a separate
no_bover.db file (it is ATTACHed automatically).

Usage:
    python download_no_bover_covers.py
    python download_no_bover_covers.py --anime-db anime.db --nb-db no_bover.db 
    python download_no_bover_covers.py --only-missing-cover      # only rows with has_cover = 0
    python download_no_bover_covers.py --workers 8 --delay 0.2

Requires: pip install requests
"""

import argparse
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

OUT_DIR = "covers"  # hardcoded output folder
ANIME_DB = "anime.db"  # hardcoded: DB with the `anime` table
NB_DB = "no_bover.db"  # hardcoded: DB with the `no_bover` table

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://myanimelist.net/",
}

print_lock = threading.Lock()


def table_exists(conn, schema, name):
    row = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def load_targets(anime_db, nb_db, only_missing_cover):
    """Return list of (mal_id, title, poster_url) for ids present in no_bover."""
    conn = sqlite3.connect(anime_db)
    conn.row_factory = sqlite3.Row

    nb_schema = "main"
    if nb_db and os.path.abspath(nb_db) != os.path.abspath(anime_db):
        conn.execute("ATTACH DATABASE ? AS nb", (nb_db,))
        nb_schema = "nb"

    if not table_exists(conn, nb_schema, "no_bover"):
        sys.exit(f"Table 'no_bover' not found in {nb_db or anime_db}")
    if not table_exists(conn, "main", "anime"):
        sys.exit(f"Table 'anime' not found in {anime_db}")

    sql = f"""
        SELECT a.mal_id, a.title, a.poster_url
        FROM {nb_schema}.no_bover n
        JOIN main.anime a ON a.mal_id = n.mal_id
        WHERE a.poster_url IS NOT NULL AND TRIM(a.poster_url) <> ''
    """
    if only_missing_cover:
        sql += " AND n.has_cover = 0"
    sql += " ORDER BY a.mal_id"

    rows = [(r["mal_id"], r["title"], r["poster_url"]) for r in conn.execute(sql)]

    # report ids in no_bover that have no usable poster_url
    total_nb = conn.execute(f"SELECT COUNT(*) FROM {nb_schema}.no_bover").fetchone()[0]
    conn.close()
    return rows, total_nb


def ext_from_url(url):
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return ext if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"


def download_one(session, mal_id, url, out_dir, retries, timeout):
    path = os.path.join(out_dir, f"{mal_id}{ext_from_url(url)}")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return mal_id, "skipped", path

    tmp = path + ".part"
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=timeout, stream=True)
            if r.status_code == 200:
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(65536):
                        if chunk:
                            f.write(chunk)
                if os.path.getsize(tmp) == 0:
                    last_err = "empty file"
                else:
                    os.replace(tmp, path)
                    return mal_id, "ok", path
            elif r.status_code in (404, 403):
                return mal_id, f"failed (HTTP {r.status_code})", url
            else:
                last_err = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(1.5 * attempt)

    if os.path.exists(tmp):
        os.remove(tmp)
    return mal_id, f"failed ({last_err})", url


def main():
    ap = argparse.ArgumentParser(description="Download covers for ids in no_bover")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.0, help="delay between task submissions (s)")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--only-missing-cover", action="store_true",
                    help="only rows where no_bover.has_cover = 0")
    args = ap.parse_args()

    if not os.path.exists(ANIME_DB):
        sys.exit(f"anime db not found: {ANIME_DB}")
    nb_db = NB_DB if os.path.exists(NB_DB) else None
    if nb_db is None:
        print(f"[info] {NB_DB} not found, assuming no_bover is inside {ANIME_DB}")

    targets, total_nb = load_targets(ANIME_DB, nb_db, args.only_missing_cover)
    os.makedirs(OUT_DIR, exist_ok=True)

    print(f"no_bover rows: {total_nb} | with poster_url to download: {len(targets)}")
    print(f"Saving to: {os.path.abspath(OUT_DIR)}\n")

    stats = {"ok": 0, "skipped": 0, "failed": 0}
    failed = []
    done = 0

    session = requests.Session()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {}
        for mal_id, title, url in targets:
            fut = pool.submit(download_one, session, mal_id, url, OUT_DIR,
                              args.retries, args.timeout)
            futures[fut] = (mal_id, title)
            if args.delay:
                time.sleep(args.delay)

        for fut in as_completed(futures):
            mal_id, title = futures[fut]
            _, status, info = fut.result()
            done += 1
            key = "failed" if status.startswith("failed") else status
            stats[key] += 1
            if key == "failed":
                failed.append((mal_id, title, status, info))
            with print_lock:
                print(f"[{done}/{len(targets)}] {mal_id} {status} - {title}")

    print(f"\nDone. downloaded={stats['ok']} skipped={stats['skipped']} failed={stats['failed']}")
    if failed:
        log = os.path.join(OUT_DIR, "failed.txt")
        with open(log, "w", encoding="utf-8") as f:
            for mal_id, title, status, info in failed:
                f.write(f"{mal_id}\t{status}\t{info}\t{title}\n")
        print(f"Failed list saved to {log} (re-run the script to retry them)")


if __name__ == "__main__":
    main()