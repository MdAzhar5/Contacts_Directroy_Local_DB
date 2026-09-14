r"""
Tests for the data-update pipeline: release listing parsers, the Foursquare and
Overture extractors on tiny synthetic inputs, and the merge into a fixture database
(insert new, update changed, keep unchanged, mark used, record version).
No network access; the real database is never touched.

Run:  python tests/test_update.py
"""
import os
import sys
import tempfile
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "leads_app"))
sys.path.insert(0, str(HERE))

import sources  # noqa: E402
from extract import extract_fsq, extract_overture  # noqa: E402
from merge import get_versions, merge  # noqa: E402
from schema import install_macros  # noqa: E402
from test_api import build_fixture, fs  # noqa: E402

S3_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>b</Name><IsTruncated>false</IsTruncated>
<Contents><Key>release/2026-08-19.0/theme=places/type=place/part-00000.zstd.parquet</Key><Size>634918674</Size></Contents>
<Contents><Key>release/2026-08-19.0/theme=places/type=place/part-00001.zstd.parquet</Key><Size>10</Size></Contents>
<CommonPrefixes><Prefix>release/2026-07-22.0/</Prefix></CommonPrefixes>
<CommonPrefixes><Prefix>release/2026-08-19.0/</Prefix></CommonPrefixes>
</ListBucketResult>"""


def test_parsers():
    prefixes, keys, token = sources.parse_s3_listing(S3_XML)
    assert prefixes == ["release/2026-07-22.0/", "release/2026-08-19.0/"] and token is None
    assert keys[0] == ("release/2026-08-19.0/theme=places/type=place/part-00000.zstd.parquet", 634918674)
    assert sources.version_from_last_modified("Sun, 13 Sep 2026 23:09:25 GMT") == "2026-09-13"
    assert "2026-08-19.0" > "2026-07-22.0" and "2026-09-13" > "2026-08-11"
    assert sources.fmt_size(10.5e9) == "10.5 GB" and sources.fmt_size(35e6) == "35 MB"
    print("parsers OK")


def test_extractors(work: Path):
    con = duckdb.connect()
    # ---- Foursquare: raw release schema subset
    fsq_dir = work / "fsq"
    fsq_dir.mkdir()
    con.execute(f"""
        COPY (
            SELECT * FROM (VALUES
                ('a1', 'Alpha Dental', '(530) 244-4772', 'https://alpha.example', NULL, '1 Main St', 'Redding', 'CA', '96001', 'US', 40.5, -122.3,
                 ['Health and Medicine > Dentist'], NULL, NULL, 111, DATE '2020-01-01', DATE '2026-01-01', NULL::DATE),
                ('a2', 'Alpha Dental dup', '530-244-4772', NULL, NULL, NULL, 'Redding', 'CA', NULL, 'US', 40.5, -122.3,
                 ['Health and Medicine > Dentist'], NULL, NULL, NULL, DATE '2019-01-01', DATE '2025-01-01', NULL::DATE),
                ('b1', 'Beta Cafe', NULL, NULL, 'HELLO@beta.example', '2 Side St', 'Austin', 'TX', '73301', 'US', 30.2, -97.7,
                 ['Dining and Drinking > Cafe'], NULL, NULL, NULL, DATE '2020-01-01', DATE '2026-01-01', NULL::DATE),
                ('c1', 'Gamma Shop', '12', NULL, NULL, NULL, 'Austin', 'TX', NULL, 'US', 30.2, -97.7,
                 ['Retail > Shop'], NULL, NULL, NULL, DATE '2020-01-01', DATE '2026-01-01', NULL::DATE),
                ('d1', 'Delta Canada', '416-555-0100', NULL, NULL, NULL, 'Toronto', 'ON', NULL, 'CA', 43.6, -79.4,
                 ['Retail > Shop'], NULL, NULL, NULL, DATE '2020-01-01', DATE '2026-01-01', NULL::DATE),
                ('e1', 'Epsilon No Contact', NULL, 'https://e.example', NULL, NULL, 'Austin', 'TX', NULL, 'US', 30.2, -97.7,
                 ['Retail > Shop'], NULL, NULL, NULL, DATE '2020-01-01', DATE '2026-01-01', NULL::DATE)
            ) t(fsq_place_id, name, tel, website, email, address, locality, region, postcode, country, latitude, longitude,
                fsq_category_labels, instagram, twitter, facebook_id, date_created, date_refreshed, date_closed)
        ) TO {fs(fsq_dir / "part-0.parquet")} (FORMAT PARQUET)
    """)
    out = work / "fsq.parquet"
    stats = extract_fsq(fsq_dir, out)
    rows = con.execute(f"SELECT source_id, business_name, phone, email, categories, date_created FROM {fs(out)} ORDER BY source_id").fetchall()
    ids = [r[0] for r in rows]
    assert ids == ["a1", "b1"], rows          # a2 = duplicate phone (less complete), c1 junk phone, d1 Canada, e1 no contact
    assert rows[0][2] == "(530) 244-4772" and rows[0][5] == "2020-01-01" and rows[1][3] == "HELLO@beta.example"
    assert stats["rows"] == 2 and stats["before_dedup"] == 3
    # ---- Overture: raw places schema subset (needs spatial for geometry)
    con.execute("INSTALL spatial; LOAD spatial")
    ov_dir = work / "ov"
    ov_dir.mkdir()
    con.execute(f"""
        COPY (
            SELECT id, {{'primary': nm}} AS names, phones, websites, emails,
                   [{{'freeform': addr, 'locality': loc, 'region': reg, 'postcode': pc, 'country': ctry}}] AS addresses,
                   ST_AsWKB(ST_Point(lon, lat)) AS geometry,
                   {{'hierarchy': hier}} AS taxonomy, {{'primary': cat}} AS categories, socials, status AS operating_status
            FROM (VALUES
                ('o1', 'Plumb Co', ['+1 512 555 0100'], ['https://plumb.example'], []::VARCHAR[], '9 Pipe Rd', 'Austin', 'US-TX', '78701', 'US', -97.7, 30.2,
                 ['services_and_business', 'home_improvement', 'plumber'], 'plumber', ['https://facebook.com/plumb'], 'open'),
                ('o2', 'Closed Diner', []::VARCHAR[], []::VARCHAR[], ['eat@diner.example'], NULL, '', 'US-CA', '', 'US', -122.3, 40.5,
                 NULL::VARCHAR[], 'restaurant', []::VARCHAR[], 'permanently_closed'),
                ('o3', 'No Contact', []::VARCHAR[], ['https://x.example'], []::VARCHAR[], NULL, 'Austin', 'US-TX', NULL, 'US', -97.7, 30.2,
                 ['retail'], 'retail', []::VARCHAR[], 'open'),
                ('o4', 'Mexico', ['+52 55 1234 5678'], []::VARCHAR[], []::VARCHAR[], NULL, 'CDMX', 'MX-CMX', NULL, 'MX', -99.1, 19.4,
                 ['retail'], 'retail', []::VARCHAR[], 'open')
            ) t(id, nm, phones, websites, emails, addr, loc, reg, pc, ctry, lon, lat, hier, cat, socials, status)
        ) TO {fs(ov_dir / "part-0.parquet")} (FORMAT PARQUET)
    """)
    out = work / "ov.parquet"
    extract_overture(ov_dir, out, geo=work / "no_geo.duckdb")
    rows = con.execute(f"SELECT source_id, business_name, phone, email, city, state, zip, categories, facebook_id, date_closed, latitude FROM {fs(out)} ORDER BY source_id").fetchall()
    assert [r[0] for r in rows] == ["overture/o1", "overture/o2"], rows
    assert rows[0][5] == "TX" and rows[0][7] == "Services and business > Home improvement > Plumber" and rows[0][8] == "https://facebook.com/plumb"
    assert rows[0][10] == 30.2 and rows[1][4] is None and rows[1][6] is None and rows[1][7] == "Other > Restaurant" and rows[1][9] == "closed"
    print("extractors OK")


def test_merge(work: Path):
    fixture = work / "fixture.duckdb"
    build_fixture(None, fixture)
    con = duckdb.connect(str(fixture))
    install_macros(con)
    src = "Overture"
    before = con.execute("SELECT count(*) FROM places WHERE source = ?", [src]).fetchone()[0]
    used_phone = con.execute("SELECT phone FROM used_contacts WHERE phone IS NOT NULL AND phone NOT IN (SELECT phone FROM places WHERE phone IS NOT NULL) LIMIT 1").fetchone()[0]
    # batch: 100 unchanged, 50 with a new phone, 20 with a new category, 30 brand new rows (one matching a used phone),
    # plus one duplicate source_id inside the batch and 5 rows of a *different* source's ids (must not touch them)
    batch = work / "batch.parquet"
    con.execute(f"""
        COPY (
            WITH ex AS (SELECT * FROM places WHERE source = '{src}' ORDER BY source_id LIMIT 170)
            SELECT source_id, business_name, phone, website, email, address, city, state, zip, country, latitude, longitude,
                   categories, instagram, twitter, facebook_id, date_created, date_refreshed, date_closed
            FROM ex QUALIFY row_number() OVER (ORDER BY source_id) <= 100
            UNION ALL
            SELECT source_id, business_name, '(555) 010-' || lpad((row_number() OVER ())::VARCHAR, 4, '0'), website, email, address, city, state, zip, country,
                   latitude, longitude, categories, instagram, twitter, facebook_id, date_created, date_refreshed, date_closed
            FROM ex QUALIFY row_number() OVER (ORDER BY source_id) BETWEEN 101 AND 150
            UNION ALL
            SELECT source_id, business_name, phone, website, email, address, city, state, zip, country, latitude, longitude,
                   'Retail > New Group > New Type', instagram, twitter, facebook_id, date_created, date_refreshed, date_closed
            FROM ex QUALIFY row_number() OVER (ORDER BY source_id) BETWEEN 151 AND 170
            UNION ALL
            SELECT 'overture/new-' || i, 'New Biz ' || i, CASE WHEN i = 0 THEN '{used_phone}' ELSE (4000000000 + i)::VARCHAR END, NULL, NULL,
                   NULL, 'Newtown', 'tx', NULL, 'US', NULL, NULL, 'Retail > Group 1 > Type 1', NULL, NULL, NULL, NULL, NULL, NULL
            FROM range(30) t(i)
            UNION ALL
            SELECT 'overture/new-0', 'New Biz 0 duplicate row', '4000000000', NULL, NULL, NULL, 'Newtown', 'tx', NULL, 'US', NULL, NULL,
                   'Retail > Group 1 > Type 1', NULL, NULL, NULL, NULL, NULL, '2025-01-01'
        ) TO {fs(batch)} (FORMAT PARQUET)
    """)
    logs = []
    stats = merge(con, src, batch, "2026-08-19.0", log=logs.append)
    assert stats["total"] == 200 and stats["inserted"] == 30 and stats["updated"] == 70 and stats["unchanged"] == 100, stats
    assert stats["marked"] >= 1 and stats["after"] == before + 30, stats
    assert con.execute("SELECT count(*) FROM places").fetchone()[0] == 45000 + 30
    assert con.execute("SELECT phone, state, used_reason FROM places WHERE source_id = 'overture/new-0'").fetchone()[0] == used_phone
    row = con.execute("SELECT state, used_at IS NOT NULL, category_list FROM places WHERE source_id = 'overture/new-0'").fetchone()
    assert row[0] == "TX" and row[1] is True and row[2] == ["Retail > Group 1 > Type 1"], row
    assert con.execute("SELECT count(*) FROM places WHERE phone LIKE '5550100%'").fetchone()[0] == 50   # phones normalized on update
    assert con.execute("SELECT count(*) FROM places WHERE categories = 'Retail > New Group > New Type'").fetchone()[0] == 20
    assert con.execute("SELECT count(*) FROM dim_tree WHERE path = 'Retail > New Group'").fetchone()[0] >= 1   # tree rebuilt
    assert get_versions(con)[src] == "2026-08-19.0"
    h = con.execute("SELECT kind, source, rows, rows_skipped, places_marked FROM history ORDER BY id DESC LIMIT 1").fetchone()
    assert h == ("update", src, 100, 100, stats["marked"]), h
    # running the same batch again changes nothing
    again = merge(con, src, batch, "2026-08-19.0", rebuild=False, log=logs.append)
    assert again["inserted"] == 0 and again["updated"] == 0 and again["unchanged"] == 200, again
    con.close()
    print("merge OK", stats)


def main():
    work = Path(tempfile.mkdtemp(prefix="leads_update_test_"))
    test_parsers()
    test_extractors(work)
    test_merge(work)
    if "--online" in sys.argv:
        res = sources.check_all(sources.get_hf_token())
        for r in res:
            print(" ", r["source"], r["version"], sources.fmt_size(r["size"]) if r["size"] else "", r["error"] or "")
        assert all(r["version"] for r in res if r["source"] != "Foursquare"), res
    print("\nALL UPDATE TESTS PASSED")


if __name__ == "__main__":
    main()
