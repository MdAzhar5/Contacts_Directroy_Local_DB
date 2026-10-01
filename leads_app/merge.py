"""
Merge a freshly extracted source release into leads.duckdb without duplicating anything.
Every release row is matched on (source, source_id):

  * already in places    -> "existing": updated in place if its contact data changed (usage marks kept),
                            otherwise left alone. Existing rows are never skipped.
  * not in places yet    -> new candidate, classified in this order:
      - no valid phone and no valid email                        -> skipped (no contact)
      - phone or email already in All Leads (any source; includes
        contacts an existing business drops in this release)    -> skipped (in leads)
      - shares a phone/email with a kept row of this release     -> skipped (duplicate)
      - everything left                                          -> FRESH: inserted, added_batch = history id

So a lead is fresh only when neither its phone nor its email is already in All Leads. Updates never read
or change Used Data and never mark leads used: only imports do that. Skipped rows are not inserted
(the extracted parquet stays on disk). Row changes, the installed version in `meta` and the History counts are
committed in one transaction; the filter tables are rebuilt afterwards and swapped in whole (see schema.build_dims),
so a failed rebuild never undoes the installed data.

NPI (CMS NPPES providers) uses the "every provider" rule instead (CONTACT_DEDUPE): a new NPI is skipped only when
it has no valid phone or email; one whose phone is already in All Leads or shared with other providers (doctors
share clinic phones) is added anyway, and those overlaps are counted for information only. In the same
transaction the provider details (npi_details) of every release NPI in All Leads are replaced, and NPIs listed in
npi_deactivated.parquet (next to the release parquet) are closed; their contact data and usage stay as they are.
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
from schema import (NPI_DETAIL_COLUMNS, NPI_SOURCE, build_dims, ensure_npi_details,  # noqa: E402
                    ensure_places_columns, install_macros)

DB = HERE / "leads.duckdb"

CONTACT_COLUMNS = [
    "business_name", "phone", "website", "email", "address", "city", "state", "zip", "country",
    "latitude", "longitude", "categories", "instagram", "twitter", "facebook_id",
    "date_created", "date_refreshed", "date_closed", "category_list", "industry_list", "is_open",
]
BATCH_COLUMNS = ["source", "source_id"] + CONTACT_COLUMNS
INSERT_COLUMNS = BATCH_COLUMNS + ["used_at", "used_reason", "added_batch"]
# why a new candidate was not inserted, in priority order; stats keys are skipped_<reason>
SKIP_REASONS = ("in_leads", "duplicate", "no_contact")
# which of several new rows sharing a phone/email is kept (same idea as pipeline/extract.py)
WINNER_ORDER = "is_open DESC, completeness DESC, source_id"
# per source: True = "fresh_only" (skip new rows whose phone/email is already in All Leads or repeated in the release);
# False = "every_provider" (add every new row with a valid phone or email; only no_contact is skipped)
CONTACT_DEDUPE = {NPI_SOURCE: False}
# written by pipeline/extract.py next to the NPI release parquet: (source_id, date_closed) of deactivated NPIs
NPI_DEACTIVATED_NAME = "npi_deactivated.parquet"


def merge_rule(source: str) -> str:
    return "fresh_only" if CONTACT_DEDUPE.get(source, True) else "every_provider"


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
    """Normalize the extracted file into a temp table with exactly the places column layout (minus usage).
    Text columns are cast first: a column an extractor writes as plain NULL (e.g. NPI's website) is INTEGER in parquet."""
    con.execute("DROP TABLE IF EXISTS batch")
    con.execute(f"""
        CREATE TEMP TABLE batch AS
        SELECT * EXCLUDE (rn) FROM (
            SELECT {fs(source)}::VARCHAR AS source, CAST(source_id AS VARCHAR) AS source_id,
                   nullif(trim(CAST(business_name AS VARCHAR)), '') AS business_name,
                   norm_phone(phone) AS phone, nullif(trim(CAST(website AS VARCHAR)), '') AS website, norm_email(email) AS email,
                   nullif(trim(CAST(address AS VARCHAR)), '') AS address, nullif(trim(CAST(city AS VARCHAR)), '') AS city,
                   norm_state(CAST(state AS VARCHAR)) AS state,
                   nullif(trim(CAST(zip AS VARCHAR)), '') AS zip, CAST(country AS VARCHAR) AS country,
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


def classify_new(con, source: str, dedupe: bool = True) -> dict:
    """Temp table batch_new: one row per release row whose (source, source_id) is not in places yet.
    status NULL = fresh, else the skip reason. Set-based passes only, so it scales to full releases.
    dedupe=False (every-provider rule): only no_contact is skipped, and `known` flags fresh rows whose phone or
    email is already in All Leads (for information; see overlap_counts).
    Returns {status or 'fresh': count}."""
    con.execute("DROP TABLE IF EXISTS batch_new")
    con.execute(f"""
        CREATE TEMP TABLE batch_new AS
        SELECT source_id, phone, email, is_open,
               (phone IS NOT NULL)::INT + (email IS NOT NULL)::INT + (website IS NOT NULL)::INT
             + (address IS NOT NULL)::INT + (city IS NOT NULL)::INT AS completeness,
               CASE WHEN phone IS NULL AND email IS NULL THEN 'no_contact' END AS status,
               false AS known
        FROM batch b
        WHERE NOT EXISTS (SELECT 1 FROM places p WHERE p.source = {fs(source)} AND p.source_id = b.source_id)
    """)
    if not dedupe:
        # one pass per column keeps a single hash table in memory at a time (as below)
        for col in ("phone", "email"):
            con.execute(f"""
                UPDATE batch_new SET known = true
                WHERE status IS NULL AND NOT known AND {col} IS NOT NULL
                  AND {col} IN (SELECT {col} FROM places WHERE {col} IS NOT NULL)
            """)
        return dict(con.execute("SELECT coalesce(status, 'fresh'), count(*) FROM batch_new GROUP BY 1").fetchall())
    # All Leads only (any source, plus contacts updated businesses just gave up); Used Data plays no part in updates
    for reason, table in (("in_leads", "places"), ("in_leads", "prev_contacts")):
        for col in ("phone", "email"):
            con.execute(f"""
                UPDATE batch_new SET status = '{reason}'
                WHERE status IS NULL AND {col} IS NOT NULL
                  AND {col} IN (SELECT {col} FROM {table} WHERE {col} IS NOT NULL)
            """)
    # duplicates inside the release: keep the best row per phone, then among those the best per email
    competing = "status IS NULL"
    while True:
        for col in ("phone", "email"):
            con.execute(f"""
                UPDATE batch_new SET status = 'duplicate'
                WHERE source_id IN (
                    SELECT source_id FROM (
                        SELECT source_id, row_number() OVER (PARTITION BY {col} ORDER BY {WINNER_ORDER}) AS rn
                        FROM batch_new WHERE {competing} AND {col} IS NOT NULL
                    ) WHERE rn > 1
                )
            """)
        con.execute("UPDATE batch_new SET status = NULL WHERE status = 'retry'")
        # a row can lose its phone to a row that then lost its email; when no kept row holds its phone or
        # email it competes again (each round keeps at least one row, so this ends; usually after one round)
        retry = con.execute("""
            UPDATE batch_new SET status = 'retry'
            WHERE status = 'duplicate'
              AND (phone IS NULL OR phone NOT IN (SELECT phone FROM batch_new WHERE status IS NULL AND phone IS NOT NULL))
              AND (email IS NULL OR email NOT IN (SELECT email FROM batch_new WHERE status IS NULL AND email IS NOT NULL))
        """).fetchone()[0]
        if not retry:
            break
        competing = "status = 'retry'"
    return dict(con.execute("SELECT coalesce(status, 'fresh'), count(*) FROM batch_new GROUP BY 1").fetchall())


def overlap_counts(con) -> tuple:
    """Every-provider rule, for information only: (fresh rows whose phone or email was already in All Leads,
    fresh rows sharing their phone with at least one other fresh row)."""
    known = con.execute("SELECT count(*) FROM batch_new WHERE status IS NULL AND known").fetchone()[0]
    shared = con.execute("""
        SELECT coalesce(sum(n), 0) FROM (
            SELECT count(*) AS n FROM batch_new WHERE status IS NULL AND phone IS NOT NULL GROUP BY phone HAVING count(*) > 1
        )
    """).fetchone()[0]
    return int(known), int(shared)


def require_columns(con, path: Path, columns) -> None:
    """Fail before the merge transaction when an extracted file lacks columns the merge needs."""
    have = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({fs(path)})").fetchall()}
    missing = [c for c in columns if c not in have]
    if missing:
        raise RuntimeError(f"{path.name} has no {', '.join(missing)} column(s); extract the release again with the current pipeline")


def replace_npi_details(con, parquet: Path) -> int:
    """npi_details of every release NPI that is in All Leads now (inserted by this merge or already there) are
    replaced by the release's details; skipped rows get none. Call after the insert. Returns the rows written."""
    kept = "SELECT source_id FROM batch WHERE source_id NOT IN (SELECT source_id FROM batch_new WHERE status IS NOT NULL)"
    con.execute(f"DELETE FROM npi_details WHERE source_id IN ({kept})")
    details = ", ".join(("norm_phone(contact_phone)" if c == "contact_phone" else f"nullif(trim(CAST({c} AS VARCHAR)), '')")
                        + f" AS {c}" for c in NPI_DETAIL_COLUMNS)
    cols = ", ".join(NPI_DETAIL_COLUMNS)
    # one row per NPI, picked like load_batch picks the places row
    return con.execute(f"""
        INSERT INTO npi_details (source_id, {cols})
        SELECT source_id, {cols} FROM (
            SELECT CAST(source_id AS VARCHAR) AS source_id, {details},
                   row_number() OVER (PARTITION BY CAST(source_id AS VARCHAR) ORDER BY (date_closed IS NULL) DESC) AS rn
            FROM read_parquet({fs(parquet)})
            WHERE source_id IS NOT NULL AND CAST(source_id AS VARCHAR) IN ({kept})
        ) WHERE rn = 1
    """).fetchone()[0]


def close_deactivated(con, path: Path, release_date: str) -> int:
    """Close the NPI leads listed in npi_deactivated.parquet: date_closed and is_open only (contact data, usage and
    batch stay). An NPI that is in this release is active and left alone. Returns the leads changed."""
    return con.execute(f"""
        UPDATE places SET date_closed = d.date_closed, is_open = false
        FROM (
            SELECT CAST(source_id AS VARCHAR) AS source_id,
                   max(coalesce(nullif(trim(CAST(date_closed AS VARCHAR)), ''), ?)) AS date_closed
            FROM read_parquet({fs(path)})
            WHERE source_id IS NOT NULL AND CAST(source_id AS VARCHAR) NOT IN (SELECT source_id FROM batch)
            GROUP BY 1
        ) d
        WHERE places.source = {fs(NPI_SOURCE)} AND places.source_id = d.source_id
          AND (places.is_open OR places.date_closed IS DISTINCT FROM d.date_closed)
    """, [release_date]).fetchone()[0]


def committed(con, hid: int, stamp: str) -> bool:
    """After COMMIT raised: did DuckDB commit anyway? (A Ctrl+C during the checkpoint DuckDB runs inside COMMIT
    raises 'Query interrupted' although the data is committed.) Looks for this run's History row from a new cursor."""
    try:
        cur = con.cursor()
        try:
            return cur.execute("SELECT count(*) FROM history WHERE id = ? AND kind = 'update' AND occurred_at = ?",
                               [hid, stamp]).fetchone()[0] > 0
        finally:
            cur.close()
    except Exception:
        return False


def merge(con, source: str, parquet: Path, version: str, rebuild: bool = True, log=print, on_saved=None) -> dict:
    """on_saved() is called as soon as the new data is committed (also when COMMIT raised after DuckDB had
    committed), before the clean-up, checkpoint and filter rebuild."""
    t0 = time.time()
    stamp = utc_now()
    parquet = Path(parquet)
    rule = merge_rule(source)
    dedupe = rule == "fresh_only"
    npi = source == NPI_SOURCE
    if ensure_places_columns(con):
        log("Added batch tracking (added_batch column) to the leads table")
    ensure_npi_details(con)
    log(f"Loading {parquet.name} ...")
    total = load_batch(con, source, parquet)
    log(f"  {total:,} rows in the new {source} release")
    deactivated = None
    if npi:
        # checked before the transaction, so a file from an older extract fails without changing anything
        require_columns(con, parquet, NPI_DETAIL_COLUMNS)
        deactivated = parquet.with_name(NPI_DEACTIVATED_NAME)
        if deactivated.exists():
            require_columns(con, deactivated, ("source_id", "date_closed"))
        else:
            log(f"  no {NPI_DEACTIVATED_NAME} next to it: no deactivated NPIs will be closed")
            deactivated = None
    before = con.execute("SELECT count(*) FROM places WHERE source = ?", [source]).fetchone()[0]

    def diff(alias: str) -> str:
        return " OR ".join(f"{alias}.{c} IS DISTINCT FROM b.{c}" for c in CONTACT_COLUMNS)

    con.execute("BEGIN TRANSACTION")
    hid = None
    try:
        hid = con.execute("SELECT nextval('seq_history')").fetchone()[0]
        if not dedupe:
            # every-provider rule: classified before the in-place update, so the informational "already in All Leads"
            # count means All Leads as it was before this merge
            log(f"Checking new {source} records against All Leads (for information only; none are skipped for it) ...")
            counts = classify_new(con, source, dedupe=False)
        log("Updating businesses already in the database whose details changed ...")
        # counted on release rows (not places rows) so existing = updated + unchanged always holds;
        # a plain join, since a correlated EXISTS with these OR terms is far slower on a full release
        updated = con.execute(f"""
            SELECT count(DISTINCT b.source_id) FROM batch b
            JOIN places p ON p.source = {fs(source)} AND p.source_id = b.source_id
            WHERE {diff('p')}
        """).fetchone()[0]
        if dedupe:
            # phones/emails the changed businesses give up were in All Leads when the update started
            con.execute("DROP TABLE IF EXISTS prev_contacts")
            con.execute(f"""
                CREATE TEMP TABLE prev_contacts AS
                SELECT CASE WHEN p.phone IS DISTINCT FROM b.phone THEN p.phone END AS phone,
                       CASE WHEN p.email IS DISTINCT FROM b.email THEN p.email END AS email
                FROM batch b JOIN places p ON p.source = {fs(source)} AND p.source_id = b.source_id
                WHERE (p.phone IS NOT NULL AND p.phone IS DISTINCT FROM b.phone)
                   OR (p.email IS NOT NULL AND p.email IS DISTINCT FROM b.email)
            """)
        if updated:
            sets = ", ".join(f"{c} = b.{c}" for c in CONTACT_COLUMNS)
            con.execute(f"""
                UPDATE places SET {sets}
                FROM batch b
                WHERE places.source = {fs(source)} AND places.source_id = b.source_id AND ({diff('places')})
            """)
        if dedupe:
            log("Checking new businesses against All Leads ...")
            counts = classify_new(con, source)
        new_candidates = sum(counts.values())
        existing = total - new_candidates
        fresh = counts.get("fresh", 0)
        skipped = {f"skipped_{r}": counts.get(r, 0) for r in SKIP_REASONS}
        skipped_total = sum(skipped.values())
        extra = {}
        log(f"  {existing:,} already in the database ({updated:,} updated, {existing - updated:,} unchanged)")
        if dedupe:
            log(f"  {new_candidates:,} new businesses in this release: {fresh:,} fresh, {skipped_total:,} old skipped")
            log(f"    {skipped['skipped_in_leads']:,} skipped: phone or email already in All Leads")
            log(f"    {skipped['skipped_duplicate']:,} skipped: duplicate phone or email inside this release")
            log(f"    {skipped['skipped_no_contact']:,} skipped: no valid phone or email")
        else:
            known, shared = overlap_counts(con)
            extra.update(phone_in_leads=known, phone_shared_in_release=shared)
            log(f"  {new_candidates:,} new records in this release: {fresh:,} added (every provider rule), {skipped_total:,} skipped")
            log(f"    {skipped['skipped_no_contact']:,} skipped: no valid phone or email")
            log(f"    {known:,} added anyway: phone or email already in All Leads (information only)")
            log(f"    {shared:,} added anyway: phone shared with another new record of this release (information only)")
        if fresh:
            log(f"Adding {fresh:,} fresh leads ...")
            con.execute(f"""
                INSERT INTO places ({", ".join(INSERT_COLUMNS)})
                SELECT {", ".join("b." + c for c in BATCH_COLUMNS)}, NULL::VARCHAR, NULL::VARCHAR, {int(hid)}::BIGINT
                FROM batch b
                WHERE b.source_id IN (SELECT source_id FROM batch_new WHERE status IS NULL)
            """)
        if npi:
            log("Saving NPI provider details ...")
            log(f"  details replaced for {replace_npi_details(con, parquet):,} providers")
            closed = 0
            if deactivated:
                log("Closing deactivated NPIs ...")
                closed = close_deactivated(con, deactivated, version[:10])
                log(f"  {closed:,} deactivated NPIs closed (contact data and usage kept)")
            extra["deactivated_closed"] = closed
        stats = {"source": source, "version": version, "release_date": version[:10], "history_id": hid,
                 "installed_at": stamp, "rule": rule, "total": total, "existing": existing, "updated": updated,
                 "unchanged": existing - updated, "new_candidates": new_candidates, "fresh": fresh, **skipped,
                 "skipped_total": skipped_total, **extra}
        set_meta(con, f"source_version:{source}", version)
        set_meta(con, f"source_updated_at:{source}", stamp)
        con.execute("""
            INSERT INTO history (id, kind, filename, occurred_at, source, industry, source_type, source_category, category,
                                 rows, rows_failed, rows_skipped, places_marked, mapping_json, filters_json, error_sample, output_path)
            VALUES (?, 'update', ?, ?, ?, NULL, NULL, NULL, NULL, ?, 0, ?, 0, NULL, ?, NULL, ?)
        """, [hid, parquet.name, stamp, source, fresh, skipped_total, json.dumps(stats), str(parquet)])
        log("Saving the update (can take several minutes; do not close this window or press Ctrl+C) ...")
        con.execute("COMMIT")
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass    # DuckDB already ended the transaction (it rejected or was interrupted during COMMIT); keep the real error
        if on_saved and hid is not None and committed(con, hid, stamp):
            on_saved()
        raise
    # from here on the update is installed, even if the clean-up, checkpoint or rebuild below fails
    log("  new data saved")
    if on_saved:
        on_saved()
    for t in ("batch_new", "prev_contacts", "batch"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    # write the merge to disk and free its memory before the long rebuild
    con.execute("CHECKPOINT")
    after = con.execute("SELECT count(*) FROM places WHERE source = ?", [source]).fetchone()[0]
    stats.update(before=before, after=after)
    if rebuild:
        # outside the merge transaction (inside it, 25M rows plus millions of pending changes ran out of memory);
        # build_dims swaps the new tables in at the end, so a failure keeps the previous filter tables
        log("Rebuilding filter tables (industry tree, states, cities) ...")
        try:
            build_dims(con)
        except Exception as exc:
            stats["dims_error"] = str(exc)
            log(f"  WARNING: the new data is installed, but the filter tables could not be rebuilt ({exc}). "
                "All Leads works; its filter-list counts stay as they were until the next update rebuilds them.")
        else:
            con.execute("CHECKPOINT")   # outside the try: once the swap is committed, a failure here is not a failed rebuild
    stats["seconds"] = round(time.time() - t0)
    if dedupe:
        log(f"Done: {source} {version}: {before:,} -> {after:,} rows (+{fresh:,} fresh leads added, "
            f"{skipped_total:,} old skipped, {updated:,} updated) in {stats['seconds']}s")
    else:
        closed = f", {stats['deactivated_closed']:,} deactivated closed" if "deactivated_closed" in stats else ""
        log(f"Done: {source} {version}: {before:,} -> {after:,} rows (+{fresh:,} added under the every provider rule, "
            f"{skipped_total:,} skipped with no phone or email, {updated:,} updated{closed}) in {stats['seconds']}s")
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
