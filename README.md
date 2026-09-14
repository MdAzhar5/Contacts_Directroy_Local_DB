# Leads-DB

A local, offline lead-management toolkit for USA business contacts:

- **Pipeline** scripts that turn three open datasets (Foursquare OS Places, Overture Maps, OpenStreetMap) into one normalized contact table of ~25M businesses.
- **Leads Explorer**, a Windows desktop app (pywebview + DuckDB) that filters and exports those leads, keeps a separate **Used Data** store of every contact already used, imports your own contact files with column mapping, and cleans new files against what you have already used.

Everything runs on one machine with a single DuckDB file. No server, no cloud.

## Repository layout

```
Leads-DB/
├── leads_app/                Desktop application
│   ├── app.py                Backend: DuckDB queries, imports, exports, filter file
│   ├── ui.html               Frontend: All Leads · Used Data · Import & Map · Filter File · History · Updates
│   ├── schema.py             Normalization macros (phone / email / state) and v2 table layout
│   ├── build_db.py           Fresh build of leads.duckdb from the pipeline outputs
│   ├── merge.py              Merge a newer source release without duplicating rows
│   ├── migrate_v2.py         One-time upgrade from the v1 layout + ContactDirectory SQLite
│   ├── Leads Explorer.bat    Double-click launcher
│   └── README.md             App documentation (tabs, rules, database layout)
├── pipeline/                 Source data: first build, and staying current
│   ├── update.py             Check online, download, extract, merge a newer release
│   ├── sources.py            Where releases are published; resumable downloads
│   ├── extract.py            The three extractors -> common contact parquet
│   ├── install_daily_check.bat   Registers a daily "new data?" Windows task
│   ├── fsq_usa_to_csv.py     Foursquare OS Places -> parquet
│   ├── build_geo_lookup.py   ZIP / lat-lon -> state lookup
│   ├── overture_usa.py       Overture Maps places -> parquet
│   ├── osm_usa.py            OpenStreetMap extracts -> parquet
│   └── README.md
├── tests/
│   ├── test_api.py           End-to-end API test on a sampled or synthetic fixture
│   ├── test_update.py        Release parsers, extractors and merge (offline)
│   └── test_secrets.py       .env handling and a secret scan of the whole git history
├── data/                     Large inputs/outputs (git-ignored)
├── .env.example              Template for the git-ignored .env that holds your secrets
├── requirements.txt          App dependencies
└── .github/workflows/ci.yml  Compile + synthetic test on every push
```

`leads_app/leads.duckdb` (the database) and everything under `data/` are git-ignored because they are multi-gigabyte. So is `.env`, which holds your secrets.

## Secrets

Credentials live in a git-ignored `.env` file in the repository root. Copy the committed template and fill it in:

```bash
cp .env.example .env
```

Only `HF_TOKEN` matters today, and only for downloading Foursquare data. Overture and OpenStreetMap are open and need nothing. A real `HF_TOKEN` environment variable overrides the file. `.env.example` is committed as documentation and must never contain a real value.

## Quick start

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cd leads_app
python app.py
```

On first launch with no database the app offers to create an empty one, so you can start from scratch and fill Used Data through **Import & Map**. To populate **All Leads**, either use the **Updates** tab to download a source release, or build from local pipeline outputs; see [pipeline/README.md](pipeline/README.md).

Data stays current through the **Updates** tab, which checks the three sources daily and installs a newer release on confirmation. From the command line:

```bash
python pipeline/update.py --check
```

## What the app does

| Tab | Purpose |
|---|---|
| All Leads | Filter 25M leads by usage, source, industry tree (main industry or specific sub-industries), location, name, ZIP and contact fields. Export CSV (logged in History; exporting alone never marks anything as used). |
| Used Data | Search and facet the contacts you have already used. Export CSV or Excel. |
| Import & Map | The only way leads become used: import CSV/XLSX with header mapping and controlled metadata. Duplicates (same phone **and** email) are skipped and counted. Leads matching an imported phone or email are marked used. |
| Filter File | Remove rows already in Used Data from a new file and normalize its phones. |
| History | Every import, export, filter run and data update. |
| Updates | Checks daily whether Foursquare, Overture or OpenStreetMap published a newer release, and installs it on confirmation like an app update: download, extract, merge without duplicates, keep Used Data marks. |

Full details, the normalization rules, and the database layout are in [leads_app/README.md](leads_app/README.md).

## Data sources and licenses

| Source | License |
|---|---|
| [Foursquare OS Places](https://huggingface.co/datasets/foursquare/fsq-os-places) | Apache 2.0 |
| [Overture Maps Places](https://overturemaps.org/) | CDLA Permissive 2.0 |
| [OpenStreetMap](https://www.openstreetmap.org/) | ODbL (share-alike) |

Every lead row carries a `source` column so attribution and license obligations can be honored per row.

## Tests

```
python tests/test_api.py               # samples a fixture from leads_app/leads.duckdb if present
python tests/test_api.py --synthetic   # generated fixture, no real data needed (this is what CI runs)
python tests/test_update.py            # release parsers, extractors and merge, all offline
python tests/test_update.py --online   # also asks the three sources for their newest release
python tests/test_secrets.py           # .env parsing, ignore rules, and a scan of every commit for tokens
```

## Requirements

Windows 10/11, Python 3.10+, `duckdb`, `pywebview`. Excel import/export uses DuckDB's `excel` extension, which DuckDB downloads once on first use. The pipeline additionally needs `huggingface_hub` (see `pipeline/requirements.txt`) and a free Hugging Face token in `.env` for the Foursquare download.
