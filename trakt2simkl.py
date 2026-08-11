#!/usr/bin/env python3
"""
trakt2simkl — Import a Trakt data export into Simkl via the Simkl API.

What it imports (everything the Simkl API currently supports):
  - Watched history (movies + episodes), with original watched_at timestamps
  - Ratings (movies, shows, episodes)
  - Watchlist -> Simkl's "Plan to Watch" status

What it CANNOT import (Simkl API limitation, not a bug in this script):
  - Trakt custom lists (user-created named lists). Simkl's Custom Lists are not yet
    exposed via the API. You'll need to recreate these by hand on simkl.com.
  - Trakt "collection" (owned copies) — Simkl has no equivalent concept.

Requires only the Python standard library — no pip installs needed.

USAGE
-----
    python3 trakt2simkl.py --export-dir /path/to/trakt-export.zip --client-id YOUR_CLIENT_ID

--export-dir accepts either the raw .zip Trakt gives you (Settings -> Data -> Export,
on trakt.tv) or an already-unzipped folder. Both flags are optional -- if omitted, the
script will prompt for them interactively.

Get a client_id (free, instant, no approval) at: https://simkl.com/settings/developer/

First run: the script prints a short code and a URL (simkl.com/pin). Open the URL on your
phone or computer, enter the code, approve, and the script continues automatically. The
access token is then cached (see --token-file) so future runs skip re-authorization.

Add --dry-run to parse the export and print a summary WITHOUT calling the Simkl API or
requiring a client_id — useful to sanity check counts before actually importing anything.

Safe to re-run: Simkl's write endpoints are idempotent for normal (non-rewatch) use —
re-sending an already-watched movie/episode is a no-op, not a duplicate.
"""

import argparse
import glob
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

VERSION = "1.0.0"
APP_NAME = "trakt2simkl"
APP_VERSION = VERSION
API_BASE = "https://api.simkl.com"

# Simkl allows 1 POST/sec per client. Stay comfortably under that.
POST_DELAY_SECONDS = 1.2
MAX_RETRIES = 5


def default_token_file():
    config_dir = os.path.join(os.path.expanduser("~"), ".trakt2simkl")
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, "token.json")


# --------------------------------------------------------------------------
# Small HTTP helper (stdlib only)
# --------------------------------------------------------------------------

def http_request(method, url, headers=None, json_body=None, timeout=30):
    data = None
    headers = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return e.code, parsed
    except urllib.error.URLError as e:
        return 0, {"error": "connection_error", "error_description": str(e)}


def api_get(path, params, token=None):
    qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
    url = f"{API_BASE}{path}?{qs}"
    headers = {"User-Agent": f"{APP_NAME}/{APP_VERSION}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return http_request("GET", url, headers=headers)


def api_post(path, client_id, token, body, extra_qs=None):
    qs = {"client_id": client_id, "app-name": APP_NAME, "app-version": APP_VERSION}
    if extra_qs:
        qs.update(extra_qs)
    query = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in qs.items())
    url = f"{API_BASE}{path}?{query}"
    headers = {
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        "Authorization": f"Bearer {token}",
    }

    attempt = 0
    wait = 2
    while True:
        attempt += 1
        status, resp = http_request("POST", url, headers=headers, json_body=body)
        if status in (200, 201):
            return resp
        retryable = status in (0, 429, 500, 502, 503) or (
            status == 400 and resp.get("error") == "rate_limit"
        )
        if retryable and attempt < MAX_RETRIES:
            print(f"    [retry] {path} -> HTTP {status} ({resp}). Waiting {wait}s...")
            time.sleep(wait)
            wait = min(wait * 2, 60)
            continue
        print(f"    [FAILED] {path} -> HTTP {status}: {resp}")
        return resp
    # unreachable


# --------------------------------------------------------------------------
# Auth: PIN flow
# --------------------------------------------------------------------------

