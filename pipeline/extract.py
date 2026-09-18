"""
Turn a raw release of each source into one parquet file with the common contact layout:

  source_id, business_name, phone, website, email, address, city, state, zip, country,
  latitude, longitude, categories, instagram, twitter, facebook_id,
  date_created, date_refreshed, date_closed

Rows keep at least one of phone / email (Foursquare also facebook_id); phones and emails
are deduplicated within the source, keeping the most complete, still-open row.
NPI is the exception: every active US provider is kept, one row per NPI, with no phone/email
dedupe (doctors share clinic phones), followed by eight NPI detail columns.
The app's own normalization (10-digit phones, lowercase emails, state codes) is applied
later, when the file is loaded by build_db.py or merged by an update.

  extract_fsq(parquet_dir, out)        Foursquare OS Places parquet files (any release)
  extract_overture(src_dir, out)       Overture places theme parquet files
  extract_osm(pbf_or_dir, out)         one .osm.pbf, or a folder of state extracts
  extract_npi(zip_path, out)           a CMS NPPES V.2 zip (monthly full file); also writes npi_deactivated.parquet

    python pipeline/extract.py NPI path/to/NPPES_Data_Dissemination_September_2026_V2.zip [out.parquet]
"""
from __future__ import annotations

import csv
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
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


def _work_db(out: Path) -> Path:
    return Path(tempfile.gettempdir()) / f"leads_extract_{Path(out).stem}.duckdb"


def _remove_work_db(out: Path) -> None:
    work = _work_db(out)
    for f in (work, Path(f"{work}.wal")):
        f.unlink(missing_ok=True)
    shutil.rmtree(f"{work}.tmp", ignore_errors=True)


def _connect(out: Path) -> duckdb.DuckDBPyConnection:
    """Work database for one extraction. It is a file in the system temp folder rather than memory, so DuckDB can move
    tables to disk when a release does not fit in RAM (Overture 2026-08 ran out of memory in-memory), and it stays out
    of the OneDrive-synced project folder. _close() deletes it; a leftover from a crashed run is removed on the next one."""
    _remove_work_db(out)
    con = duckdb.connect(str(_work_db(out)))
    con.execute(f"PRAGMA threads={max(2, os.cpu_count() or 4)}")
    con.execute("SET preserve_insertion_order = false")   # every output is written with an explicit ORDER BY
    # "car_repair" -> "Car repair"
    con.execute("CREATE MACRO pretty(x) AS upper(substr(replace(x, '_', ' '), 1, 1)) || substr(replace(x, '_', ' '), 2)")
    return con


def _close(con, out: Path) -> None:
    con.close()
    _remove_work_db(out)


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
    con.execute(f"DROP TABLE {table}")   # one copy of the rows at a time: a full release does not fit in RAM several times over
    con.execute("UPDATE cleaned SET phone = NULL, phone_key = NULL WHERE length(phone_key) < 7")
    con.execute("UPDATE cleaned SET email = NULL, email_key = NULL WHERE email_key IS NULL")
    con.execute(f"DELETE FROM cleaned WHERE {contact}")
    n0 = con.execute("SELECT count(*) FROM cleaned").fetchone()[0]
    con.execute(f"""
        CREATE TABLE d1 AS SELECT * EXCLUDE (rn) FROM (
            SELECT *, row_number() OVER (PARTITION BY phone_key ORDER BY {order}) rn FROM cleaned
        ) WHERE phone_key IS NULL OR rn = 1
    """)
    con.execute("DROP TABLE cleaned")
    con.execute(f"""
        CREATE TABLE d2 AS SELECT * EXCLUDE (rn, phone_key, email_key, completeness) FROM (
            SELECT *, row_number() OVER (PARTITION BY email_key ORDER BY {order}) rn FROM d1
        ) WHERE email_key IS NULL OR rn = 1
    """)
    con.execute("DROP TABLE d1")
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
    con = _connect(out)
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
    _close(con, out)
    print(f"Foursquare done in {(time.time() - t0) / 60:.1f} min -> {out}")
    return stats


