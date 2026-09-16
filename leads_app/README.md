# Leads Explorer

Desktop app (pywebview + DuckDB) with two stores in one database file, `leads.duckdb`:

| Store | Table | What it holds |
|---|---|---|
| All Leads | `places` | 25M USA businesses from Foursquare OS Places, Overture Maps and OpenStreetMap |
| Used Data | `used_contacts` | Contacts you have already used: the ContactDirectory database plus every file you import through Import & Map |

A lead is **used** when its phone or email matches a contact in Used Data. Only imports feed Used Data; downloading a CSV from All Leads never marks anything. Used leads are hidden from All Leads by default (Usage filter: Unused only / All / Used only).

## Run

Double-click **Leads Explorer.bat**, or `python app.py`. Requirements: Python 3.10+, `pip install duckdb pywebview`.
Secrets (currently only a Hugging Face token for Foursquare downloads) go in a git-ignored `.env` in the repository root; copy `.env.example` to start.
Excel import/export uses DuckDB's `excel` extension (downloaded automatically the first time; needs internet once).

## Tabs

**All Leads** - filter by usage, source, data batch, industry, country, state, city, name, ZIP, has phone/email/website/Facebook, open only. **Data batch** tells the original data (everything from before batch tracking) apart from the fresh leads each installed update added: tick one release (for example `Overture 2026-09-17`) to see only what that update brought in, tick several to compare, or tick Original. Before any tracked update is installed the section just says so. The preview has an **Added in** column and the CSV an `added_in` column (`original`, or `<source> release <date> installed <date>`). The **Industry** filter is a tree: tick a main industry (for example Retail) to take everything under it, or click ▸ to open it and tick specific sub-industries at any depth (Retail > Fashion Retail > Shoe Store). A ticked parent already covers its children. The search box finds sub-industries by name across all levels. Each source has its own taxonomy (security firms are Foursquare "Security and Safety", but Overture splits them into "Home security", "Security systems" and "Security service", and OSM uses "Office > Security"), so one tree item holds only part of an industry. **Industry keywords** fix that: comma-separated words matched as whole words (plurals included) against the categories of every source, and optionally business names, which also catches businesses whose source category is generic or missing. An exclude box drops false hits (for example `social security, bank, mortgage`). Ticked industries and keywords combine as either/or. Live counts and a 200-row preview. **Download CSV** exports every matching row and is logged in History. It does not change usage: when you have actually used a file, import it through Import & Map and the matching leads become used.

**Used Data** - search (name, email, phone, address, website, NPI, specialty) plus facets for source, source type, source category, industry, category, country, state, city and source file, all with counts that respond to the other filters. Download the filtered contacts as CSV or Excel with all 27 fields.

**Import & Map** - pick a CSV or XLSX (sheet selectable), choose the controlled metadata (Source, Industry, Source Type, Source Category are required; Category optional; type a new value and click Add to extend a catalog), review the suggested column mapping, preview, then import. **Duplicate rule (always on):** a row is skipped when both its phone and its email already exist in Used Data, or when it repeats an earlier row of the same file with the same phone and email. A row where only the phone or only the email matches is kept. The result shows how many rows were skipped and why. Options: a stricter mode that also skips rows where just the phone or just the email is already used; skip rows with neither phone nor email. After the import, every lead in All Leads with a matching phone or email is marked used. The whole original row is kept in `raw_data`.

**Filter File** - pick a CSV or XLSX, map its phone and/or email column, and create a clean CSV with every row already in Used Data removed. Phones in the output are rewritten to the canonical form. The database is not changed.

**History** - every import, export, filter run and data update with counts, metadata and a link to the output file.

**Updates** - checks online (once a day at startup, or on demand) whether Foursquare, Overture or OpenStreetMap published a newer release. The table shows, per source, the installed release date, when it was installed, the latest release date, how many days newer it is, and the download size. **Download & install** asks first (showing installed vs latest release), then opens the updater in its own console and closes the app so the database is free; it downloads, extracts, merges and starts the app again. The merge rule:

- A business already in All Leads (same source and source id) is updated in place when its details changed, otherwise left alone. It is never counted as fresh.
- Every other row is added only when it is **fresh**: its phone or email is not already in All Leads (any source) or Used Data. Rows with no valid phone or email, rows whose phone or email is already known, and repeats of the same phone or email inside the release are **skipped** and counted, not inserted.
- Fresh leads are stamped with the update (`places.added_batch`), so they show up as their own batch in All Leads. Used marks survive.

