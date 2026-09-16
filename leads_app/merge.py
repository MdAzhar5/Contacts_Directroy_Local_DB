"""
Merge a freshly extracted source release into leads.duckdb without duplicating anything.
Every release row is matched on (source, source_id):

  * already in places    -> "existing": updated in place if its contact data changed (usage marks kept),
                            otherwise left alone. Existing rows are never skipped.
  * not in places yet    -> new candidate, classified in this order:
      - no valid phone and no valid email                        -> skipped (no contact)
      - phone or email already in All Leads (any source)         -> skipped (in leads)
      - phone or email already in Used Data                      -> skipped (in used)
      - shares a phone/email with a better row of this release   -> skipped (duplicate)
      - everything left                                          -> FRESH: inserted, added_batch = history id
  * existing rows whose phone or email is already in Used Data   -> marked used

Skipped rows are not inserted (the extracted parquet stays on disk). Then the filter tables are
rebuilt, the installed version is recorded in `meta` and the counts are written to History.
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
from schema import build_dims, ensure_places_columns, install_macros  # noqa: E402

DB = HERE / "leads.duckdb"

CONTACT_COLUMNS = [
    "business_name", "phone", "website", "email", "address", "city", "state", "zip", "country",
    "latitude", "longitude", "categories", "instagram", "twitter", "facebook_id",
    "date_created", "date_refreshed", "date_closed", "category_list", "industry_list", "is_open",
]
BATCH_COLUMNS = ["source", "source_id"] + CONTACT_COLUMNS
INSERT_COLUMNS = BATCH_COLUMNS + ["used_at", "used_reason", "added_batch"]
# why a new candidate was not inserted, in priority order; stats keys are skipped_<reason>
SKIP_REASONS = ("in_leads", "in_used", "duplicate", "no_contact")
# which of several new rows sharing a phone/email is kept (same idea as pipeline/extract.py)
WINNER_ORDER = "is_open DESC, completeness DESC, source_id"


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


def classify_new(con, source: str) -> dict:
    """Temp table batch_new: one row per release row whose (source, source_id) is not in places yet.
    status NULL = fresh, else the skip reason. Set-based passes only, so it scales to full releases.
    Returns {status or 'fresh': count}."""
    con.execute("DROP TABLE IF EXISTS batch_new")
    con.execute(f"""
        CREATE TEMP TABLE batch_new AS
        SELECT source_id, phone, email, is_open,
               (phone IS NOT NULL)::INT + (email IS NOT NULL)::INT + (website IS NOT NULL)::INT
             + (address IS NOT NULL)::INT + (city IS NOT NULL)::INT AS completeness,
               CASE WHEN phone IS NULL AND email IS NULL THEN 'no_contact' END AS status
        FROM batch b
        WHERE NOT EXISTS (SELECT 1 FROM places p WHERE p.source = {fs(source)} AND p.source_id = b.source_id)
    """)
    # All Leads first (any source), then Used Data; a row keeps the first reason it hits
    for reason, table in (("in_leads", "places"), ("in_used", "used_contacts")):
        for col in ("phone", "email"):
            con.execute(f"""
                UPDATE batch_new SET status = '{reason}'
                WHERE status IS NULL AND {col} IS NOT NULL
                  AND {col} IN (SELECT {col} FROM {table} WHERE {col} IS NOT NULL)
            """)
    # duplicates inside the release: keep the best row per phone, then among those the best per email
    for col in ("phone", "email"):
        con.execute(f"""
            UPDATE batch_new SET status = 'duplicate'
            WHERE source_id IN (
                SELECT source_id FROM (
                    SELECT source_id, row_number() OVER (PARTITION BY {col} ORDER BY {WINNER_ORDER}) AS rn
                    FROM batch_new WHERE status IS NULL AND {col} IS NOT NULL
                ) WHERE rn > 1
            )
        """)
    return dict(con.execute("SELECT coalesce(status, 'fresh'), count(*) FROM batch_new GROUP BY 1").fetchall())


def merge(con, source: str, parquet: Path, version: str, rebuild: bool = True, log=print) -> dict:
    t0 = time.time()
    stamp = utc_now()
    parquet = Path(parquet)
    if ensure_places_columns(con):
        log("Added batch tracking (added_batch column) to the leads table")
    log(f"Loading {parquet.name} ...")
    total = load_batch(con, source, parquet)
    log(f"  {total:,} rows in the new {source} release")
    before = con.execute("SELECT count(*) FROM places WHERE source = ?", [source]).fetchone()[0]

    def diff(alias: str) -> str:
        return " OR ".join(f"{alias}.{c} IS DISTINCT FROM b.{c}" for c in CONTACT_COLUMNS)

    con.execute("BEGIN TRANSACTION")
    try:
        hid = con.execute("SELECT nextval('seq_history')").fetchone()[0]
        log("Updating businesses already in the database whose details changed ...")
        # counted on release rows (not places rows) so existing = updated + unchanged always holds
        updated = con.execute(f"""
            SELECT count(*) FROM batch b
            WHERE EXISTS (SELECT 1 FROM places p WHERE p.source = {fs(source)} AND p.source_id = b.source_id AND ({diff('p')}))
        """).fetchone()[0]
        if updated:
            sets = ", ".join(f"{c} = b.{c}" for c in CONTACT_COLUMNS)
            con.execute(f"""
                UPDATE places SET {sets}
                FROM batch b
                WHERE places.source = {fs(source)} AND places.source_id = b.source_id AND ({diff('places')})
            """)
        log("Checking new businesses against All Leads and Used Data ...")
        counts = classify_new(con, source)
        new_candidates = sum(counts.values())
        existing = total - new_candidates
        fresh = counts.get("fresh", 0)
        skipped = {f"skipped_{r}": counts.get(r, 0) for r in SKIP_REASONS}
        skipped_total = sum(skipped.values())
        log(f"  {existing:,} already in the database ({updated:,} updated, {existing - updated:,} unchanged)")
        log(f"  {new_candidates:,} new businesses in this release: {fresh:,} fresh, {skipped_total:,} old skipped")
        log(f"    {skipped['skipped_in_leads']:,} skipped: phone or email already in All Leads")
        log(f"    {skipped['skipped_in_used']:,} skipped: phone or email already in Used Data")
        log(f"    {skipped['skipped_duplicate']:,} skipped: duplicate phone or email inside this release")
        log(f"    {skipped['skipped_no_contact']:,} skipped: no valid phone or email")
        if fresh:
            log(f"Adding {fresh:,} fresh leads ...")
            con.execute(f"""
                INSERT INTO places ({", ".join(INSERT_COLUMNS)})
                SELECT {", ".join("b." + c for c in BATCH_COLUMNS)}, NULL::VARCHAR, NULL::VARCHAR, {int(hid)}::BIGINT
                FROM batch b
                WHERE b.source_id IN (SELECT source_id FROM batch_new WHERE status IS NULL)
            """)
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
        stats = {"source": source, "version": version, "release_date": version[:10], "history_id": hid,
                 "installed_at": stamp, "total": total, "existing": existing, "updated": updated,
                 "unchanged": existing - updated, "new_candidates": new_candidates, "fresh": fresh, **skipped,
                 "skipped_total": skipped_total, "marked": marked}
        set_meta(con, f"source_version:{source}", version)
        set_meta(con, f"source_updated_at:{source}", stamp)
        con.execute("""
            INSERT INTO history (id, kind, filename, occurred_at, source, industry, source_type, source_category, category,
                                 rows, rows_failed, rows_skipped, places_marked, mapping_json, filters_json, error_sample, output_path)
            VALUES (?, 'update', ?, ?, ?, NULL, NULL, NULL, NULL, ?, 0, ?, ?, NULL, ?, NULL, ?)
        """, [hid, parquet.name, stamp, source, fresh, skipped_total, marked, json.dumps(stats), str(parquet)])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    con.execute("DROP TABLE IF EXISTS batch_new")
    con.execute("DROP TABLE IF EXISTS batch")
    if rebuild:
        log("Rebuilding filter tables (industry tree, states, cities) ...")
        build_dims(con)
    con.execute("CHECKPOINT")
    after = con.execute("SELECT count(*) FROM places WHERE source = ?", [source]).fetchone()[0]
    stats.update(before=before, after=after, seconds=round(time.time() - t0))
    log(f"Done: {source} {version}: {before:,} -> {after:,} rows (+{fresh:,} fresh leads added, "
        f"{skipped_total:,} old skipped, {updated:,} updated) in {stats['seconds']}s")
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