def get_access_token(client_id, token_file):
    if os.path.exists(token_file):
        with open(token_file) as f:
            saved = json.load(f)
        if saved.get("client_id") == client_id and saved.get("access_token"):
            print(f"Using cached Simkl access token from {token_file}")
            return saved["access_token"]

    print("No cached token found — starting Simkl PIN authorization...")
    status, resp = api_get(
        "/oauth/pin",
        {"client_id": client_id, "app-name": APP_NAME, "app-version": APP_VERSION},
    )
    if status != 200 or resp.get("result") != "OK":
        print(f"Failed to request a device code: HTTP {status} {resp}")
        sys.exit(1)

    user_code = resp["user_code"]
    verification_uri = resp.get("verification_uri", "https://simkl.com/pin")
    interval = resp.get("interval", 5)
    expires_in = resp.get("expires_in", 900)

    print()
    print("=" * 60)
    print(f"  1. Open:  {verification_uri}")
    print(f"  2. Enter code:  {user_code}")
    print("  3. Approve access for this app")
    print("=" * 60)
    print("Waiting for approval...")

    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        status, poll = api_get(
            "/oauth/pin/" + user_code,
            {"client_id": client_id, "app-name": APP_NAME, "app-version": APP_VERSION},
        )
        if poll.get("result") == "OK" and poll.get("access_token"):
            token = poll["access_token"]
            with open(token_file, "w") as f:
                json.dump({"client_id": client_id, "access_token": token}, f)
            print(f"Authorized! Token cached at {token_file} for future runs.\n")
            return token
        if "device_code" in poll:
            print("The code expired before it was approved. Re-run the script to try again.")
            sys.exit(1)
        # otherwise: still pending, keep polling
    print("Timed out waiting for approval. Re-run the script to try again.")
    sys.exit(1)


# --------------------------------------------------------------------------
# Parsing the Trakt export
# --------------------------------------------------------------------------