# ---------------------------------------------------------------------------
# Overture Maps places
# ---------------------------------------------------------------------------
def extract_overture(src_dir: Path, out: Path, geo: Path = GEO) -> dict:
    files = sorted(Path(src_dir).glob("*.parquet"))
    if not files:
        raise SystemExit(f"No Overture parquet files in {src_dir}")
    con = _connect(out)
    con.execute("INSTALL spatial; LOAD spatial")
    t0 = time.time()
    print(f"Overture: reading {len(files)} files, filtering to US places with phone or email...")
    # GeoParquet files come back as GEOMETRY (DuckDB 1.5+ adds the CRS: GEOMETRY('OGC:CRS84')); a plain parquet keeps the raw WKB blob
    types = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{p(Path(src_dir) / '*.parquet')}')").fetchall()}
    geom = "geometry" if (types.get("geometry") or "").startswith("GEOMETRY") else "ST_GeomFromWKB(geometry)"
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
    _close(con, out)
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
    con = _connect(out)
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
    for t in ("raw", "way_coords", "need_nodes"):
        con.execute(f"DROP TABLE {t}")
    _fill_from_geo(con, "places", geo)
    stats = _dedupe_and_write(con, "places", out, "completeness DESC, source_id")
    _close(con, out)
    print(f"OSM done in {(time.time() - t0) / 60:.1f} min -> {out}")
    return stats


# ---------------------------------------------------------------------------
# NPI (CMS NPPES Data Dissemination, V.2)
# ---------------------------------------------------------------------------
NUCC_PAGE = "https://www.nucc.org/index.php/code-sets-mainmenu-41/provider-taxonomy-mainmenu-40/csv-mainmenu-57"
NUCC_CACHE = DATA / "nucc_taxonomy.csv"
NUCC_COLUMNS = ("Code", "Grouping", "Classification", "Specialization", "Display Name")
NPI_DETAIL_COLUMNS = "npi, provider_type, credential, specialty, taxonomy_code, contact_name, contact_title, contact_phone"
NPI_DEACTIVATED = "npi_deactivated.parquet"
# the columns used from the ~330 in npidata_pfile_*.csv (names as in the V.2 header), plus 15 taxonomy slots
NPI_FIELDS = {
    "npi": "NPI",
    "entity": "Entity Type Code",
    "org_name": "Provider Organization Name (Legal Business Name)",
    "last_name": "Provider Last Name (Legal Name)",
    "first_name": "Provider First Name",
    "middle_name": "Provider Middle Name",
    "credential": "Provider Credential Text",
    "mail_phone": "Provider Business Mailing Address Telephone Number",
    "pl_line1": "Provider First Line Business Practice Location Address",
    "pl_line2": "Provider Second Line Business Practice Location Address",
    "pl_city": "Provider Business Practice Location Address City Name",
    "pl_state": "Provider Business Practice Location Address State Name",
    "pl_postal": "Provider Business Practice Location Address Postal Code",
    "pl_country": "Provider Business Practice Location Address Country Code (If outside U.S.)",
    "pl_phone": "Provider Business Practice Location Address Telephone Number",
    "enumerated": "Provider Enumeration Date",
    "last_update": "Last Update Date",
    "deactivated": "NPI Deactivation Date",
    "reactivated": "NPI Reactivation Date",
    "ao_last": "Authorized Official Last Name",
    "ao_first": "Authorized Official First Name",
    "ao_title": "Authorized Official Title or Position",
    "ao_phone": "Authorized Official Telephone Number",
}
NPI_TAXONOMY_SLOTS = 15
NPI_TAXONOMY_CODE = "Healthcare Provider Taxonomy Code_{}"
NPI_TAXONOMY_SWITCH = "Healthcare Provider Primary Taxonomy Switch_{}"


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "leads-explorer/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def nucc_taxonomy(cache: Path | None = None, page: str | None = None) -> Path:
    """The current NUCC Health Care Provider Taxonomy CSV (code -> grouping / classification / specialization / display
    name). Downloaded from the NUCC CSV page on every NPI extract and cached as data/nucc_taxonomy.csv; when the site is
    unreachable (or the file looks wrong) the cached copy is used with a warning."""
    cache, page = Path(cache or NUCC_CACHE), page or NUCC_PAGE
    try:
        html = _get(page).decode("utf-8", "replace")
        links = re.findall(r"""href=["']([^"']*nucc_taxonomy_(\d+)\.csv)["']""", html, re.I)
        if not links:
            raise ValueError("no nucc_taxonomy_*.csv link on the NUCC CSV page")
        href = max(links, key=lambda link: int(link[1]))[0]       # 261 = version 26.1
        body = _get(urllib.request.urljoin(page, href))
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError:          # older files were Windows-1252
            text = body.decode("cp1252")
        header = next(csv.reader([text.splitlines()[0] if text else ""]), [])
        missing = [c for c in NUCC_COLUMNS if c not in header]
        if missing:
            raise ValueError(f"{href.rsplit('/', 1)[-1]} has no {', '.join(missing)} column")
        cache.parent.mkdir(parents=True, exist_ok=True)
        part = cache.with_name(cache.name + ".part")
        part.write_bytes(text.encode("utf-8"))
        part.replace(cache)
        print(f"  NUCC taxonomy: {href.rsplit('/', 1)[-1]} ({len(text.splitlines()) - 1:,} lines) -> {cache.name}")
    except Exception as exc:
        if not cache.exists():
            raise SystemExit(f"Could not download the NUCC taxonomy from {page} ({exc}) and there is no cached copy at {cache}.")
        print(f"  WARNING: could not download the NUCC taxonomy ({exc}); using the cached copy {cache}")
    return cache


