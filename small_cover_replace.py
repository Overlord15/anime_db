#!/usr/bin/env python3
"""
recheck_small.py - find small cover (or banner) images and try to replace
them with a better one from AniList, searching by the anime's NAME.

Layout expected (same as kitsu.py):
    covers/<mal_id>.<ext>
    banners/<mal_id>.<ext>
    anime.db   (table: anime)   -> names for each mal_id
    bover.db   (table: bover)   -> fallback name, and updated after replacing

Usage:
    python recheck_small.py                 # covers under 45 KB
    python recheck_small.py --dry-run       # only list what would be checked
    python recheck_small.py --kind banner   # same, for the banners folder
    python recheck_small.py --min-kb 60     # change the size limit
    python recheck_small.py --retry         # re-check ones already finished
"""
import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
import time

import requests

try:
    from colorama import init as _cinit
    _cinit()
except ImportError:
    pass

ANILIST = "https://graphql.anilist.co"
QUERY = """
query ($search: String) {
  Page(perPage: 10) {
    media(search: $search, type: ANIME) {
      id idMal
      title { romaji english native }
      synonyms
      startDate { year }
      coverImage { extraLarge large }
      bannerImage
    }
  }
}
"""
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
G, Y, R, C, X = "\033[92m", "\033[93m", "\033[91m", "\033[96m", "\033[0m"

_last_call = 0.0


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# ---------------------------------------------------------------- database
def load_names(anime_db, bover_db):
    """mal_id -> {'names': [...], 'year': int|None}"""
    out = {}
    if os.path.exists(anime_db):
        con = sqlite3.connect(anime_db)
        con.row_factory = sqlite3.Row
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(anime)")}
            for row in con.execute("SELECT * FROM anime"):
                d = dict(row)
                names = []
                for c in ("title_english", "title", "title_japanese"):
                    if c in cols and d.get(c):
                        names.append(str(d[c]).strip())
                if "synonyms" in cols and d.get("synonyms"):
                    names += [s.strip() for s in str(d["synonyms"]).split(",") if s.strip()]
                year = None
                if "aired_from" in cols and d.get("aired_from"):
                    m = re.match(r"(\d{4})", str(d["aired_from"]))
                    year = int(m.group(1)) if m else None
                out[int(d["mal_id"])] = {"names": names, "year": year}
        except sqlite3.Error as e:
            print(f"{Y}Could not read {anime_db}: {e}{X}")
        con.close()
    if os.path.exists(bover_db):
        con = sqlite3.connect(bover_db)
        try:
            for mid, name in con.execute("SELECT mal_id, name FROM bover"):
                if name:
                    e = out.setdefault(int(mid), {"names": [], "year": None})
                    if name not in e["names"]:
                        e["names"].append(name)
        except sqlite3.Error:
            pass
        con.close()
    return out


def update_bover(bover_db, mal_id, kind, filename):
    if not os.path.exists(bover_db):
        return
    try:
        con = sqlite3.connect(bover_db)
        con.execute(
            f"UPDATE bover SET {kind}_source='anilist', {kind}_file=? WHERE mal_id=?",
            (filename, mal_id),
        )
        con.commit()
        con.close()
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------- anilist
def anilist_search(name, delay):
    """Return list of media dicts. Raises requests.RequestException on network failure."""
    global _last_call
    for _ in range(5):
        wait = delay - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        r = requests.post(ANILIST, json={"query": QUERY, "variables": {"search": name}}, timeout=30)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "30")) + 1)
            continue
        if r.status_code >= 500:
            time.sleep(5)
            continue
        r.raise_for_status()
        return (r.json().get("data") or {}).get("Page", {}).get("media") or []
    return []


def score(media, names):
    titles = [media["title"].get(k) for k in ("romaji", "english", "native")]
    titles += media.get("synonyms") or []
    tn = [norm(t) for t in titles if t]
    best = 0.0
    for n in names:
        n = norm(n)
        if not n:
            continue
        for t in tn:
            best = max(best, difflib.SequenceMatcher(None, n, t).ratio())
    return best


def pick(results, mal_id, names, year, threshold=0.85):
    # 1) exact MAL id match is always trusted
    for m in results:
        if m.get("idMal") == mal_id:
            return m
    # 2) fuzzy name match, but never a result that belongs to another MAL entry
    best, best_s = None, 0.0
    for m in results:
        if m.get("idMal") not in (None, mal_id):
            continue
        y = (m.get("startDate") or {}).get("year")
        if year and y and abs(year - y) > 1:
            continue
        s = score(m, names)
        if s > best_s:
            best, best_s = m, s
    return best if best_s >= threshold else None


# ---------------------------------------------------------------- files
def ext_from_url(url):
    ext = os.path.splitext(url.split("?")[0])[1].lower()
    return ext if ext in IMG_EXT else ".jpg"


