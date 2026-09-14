# Data pipeline

Two ways to get lead data into `leads_app/leads.duckdb`:

- **`update.py`** (normal use) checks online for a newer release of a source, downloads it, extracts USA contacts and **merges** it into an existing database. Nothing is duplicated and Used Data marks are kept. This is what the app's **Updates** tab runs.
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

Close the app first, or let the updater wait for it; the database is opened exclusively during the merge. Downloads resume if interrupted, so re-running the same command after a failure continues where it stopped. A merge is atomic: if anything fails, the database is left untouched.

### Where releases come from

| Source | Published at | Version looks like |
|---|---|---|
| Foursquare OS Places | Hugging Face dataset `foursquare/fsq-os-places`, folders `release/dt=…` (gated, needs a token) | `2026-08-11` |
| Overture Maps Places | Public S3 bucket `overturemaps-us-west-2`, folders `release/…` | `2026-08-19.0` |
| OpenStreetMap | Geofabrik `us-latest.osm.pbf`, versioned by its `Last-Modified` date | `2026-09-13` |

### What merging does

Rows are matched on `(source, source_id)`:

- **new** businesses are inserted,
- businesses whose contact details changed are **updated in place**, keeping `used_at`,
- unchanged businesses are left alone,
- any new or changed row whose phone or email is already in Used Data is **marked used**,
- the industry tree and the state/city lists are rebuilt, and the installed version is recorded in `meta`.

The run is written to History as kind `update` with the counts.

## Building a database from scratch

| Step | Command | Needs | Writes to `data/` |
|---|---|---|---|
| 1 | `python pipeline/fsq_usa_to_csv.py` | Hugging Face token in `.env` (see [Secrets](#secrets)) after accepting the dataset terms | `foursquare/<version>/` (~12 GB), `foursquare_usa_contacts.parquet` |
| 2 | `python pipeline/build_geo_lookup.py` | step 1 | `geo_lookup.duckdb` (ZIP prefix and lat/lon cell to state, fills gaps in the other two sources) |
| 3 | `python pipeline/overture_usa.py` | Overture place parquet files in `data/overture_data/` | `overture_usa_contacts.parquet` |
| 4 | `python pipeline/osm_usa.py` | `.osm.pbf` extracts in `data/osm_data/` | `osm_usa_contacts.parquet` |
| 5 | `python leads_app/build_db.py` | any of the outputs above | `leads_app/leads.duckdb` |

Steps 3 and 4 can instead be done with `update.py --update <source>`, which downloads the input for you.

## Modules

| File | Purpose |
|---|---|
| `sources.py` | Where each source publishes releases, how to find the newest one, resumable download |
| `extract.py` | The three extractors that turn a raw release into the common 19-column contact parquet |
| `update.py` | The command-line updater: check, download, extract, merge, relaunch |
| `../leads_app/merge.py` | The merge itself (insert / update / mark used / rebuild / record version) |
| `fsq_usa_to_csv.py`, `overture_usa.py`, `osm_usa.py`, `build_geo_lookup.py` | Thin wrappers for building from scratch |

## Common layout

Each extractor emits the same columns so they can be unioned: `source_id, business_name, phone, website, email, address, city, state, zip, country, latitude, longitude, categories, instagram, twitter, facebook_id, date_created, date_refreshed, date_closed`. Categories are `Industry > Group > Detail` strings joined by ` | `, which is what the app's industry tree is built from.

Within a source, phones (digits only) and emails (lowercased) are deduplicated, keeping the most complete, still-open, most recently refreshed row; phones with fewer than 7 digits are dropped. Loading applies the app-wide rules: 10-digit phones, lowercase emails, normalized state codes.

Steps that read `.osm.pbf` or Overture geometry need DuckDB's `spatial` extension, downloaded automatically on first use.
