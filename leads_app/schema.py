"""
Shared SQL helpers for Leads Explorer: normalization macros used by both the
migration and the running app. Macros are (re)created on every connection.
"""
US_STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY "
    "PR VI GU AS MP"
).split()
CA_PROVINCES = "AB BC MB NB NL NS NT NU ON PE QC SK YT".split()
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "calif": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "washington dc": "DC", "washington d.c.": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "puerto rico": "PR", "guam": "GU", "virgin islands": "VI", "american samoa": "AS", "northern mariana islands": "MP",
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB", "new brunswick": "NB", "newfoundland and labrador": "NL",
    "nova scotia": "NS", "northwest territories": "NT", "nunavut": "NU", "ontario": "ON", "prince edward island": "PE",
    "quebec": "QC", "québec": "QC", "saskatchewan": "SK", "yukon": "YT",
}

_VALID = ", ".join(f"'{s}'" for s in US_STATES + CA_PROVINCES)
_NAMES = " ".join(f"WHEN lower(trim(s)) = '{k}' THEN '{v}'" for k, v in STATE_NAMES.items())

STATE_SQL = f"""
CREATE OR REPLACE MACRO norm_state(s) AS (
    CASE
        WHEN s IS NULL OR trim(s) = '' THEN NULL
        WHEN upper(trim(s)) IN ({_VALID}) THEN upper(trim(s))
        WHEN replace(replace(upper(trim(s)), '.', ''), ' ', '') IN ({_VALID}) THEN replace(replace(upper(trim(s)), '.', ''), ' ', '')
        WHEN upper(trim(s)) LIKE 'US-__' AND substr(upper(trim(s)), 4, 2) IN ({_VALID}) THEN substr(upper(trim(s)), 4, 2)
        WHEN upper(trim(s)) LIKE 'CA-__' AND substr(upper(trim(s)), 4, 2) IN ({_VALID}) THEN substr(upper(trim(s)), 4, 2)
        {_NAMES}
        ELSE trim(s)
    END
)
"""

# Canonical US phone: digits only; drop a leading 1 from 11-digit numbers; keep only exactly 10 digits.
PHONE_SQL = """
CREATE OR REPLACE MACRO norm_phone(x) AS (
    CASE
        WHEN x IS NULL THEN NULL
        WHEN length(regexp_replace(CAST(x AS VARCHAR), '[^0-9]', '', 'g')) = 10
             THEN regexp_replace(CAST(x AS VARCHAR), '[^0-9]', '', 'g')
        WHEN length(regexp_replace(CAST(x AS VARCHAR), '[^0-9]', '', 'g')) = 11
             AND regexp_replace(CAST(x AS VARCHAR), '[^0-9]', '', 'g') LIKE '1%'
             THEN substr(regexp_replace(CAST(x AS VARCHAR), '[^0-9]', '', 'g'), 2)
        ELSE NULL
    END
)
"""

EMAIL_SQL = """
CREATE OR REPLACE MACRO norm_email(x) AS (
    CASE WHEN x IS NULL THEN NULL
         WHEN lower(trim(CAST(x AS VARCHAR))) LIKE '%_@_%.__%' THEN lower(trim(CAST(x AS VARCHAR)))
         ELSE NULL END
)
"""


def install_macros(con) -> None:
    con.execute(PHONE_SQL)
    con.execute(EMAIL_SQL)
    con.execute(STATE_SQL)


# ---------------------------------------------------------------------------
# Schema v2 store tables (used by build_db.py for fresh builds; migrate_v2.py
# creates the same layout from the SQLite contacts.db).
# ---------------------------------------------------------------------------
USED_CONTACT_COLUMNS = [
    "name", "email", "address_line_1", "address_line_2", "city", "state", "postal_code", "country",
    "phone", "fax", "website", "industry", "original_industry", "category", "alternate_categories",
    "credentials", "specialty", "rating", "reviews", "npi", "enumeration_date", "source", "source_file",
    "source_type", "source_sheet", "source_category", "uploaded_at", "raw_data", "created_at",
]

USED_CONTACTS_DDL = "CREATE TABLE IF NOT EXISTS used_contacts (id BIGINT, " + \
    ", ".join(f"{c} VARCHAR" for c in USED_CONTACT_COLUMNS) + ", import_id BIGINT)"

HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS history (
    id BIGINT, kind VARCHAR, filename VARCHAR, occurred_at VARCHAR,
    source VARCHAR, industry VARCHAR, source_type VARCHAR, source_category VARCHAR, category VARCHAR,
    rows INTEGER, rows_failed INTEGER, rows_skipped INTEGER, places_marked INTEGER,
    mapping_json VARCHAR, filters_json VARCHAR, error_sample VARCHAR, output_path VARCHAR
)
"""

CATALOGS_DDL = "CREATE TABLE IF NOT EXISTS catalogs (kind VARCHAR, name VARCHAR, created_at VARCHAR)"


def create_stores(con, used_seq_start: int = 1, history_seq_start: int = 1, stamp: str = "") -> None:
    """Create the Used Data / history / catalog tables, sequences, indexes and meta for a v2 database."""
    con.execute(USED_CONTACTS_DDL)
    con.execute(HISTORY_DDL)
    con.execute(CATALOGS_DDL)
    con.execute(f"CREATE SEQUENCE IF NOT EXISTS seq_used_contacts START {int(used_seq_start)}")
    con.execute(f"CREATE SEQUENCE IF NOT EXISTS seq_history START {int(history_seq_start)}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_used_phone ON used_contacts(phone)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_used_email ON used_contacts(email)")
    con.execute("INSERT INTO catalogs SELECT 'source_type', v, ? FROM (VALUES ('Med'), ('Not Med')) t(v) "
                "WHERE NOT EXISTS (SELECT 1 FROM catalogs WHERE kind = 'source_type' AND name = v)", [stamp])
    con.execute("INSERT INTO catalogs SELECT 'source_category', v, ? FROM (VALUES ('medical'), ('non_medical')) t(v) "
                "WHERE NOT EXISTS (SELECT 1 FROM catalogs WHERE kind = 'source_category' AND name = v)", [stamp])
    con.execute("CREATE TABLE IF NOT EXISTS meta (key VARCHAR, value VARCHAR)")
    con.execute("DELETE FROM meta WHERE key = 'schema_version'")
    con.execute("INSERT INTO meta VALUES ('schema_version', '2')")


PLACES_DDL = """
CREATE TABLE IF NOT EXISTS places (
    source VARCHAR, source_id VARCHAR, business_name VARCHAR, phone VARCHAR, website VARCHAR, email VARCHAR,
    address VARCHAR, city VARCHAR, state VARCHAR, zip VARCHAR, country VARCHAR,
    latitude DOUBLE, longitude DOUBLE, categories VARCHAR, instagram VARCHAR, twitter VARCHAR, facebook_id VARCHAR,
    date_created VARCHAR, date_refreshed VARCHAR, date_closed VARCHAR,
    category_list VARCHAR[], industry_list VARCHAR[], is_open BOOLEAN, used_at VARCHAR, used_reason VARCHAR
)
"""


def create_empty_db(path, stamp: str = "") -> None:
    """Create a brand-new, empty v2 database: no leads, no used contacts, default catalogs only."""
    import duckdb

    path = str(path)
    con = duckdb.connect(path)
    install_macros(con)
    con.execute(PLACES_DDL)
    build_dims(con)
    create_stores(con, stamp=stamp)
    con.execute("CHECKPOINT")
    con.close()


def build_tree(con) -> None:
    """Industry tree: every prefix of every 'A > B > C' category, per source, with the number of
    places under it. Level 1 = main industry, level 2+ = sub-industries."""
    con.execute("DROP TABLE IF EXISTS dim_tree")
    con.execute("""
        CREATE TABLE dim_tree AS
        SELECT source, path, level, parent, name, count(DISTINCT source_id) AS n
        FROM (
            SELECT source, source_id, lvl AS level,
                   array_to_string(segs[1:lvl], ' > ') AS path,
                   CASE WHEN lvl = 1 THEN NULL ELSE array_to_string(segs[1:lvl - 1], ' > ') END AS parent,
                   segs[lvl] AS name
            FROM (
                SELECT source, source_id, segs, unnest(generate_series(1, len(segs))) AS lvl
                FROM (SELECT source, source_id, string_split(unnest(category_list), ' > ') AS segs FROM places)
            )
        )
        GROUP BY 1, 2, 3, 4, 5
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_tree_parent ON dim_tree(parent)")


def build_dims(con) -> None:
    """(Re)build the dim_* lookup tables that drive the All Leads filter lists."""
    for t in ("dim_source", "dim_industry", "dim_category", "dim_state", "dim_city", "dim_country"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    build_tree(con)
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
