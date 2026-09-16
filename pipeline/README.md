# Data pipeline

Two ways to get lead data into `leads_app/leads.duckdb`:

- **`update.py`** (normal use) checks online for a newer release of a source, downloads it, extracts USA contacts and **merges** it into an existing database. Nothing is duplicated, and Used Data and usage marks are never touched (only imports change them). This is what the app's **Updates** tab runs.
- **The individual scripts** below produce the parquet files that `leads_app/build_db.py` reads when building a database **from scratch**.

Everything is read from and written to `../data/` (git-ignored).

```bat
pip install -r pipeline/requirements.txt
```

## Secrets

Only Foursquare needs credentials. Copy `.env.example` to `.env` in the repository root and put your Hugging Face token in it:

```
HF_TOKEN=hf_your_token_here
```

`.env` is git-ignored, so it is never committed or pushed; `.env.example` is the committed template and must stay empty. Create a token with read scope at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) and accept the dataset terms at [the dataset page](https://huggingface.co/datasets/foursquare/fsq-os-places). `HF_TOKEN` set in the real environment takes precedence over the file, which is handy on a build server. Overture and OpenStreetMap need no credentials at all.

If you ever paste a token somewhere it does not belong, revoke it on the Hugging Face tokens page and create a new one.

## Keeping data current

| Command | What it does |
|---|---|
| `python pipeline/update.py --check` | Print installed vs newest available version for all three sources |
| `python pipeline/update.py --check --notify` | Same, plus a Windows popup if something is newer (used by the daily task) |
| `python pipeline/update.py --update Overture` | Ask, then download, extract and merge the newest Overture release |
| `python pipeline/update.py --update OpenStreetMap --yes` | Same without the confirmation prompt |
| `python pipeline/update.py --set-version Overture 2026-08-19.0` | Record an installed version without downloading anything |
| `pipeline\install_daily_check.bat` | Register a Windows task that checks daily at 09:30 and pops up when new data exists |

Close the app first, or let the updater wait for it; the database is opened exclusively during the merge. Downloads resume if interrupted, so re-running the same command after a failure continues where it stopped. The row changes, the installed version in `meta` and the History entry are committed together: if anything fails before that commit, the database is left untouched. The filter tables (industry tree, states, cities) are rebuilt after the commit and swapped in whole; if that rebuild fails, the update stays installed, the previous filter lists (with their old counts) stay in place until the next rebuild, and the updater prints a warning. Whenever a failure or Ctrl+C comes after the commit, or only the removal of the raw download fails, the updater says the update is already installed; do not install the same release again.

### Where releases come from

| Source | Published at | Version looks like |
|---|---|---|
| Foursquare OS Places | Hugging Face dataset `foursquare/fsq-os-places`, folders `release/dt=…` (gated, needs a token) | `2026-08-11` |
| Overture Maps Places | Public S3 bucket `overturemaps-us-west-2`, folders `release/…` | `2026-08-19.0` |
| OpenStreetMap | Geofabrik `us-latest.osm.pbf`, versioned by its `Last-Modified` date | `2026-09-13` |

### What merging does

Every row of the release is matched on `(source, source_id)`:

- businesses **already in the database** are updated in place when their contact details changed (keeping `used_at`) and left alone otherwise. They are never skipped and never counted as fresh.
- every other row is a **new candidate**, checked in this order:

  | Check | Result |
  |---|---|
  | no valid phone and no valid email | skipped: no contact |
  | phone or email already in All Leads (any source, including a phone or email an existing business drops in this release) | skipped: already in All Leads |
  | shares a phone or email with another new row of the same release | the best row per phone, then per email, is kept (open first, then the most filled-in of phone/email/website/address/city, then source id); a row is skipped as a duplicate only when a kept row holds its phone or email, otherwise it competes again |
  | anything left | **fresh**: inserted |

  So a new row is fresh only when neither its phone nor its email is already in All Leads. Merging the same release twice adds nothing the second time.

- Used Data is not read or changed and no lead is marked used; usage changes only through imports,
- the installed version is recorded in `meta`, and after the commit the industry tree and the state/city lists are rebuilt.

Skipped rows are not inserted, but nothing is lost: the extracted parquet stays in `data/<source>/<version>/`.

Each fresh row is stamped with `places.added_batch` = the id of the run's History entry (`NULL` = data from the original build, before tracking). The app uses it for the **Data batch** filter in All Leads, the **Added in** preview column, the `added_in` export column and the **Installed updates** list on the Updates tab. Databases built before this column existed get it added automatically (the app when it starts, the updater at the start of the merge).

The run is written to History as kind `update`: `rows` = fresh leads added, `rows_skipped` = old leads skipped, `places_marked` = 0 (updates mark nothing), and `filters_json` holds the full breakdown (`release_date`, `installed_at`, `total`, `existing`, `updated`, `unchanged`, `new_candidates`, `fresh`, `skipped_in_leads`, `skipped_duplicate`, `skipped_no_contact`, `skipped_total`). `update.py` prints the same summary at the end, for example:

```
Overture release 2026-09-17 (2026-09-17.0), installed 2026-09-20
  Fresh leads added (neither phone nor email seen before):       812,334
  Old leads skipped:                                           8,710,571
    phone or email already in All Leads:                       8,650,120
    duplicate phone/email inside this release:                    55,210
    no valid phone or email:                                       5,241
  Businesses already in the database (same source id): 2,104,551 (35,012 updated, 2,069,539 unchanged)
  Overture rows in All Leads: 11,954,321 -> 12,766,655
```

With `--relaunch` (how the Updates tab starts it) the summary stays on screen for about 8 seconds, or until Enter, before the app reopens.

## Building a database from scratch

| Step | Command | Needs | Writes to `data/` |
|---|---|---|---|
| 1 | `python pipeline/fsq_usa_to_csv.py` | Hugging Face token in `.env` (see [Secrets](#secrets)) after accepting the dataset terms | `foursquare/<version>/` (~12 GB), `foursquare_usa_contacts.parquet` |
| 2 | `python pipeline/build_geo_lookup.py` | step 1 | `geo_lookup.duckdb` (ZIP prefix and lat/lon cell to state, fills gaps in the other two sources) |
| 3 | `python pipeline/overture_usa.py` | Overture place parquet files in `data/overture_data/` | `overture_usa_contacts.parquet` |
| 4 | `python pipeline/osm_usa.py` | `.osm.pbf` extracts in `data/osm_data/` | `osm_usa_contacts.parquet` |
| 5 | `python leads_app/build_db.py` (add `--replace` to overwrite an existing database; back it up first) | any of the outputs above | `leads_app/leads.duckdb` |

Steps 3 and 4 can instead be done with `update.py --update <source>`, which downloads the input for you.

## Modules

| File | Purpose |
|---|---|
| `sources.py` | Where each source publishes releases, how to find the newest one, resumable download |
| `extract.py` | The three extractors that turn a raw release into the common 19-column contact parquet |
| `update.py` | The command-line updater: check, download, extract, merge, relaunch |
| `../leads_app/merge.py` | The merge itself (update existing / add fresh, skip old / record version / rebuild filters) |
| `fsq_usa_to_csv.py`, `overture_usa.py`, `osm_usa.py`, `build_geo_lookup.py` | Thin wrappers for building from scratch |

## Common layout

Each extractor emits the same columns so they can be unioned: `source_id, business_name, phone, website, email, address, city, state, zip, country, latitude, longitude, categories, instagram, twitter, facebook_id, date_created, date_refreshed, date_closed`. Categories are `Industry > Group > Detail` strings joined by ` | `, which is what the app's industry tree is built from.

Within a source, phones (digits only) and emails (lowercased) are deduplicated, keeping the most complete, still-open, most recently refreshed row; phones with fewer than 7 digits are dropped. Loading applies the app-wide rules: 10-digit phones, lowercase emails, normalized state codes.

Steps that read `.osm.pbf` or Overture geometry need DuckDB's `spatial` extension, downloaded automatically on first use.
