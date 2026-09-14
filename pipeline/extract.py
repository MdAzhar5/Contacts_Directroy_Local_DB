"""
Turn a raw release of each source into one parquet file with the common contact layout:

  source_id, business_name, phone, website, email, address, city, state, zip, country,
  latitude, longitude, categories, instagram, twitter, facebook_id,
  date_created, date_refreshed, date_closed

Rows keep at least one of phone / email (Foursquare also facebook_id); phones and emails
are deduplicated within the source, keeping the most complete, still-open row.
The app's own normalization (10-digit phones, lowercase emails, state codes) is applied
later, when the file is loaded by build_db.py or merged by an update.

  extract_fsq(parquet_dir, out)        Foursquare OS Places parquet files (any release)
  extract_overture(src_dir, out)       Overture places theme parquet files
  extract_osm(pbf_or_dir, out)         one .osm.pbf, or a folder of state extracts
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
GEO = DATA / "geo_lookup.duckdb"

VALID_STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY "
    "PR VI GU AS MP"
).split()

OUT_COLUMNS = """source_id, business_name, phone, website, email, address, city, state, zip, country,
                 latitude, longitude, categories, instagram, twitter, facebook_id,
                 date_created, date_refreshed, date_closed"""


def p(path) -> str:
    return str(path).replace("\\", "/")


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"PRAGMA threads={max(2, os.cpu_count() or 4)}")
    # "car_repair" -> "Car repair"
    con.execute("CREATE MACRO pretty(x) AS upper(substr(replace(x, '_', ' '), 1, 1)) || substr(replace(x, '_', ' '), 2)")
    return con


def _fill_from_geo(con, table: str, geo: Path = GEO) -> None:
    """Fill a missing state from the ZIP prefix, then from the lat/lon cell; missing city from the cell."""
    if not geo.exists():
        print("  (geo_lookup.duckdb not found; missing states stay empty)")
        return
    con.execute(f"ATTACH '{p(geo)}' AS geo (READ_ONLY)")
    con.execute(f"""
        UPDATE {table} SET state = z.state FROM geo.zip3_state z
        WHERE {table}.state IS NULL AND {table}.zip IS NOT NULL AND substr({table}.zip, 1, 3) = z.zip3
    """)
    con.execute(f"""
        UPDATE {table} SET state = c.state FROM geo.cell_state c
        WHERE {table}.state IS NULL AND {table}.latitude IS NOT NULL
          AND round({table}.latitude * 20) = c.cell_lat AND round({table}.longitude * 20) = c.cell_lon
    """)
    con.execute(f"""
        UPDATE {table} SET city = c.city FROM geo.cell_state c
        WHERE {table}.city IS NULL AND {table}.latitude IS NOT NULL
          AND round({table}.latitude * 20) = c.cell_lat AND round({table}.longitude * 20) = c.cell_lon
    """)
    con.execute("DETACH geo")


def _dedupe_and_write(con, table: str, out: Path, order: str, keep_facebook_only: bool = False) -> dict:
    """Blank junk phones, drop rows without a contact, dedupe on phone then email, write parquet."""
    contact = "phone IS NULL AND email IS NULL" + (" AND facebook_id IS NULL" if keep_facebook_only else "")
    con.execute(f"""
        CREATE TABLE cleaned AS
        SELECT *,
               nullif(regexp_replace(coalesce(phone, ''), '[^0-9]', '', 'g'), '') AS phone_key,
               nullif(lower(trim(email)), '') AS email_key,
               (phone IS NOT NULL)::INT + (email IS NOT NULL)::INT + (website IS NOT NULL)::INT
             + (facebook_id IS NOT NULL)::INT + (address IS NOT NULL)::INT + (city IS NOT NULL)::INT AS completeness
        FROM {table}
    """)
    con.execute("UPDATE cleaned SET phone = NULL, phone_key = NULL WHERE length(phone_key) < 7")
    con.execute("UPDATE cleaned SET email = NULL, email_key = NULL WHERE email_key IS NULL")
    con.execute(f"DELETE FROM cleaned WHERE {contact}")
    n0 = con.execute("SELECT count(*) FROM cleaned").fetchone()[0]
    con.execute(f"""
        CREATE TABLE d1 AS SELECT * EXCLUDE (rn) FROM (
            SELECT *, row_number() OVER (PARTITION BY phone_key ORDER BY {order}) rn FROM cleaned
        ) WHERE phone_key IS NULL OR rn = 1
    """)
    con.execute(f"""
        CREATE TABLE d2 AS SELECT * EXCLUDE (rn, phone_key, email_key, completeness) FROM (
            SELECT *, row_number() OVER (PARTITION BY email_key ORDER BY {order}) rn FROM d1
        ) WHERE email_key IS NULL OR rn = 1
    """)
    out.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY (SELECT {OUT_COLUMNS} FROM d2 ORDER BY state, city, business_name) TO '{p(out)}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    s = con.execute("""
        SELECT count(*), count(phone), count(email), count(website), count(facebook_id), count(state), count(city),
               count(*) FILTER (WHERE date_closed IS NULL)
        FROM d2
    """).fetchone()
    stats = {"rows": s[0], "before_dedup": n0, "phone": s[1], "email": s[2], "website": s[3], "facebook": s[4],
             "state": s[5], "city": s[6], "open": s[7], "out": str(out)}
    print(f"  rows {s[0]:,} (before dedup {n0:,}) | phone {s[1]:,} | email {s[2]:,} | website {s[3]:,} | state {s[5]:,} | open {s[7]:,}")
    return stats


# ---------------------------------------------------------------------------
# Foursquare OS Places
# ---------------------------------------------------------------------------
def extract_fsq(parquet_dir: Path, out: Path) -> dict:
    """USA rows with a phone, email or Facebook id from a Foursquare release's places/parquet folder."""
    files = sorted(Path(parquet_dir).glob("*.parquet"))
    if not files:
        raise SystemExit(f"No parquet files in {parquet_dir}")
    con = _connect()
    t0 = time.time()
    print(f"Foursquare: reading {len(files)} files, filtering to USA rows with a contact field...")
    con.execute(f"""
        CREATE TABLE places AS
        SELECT fsq_place_id                                    AS source_id,
               nullif(trim(name), '')                          AS business_name,
               nullif(trim(tel), '')                           AS phone,
               nullif(trim(website), '')                       AS website,
               nullif(trim(email), '')                         AS email,
               nullif(trim(address), '')                       AS address,
               nullif(trim(locality), '')                      AS city,
               nullif(trim(region), '')                        AS state,
               nullif(trim(postcode), '')                      AS zip,
               country,
               TRY_CAST(latitude AS DOUBLE)                    AS latitude,
               TRY_CAST(longitude AS DOUBLE)                   AS longitude,
               array_to_string(fsq_category_labels, ' | ')     AS categories,
               nullif(trim(CAST(instagram AS VARCHAR)), '')    AS instagram,
               nullif(trim(CAST(twitter AS VARCHAR)), '')      AS twitter,
               nullif(trim(CAST(facebook_id AS VARCHAR)), '')  AS facebook_id,
               CAST(date_created AS VARCHAR)                   AS date_created,
               CAST(date_refreshed AS VARCHAR)                 AS date_refreshed,
               CAST(date_closed AS VARCHAR)                    AS date_closed
        FROM read_parquet('{p(Path(parquet_dir) / "*.parquet")}')
        WHERE country = 'US' AND (tel IS NOT NULL OR email IS NOT NULL OR facebook_id IS NOT NULL)
    """)
    print(f"  {con.execute('SELECT count(*) FROM places').fetchone()[0]:,} rows in {(time.time() - t0) / 60:.1f} min")
    stats = _dedupe_and_write(con, "places", out,
                              "(date_closed IS NULL) DESC, completeness DESC, date_refreshed DESC, source_id",
                              keep_facebook_only=True)
    con.close()
    print(f"Foursquare done in {(time.time() - t0) / 60:.1f} min -> {out}")
    return stats