def _npi_unzip_dir(out: Path) -> Path:
    return Path(tempfile.gettempdir()) / f"leads_extract_{Path(out).stem}_csv"


def _unzip_npidata(zip_path: Path, dest: Path) -> Path:
    """Stream the main npidata_pfile_*.csv (not its _fileheader.csv) out of the NPPES zip into dest."""
    with zipfile.ZipFile(zip_path) as z:
        members = [i for i in z.infolist() if re.fullmatch(r"npidata_pfile_.*\.csv", Path(i.filename).name, re.I)
                   and not i.filename.lower().endswith("_fileheader.csv")]
        if not members:
            raise SystemExit(f"No npidata_pfile_*.csv in {zip_path.name}; is it an NPPES Data Dissemination zip?")
        info = max(members, key=lambda i: i.file_size)
        free = shutil.disk_usage(dest).free
        if free < info.file_size + (2 << 30):
            raise SystemExit(f"Not enough free space in {dest.parent} to unzip {Path(info.filename).name}: "
                             f"needs about {(info.file_size + (2 << 30)) / 1e9:.1f} GB, {free / 1e9:.1f} GB free.")
        target = dest / Path(info.filename).name
        tu = time.time()
        print(f"  unzipping {target.name} ({info.file_size / 1e9:.1f} GB) to {dest} ...")
        with z.open(info) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst, 16 << 20)
        print(f"  unzipped in {time.time() - tu:.0f}s")
    return target