def load_json(export_dir, filename):
    path = os.path.join(export_dir, filename)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_glob(export_dir, pattern):
    items = []
    for path in sorted(glob.glob(os.path.join(export_dir, pattern))):
        with open(path, encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                continue
        if isinstance(data, list):
            items.extend(data)
    return items


def movie_ids(trakt_ids, title=None, year=None):
    ids = {}
    if trakt_ids.get("imdb"):
        ids["imdb"] = trakt_ids["imdb"]
    if trakt_ids.get("tmdb"):
        ids["tmdb"] = trakt_ids["tmdb"]
    if trakt_ids.get("slug"):
        ids["traktslug"] = trakt_ids["slug"]
    out = {"ids": ids}
    if title:
        out["title"] = title
    if year:
        out["year"] = year
    return out


def show_ids(trakt_ids, title=None, year=None):
    ids = {}
    if trakt_ids.get("imdb"):
        ids["imdb"] = trakt_ids["imdb"]
    if trakt_ids.get("tmdb"):
        ids["tmdb"] = trakt_ids["tmdb"]
    if trakt_ids.get("tvdb"):
        ids["tvdb"] = trakt_ids["tvdb"]
    if trakt_ids.get("slug"):
        ids["traktslug"] = trakt_ids["slug"]
    out = {"ids": ids}
    if title:
        out["title"] = title
    if year:
        out["year"] = year
    return out


def show_key(trakt_ids, title, year):
    # Best-effort stable key to dedupe/group a show across many watched-history files.
    return (
        trakt_ids.get("imdb")
        or trakt_ids.get("tmdb")
        or trakt_ids.get("tvdb")
        or trakt_ids.get("slug")
        or f"{title}|{year}"
    )


def movie_key(trakt_ids, title, year):
    return (
        trakt_ids.get("imdb")
        or trakt_ids.get("tmdb")
        or trakt_ids.get("slug")
        or f"{title}|{year}"
    )


def latest(a, b):
    # Trakt's export has one row per watch event, not just the most recent — a
    # rewatched episode/movie has multiple entries with different watched_at
    # values. Progress tracking on Simkl needs the most recent watch, so this
    # must keep the maximum, not the minimum (ISO 8601 timestamps compare
    # correctly as plain strings).
    if a is None:
        return b
    if b is None:
        return a
    return a if a > b else b


def parse_watched_history(export_dir):
    """Returns (movies: dict[key -> record], shows: dict[key -> record],
    episode_tvdb_ids: dict[tvdb_episode_id -> watched_at]).

    episode_tvdb_ids is a second, independent representation of the same episode
    watches, keyed by each episode's own TVDB id (present in Trakt's export) rather
    than by show + season/episode number. Some shows — anime especially (e.g. JoJo's
    Bizarre Adventure) — are split into seasons differently on Simkl than on
    Trakt/TMDB, so sending season/episode *numbers* silently fails to match anything
    past wherever the two numbering schemes diverge. Simkl's API also accepts
    matching an episode directly by its own TVDB id, which sidesteps that mismatch
    entirely — see markEpisodeAsWatched()'s TVDB-id fallback in this addon's own
    simkl.py for the confirmed working request shape this mirrors.
    """
    events = load_glob(export_dir, "watched-history-*.json")

    movies = {}  # key -> {ids_obj, watched_at}
    shows = {}  # key -> {ids_obj, seasons: {season_num: {ep_num: watched_at}}}
    episode_tvdb_ids = {}  # tvdb_episode_id -> watched_at

    for ev in events:
        watched_at = ev.get("watched_at")
        if ev.get("type") == "movie":
            m = ev["movie"]
            k = movie_key(m["ids"], m.get("title"), m.get("year"))
            rec = movies.setdefault(
                k, {"ids_obj": movie_ids(m["ids"], m.get("title"), m.get("year")), "watched_at": None}
            )
            rec["watched_at"] = latest(rec["watched_at"], watched_at)
        elif ev.get("type") == "episode":
            show = ev["show"]
            ep = ev["episode"]
            k = show_key(show["ids"], show.get("title"), show.get("year"))
            rec = shows.setdefault(
                k, {"ids_obj": show_ids(show["ids"], show.get("title"), show.get("year")), "seasons": {}}
            )
            season_num = ep.get("season")
            ep_num = ep.get("number")
            if season_num is not None and ep_num is not None:
                season_bucket = rec["seasons"].setdefault(season_num, {})
                season_bucket[ep_num] = latest(season_bucket.get(ep_num), watched_at)
            ep_tvdb = ep.get("ids", {}).get("tvdb")
            if ep_tvdb:
                episode_tvdb_ids[ep_tvdb] = latest(episode_tvdb_ids.get(ep_tvdb), watched_at)

    return movies, shows, episode_tvdb_ids


def build_history_episode_id_payloads(episode_tvdb_ids):
    return [{"ids": {"tvdb": tvdb_id}, "watched_at": watched_at} for tvdb_id, watched_at in episode_tvdb_ids.items()]


def parse_ratings(export_dir):
    """Returns (movie_ratings, show_ratings, episode_ratings) as lists of API-ready dicts."""
    movie_ratings = []
    for r in load_json(export_dir, "ratings-movies.json") or []:
        m = r["movie"]
        item = movie_ids(m["ids"], m.get("title"), m.get("year"))
        item["rating"] = r["rating"]
        movie_ratings.append(item)

    show_ratings = []
    for r in load_json(export_dir, "ratings-shows.json") or []:
        s = r["show"]
        item = show_ids(s["ids"], s.get("title"), s.get("year"))
        item["rating"] = r["rating"]
        show_ratings.append(item)

    episode_ratings = []
    for r in load_json(export_dir, "ratings-episodes.json") or []:
        s = r["show"]
        ep = r["episode"]
        item = show_ids(s["ids"], s.get("title"), s.get("year"))
        item["seasons"] = [
            {"number": ep["season"], "episodes": [{"number": ep["number"], "rating": r["rating"]}]}
        ]
        episode_ratings.append(item)

    return movie_ratings, show_ratings, episode_ratings


def parse_watchlist(export_dir):
    """Returns (movies, shows, skipped) to move to plantowatch.

    Episode-type entries map to their parent show (Simkl's watchlist has no
    per-episode status).
    """
    entries = load_json(export_dir, "lists-watchlist.json") or []
    movies = {}
    shows = {}
    skipped = 0
    for e in entries:
        t = e.get("type")
        if t == "movie":
            m = e["movie"]
            k = movie_key(m["ids"], m.get("title"), m.get("year"))
            movies[k] = movie_ids(m["ids"], m.get("title"), m.get("year"))
        elif t == "show":
            s = e["show"]
            k = show_key(s["ids"], s.get("title"), s.get("year"))
            shows[k] = show_ids(s["ids"], s.get("title"), s.get("year"))
        elif t == "episode" and "show" in e:
            s = e["show"]
            k = show_key(s["ids"], s.get("title"), s.get("year"))
            shows[k] = show_ids(s["ids"], s.get("title"), s.get("year"))
        else:
            skipped += 1
    return list(movies.values()), list(shows.values()), skipped


# --------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------

def chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def build_history_show_payload(rec):
    out = dict(rec["ids_obj"])
    seasons = []
    for season_num, eps in sorted(rec["seasons"].items()):
        episodes = [
            {"number": ep_num, "watched_at": watched_at}
            for ep_num, watched_at in sorted(eps.items())
        ]
        seasons.append({"number": season_num, "episodes": episodes})
    out["seasons"] = seasons
    return out


def build_history_movie_payload(rec):
    out = dict(rec["ids_obj"])
    if rec["watched_at"]:
        out["watched_at"] = rec["watched_at"]
    return out


def report_not_found(resp):
    # Simkl's /sync/history response mirrors Trakt's own sync-response shape: items it
    # couldn't match get listed under "not_found" instead of raising an error for the
    # whole batch, so a failed match is otherwise completely silent. Print whatever
    # Simkl gives back for them (title if it echoed one, otherwise the ids we sent) so a
    # show that silently failed to import (wrong/missing ids, no Simkl match, etc.) is
    # actually visible instead of just missing later with no explanation.
    try:
        not_found = (resp or {}).get("not_found") or {}
        for bucket, items in not_found.items():
            if not items:
                continue
            print(f"    [Simkl couldn't match {len(items)} {bucket}]:")
            for item in items:
                ids = item.get("ids", item)
                label = item.get("title") or ids
                print(f"      - {label}")
    except Exception as e:
        print(f"    (couldn't parse not_found from response: {e})")


# --------------------------------------------------------------------------
# Interactive prompts (used when a required value isn't passed as a flag)
# --------------------------------------------------------------------------

def prompt_export_dir():
    while True:
        path = input("Path to your Trakt export folder or .zip file: ").strip().strip('"')
        path = os.path.expanduser(path)
        if os.path.isdir(path) or (os.path.isfile(path) and path.lower().endswith(".zip")):
            return path
        print(f"  '{path}' is not a folder or a .zip file. Try again.")


def resolve_export_dir(path):
    """Accepts a folder OR the raw .zip Trakt gives you, and returns a folder
    containing the exported .json files. Handles zips that extract into a
    nested subfolder instead of dumping files at the top level."""
    path = os.path.expanduser(path)

    if os.path.isfile(path) and path.lower().endswith(".zip"):
        extract_dir = tempfile.mkdtemp(prefix="trakt_export_")
        with zipfile.ZipFile(path) as zf:
            zf.extractall(extract_dir)
        print(f"Extracted {path} -> {extract_dir}")
        path = extract_dir

    if not os.path.isdir(path):
        print(f"'{path}' is not a folder or a .zip file.")
        sys.exit(1)

    marker_files = {"watched-history-1.json", "user-profile.json", "ratings-movies.json", "lists-watchlist.json"}
    for root, dirs, files in os.walk(path):
        if marker_files & set(files):
            return root
        depth = root[len(path):].count(os.sep)
        if depth >= 3:
            dirs[:] = []  # don't descend indefinitely into unrelated folders
    return path


def prompt_client_id():
    print()
    print("A Simkl client_id is required. Get one free at:")
    print("  https://simkl.com/settings/developer/")
    print("(client_id is a public identifier, not a secret.)")
    while True:
        client_id = input("Simkl client_id: ").strip()
        if client_id:
            return client_id


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="trakt2simkl",
        description="Import a Trakt data export into Simkl via the Simkl API.",
    )
    ap.add_argument(
        "--export-dir",
        help="Path to your Trakt export -- a folder of .json files, or the raw .zip "
        "(prompted if omitted)",
    )
    ap.add_argument(
        "--client-id",
        default=os.environ.get("SIMKL_CLIENT_ID"),
        help="Simkl API client_id (or set SIMKL_CLIENT_ID env var; prompted if omitted)",
    )
    ap.add_argument(
        "--token-file",
        default=None,
        help="Where to cache the Simkl access token (default: ~/.trakt2simkl/token.json)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Parse and print counts only, no API calls, no client-id needed")
    ap.add_argument("--skip-history", action="store_true", help="Don't import watched history")
    ap.add_argument("--skip-ratings", action="store_true", help="Don't import ratings")
    ap.add_argument("--skip-watchlist", action="store_true", help="Don't import the watchlist")
    ap.add_argument("--version", action="version", version=f"trakt2simkl {VERSION}")
    args = ap.parse_args()

    export_dir = resolve_export_dir(args.export_dir or prompt_export_dir())

    print("Parsing Trakt export...")
    movies, shows, episode_tvdb_ids = parse_watched_history(export_dir)
    movie_ratings, show_ratings, episode_ratings = parse_ratings(export_dir)
    wl_movies, wl_shows, wl_skipped = parse_watchlist(export_dir)

    total_episodes = sum(len(eps) for rec in shows.values() for eps in rec["seasons"].values())

    print()
    print("Summary of what will be imported:")
    print(f"  Watched movies:        {len(movies)}")
    print(f"  Watched shows:         {len(shows)}  ({total_episodes} episode watch events)")
    print(f"  Episode TVDB-id corrections: {len(episode_tvdb_ids)}  (fixes season-numbering mismatches, e.g. some anime)")
    print(f"  Movie ratings:         {len(movie_ratings)}")
    print(f"  Show ratings:          {len(show_ratings)}")
    print(f"  Episode ratings:       {len(episode_ratings)}")
    print(f"  Watchlist -> movies:   {len(wl_movies)}")
    print(f"  Watchlist -> shows:    {len(wl_shows)}")
    if wl_skipped:
        print(f"  Watchlist entries skipped (no usable parent): {wl_skipped}")
    print()
    print("NOT imported (unsupported by Simkl API): custom lists, collection/owned items.")
    print()

    if args.dry_run:
        print("Dry run only — no API calls made.")
        return

    client_id = args.client_id or prompt_client_id()
    token_file = args.token_file or default_token_file()
    token = get_access_token(client_id, token_file)

    # ---- Watched history ----
    if not args.skip_history:
        movie_payloads = [build_history_movie_payload(r) for r in movies.values()]
        show_payloads = [build_history_show_payload(r) for r in shows.values()]

        print(f"Sending {len(movie_payloads)} watched movies...")
        for batch in chunk(movie_payloads, 250):
            resp = api_post("/sync/history", client_id, token, {"movies": batch})
            report_not_found(resp)
            time.sleep(POST_DELAY_SECONDS)

        print(f"Sending {len(show_payloads)} shows with episode watch data...")
        # Group shows into batches capped by total episode count to keep payloads reasonable.
        batch = []
        batch_eps = 0
        for payload in show_payloads:
            ep_count = sum(len(s["episodes"]) for s in payload["seasons"])
            if batch and (batch_eps + ep_count > 1500 or len(batch) >= 25):
                resp = api_post("/sync/history", client_id, token, {"shows": batch})
                report_not_found(resp)
                time.sleep(POST_DELAY_SECONDS)
                batch, batch_eps = [], 0
            batch.append(payload)
            batch_eps += ep_count
        if batch:
            resp = api_post("/sync/history", client_id, token, {"shows": batch})
            report_not_found(resp)
            time.sleep(POST_DELAY_SECONDS)

        # Supplementary pass: re-send every watched episode matched by its own TVDB
        # episode id rather than show + season/episode number. The season-numbered
        # sends above cover the normal case; this corrects shows where Simkl's season
        # numbering diverges from Trakt/TMDB's (common for anime) and the numbered
        # send silently matched nothing past the divergence point. Safe/idempotent to
        # send for every episode, not just the mismatched ones, since Simkl no-ops an
        # already-recorded watch.
        episode_id_payloads = build_history_episode_id_payloads(episode_tvdb_ids)
        if episode_id_payloads:
            print(f"Sending {len(episode_id_payloads)} episode watches by TVDB episode id "
                  f"(corrects season-numbering mismatches)...")
            for batch in chunk(episode_id_payloads, 250):
                resp = api_post("/sync/history", client_id, token, {"episodes": batch})
                report_not_found(resp)
                time.sleep(POST_DELAY_SECONDS)

    # ---- Ratings ----
    if not args.skip_ratings:
        if movie_ratings or show_ratings:
            print(f"Sending {len(movie_ratings)} movie + {len(show_ratings)} show ratings...")
            body = {}
            if movie_ratings:
                body["movies"] = movie_ratings
            if show_ratings:
                body["shows"] = show_ratings
            api_post("/sync/ratings", client_id, token, body)
            time.sleep(POST_DELAY_SECONDS)

        if episode_ratings:
            print(f"Sending {len(episode_ratings)} episode ratings...")
            for batch in chunk(episode_ratings, 100):
                api_post("/sync/ratings", client_id, token, {"shows": batch})
                time.sleep(POST_DELAY_SECONDS)

    # ---- Watchlist -> plantowatch ----
    if not args.skip_watchlist and (wl_movies or wl_shows):
        print(f"Adding {len(wl_movies)} movies + {len(wl_shows)} shows to Plan to Watch...")
        # NOTE: `to` must be set PER ITEM, not as a top-level body field. Simkl's own
        # sync guide shows a top-level "to", but the live API rejects that shape with
        # `400 empty_field: Missed "to" parameter` -- the API reference confirms this
        # is a documentation bug inherited from the old Apiary spec.
        body = {}
        if wl_movies:
            body["movies"] = [dict(m, to="plantowatch") for m in wl_movies]
        if wl_shows:
            body["shows"] = [dict(s, to="plantowatch") for s in wl_shows]
        api_post("/sync/add-to-list", client_id, token, body)
        time.sleep(POST_DELAY_SECONDS)

    print()
    print("Done. Check https://simkl.com/dashboard/ to verify, and re-run this script anytime")
    print("(it's safe to re-run — already-imported items are no-ops).")


if __name__ == "__main__":
    main()