# ---------------------------------------------------------------------------
# Overture Maps places
# ---------------------------------------------------------------------------
def extract_overture(src_dir: Path, out: Path, geo: Path = GEO) -> dict:
    files = sorted(Path(src_dir).glob("*.parquet"))
    if not files:
        raise SystemExit(f"No Overture parquet files in {src_dir}")
    con = _connect()
    con.execute("INSTALL spatial; LOAD spatial")
    t0 = time.time()
    print(f"Overture: reading {len(files)} files, filtering to US places with phone or email...")
    # GeoParquet files come back as GEOMETRY; a plain parquet keeps the raw WKB blob
    types = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{p(Path(src_dir) / '*.parquet')}')").fetchall()}
    geom = "geometry" if types.get("geometry") == "GEOMETRY" else "ST_GeomFromWKB(geometry)"
    con.execute(f"""
        CREATE TABLE places AS
        SELECT
            'overture/' || id                                                       AS source_id,
            names.primary                                                           AS business_name,
            phones[1]                                                               AS phone,
            websites[1]                                                             AS website,
            emails[1]                                                               AS email,
            addresses[1].freeform                                                   AS address,
            addresses[1].locality                                                   AS city,
            CASE WHEN addresses[1].region LIKE 'US-%' THEN substr(addresses[1].region, 4)
                 ELSE addresses[1].region END                                       AS state,
            addresses[1].postcode                                                   AS zip,
            'US'                                                                    AS country,
            ST_Y({geom})                                                            AS latitude,
            ST_X({geom})                                                            AS longitude,
            CASE WHEN taxonomy.hierarchy IS NOT NULL AND len(taxonomy.hierarchy) > 0
                 THEN list_aggregate(list_transform(taxonomy.hierarchy, x -> pretty(x)), 'string_agg', ' > ')
                 WHEN categories.primary IS NOT NULL THEN 'Other > ' || pretty(categories.primary)
                 END                                                                AS categories,
            list_filter(socials, s -> s ILIKE '%instagram.com%')[1]                 AS instagram,
            list_filter(socials, s -> s ILIKE '%twitter.com%' OR s ILIKE '%x.com/%')[1] AS twitter,
            list_filter(socials, s -> s ILIKE '%facebook.com%')[1]                  AS facebook_id,
            NULL::VARCHAR                                                           AS date_created,
            NULL::VARCHAR                                                           AS date_refreshed,
            CASE WHEN operating_status = 'permanently_closed' THEN 'closed' END    AS date_closed
        FROM read_parquet('{p(Path(src_dir) / "*.parquet")}')
        WHERE addresses[1].country = 'US'
          AND names.primary IS NOT NULL
          AND (len(phones) > 0 OR len(emails) > 0)
    """)
    con.execute("UPDATE places SET state = NULL WHERE trim(state) = ''")
    con.execute("UPDATE places SET city = NULL WHERE trim(city) = ''")
    con.execute("UPDATE places SET zip = NULL WHERE trim(zip) = ''")
    print(f"  {con.execute('SELECT count(*) FROM places').fetchone()[0]:,} rows in {(time.time() - t0) / 60:.1f} min")
    _fill_from_geo(con, "places", geo)
    stats = _dedupe_and_write(con, "places", out, "(date_closed IS NULL) DESC, completeness DESC, source_id")
    con.close()
    print(f"Overture done in {(time.time() - t0) / 60:.1f} min -> {out}")
    return stats


