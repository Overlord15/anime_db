# anime_db

A local anime database (`anime.db`) plus a tool that fills it with **cover** and **banner** images from [Kitsu](https://kitsu.io) and [AniList](https://anilist.co). No API keys needed.

Every image is stored by its MyAnimeList ID, so it is easy to join with your database:

```
covers/<mal_id>.jpg
banners/<mal_id>.jpg
```

## Features

- Looks up every anime in `anime.db` on **Kitsu** first, then uses **AniList** only for whatever Kitsu is missing.
- Exact lookups through MAL ID mapping, with a name-based fallback (fuzzy title matching plus a year check).
- **All-or-nothing saving:** an anime is saved only when both a cover and a banner were found.
- **Resume anywhere:** stop with `Ctrl+C` (or lose power) and run again; finished anime are skipped.
- **Download Partials mode:** go back for anime that only had one of the two images and save what is available.
- Safe downloads: images are written to `.part` files and renamed only when complete, so there are no half-written images.
- Live single-line progress display with ETA and running totals (or one line per anime with `--scroll`).
- Automatic retries, rate-limit handling, and a clean stop when the network is down.

## Requirements

- Python 3.8+
- [`requests`](https://pypi.org/project/requests/)
- [`colorama`](https://pypi.org/project/colorama/) (optional, for colors on older Windows consoles)

```bash
pip install requests colorama
```

## Setup

```bash
git clone https://github.com/<your-username>/anime_db.git
cd anime_db
pip install requests colorama
```

Put your `anime.db` in the project folder. The script reads a table named `anime` with these columns:

| Column | Used for |
| --- | --- |
| `mal_id` | Primary key, exact lookups, and image file names |
| `title` | Name matching |
| `title_english` | Name matching |
| `title_japanese` | Name matching |
| `synonyms` | Comma-separated alternative names, name matching |
| `aired_from` | Start date (`YYYY-...`), used to reject same-name series from another year |

Only `mal_id` is required. Missing columns are simply skipped.

## Usage

```bash
python kitsu.py
```

You get a start menu:

```
1. Scrape / Resume
2. Download Partials (N waiting)
q. Quit
```

### 1. Scrape / Resume

The normal run. It goes through `anime.db`, skips everything already finished, and processes the rest. Run it again at any time to continue where you stopped.

### 2. Download Partials

Picks up the anime waiting in `no_bover.db` as `partial` (a cover and/or banner exists on Kitsu or AniList, but the pair was incomplete). It uses the Kitsu/AniList IDs stored in the first run to get the image URLs, downloads whatever is available, **inserts them into `bover.db`**, and removes them from `no_bover.db`. Anime whose links are dead stay in `no_bover.db`.

### Command-line options

| Option | Description |
| --- | --- |
| `--mode {scrape,partials}` | Skip the menu and run that mode directly |
| `--db PATH` | Source database (default: `anime.db`) |
| `--progress PATH` | Progress file (default: `progress.jsonl`) |
| `--delay SECONDS` | Pause between API calls (default: `1`) |
| `--retry-incomplete` | *(mode 1)* Try the `partial` / `no_match` anime again |
| `--names-only` | *(mode 1)* Skip exact MAL ID lookups, match by name only |
| `--scroll` | Print one line per anime instead of one refreshing line |

Examples:

```bash
python kitsu.py --mode scrape --scroll
python kitsu.py --mode partials
python kitsu.py --retry-incomplete
```

If the script runs without a keyboard (a scheduler or a pipe), it skips the menu and runs mode 1.

## Output

| Path | Contents |
| --- | --- |
| `covers/` | Cover images, named `<mal_id>.<ext>` |
| `banners/` | Banner images, named `<mal_id>.<ext>` |
| `bover.db` | Table `bover`: anime with images saved |
| `no_bover.db` | Table `no_bover`: anime that are `partial` or `no_match` |
| `progress.jsonl` | One JSON line per finished anime, used for resuming |

### `bover.db`

Anime with saved images. Rows with both `cover_file` and `banner_file` filled are complete; rows with one of them empty were saved through *Download Partials*.

| Column | Description |
| --- | --- |
| `mal_id` | MyAnimeList ID (primary key) |
| `name` | Title |
| `kitsu_id`, `anilist_id` | IDs on the matched sites |
| `cover_source`, `banner_source` | `kitsu`, `anilist` or `disk` |
| `cover_file`, `banner_file` | File names inside `covers/` and `banners/` |
| `saved_at` | UTC timestamp |

### `no_bover.db`

| Column | Description |
| --- | --- |
| `mal_id` | MyAnimeList ID (primary key) |
| `name` | Title |
| `status` | `partial` (one image found) or `no_match` (nothing found) |
| `has_cover`, `has_banner` | `1` if found |
| `cover_source`, `banner_source` | Where each image was found |
| `kitsu_id`, `anilist_id` | IDs on the matched sites |
| `checked_at` | UTC timestamp |

## How it works

1. **Kitsu:** exact match through Kitsu's MyAnimeList mapping; if that fails, a title search where the best result must score at least 0.85 similarity.
2. **AniList:** only if the cover or banner is still missing, queried by MAL ID, then by name (results with a different MAL ID are ignored so other seasons are not mixed up).
3. **Save:** only when both images were found are they downloaded. Images already on disk are reused and never downloaded twice.
4. **Record:** the result goes to `bover.db` or `no_bover.db`, then a line is appended to `progress.jsonl`.

## Notes

- Be polite to the APIs: the default delay is 1 second, and AniList is never queried faster than every 0.7 seconds (it allows about 90 requests per minute).
- After 5 network errors in a row the script stops by itself. Run it again later to continue.
- Anime that hit an error are not recorded, so they are retried on the next run.
- To start completely over, delete `progress.jsonl`, `bover.db`, `no_bover.db` and the `covers/` and `banners/` folders.

## License

Add your license here (for example MIT) and a `LICENSE` file to the repository.

## Credits

Image data comes from [Kitsu](https://kitsu.io) and [AniList](https://anilist.co). Anime IDs refer to [MyAnimeList](https://myanimelist.net). This project is not affiliated with any of them.