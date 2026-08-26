# Contact Directory — Final Desktop Edition

This packet is a **native Windows desktop application** built with Tkinter and SQLite. It contains no Flask server, web templates, browser frontend, or web runtime. The application is designed for a large contact directory and supports database-backed imports, controlled metadata, duplicate filtering, filtered exports, and audit history.

## Main capabilities

| Area | Included behavior |
|---|---|
| Directory | Search, exact filters, database-backed filter values, paginated results, and source metadata visibility |
| Import & Map | CSV/XLSX/XLS upload, header mapping, controlled Source/Industry/Source Type/Source Category/Category selectors, Add New catalog values, automatic UTC upload date, preview, and validation |
| Filter File | Map Email and/or Phone, compare against SQLite, remove database matches from a new clean CSV, and standardize retained phone values |
| Import History | Audited filename, upload date, source, industry, source type, source category, category, row counts, errors, and mapping |
| Directory exports | Download the active filtered database result as CSV or Excel workbook without changing SQLite |
| Storage | Local SQLite database with indexes, reusable option catalogs, raw source traceability, and migration support |

## Run from source on Windows

Install Python 3.10 or newer on Windows, open Command Prompt in this folder, and run:

```bat
py -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python desktop_app.py
```

The bundled database is `data\contacts.db`. To rebuild it from the canonical seed file, use:

```bat
.venv\Scripts\python.exe init_db.py --reset --csv data\normalized_merged_all.csv --source "Initial database seed" --source-category "Initial Dataset"
```

## Build the Windows EXE

A Windows EXE must be packaged on Windows so PyInstaller can bundle the Windows Python and Tk runtime. Double-click `build_windows.bat`, or run it from Command Prompt:

```bat
build_windows.bat
```

The script creates a virtual environment, installs only the desktop dependencies, compiles the source files, builds the application, and copies the seeded writable data directory. The result is:

```text
dist\ContactDirectory\ContactDirectory.exe
```

Copy the complete `dist\ContactDirectory` folder to another computer. Do not copy only the EXE; the folder contains its runtime and writable SQLite data directory.

## Import workflow

Open **Import & Map**, select a CSV or Excel file, and review the suggested field mappings. Source, Industry, Source Type, and Source Category are required controlled metadata values. Category is optional but also comes from a reusable SQLite catalog. Existing values appear in dropdowns. To create a new value, type it in the adjacent Add New field and choose **Add New**. The value is saved to the database catalog and selected for the import.

The application records the upload date automatically in UTC. Controlled metadata overrides conflicting same-named fields in the uploaded file, which keeps classification consistent. The original source row remains available in `raw_data`, and Excel worksheet names are retained when available. The import is rejected unless at least one identifier—Name, First/Last Name, Email, or Phone—is mapped.

## Phone-number standardization

The application uses one phone rule across seeding, imports, filtering, search, and exports:

1. Remove spaces, dots, plus signs, hyphens, and parentheses.
2. If the result is 11 digits and begins with `1`, remove that leading `1`.
3. Store only an exactly 10-digit numeric result.
4. Store an invalid or unsafe value as blank in the canonical Phone field while retaining the original source value in traceability data.

The database invariant is that every nonblank stored phone contains exactly 10 digits. Scientific-notation-like spreadsheet values are expanded when safely possible before validation.

## Filter File workflow

Open **Filter File**, select a CSV or Excel file, map its Email and/or Phone columns, and choose the matching rules. A new clean CSV is produced. Rows matching an existing normalized database email or phone are removed from that output only; the database is never modified. Phone values retained in the clean output are rewritten to the canonical 10-digit form.

## Directory exports

In **Directory**, apply any combination of Search, Industry, City, State, Country, Category, Source, Source Type, Source Category, and Source File filters. Select **Download CSV** or **Download Excel** beside the filter buttons. The export queries SQLite using the active filters, includes all normalized database fields, runs in the background for large result sets, and does not change the database.

## Final seed data and traceability

