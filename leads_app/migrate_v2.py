r"""
One-time migration: build leads_v2.duckdb from the existing leads.duckdb plus
ContactDirectory's SQLite contacts.db. The original files are only read.

  places          25M open-source leads. phone -> canonical 10 digits (else NULL),
                  email lowercased, blank strings -> NULL, state codes normalized,
                  new columns used_at / used_reason.
  used_contacts   1.7M contacts from ContactDirectory (same 27 fields + import_id).
  history         import_history (kind='import') and future exports (kind='export').
  catalogs        sources / industries / source_types / source_categories / categories.

Then every place whose phone or email matches a used contact is marked used.
Run:  python migrate_v2.py     (close the app first)
"""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OLD = HERE / "leads.duckdb"
NEW = HERE / "leads_v2.duckdb"
SQLITE = ROOT / "data" / "contacts.db"   # the old ContactDirectory SQLite file

sys.path.insert(0, str(HERE))
from schema import install_macros  # noqa: E402


def p(path: Path) -> str:
    return str(path).replace("\\", "/")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    if NEW.exists():
        NEW.unlink()
    for f in (OLD, SQLITE):
        if not f.exists():
            raise SystemExit(f"Missing input: {f}")
    t0 = time.time()
    con = duckdb.connect(str(NEW))
    con.execute("INSTALL sqlite; LOAD sqlite")
    con.execute(f"ATTACH '{p(OLD)}' AS old (READ_ONLY)")
    con.execute(f"ATTACH '{p(SQLITE)}' AS sq (TYPE sqlite, READ_ONLY)")
    install_macros(con)

    print("1/6 places: normalizing phone/email/state and adding usage columns...", flush=True)
    con.execute("""
        CREATE TABLE places AS
        SELECT source, source_id, business_name,
               norm_phone(phone)                    AS phone,
               nullif(trim(website), '')            AS website,
               norm_email(email)                    AS email,
               nullif(trim(address), '')            AS address,
               nullif(trim(city), '')               AS city,
               norm_state(state)                    AS state,
               nullif(trim(zip), '')                AS zip,
               country, latitude, longitude, categories, instagram, twitter, facebook_id,
               date_created, date_refreshed, date_closed, category_list, industry_list, is_open,
               NULL::VARCHAR AS used_at, NULL::VARCHAR AS used_reason
        FROM old.places
    """)
    old_phone = con.execute("SELECT count(phone) FROM old.places").fetchone()[0]
    new_phone = con.execute("SELECT count(phone) FROM places").fetchone()[0]
    print(f"    rows {con.execute('SELECT count(*) FROM places').fetchone()[0]:,} | phones {old_phone:,} -> {new_phone:,} "
          f"(blanked {old_phone - new_phone:,} invalid) | {time.time() - t0:.0f}s", flush=True)

    print("2/6 used_contacts from SQLite...", flush=True)
    con.execute("""
        CREATE TABLE used_contacts AS
        SELECT id::BIGINT AS id,
               nullif(trim(name), '') AS name, norm_email(email) AS email,
               nullif(trim(address_line_1), '') AS address_line_1, nullif(trim(address_line_2), '') AS address_line_2,
               nullif(trim(city), '') AS city, norm_state(state) AS state,
               nullif(trim(postal_code), '') AS postal_code, nullif(upper(trim(country)), '') AS country,
               norm_phone(phone) AS phone, nullif(trim(fax), '') AS fax, nullif(trim(website), '') AS website,
               nullif(trim(industry), '') AS industry, nullif(trim(original_industry), '') AS original_industry,
               nullif(trim(category), '') AS category, nullif(trim(alternate_categories), '') AS alternate_categories,
               nullif(trim(credentials), '') AS credentials, nullif(trim(specialty), '') AS specialty,
               nullif(trim(rating), '') AS rating, nullif(trim(reviews), '') AS reviews, nullif(trim(npi), '') AS npi,
               nullif(trim(enumeration_date), '') AS enumeration_date,
               nullif(trim(source), '') AS source, nullif(trim(source_file), '') AS source_file,
               nullif(trim(source_type), '') AS source_type, nullif(trim(source_sheet), '') AS source_sheet,
               nullif(trim(source_category), '') AS source_category,
               nullif(trim(uploaded_at), '') AS uploaded_at, raw_data, nullif(trim(created_at), '') AS created_at,
               NULL::BIGINT AS import_id
        FROM sq.contacts
    """)
    n_used = con.execute("SELECT count(*) FROM used_contacts").fetchone()[0]
    max_id = con.execute("SELECT coalesce(max(id), 0) FROM used_contacts").fetchone()[0]
    con.execute(f"CREATE SEQUENCE seq_used_contacts START {max_id + 1}")
    print(f"    {n_used:,} contacts | {time.time() - t0:.0f}s", flush=True)

    print("3/6 history + catalogs...", flush=True)
    con.execute("""
        CREATE TABLE history (
            id BIGINT, kind VARCHAR, filename VARCHAR, occurred_at VARCHAR,
            source VARCHAR, industry VARCHAR, source_type VARCHAR, source_category VARCHAR, category VARCHAR,
            rows INTEGER, rows_failed INTEGER, rows_skipped INTEGER, places_marked INTEGER,
            mapping_json VARCHAR, filters_json VARCHAR, error_sample VARCHAR, output_path VARCHAR
        )
    """)
    con.execute("""
        INSERT INTO history
        SELECT id::BIGINT, 'import', original_filename, coalesce(nullif(uploaded_at, ''), imported_at),
               nullif(source, ''), nullif(industry, ''), nullif(source_type, ''), nullif(source_category, ''), nullif(category, ''),
               rows_imported, rows_failed, 0, NULL, mapping_json, NULL, nullif(error_sample, ''), NULL
        FROM sq.import_history
    """)
    max_h = con.execute("SELECT coalesce(max(id), 0) FROM history").fetchone()[0]
    con.execute(f"CREATE SEQUENCE seq_history START {max_h + 1}")
    con.execute("""
        UPDATE used_contacts SET import_id = h.id
        FROM history h
        WHERE used_contacts.source_file = h.filename AND used_contacts.uploaded_at = h.occurred_at
    """)
    linked = con.execute("SELECT count(import_id) FROM used_contacts").fetchone()[0]
    con.execute("CREATE TABLE catalogs (kind VARCHAR, name VARCHAR, created_at VARCHAR)")
    for kind, table in (("source", "sources"), ("industry", "industries"), ("source_type", "source_types"),
                        ("source_category", "source_categories"), ("category", "categories")):
        con.execute(f"INSERT INTO catalogs SELECT '{kind}', name, created_at FROM sq.{table}")
    # make sure every value actually used in contacts is in its catalog
    for kind in ("source", "industry", "source_type", "source_category", "category"):
        con.execute(f"""
            INSERT INTO catalogs
            SELECT DISTINCT '{kind}', {kind}, ? FROM used_contacts u
            WHERE {kind} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM catalogs c WHERE c.kind = '{kind}' AND c.name = u.{kind})
        """, [now()])
    print(f"    history {con.execute('SELECT count(*) FROM history').fetchone()[0]} rows, contacts linked to imports {linked:,}, "
          f"catalog entries {con.execute('SELECT count(*) FROM catalogs').fetchone()[0]:,}", flush=True)

    print("4/6 marking places already used (phone / email match)...", flush=True)
    t1 = time.time()
    stamp = now()
    con.execute("""
        UPDATE places SET used_at = ?, used_reason = 'match:legacy'
        WHERE used_at IS NULL AND phone IN (SELECT DISTINCT phone FROM used_contacts WHERE phone IS NOT NULL)
    """, [stamp])
    by_phone = con.execute("SELECT count(used_at) FROM places").fetchone()[0]
    con.execute("""
        UPDATE places SET used_at = ?, used_reason = 'match:legacy'
        WHERE used_at IS NULL AND email IN (SELECT DISTINCT email FROM used_contacts WHERE email IS NOT NULL)
    """, [stamp])
    total_used = con.execute("SELECT count(used_at) FROM places").fetchone()[0]
    print(f"    by phone {by_phone:,}, by email +{total_used - by_phone:,} = {total_used:,} used places | {time.time() - t1:.0f}s", flush=True)

    print("5/6 dimension tables + indexes...", flush=True)
    con.execute("CREATE TABLE dim_source AS SELECT source, count(*) AS n FROM places GROUP BY 1 ORDER BY n DESC")
    con.execute("""
        CREATE TABLE dim_industry AS
        SELECT source, industry, count(*) AS n
        FROM (SELECT source, unnest(industry_list) AS industry FROM places) GROUP BY 1, 2 ORDER BY n DESC
    """)
    con.execute("""
        CREATE TABLE dim_category AS
        SELECT source, category, split_part(category, ' > ', 1) AS industry, count(*) AS n
        FROM (SELECT source, unnest(category_list) AS category FROM places) GROUP BY 1, 2, 3 ORDER BY n DESC
    """)
    con.execute("CREATE TABLE dim_state AS SELECT source, state, count(*) AS n FROM places WHERE state IS NOT NULL GROUP BY 1, 2 ORDER BY n DESC")
    con.execute("CREATE TABLE dim_city AS SELECT source, state, city, count(*) AS n FROM places WHERE state IS NOT NULL AND city IS NOT NULL GROUP BY 1, 2, 3 ORDER BY n DESC")
    con.execute("CREATE TABLE dim_country AS SELECT source, country, count(*) AS n FROM places WHERE country IS NOT NULL GROUP BY 1, 2 ORDER BY n DESC")
    con.execute("CREATE INDEX idx_used_phone ON used_contacts(phone)")
    con.execute("CREATE INDEX idx_used_email ON used_contacts(email)")
    con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
    con.execute("INSERT INTO meta VALUES ('schema_version', '2'), ('migrated_at', ?)", [now()])

    print("6/6 checkpoint...", flush=True)
    con.execute("DETACH old")
    con.execute("DETACH sq")
    con.execute("CHECKPOINT")
    con.close()
    print(f"\nDone in {(time.time() - t0) / 60:.1f} min -> {NEW} ({NEW.stat().st_size / 1e9:.2f} GB)")
    print("Next: rename leads.duckdb -> leads_v1_backup.duckdb and leads_v2.duckdb -> leads.duckdb")


if __name__ == "__main__":
    main()
