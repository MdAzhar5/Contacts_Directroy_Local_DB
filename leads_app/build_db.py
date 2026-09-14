r"""
Fresh build of leads.duckdb (schema v2) from the pipeline outputs in ..\data:

  Foursquare     ..\data\foursquare_usa_contacts.parquet     (pipeline/fsq_usa_to_csv.py)
  Overture       ..\data\overture_usa_contacts.parquet       (pipeline/overture_usa.py)
  OpenStreetMap  ..\data\osm_usa_contacts.parquet            (pipeline/osm_usa.py)

For incremental refreshes of an existing database use pipeline/update.py instead.

Missing inputs are skipped with a warning. Phones are stored as canonical 10 digits,
emails lowercased, state codes normalized. The Used Data store starts EMPTY:
import your contacts through the app, or use migrate_v2.py to upgrade an existing
database while keeping its Used Data.

Close the app before running this, since it replaces the database file.
Run:  python build_db.py
"""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
DB = HERE / "leads.duckdb"

sys.path.insert(0, str(HERE))
from schema import build_dims, create_empty_db, create_stores, install_macros  # noqa: E402

SOURCES = [
    ("Foursquare", DATA / "foursquare_usa_contacts.parquet"),
    ("Overture", DATA / "overture_usa_contacts.parquet"),
    ("OpenStreetMap", DATA / "osm_usa_contacts.parquet"),
]

COMMON = """
    business_name, phone, website, email, address, city, state, zip, country,
    TRY_CAST(latitude AS DOUBLE) AS latitude, TRY_CAST(longitude AS DOUBLE) AS longitude,
    categories, instagram, twitter, facebook_id, date_created, date_refreshed, date_closed
"""


def p(path: Path) -> str:
    return str(path).replace("\\", "/")


def main() -> None:
    t0 = time.time()
    selects = []
    for name, path in SOURCES:
        if not path.exists():
            print(f"WARNING: {name} input not found, skipping: {path}")
            continue
        if path.suffix == ".csv":
            reader = f"read_csv('{p(path)}', header=true, all_varchar=true)"
            sid = "fsq_place_id"
        else:
            reader = f"read_parquet('{p(path)}')"
            sid = "source_id"
        selects.append(f"SELECT '{name}' AS source, {sid} AS source_id, {COMMON} FROM {reader}")
        print(f"Including {name}: {path.name}")
    if DB.exists():
        DB.unlink()
    if not selects:
        create_empty_db(DB, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        print(f"No pipeline inputs found. Created an EMPTY database instead -> {DB}")
        return
    con = duckdb.connect(str(DB))
    install_macros(con)

    print("Loading into table 'places'...")
    con.execute(f"""
        CREATE TABLE places AS
        SELECT source, source_id, business_name,
               norm_phone(phone)            AS phone,
               nullif(trim(website), '')    AS website,
               norm_email(email)            AS email,
               nullif(trim(address), '')    AS address,
               nullif(trim(city), '')       AS city,
               norm_state(state)            AS state,
               nullif(trim(zip), '')        AS zip,
               country, latitude, longitude, categories, instagram, twitter, facebook_id,
               date_created, date_refreshed, date_closed,
               CASE WHEN categories IS NULL THEN []::VARCHAR[]
                    ELSE list_transform(string_split(categories, ' | '), x -> trim(x)) END AS category_list,
               CASE WHEN categories IS NULL THEN []::VARCHAR[]
                    ELSE list_distinct(list_transform(string_split(categories, ' | '), x -> split_part(trim(x), ' > ', 1))) END AS industry_list,
               (date_closed IS NULL) AS is_open,
               NULL::VARCHAR AS used_at, NULL::VARCHAR AS used_reason
        FROM ({' UNION ALL '.join(selects)})
    """)
    for src, n in con.execute("SELECT source, count(*) FROM places GROUP BY 1 ORDER BY 2 DESC").fetchall():
        print(f"  {src}: {n:,} rows")
    print(f"  total {con.execute('SELECT count(*) FROM places').fetchone()[0]:,} in {time.time() - t0:.0f}s")

    print("Building lookup tables and empty Used Data store...")
    build_dims(con)
    create_stores(con, stamp=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    for t in ("dim_source", "dim_industry", "dim_category", "dim_state", "dim_city", "dim_country"):
        print(f"  {t}: {con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]:,} rows")
    con.execute("CHECKPOINT")
    con.close()
    print(f"\nDone in {(time.time() - t0) / 60:.1f} min -> {DB}  ({DB.stat().st_size / 1e9:.2f} GB)")


if __name__ == "__main__":
    main()