`data\normalized_merged_all.csv` is the canonical 27-column seed file. Safe equivalent fields are consolidated into Address Line 1, Address Line 2, Postal Code, State, and Name where first and last names are available. The original uploaded file is retained as `data\normalized_merged_all_input_original.csv`, and each normalized row retains all original source fields in `Original Values`. SQLite stores the same source-row traceability in `raw_data`.

The final seed contains **279,522 contacts**. It contains **208,022 Med** rows and **71,500 Not Med** rows. Four rows have no source name and therefore retain a blank canonical Name rather than receiving an invented value.

## Important files

| File | Purpose |
|---|---|
| `desktop_app.py` | Native Tkinter application and all four tabs |
| `db.py` | SQLite schema, migrations, phone normalization, queries, imports, matching, and exports |
| `importers.py` | CSV/XLSX/XLS encoding, preview, worksheet, and row readers |
| `init_db.py` | Clean database initialization and seed loading |
| `normalize_main_csv.py` | Lossless canonical seed-data normalizer |
| `audit_main_csv.py` | Streaming source-data audit utility |
| `build_windows.bat` | Windows PyInstaller build script |
| `requirements-desktop.txt` | pandas, openpyxl, and PyInstaller dependencies |
| `data/contacts.db` | Clean seeded SQLite database |
| `data/normalized_merged_all.csv` | Canonical seed CSV |
| `data/normalized_merged_all_input_original.csv` | Original uploaded seed CSV |
| `main_csv_audit.json` | Source CSV quality audit |
| `main_csv_normalization_report.json` | Canonicalization and phone-cleaning report |
| `test_desktop.py` | Native GUI startup and tab smoke test |
| `test_export.py` | Filtered CSV/Excel export test |
| `test_final_hardening.py` | Phone, import, duplicate-filter, and invariant regression test |
| `test_catalogs.py` | Catalog, timestamp, and source-type validation |

## Validation completed

The final build was syntax-checked and tested under a virtual display. All four tabs initialized and closed cleanly. Filtered CSV and Excel exports matched the SQLite result count and full 27-column schema. Phone normalization, import storage, duplicate matching, and clean-file output passed regression tests. The final database contains 279,522 contacts, no invalid nonblank phone values, no blank upload dates, and only the controlled Med/Not Med source types.

## Hosted DNC/TCPA filtering integration

The **Filter File** tab now supports a two-pass workflow. The application first removes rows that match the local SQLite database by mapped canonical phone and/or email. If the user enables **After local database filtering, run the remaining CSV through the hosted DNC cleaner**, the remaining CSV is then submitted to the configured hosted CSV Cleaner URL. The hosted service runs its internal DNC and optional TCPA phone scrub, and the desktop application saves the returned cleaned CSV to the user-selected destination.

The default service URL is `https://csv-cleaner-app-uc0p.onrender.com/`. The URL is editable in the Filter File tab, and **Check service** verifies only the public health endpoint before any file is uploaded. The hosted pass requires a mapped Phone column because the cleaner is a phone-based suppression step. A progress status reports local filtering, upload, hosted cleaning, and final output stages. If hosted cleaning fails, the error is shown and the user can rerun with the local-only workflow; the local database is never modified.

Because the hosted app is a Streamlit browser interface rather than a documented REST API, this integration uses Playwright browser automation. The Windows build script installs and bundles Chromium into the distribution folder. An internet connection is required only when the hosted pass is enabled. Contact files sent through this option leave the local computer and are processed by the configured hosted service, so users should enable it only for data they are authorized to upload.

## Hosted uploader reliability fix

The hosted DNC integration targets the main uploader by its accessible label, waits for Streamlit hydration, handles the hidden file input safely, and waits for the uploaded-file confirmation chip before continuing. It no longer assumes that the first generic file input is visible. The application also discovers a bundled Playwright Chromium browser in the Windows distribution and falls back to installed Chrome or Chromium when available.

A live upload-only diagnostic against the configured hosted service passed with a synthetic non-sensitive CSV. This verifies that the previous `Page.wait_for_selector` timeout has been corrected. The full cleaning pass can take longer because the hosted service must process the file and expose its result download; progress messages are shown in the desktop Filter File tab.
