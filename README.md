# simkl_trakt_zip_import

Import your [Trakt](https://trakt.tv) watch history, ratings, and watchlist into
[Simkl](https://simkl.com) using the Simkl API — no Trakt account connection required on
the Simkl side, and no third-party service touches your data. It talks directly from your
computer to `api.simkl.com`.

Give it the `.zip` file Trakt gives you when you export your data (or an already-unzipped
folder), and it takes care of the rest.

## What it imports

- **Watched history** — movies and episodes, with your original watch timestamps preserved
- **Ratings** — movies, shows, and episodes
- **Watchlist** — mapped to Simkl's "Plan to Watch" status

## What it can't import (Simkl API limitations, not bugs here)

- **Custom lists** (any named list you made on Trakt) — Simkl hasn't shipped an API for
  writing to Custom Lists yet. You'll need to recreate these by hand on simkl.com.
- **Collection** (owned copies) — Simkl has no equivalent concept.

## Requirements

- Python 3.7+
- No third-party packages — everything here uses the Python standard library.

## 1. Get your Trakt export

On [trakt.tv](https://trakt.tv), go to **Settings → Data → Export** and download the
`.zip` file. Keep it zipped — this script can read it directly.

## 2. Get a free Simkl API client_id

Go to [simkl.com/settings/developer](https://simkl.com/settings/developer/) and create a
new app (instant, no approval needed). Copy the `client_id` it gives you — you don't need
the `client_secret` for this script. A `client_id` is a public identifier, not a secret,
so it's fine to keep it in your shell history or a config file.

## 3. Run it

```bash
git clone https://github.com/YOUR_USERNAME/simkl_trakt_zip_import.git
cd simkl_trakt_zip_import
python3 trakt2simkl.py
```

Run it with no flags and it will prompt you for the path to your Trakt export and your
Simkl `client_id`. Or pass them directly:

```bash
python3 trakt2simkl.py --export-dir ~/Downloads/trakt-export.zip --client-id YOUR_CLIENT_ID
```

The first time you run it, it prints a short code and a link to `simkl.com/pin`. Open the
link on any device, enter the code, approve access, and the script picks up automatically.
The resulting token is cached (default: `~/.trakt2simkl/token.json`) so you won't have to
re-authorize on later runs.

### Want to check what will be imported before it writes anything?

```bash
python3 trakt2simkl.py --export-dir ~/Downloads/trakt-export.zip --dry-run
```

`--dry-run` parses your export and prints counts only — no `client_id` needed, no API
calls made.

### Other flags

| Flag | What it does |
|---|---|
| `--dry-run` | Parse and print counts only, no writes |
| `--skip-history` | Don't import watched history |
| `--skip-ratings` | Don't import ratings |
| `--skip-watchlist` | Don't import the watchlist |
| `--token-file PATH` | Where to cache the access token (default `~/.trakt2simkl/token.json`) |
| `--client-id ID` | Simkl `client_id` (or set `SIMKL_CLIENT_ID` env var) |
| `--version` | Print the version and exit |

## Notes on how it works

- **Batched writes.** Everything is sent in batches (movies, shows/episodes, ratings) to
  stay well under Simkl's rate limits (1 POST/sec per client_id).
- **Safe to re-run.** Simkl's write endpoints are idempotent for normal use — re-sending
  an already-watched movie or episode is a no-op, not a duplicate. If a run gets
  interrupted, just run it again.
- **ID matching.** Each item is sent with every identifier available in the Trakt export
  (IMDB, TMDB, TVDB, and the Trakt slug) plus title/year, so Simkl can match it even
  without a Simkl ID.
- **Watchlist quirk.** Simkl's own docs show `POST /sync/add-to-list` taking a top-level
  `to` field — the live API actually requires `to` on each item instead, and rejects the
  documented shape with `400 empty_field`. This script uses the working (per-item) shape.

## Privacy

Your access token is cached locally on your machine only (`~/.trakt2simkl/token.json` by
default). It's never sent anywhere except in requests to `api.simkl.com`. Don't commit
that file — it's already covered by `.gitignore`.

## License

MIT — see [LICENSE](LICENSE).