# ---------------------------------------------------------------------------
# OpenStreetMap
# ---------------------------------------------------------------------------
BUSINESS_KEYS = [
    ("shop", "Retail"), ("amenity", "Amenity"), ("office", "Office"), ("craft", "Craft"),
    ("healthcare", "Healthcare"), ("tourism", "Tourism"), ("leisure", "Leisure"), ("club", "Club"),
    ("industrial", "Industrial"),
]
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR", "guam": "GU",
    "virgin islands": "VI", "american samoa": "AS", "northern mariana islands": "MP",
}


def extract_osm(pbf: Path, out: Path, geo: Path = GEO) -> dict:
    """Named businesses with a phone or email from one .osm.pbf, or from every *-latest.osm.pbf in a folder."""
    pbf = Path(pbf)
    if not pbf.exists():
        raise SystemExit(f"PBF not found: {pbf}")
    pbfs = sorted(pbf.glob("*.osm.pbf")) if pbf.is_dir() else [pbf]
    if len(pbfs) > 1:
        pbfs = [f for f in pbfs if f.name != "us-latest.osm.pbf"]
    if not pbfs:
        raise SystemExit(f"No .osm.pbf files found in {pbf}")
    con = _connect()
    con.execute("INSTALL spatial; LOAD spatial")
    t0 = time.time()

    biz_keys_sql = " OR ".join(f"tags['{k}'] IS NOT NULL" for k, _ in BUSINESS_KEYS)
    contact_sql = ("(tags['phone'] IS NOT NULL OR tags['contact:phone'] IS NOT NULL OR tags['contact:mobile'] IS NOT NULL "
                   "OR tags['mobile'] IS NOT NULL OR tags['email'] IS NOT NULL OR tags['contact:email'] IS NOT NULL)")
    cat_case = "CASE " + " ".join(
        f"WHEN tags['{k}'] IS NOT NULL THEN '{lbl} > ' || pretty(split_part(tags['{k}'], ';', 1))" for k, lbl in BUSINESS_KEYS
    ) + " END"

    print(f"OSM pass 1: scanning {len(pbfs)} file(s) for named businesses with phone/email...")
    con.execute("CREATE TABLE raw (kind VARCHAR, id BIGINT, lat DOUBLE, lon DOUBLE, refs BIGINT[], tags MAP(VARCHAR, VARCHAR), src VARCHAR)")
    for f in pbfs:
        tf = time.time()
        con.execute(f"""
            INSERT INTO raw
            SELECT kind, id, lat, lon, refs, tags, '{f.name}'
            FROM st_readosm('{p(f)}')
            WHERE kind IN ('node', 'way') AND tags['name'] IS NOT NULL AND ({biz_keys_sql}) AND {contact_sql}
        """)
        print(f"  {f.name}: {con.execute('SELECT count(*) FROM raw WHERE src = ?', [f.name]).fetchone()[0]:,} in {time.time() - tf:.0f}s")

    print("OSM pass 2: fetching coordinates for way entities...")
    con.execute("CREATE TABLE need_nodes AS SELECT DISTINCT refs[1] AS nid, src FROM raw WHERE kind='way' AND len(refs) > 0")
    con.execute("CREATE TABLE way_coords (nid BIGINT, lat DOUBLE, lon DOUBLE)")
    for f in pbfs:
        con.execute(f"""
            INSERT INTO way_coords
            SELECT n.id, n.lat, n.lon FROM st_readosm('{p(f)}') n
            SEMI JOIN (SELECT nid FROM need_nodes WHERE src = '{f.name}') nn ON n.id = nn.nid
            WHERE n.kind = 'node'
        """)
    con.execute("CREATE TABLE wc2 AS SELECT nid, any_value(lat) lat, any_value(lon) lon FROM way_coords GROUP BY nid")
    con.execute("DROP TABLE way_coords")
    con.execute("ALTER TABLE wc2 RENAME TO way_coords")
    con.execute("CREATE TABLE raw2 AS SELECT * EXCLUDE (rn) FROM (SELECT *, row_number() OVER (PARTITION BY kind, id ORDER BY src) rn FROM raw) WHERE rn = 1")
    con.execute("DROP TABLE raw")
    con.execute("ALTER TABLE raw2 RENAME TO raw")

    valid = ", ".join(f"'{v}'" for v in VALID_STATES)
    state_map_sql = "CASE " + " ".join(f"WHEN lower(trim(s)) = '{k}' THEN '{v}'" for k, v in STATE_NAMES.items()) + \
        f" WHEN upper(trim(s)) IN ({valid}) THEN upper(trim(s))" \
        " WHEN upper(trim(s)) LIKE 'US-__' THEN substr(upper(trim(s)), 4, 2) END"
    con.execute(f"CREATE MACRO norm_state_osm(s) AS ({state_map_sql})")
    con.execute(f"""
        CREATE TABLE places AS
        SELECT
            'osm/' || kind || '/' || id                                        AS source_id,
            tags['name']                                                       AS business_name,
            coalesce(tags['phone'], tags['contact:phone'], tags['contact:mobile'], tags['mobile']) AS phone,
            coalesce(tags['website'], tags['contact:website'], tags['url'])    AS website,
            coalesce(tags['email'], tags['contact:email'])                     AS email,
            nullif(trim(coalesce(tags['addr:housenumber'] || ' ', '') || coalesce(tags['addr:street'], '')), '') AS address,
            tags['addr:city']                                                  AS city,
            norm_state_osm(tags['addr:state'])                                 AS state,
            tags['addr:postcode']                                              AS zip,
            'US'                                                               AS country,
            coalesce(r.lat, w.lat)                                             AS latitude,
            coalesce(r.lon, w.lon)                                             AS longitude,
            {cat_case}                                                         AS categories,
            coalesce(tags['contact:instagram'], tags['instagram'])             AS instagram,
            coalesce(tags['contact:twitter'], tags['twitter'])                 AS twitter,
            coalesce(tags['contact:facebook'], tags['facebook'])               AS facebook_id,
            NULL::VARCHAR AS date_created, NULL::VARCHAR AS date_refreshed, NULL::VARCHAR AS date_closed
        FROM raw r LEFT JOIN way_coords w ON r.kind = 'way' AND r.refs[1] = w.nid
    """)
    _fill_from_geo(con, "places", geo)
    stats = _dedupe_and_write(con, "places", out, "completeness DESC, source_id")
    con.close()
    print(f"OSM done in {(time.time() - t0) / 60:.1f} min -> {out}")
    return stats


EXTRACTORS = {"Foursquare": extract_fsq, "Overture": extract_overture, "OpenStreetMap": extract_osm}
OUTPUT_NAMES = {"Foursquare": "foursquare_usa_contacts.parquet", "Overture": "overture_usa_contacts.parquet",
                "OpenStreetMap": "osm_usa_contacts.parquet"}
