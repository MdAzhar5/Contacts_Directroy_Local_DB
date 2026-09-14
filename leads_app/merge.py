"""
Merge a freshly extracted source release into leads.duckdb without duplicating anything:

  * rows whose (source, source_id) is new            -> inserted
  * rows that exist but whose contact data changed  -> updated in place (usage marks kept)
  * rows that exist and are unchanged               -> left alone
  * new/changed rows whose phone or email is already in Used Data -> marked used

Then the filter tables are rebuilt and the installed version is recorded in `meta`.
Used by pipeline/update.py; can also be called on its own:

    python merge.py Overture ../data/overture/2026-08-19.0/overture_usa_contacts.parquet 2026-08-19.0
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from schema import build_dims, install_macros  # noqa: E402

DB = HERE / "leads.duckdb"

CONTACT_COLUMNS = [
    "business_name", "phone", "website", "email", "address", "city", "state", "zip", "country",
    "latitude", "longitude", "categories", "instagram", "twitter", "facebook_id",
    "date_created", "date_refreshed", "date_closed", "category_list", "industry_list", "is_open",
]


def fs(path) -> str:
    return "'" + str(path).replace("\\", "/").replace("'", "''") + "'"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_versions(con) -> dict:
    rows = con.execute("SELECT key, value FROM meta WHERE key LIKE 'source_version:%'").fetchall()
    return {k.split(":", 1)[1]: v for k, v in rows}


def set_meta(con, key: str, value: str) -> None:
    con.execute("DELETE FROM meta WHERE key = ?", [key])
    con.execute("INSERT INTO meta VALUES (?, ?)", [key, value])


def load_batch(con, source: str, parquet: Path) -> int:
    """Normalize the extracted file into a temp table with exactly the places column layout (minus usage)."""
    con.execute("DROP TABLE IF EXISTS batch")
    con.execute(f"""
        CREATE TEMP TABLE batch AS
        SELECT * EXCLUDE (rn) FROM (
            SELECT {fs(source)}::VARCHAR AS source, CAST(source_id AS VARCHAR) AS source_id,
                   nullif(trim(business_name), '') AS business_name,
                   norm_phone(phone) AS phone, nullif(trim(website), '') AS website, norm_email(email) AS email,
                   nullif(trim(address), '') AS address, nullif(trim(city), '') AS city, norm_state(state) AS state,
                   nullif(trim(zip), '') AS zip, CAST(country AS VARCHAR) AS country,
                   TRY_CAST(latitude AS DOUBLE) AS latitude, TRY_CAST(longitude AS DOUBLE) AS longitude,
                   CAST(categories AS VARCHAR) AS categories, CAST(instagram AS VARCHAR) AS instagram,
                   CAST(twitter AS VARCHAR) AS twitter, CAST(facebook_id AS VARCHAR) AS facebook_id,
                   CAST(date_created AS VARCHAR) AS date_created, CAST(date_refreshed AS VARCHAR) AS date_refreshed,
                   CAST(date_closed AS VARCHAR) AS date_closed,
                   CASE WHEN categories IS NULL THEN []::VARCHAR[]
                        ELSE list_transform(string_split(CAST(categories AS VARCHAR), ' | '), x -> trim(x)) END AS category_list,
                   CASE WHEN categories IS NULL THEN []::VARCHAR[]
                        ELSE list_distinct(list_transform(string_split(CAST(categories AS VARCHAR), ' | '), x -> split_part(trim(x), ' > ', 1))) END AS industry_list,
                   (date_closed IS NULL) AS is_open,
                   row_number() OVER (PARTITION BY source_id ORDER BY (date_closed IS NULL) DESC) AS rn
            FROM read_parquet({fs(parquet)})
            WHERE source_id IS NOT NULL
        ) WHERE rn = 1
    """)
    return con.execute("SELECT count(*) FROM batch").fetchone()[0]


def merge(con, source: str, parquet: Path, version: str, rebuild: bool = True, log=print) -> dict:
    t0 = time.time()
    stamp = utc_now()
    log(f"Loading {parquet.name} ...")
    total = load_batch(con, source, parquet)
    log(f"  {total:,} rows in the new {source} release")
    before = con.execute("SELECT count(*) FROM places WHERE source = ?", [source]).fetchone()[0]

    con.execute("BEGIN TRANSACTION")
    try:
        hid = con.execute("SELECT nextval('seq_history')").fetchone()[0]
        log("Inserting new businesses ...")
        inserted = con.execute(f"""
            INSERT INTO places
            SELECT b.*, NULL::VARCHAR, NULL::VARCHAR FROM batch b
            WHERE NOT EXISTS (SELECT 1 FROM places p WHERE p.source = {fs(source)} AND p.source_id = b.source_id)
        """).fetchone()[0]
        log(f"  {inserted:,} new")
        log("Updating businesses whose details changed ...")
        sets = ", ".join(f"{c} = b.{c}" for c in CONTACT_COLUMNS)
        diff = " OR ".join(f"places.{c} IS DISTINCT FROM b.{c}" for c in CONTACT_COLUMNS)
        updated = con.execute(f"""
            UPDATE places SET {sets}
            FROM batch b
            WHERE places.source = {fs(source)} AND places.source_id = b.source_id AND ({diff})
        """).fetchone()[0]
        log(f"  {updated:,} updated, {total - inserted - updated:,} unchanged")
        log("Marking rows already present in Used Data ...")
        marked = con.execute(f"""
            UPDATE places SET used_at = ?, used_reason = 'update:{hid}'
            WHERE source = ? AND used_at IS NULL AND phone IS NOT NULL
              AND phone IN (SELECT phone FROM used_contacts WHERE phone IS NOT NULL)
        """, [stamp, source]).fetchone()[0]
        marked += con.execute(f"""
            UPDATE places SET used_at = ?, used_reason = 'update:{hid}'
            WHERE source = ? AND used_at IS NULL AND email IS NOT NULL
              AND email IN (SELECT email FROM used_contacts WHERE email IS NOT NULL)
        """, [stamp, source]).fetchone()[0]
        log(f"  {marked:,} marked used")
        set_meta(con, f"source_version:{source}", version)
        set_meta(con, f"source_updated_at:{source}", stamp)
        con.execute("""
            INSERT INTO history (id, kind, filename, occurred_at, source, industry, source_type, source_category, category,
                                 rows, rows_failed, rows_skipped, places_marked, mapping_json, filters_json, error_sample, output_path)
            VALUES (?, 'update', ?, ?, ?, NULL, NULL, NULL, NULL, ?, 0, ?, ?, NULL, ?, NULL, ?)
        """, [hid, parquet.name, stamp, source, inserted + updated, total - inserted - updated, marked,
              json.dumps({"version": version, "new": inserted, "updated": updated, "unchanged": total - inserted - updated}),
              str(parquet)])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    con.execute("DROP TABLE IF EXISTS batch")
    if rebuild:
        log("Rebuilding filter tables (industry tree, states, cities) ...")
        build_dims(con)
    con.execute("CHECKPOINT")
    after = con.execute("SELECT count(*) FROM places WHERE source = ?", [source]).fetchone()[0]
    stats = {"source": source, "version": version, "total": total, "inserted": inserted, "updated": updated,
             "unchanged": total - inserted - updated, "marked": marked, "before": before, "after": after,
             "seconds": round(time.time() - t0)}
    log(f"Done: {source} {version}: {before:,} -> {after:,} rows (+{inserted:,} new, {updated:,} updated) in {stats['seconds']}s")
    return stats


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    source, parquet, version = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    con = duckdb.connect(str(DB))
    install_macros(con)
    merge(con, source, parquet, version)
    con.close()


if __name__ == "__main__":
    main()