The updater console prints fresh added vs old skipped (with the reasons) before the app restarts, and the app shows a one-time notice for a newly installed batch. The **Installed updates** card lists every installed release: release date, installed on, fresh added, old skipped (split into already in All Leads / already in Used Data / repeated inside the release / no valid phone or email), existing businesses updated / unchanged, how many of its fresh leads are still unused, and **View in All Leads** to open just that batch. Updates installed before batch tracking show "not tracked" and their rows count as original data. See [pipeline/README.md](../pipeline/README.md) for the command-line equivalent and the daily background check.

## Normalization rules (applied everywhere: pipeline builds, imports, filter files, exports)

- **Phone**: digits only; a leading `1` is dropped from 11-digit numbers; anything that is not exactly 10 digits becomes blank. Stored in `phone` for both stores.
- **Email**: trimmed and lowercased; values without an `@` and a dot become blank.
- **State**: 2-letter codes uppercased; `D.C.` to `DC`; `US-TX` to `TX`; full US state and Canadian province names to codes. Unknown values are kept as typed.
- Blank strings are stored as NULL.

## Files

| File | Purpose |
|---|---|
| `Leads Explorer.bat` | Launcher. If `leads.duckdb` is missing, the app asks whether to create a new empty database so you can start from scratch (Used Data fills through Import & Map; All Leads stays empty until `build_db.py` is run with pipeline files). |
| `app.py` | Desktop app: native window plus every API the UI calls. `LEADS_DB` env var overrides the database path. |
| `ui.html` | The interface (five tabs). |
| `schema.py` | Shared normalization macros, v2 store DDL, and dim-table builder. |
| `build_db.py` | Fresh build of `leads.duckdb` from the pipeline outputs; Used Data starts empty. |
| `merge.py` | Merges a newly downloaded source release into an existing database (update existing businesses, insert only fresh leads stamped with the batch, count the skipped ones, mark used, rebuild filters, record the version). |
| `migrate_v2.py` | The one-time upgrade that was applied on 2026-09-14 (old `leads.duckdb` + ContactDirectory's `contacts.db` to the v2 layout). Kept for reference; it expects the SQLite file at `../ContactDirectory/data/contacts.db`. |
| `test_api.py` | GUI-free regression test on a sampled fixture (never writes to the real database). |
| `leads.duckdb` | The database (schema v2). This is the only copy of the data: the source files were removed after the build, so back it up rather than rebuild it. |

## Database layout (v2)

- `places` - lead columns plus `category_list`, `industry_list`, `is_open`, `used_at`, `used_reason` (`import:<id>`, or `match:legacy` for matches found during migration), `added_batch` (the `history.id` of the update that inserted the lead; NULL = original data). The app adds `added_batch` to an older database on start.
- `used_contacts` - `id` plus the 27 ContactDirectory fields, `raw_data`, `created_at`, `import_id` (links to `history`).
- `history` - `kind` is `import`, `export`, `filter` or `update`; `places_marked` says how many leads that action marked as used. For `update`, `rows` is fresh leads added, `rows_skipped` the skipped ones, and `filters_json` holds the full merge counts (release date, installed at, existing / updated / unchanged, fresh, and each skip reason).
- `meta` - `schema_version`, plus `source_version:<source>` and `source_updated_at:<source>` for each installed data release, the cached `update_check` result, and `ui_last_seen_batch` (the newest batch the app has already announced).
- `catalogs` - `(kind, name)` for source, industry, source_type, source_category, category.
- `dim_*` - per-source counts driving the All Leads filter lists. `meta` - `schema_version = 2`.

## Data pipeline

The scripts that produce the All Leads data live in [`../pipeline`](../pipeline/README.md) and write everything to `../data` (git-ignored). `build_db.py` reads its inputs from there. Close the app before running `build_db.py` or `migrate_v2.py`. A fresh `build_db.py` starts with an empty Used Data store; export Used Data first from the Used Data tab and import it again afterwards.

## Tests

```
python ../tests/test_api.py               # samples a fixture from leads.duckdb if present, else synthetic data
python ../tests/test_api.py --synthetic   # what CI runs
```