def download(url, dest):
    part = dest + ".part"
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
        return part
    except Exception:
        if os.path.exists(part):
            os.remove(part)
        raise


def load_done(path):
    done = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    j = json.loads(line)
                    done[int(j["mal_id"])] = j["status"]
                except Exception:
                    pass
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["cover", "banner"], default="cover")
    ap.add_argument("--min-kb", type=float, default=45, help="re-check files smaller than this (default 45)")
    ap.add_argument("--anime-db", default="anime.db")
    ap.add_argument("--bover-db", default="bover.db")
    ap.add_argument("--progress", default=None, help="default: recheck_<kind>_progress.jsonl")
    ap.add_argument("--delay", type=float, default=0.7, help="seconds between AniList calls")
    ap.add_argument("--retry", action="store_true", help="re-check ones already finished")
    ap.add_argument("--dry-run", action="store_true", help="list small files only, no network")
    args = ap.parse_args()

    folder = "covers" if args.kind == "cover" else "banners"
    progress = args.progress or f"recheck_{args.kind}_progress.jsonl"
    limit = int(args.min_kb * 1024)

    if not os.path.isdir(folder):
        sys.exit(f"{R}Folder '{folder}' not found. Run this from the project folder.{X}")

    small = []
    for fn in os.listdir(folder):
        stem, ext = os.path.splitext(fn)
        if ext.lower() not in IMG_EXT or not stem.isdigit():
            continue
        size = os.path.getsize(os.path.join(folder, fn))
        if size < limit:
            small.append((int(stem), fn, size))
    small.sort()

    print(f"{C}{len(small)} {args.kind} file(s) under {args.min_kb:g} KB in {folder}/{X}")
    if args.dry_run:
        for mid, fn, size in small:
            print(f"  {fn}  {size/1024:.1f} KB")
        return

    done = {} if args.retry else load_done(progress)
    todo = [t for t in small if t[0] not in done]
    print(f"{len(small) - len(todo)} already finished, {len(todo)} to check\n")
    if not todo:
        return

    info = load_names(args.anime_db, args.bover_db)
    counts = {"replaced": 0, "no_better": 0, "no_match": 0, "no_name": 0}
    net_errors = 0
    start = time.time()

    with open(progress, "a", encoding="utf-8") as plog:
        for i, (mid, fn, old_size) in enumerate(todo, 1):
            entry = info.get(mid)
            status, new_size, label = None, None, ""

            if not entry or not entry["names"]:
                status, label = "no_name", f"{Y}no name found{X}"
            else:
                try:
                    media = None
                    tried = set()
                    for name in entry["names"]:
                        if norm(name) in tried or len(tried) >= 3:
                            continue
                        tried.add(norm(name))
                        media = pick(anilist_search(name, args.delay), mid, entry["names"], entry["year"])
                        if media:
                            break
                    net_errors = 0

                    if not media:
                        status, label = "no_match", f"{Y}no AniList match{X}"
                    else:
                        if args.kind == "cover":
                            ci = media.get("coverImage") or {}
                            url = ci.get("extraLarge") or ci.get("large")
                        else:
                            url = media.get("bannerImage")
                        if not url:
                            status, label = "no_better", f"{Y}AniList has no {args.kind}{X}"
                        else:
                            new_ext = ext_from_url(url)
                            dest = os.path.join(folder, f"{mid}{new_ext}")
                            part = download(url, dest)
                            new_size = os.path.getsize(part)
                            if new_size > old_size:
                                os.replace(part, dest)
                                if dest != os.path.join(folder, fn):
                                    os.remove(os.path.join(folder, fn))
                                update_bover(args.bover_db, mid, args.kind, os.path.basename(dest))
                                status = "replaced"
                                label = f"{G}replaced {old_size/1024:.0f} -> {new_size/1024:.0f} KB{X}"
                            else:
                                os.remove(part)
                                status = "no_better"
                                label = f"{Y}AniList not bigger ({new_size/1024:.0f} KB){X}"
                except requests.RequestException as e:
                    net_errors += 1
                    print(f"[{i}/{len(todo)}] {mid} {R}network error: {e}{X}")
                    if net_errors >= 5:
                        print(f"{R}5 network errors in a row - stopping. Run again later to continue.{X}")
                        break
                    continue  # not recorded, so it is retried next run

            counts[status] += 1
            plog.write(json.dumps({"mal_id": mid, "status": status,
                                   "old_size": old_size, "new_size": new_size}) + "\n")
            plog.flush()
            elapsed = time.time() - start
            eta = elapsed / i * (len(todo) - i)
            print(f"[{i}/{len(todo)}] {mid}  {label}   (ETA {int(eta//60)}m{int(eta%60):02d}s)")

    print(f"\n{G}replaced {counts['replaced']}{X} | no_better {counts['no_better']} | "
          f"no_match {counts['no_match']} | no_name {counts['no_name']}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{Y}Stopped. Run again to continue.{X}")