def extract_npi(zip_path: Path, out: Path, nucc: Path | None = None) -> dict:
    """Every active NPI (individuals and organizations) with a US practice location, one row per NPI, from a CMS NPPES
    V.2 zip. The CSV is unzipped to the temp folder and deleted again afterwards, also when the extraction fails.
    Also writes npi_deactivated.parquet (source_id, date_closed) next to out, for the merge to close those NPIs."""
    zip_path, out = Path(zip_path), Path(out)
    if not zip_path.exists():
        raise SystemExit(f"NPPES zip not found: {zip_path}")
    t0 = time.time()
    print(f"NPI: reading {zip_path.name} ...")
    nucc = Path(nucc) if nucc else nucc_taxonomy()
    unzip_dir = _npi_unzip_dir(out)
    shutil.rmtree(unzip_dir, ignore_errors=True)      # a leftover from a crashed run
    unzip_dir.mkdir(parents=True)
    con = None
    try:
        csv_path = _unzip_npidata(zip_path, unzip_dir)
        with csv_path.open(encoding="latin-1", newline="") as f:
            header = next(csv.reader(f), [])
        wanted = list(NPI_FIELDS.values()) + [c.format(i) for i in range(1, NPI_TAXONOMY_SLOTS + 1)
                                              for c in (NPI_TAXONOMY_CODE, NPI_TAXONOMY_SWITCH)]
        missing = [c for c in wanted if c not in header]
        if missing:
            raise SystemExit(f"The NPPES file layout changed; {csv_path.name} has no column(s): {', '.join(missing)}")

        con = _connect(out)
        # "SMITH-JONES" -> "Smith-Jones", "O'BRIEN" -> "O'Brien"
        con.execute("""
            CREATE MACRO title_case(x) AS nullif(array_to_string(list_transform(
                string_split(lower(regexp_replace(trim(x), '\\s+', ' ', 'g')), ' '),
                w -> array_to_string(list_transform(string_split(w, '-'),
                    h -> array_to_string(list_transform(string_split(h, ''''),
                        a -> upper(left(a, 1)) || substr(a, 2)), '''')), '-')), ' '), '')
        """)
        con.execute("CREATE MACRO npi_date(x) AS TRY_STRPTIME(x, '%m/%d/%Y')::DATE")
        cols = ",\n".join(f"nullif(trim(\"{src}\"), '') AS {name}" for name, src in NPI_FIELDS.items())
        codes = ", ".join(f"nullif(trim(\"{NPI_TAXONOMY_CODE.format(i)}\"), '')" for i in range(1, NPI_TAXONOMY_SLOTS + 1))
        switches = ", ".join(f"\"{NPI_TAXONOMY_SWITCH.format(i)}\"" for i in range(1, NPI_TAXONOMY_SLOTS + 1))
        for encoding in ("utf-8", "latin-1"):
            reader = (f"read_csv('{p(csv_path)}', header = true, all_varchar = true, delim = ',', quote = '\"', "
                      f"escape = '\"', encoding = '{encoding}')")
            try:
                con.execute(f"""
                    CREATE TABLE raw AS
                    SELECT {cols},
                           [{codes}] AS codes,
                           list_position([{switches}], 'Y') AS primary_slot
                    FROM {reader}
                """)
                break
            except duckdb.Error as exc:
                if encoding != "utf-8" or "unicode" not in str(exc).lower():
                    raise
                print("  the file is not valid UTF-8; reading it as Latin-1 instead")
        n_file = con.execute("SELECT count(*) FROM raw").fetchone()[0]
        print(f"  {n_file:,} NPI records in the file ({(time.time() - t0) / 60:.1f} min)")

        # active = never deactivated, or reactivated on/after the deactivation date; the full file lists deactivated
        # NPIs with the number and the date only
        con.execute("CREATE MACRO npi_active(off, back) AS npi_date(off) IS NULL OR coalesce(npi_date(back) >= npi_date(off), false)")
        deactivated_out = out.with_name(NPI_DEACTIVATED)
        out.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"""
            COPY (SELECT npi AS source_id, strftime(npi_date(deactivated), '%Y-%m-%d') AS date_closed FROM raw
                  WHERE npi IS NOT NULL AND NOT npi_active(deactivated, reactivated) ORDER BY npi)
            TO '{p(deactivated_out)}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        # taxonomies without repeats, primary (switch 'Y', else the first one) first
        con.execute("""
            CREATE TABLE prov AS
            SELECT * EXCLUDE (tax_list, codes, primary_slot),
                   CASE WHEN primary_code IS NULL THEN []::VARCHAR[]
                        ELSE list_prepend(primary_code, list_filter(tax_list, x -> x <> primary_code)) END AS tax_list
            FROM (
                SELECT *, coalesce(codes[primary_slot], tax_list[1]) AS primary_code
                FROM (SELECT * EXCLUDE (deactivated, reactivated),
                             list_filter(codes, (x, i) -> x IS NOT NULL AND list_position(codes, x) = i) AS tax_list
                      FROM raw
                      WHERE npi IS NOT NULL AND entity IN ('1', '2') AND pl_country = 'US' AND npi_active(deactivated, reactivated))
            )
        """)
        con.execute("DROP TABLE raw")
        con.execute(f"""
            CREATE TABLE nucc AS
            SELECT trim(Code) AS code,
                   'Healthcare > ' || trim(Grouping) || ' > ' || trim(Classification)
                       || coalesce(' > ' || nullif(trim(Specialization), ''), '') AS label,
                   nullif(trim("Display Name"), '') AS display
            FROM read_csv('{p(nucc)}', header = true, all_varchar = true)
            WHERE nullif(trim(Code), '') IS NOT NULL
        """)
        # every taxonomy of the provider, primary first: "Healthcare > Grouping > Classification[ > Specialization]"
        con.execute("""
            CREATE TABLE cats AS
            SELECT t.npi, string_agg(coalesce(n.label, 'Healthcare > Other > ' || t.code), ' | ' ORDER BY t.pos) AS categories
            FROM (SELECT npi, unnest(tax_list) AS code, unnest(generate_series(1, len(tax_list))) AS pos FROM prov) t
            LEFT JOIN nucc n ON n.code = t.code
            GROUP BY t.npi
        """)
        con.execute(f"""
            COPY (
                SELECT p.npi AS source_id,
                       CASE WHEN p.entity = '2' THEN p.org_name
                            ELSE nullif(concat_ws(' ', title_case(p.first_name),
                                                  CASE WHEN regexp_matches(p.middle_name, '^[A-Za-z]') THEN upper(left(p.middle_name, 1)) || '.' END,
                                                  title_case(p.last_name)), '') END AS business_name,
                       coalesce(p.pl_phone, p.mail_phone)                                     AS phone,
                       NULL::VARCHAR                                                          AS website,
                       NULL::VARCHAR                                                          AS email,
                       nullif(concat_ws(' ', p.pl_line1, p.pl_line2), '')                     AS address,
                       title_case(p.pl_city)                                                  AS city,
                       p.pl_state                                                             AS state,
                       nullif(left(regexp_replace(coalesce(p.pl_postal, ''), '[^0-9]', '', 'g'), 5), '') AS zip,
                       'US'                                                                   AS country,
                       NULL::DOUBLE AS latitude, NULL::DOUBLE AS longitude,
                       c.categories,
                       NULL::VARCHAR AS instagram, NULL::VARCHAR AS twitter, NULL::VARCHAR AS facebook_id,
                       strftime(npi_date(p.enumerated), '%Y-%m-%d')                           AS date_created,
                       strftime(npi_date(p.last_update), '%Y-%m-%d')                          AS date_refreshed,
                       NULL::VARCHAR                                                          AS date_closed,
                       p.npi,
                       CASE WHEN p.entity = '2' THEN 'Organization' ELSE 'Individual' END     AS provider_type,
                       CASE WHEN p.entity = '1' THEN p.credential END                         AS credential,
                       n.display                                                              AS specialty,
                       p.primary_code                                                         AS taxonomy_code,
                       CASE WHEN p.entity = '2' THEN nullif(concat_ws(' ', title_case(p.ao_first), title_case(p.ao_last)), '') END AS contact_name,
                       CASE WHEN p.entity = '2' THEN p.ao_title END                           AS contact_title,
                       CASE WHEN p.entity = '2' THEN p.ao_phone END                           AS contact_phone
                FROM prov p LEFT JOIN cats c ON c.npi = p.npi LEFT JOIN nucc n ON n.code = p.primary_code
                ORDER BY state, city, business_name
            ) TO '{p(out)}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        s = con.execute(f"""
            SELECT count(*), count(*) FILTER (WHERE provider_type = 'Individual'), count(*) FILTER (WHERE provider_type = 'Organization'),
                   count(phone), count(categories), count(specialty), count(contact_phone),
                   (SELECT count(*) FROM read_parquet('{p(deactivated_out)}'))
            FROM read_parquet('{p(out)}')
        """).fetchone()
        stats = {"rows": s[0], "individuals": s[1], "organizations": s[2], "phone": s[3], "categories": s[4], "specialty": s[5],
                 "contact_phone": s[6], "deactivated": s[7], "file_records": n_file,
                 "not_kept": n_file - s[0] - s[7], "out": str(out), "deactivated_out": str(deactivated_out)}
        print(f"  rows {s[0]:,} (individuals {s[1]:,}, organizations {s[2]:,}) | phone {s[3]:,} | specialty {s[5]:,} | "
              f"deactivated {s[7]:,} | other skipped (outside US, no type) {stats['not_kept']:,}")
    finally:
        if con is not None:
            _close(con, out)
        shutil.rmtree(unzip_dir, ignore_errors=True)
        if unzip_dir.exists():
            print(f"  WARNING: could not delete the unzipped NPPES file; delete {unzip_dir} by hand.")
    print(f"NPI done in {(time.time() - t0) / 60:.1f} min -> {out}")
    return stats


EXTRACTORS = {"Foursquare": extract_fsq, "Overture": extract_overture, "OpenStreetMap": extract_osm, "NPI": extract_npi}
OUTPUT_NAMES = {"Foursquare": "foursquare_usa_contacts.parquet", "Overture": "overture_usa_contacts.parquet",
                "OpenStreetMap": "osm_usa_contacts.parquet", "NPI": "npi_usa_contacts.parquet"}


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in EXTRACTORS:
        raise SystemExit(f"usage: python pipeline/extract.py {{{'|'.join(EXTRACTORS)}}} <raw release> [out.parquet]")
    EXTRACTORS[sys.argv[1]](Path(sys.argv[2]), Path(sys.argv[3]) if len(sys.argv) > 3 else DATA / OUTPUT_NAMES[sys.argv[1]])
