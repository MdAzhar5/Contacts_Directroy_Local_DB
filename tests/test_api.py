r"""
GUI-free regression test for the Leads Explorer API.

Builds a small fixture database and exercises every API method the UI calls:
filtering, Used Data facets, import with mapping and the duplicate rule, filter
file, export (which never changes usage), history, and the start-from-scratch empty database.

The fixture is sampled from a real v2 database when one is given (or found at
leads_app/leads.duckdb); otherwise, or with --synthetic, it is generated. The real
database is only ever read.

Run:  python tests/test_api.py [path\to\leads.duckdb | --synthetic]
"""
import os
import sys
import tempfile
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
APP_DIR = ROOT / "leads_app"
sys.path.insert(0, str(APP_DIR))
from schema import build_dims, create_empty_db, create_stores, install_macros  # noqa: E402

SEED_PHONE, SEED_EMAIL = "9995550123", "both@example.com"   # one contact that has BOTH fields


def fs(path) -> str:
    return "'" + str(path).replace("\\", "/").replace("'", "''") + "'"


def build_fixture(src, dst: Path) -> None:
    if dst.exists():
        dst.unlink()
    con = duckdb.connect(str(dst))
    install_macros(con)
    if src:
        con.execute(f"ATTACH {fs(src)} AS src (READ_ONLY)")
        con.execute("CREATE TABLE places AS SELECT * FROM src.places USING SAMPLE 40000 ROWS")
        con.execute("INSERT INTO places SELECT * FROM (SELECT * FROM src.places WHERE used_at IS NOT NULL LIMIT 2000) WHERE source_id NOT IN (SELECT source_id FROM places)")
        con.execute("INSERT INTO places SELECT * FROM (SELECT * FROM src.places WHERE state = 'AK' AND phone IS NOT NULL LIMIT 500) WHERE source_id NOT IN (SELECT source_id FROM places)")
        con.execute("CREATE TABLE used_contacts AS SELECT * FROM src.used_contacts USING SAMPLE 20000 ROWS")
        con.execute("CREATE TABLE history AS SELECT * FROM src.history")
        con.execute("CREATE TABLE catalogs AS SELECT * FROM src.catalogs")
        con.execute("DETACH src")
    else:
        # ---- synthetic leads: 45K rows, 6 states, 3 sources, ~10% without phone
        con.execute("""
            CREATE TABLE places AS
            SELECT *, [categories] AS category_list, [split_part(categories, ' > ', 1)] AS industry_list,
                   (date_closed IS NULL) AS is_open, NULL::VARCHAR AS used_at, NULL::VARCHAR AS used_reason
            FROM (
                SELECT ['Foursquare', 'Overture', 'OpenStreetMap'][(i % 3) + 1] AS source,
                       'syn/' || i AS source_id,
                       CASE WHEN i % 7 = 0 THEN 'Smith Business ' || i ELSE 'Business ' || i END AS business_name,
                       CASE WHEN i % 10 = 0 THEN NULL ELSE (2000000000 + (i * 7919) % 7999999999)::BIGINT::VARCHAR END AS phone,
                       CASE WHEN i % 3 = 0 THEN 'https://biz' || i || '.example.com' END AS website,
                       CASE WHEN i % 4 = 0 THEN 'biz' || i || '@example.com' END AS email,
                       (i % 900 + 1) || ' Main St' AS address,
                       'City ' || (i % 40) AS city,
                       ['AK', 'CA', 'TX', 'NY', 'FL', 'WA'][(i % 6) + 1] AS state,
                       lpad(((i * 37) % 99999)::VARCHAR, 5, '0') AS zip, 'US' AS country,
                       NULL::DOUBLE AS latitude, NULL::DOUBLE AS longitude,
                       ['Retail', 'Dining and Drinking', 'Health and Medicine'][(i % 3) + 1] || ' > Group ' || (i % 5) || ' > Type ' || (i % 20) AS categories,
                       NULL::VARCHAR AS instagram, NULL::VARCHAR AS twitter,
                       CASE WHEN i % 5 = 0 THEN 'fb' || i END AS facebook_id,
                       NULL::VARCHAR AS date_created, NULL::VARCHAR AS date_refreshed,
                       CASE WHEN i % 50 = 0 THEN '2025-01-01' END AS date_closed
                FROM range(45000) t(i))
        """)
        create_stores(con, used_seq_start=1, stamp="2026-01-01T00:00:00+00:00")
        # ---- 20K synthetic used contacts; the first 1000 odd ones share a phone with a lead
        con.execute("""
            INSERT INTO used_contacts (id, name, email, phone, city, state, country, industry, category, source, source_file,
                                       source_type, source_category, uploaded_at, raw_data, created_at)
            SELECT i + 1,
                   CASE WHEN i % 9 = 0 THEN 'John Smith ' || i ELSE 'Contact ' || i END,
                   CASE WHEN i % 2 = 0 THEN 'contact' || i || '@example.org' END,
                   CASE WHEN i % 2 = 1 THEN CASE WHEN i < 2000 THEN (2000000000 + (i * 7919) % 7999999999)::BIGINT::VARCHAR
                                                 ELSE (3000000000 + i)::VARCHAR END END,
                   'City ' || (i % 40), ['CA', 'TX', 'ON'][(i % 3) + 1], 'US',
                   ['Family Medicine', 'Accountant', 'Mental Health'][(i % 3) + 1],
                   CASE WHEN i % 4 = 0 THEN 'Cat ' || (i % 6) END,
                   'Seed', 'seed.csv', CASE WHEN i % 3 = 0 THEN 'Med' ELSE 'Not Med' END,
                   CASE WHEN i % 3 = 0 THEN 'medical' ELSE 'non_medical' END,
                   '2026-01-01T00:00:00+00:00', '{}', '2026-01-01T00:00:00+00:00'
            FROM range(20000) t(i)
        """)
        con.execute("UPDATE places SET used_at = '2026-01-01T00:00:00+00:00', used_reason = 'match:legacy' "
                    "WHERE phone IN (SELECT phone FROM used_contacts WHERE phone IS NOT NULL)")
        for kind in ("source", "industry", "category"):
            con.execute(f"INSERT INTO catalogs SELECT DISTINCT '{kind}', {kind}, '2026-01-01T00:00:00+00:00' FROM used_contacts WHERE {kind} IS NOT NULL")
        con.execute("DROP SEQUENCE seq_used_contacts")
        con.execute("DROP SEQUENCE seq_history")
    # one deterministic contact that has BOTH a phone and an email (rare in the real data)
    con.execute("""INSERT INTO used_contacts (id, name, email, phone, source, source_type, source_category, uploaded_at, raw_data, created_at)
                   VALUES ((SELECT coalesce(max(id), 0) + 1 FROM used_contacts), 'Both Fields', ?, ?,
                           'Fixture', 'Not Med', 'non_medical', '2026-01-01T00:00:00+00:00', '{}', '2026-01-01T00:00:00+00:00')""",
                [SEED_EMAIL, SEED_PHONE])
    mu = con.execute("SELECT coalesce(max(id), 0) + 1 FROM used_contacts").fetchone()[0]
    mh = con.execute("SELECT coalesce(max(id), 0) + 1 FROM history").fetchone()[0]
    con.execute(f"CREATE SEQUENCE seq_used_contacts START {mu}")
    con.execute(f"CREATE SEQUENCE seq_history START {mh}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_used_phone ON used_contacts(phone)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_used_email ON used_contacts(email)")
    con.execute("CREATE TABLE IF NOT EXISTS meta (key VARCHAR, value VARCHAR)")
    con.execute("DELETE FROM meta WHERE key = 'schema_version'")
    con.execute("INSERT INTO meta VALUES ('schema_version', '2')")
    build_dims(con)
    con.close()


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--synthetic":
        src = None
    elif arg:
        src = Path(arg)
    else:
        src = APP_DIR / "leads.duckdb" if (APP_DIR / "leads.duckdb").exists() else None
    work = Path(tempfile.mkdtemp(prefix="leads_test_"))
    fixture = work / "fixture.duckdb"
    print(f"Fixture from {src.name if src else 'synthetic data'} -> {fixture}")
    build_fixture(src, fixture)
    os.environ["LEADS_DB"] = str(fixture)
    import app  # noqa: E402  (reads LEADS_DB at import)

    api = app.Api()

    class FakeWindow:
        def create_file_dialog(self, kind, **kw):
            return None

    api._window = FakeWindow()
    api._save_dialog = lambda filename, kind="csv": str(work / filename)

    # ---- All Leads
    o = api.get_options()
    assert o["total"] > 40000 and o["used"] > 0 and o["used_contacts"] == 20001, o
    unused = api.count({"usage": "unused"})["total"]
    used = api.count({"usage": "used"})["total"]
    alln = api.count({"usage": "all"})["total"]
    assert unused + used == alln == o["total"], (unused, used, alln)
    bad = api._rows("SELECT count(*) FROM places WHERE phone IS NOT NULL AND (length(phone) <> 10 OR regexp_matches(phone, '[^0-9]'))")[0][0]
    assert bad == 0, f"{bad} non-canonical phones in places"
    pv = api.preview({"usage": "unused", "has_phone": True}, 5, 0)
    assert "used_at" in pv["columns"] and len(pv["rows"]) == 5
    assert api.get_states([]) and api.get_cities([], "", [])
    # industry tree: main industries, sub-industries, search, and parent-covers-children filtering
    top = api.get_tree(None, [], "")
    assert top and all(t["level"] == 1 and t["parent"] is None for t in top) and any(t["kids"] for t in top), top
    first = next(t for t in top if t["kids"])
    kids = api.get_tree(first["v"], [], "")
    assert kids and all(k["parent"] == first["v"] and k["level"] == 2 for k in kids), kids
    n_top = api.count({"usage": "all", "tree": [first["v"]]})["total"]
    assert n_top > 0 and n_top == api._rows("SELECT count(*) FROM places WHERE list_has_any(industry_list, [?])", [first["v"]])[0][0]
    n_kid = api.count({"usage": "all", "tree": [kids[0]["v"]]})["total"]
    assert 0 < n_kid <= n_top, (n_kid, n_top)
    assert n_kid == api._rows("SELECT count(*) FROM places WHERE len(list_filter(category_list, c -> c = ? OR starts_with(c, ?))) > 0",
                              [kids[0]["v"], kids[0]["v"] + " > "])[0][0]
    assert api.count({"usage": "all", "tree": [first["v"], kids[0]["v"]]})["total"] == n_top   # parent already covers the child
    found = api.get_tree(None, [], kids[0]["name"][:6])
    assert any(x["v"] == kids[0]["v"] for x in found), found
    print("All Leads OK  unused", unused, "used", used, "| tree:", first["v"], n_top, "->", kids[0]["name"], n_kid)

    # ---- Used Data
    f = {"source_type": ["Med"]}
    facets = api.used_facets(f)
    assert set(facets) == set(app.USED_FACETS) and all(x["v"] in ("Med", "Not Med") for x in facets["source_type"])
    uc = api.used_count(f)
    assert uc["total"] == api._rows("SELECT count(*) FROM used_contacts WHERE source_type = 'Med'")[0][0]
    up = api.used_preview(f, 10, 0)
    assert up["columns"] == app.USED_PREVIEW and len(up["rows"]) == min(10, uc["total"])
    q = api.used_count({"q": "smith"})["total"]
    assert q == api._rows("SELECT count(*) FROM used_contacts WHERE name ILIKE '%smith%' OR email ILIKE '%smith%' OR address_line_1 ILIKE '%smith%' OR website ILIKE '%smith%' OR npi ILIKE '%smith%' OR specialty ILIKE '%smith%'")[0][0]
    r = api.used_export({"source_type": ["Med"]}, "csv")
    assert r["ok"] and r["rows"] == uc["total"] and Path(r["path"]).exists()
    if api.excel_ok:
        r = api.used_export({"source_type": ["Med"], "has_phone": True}, "xlsx")
        assert r["ok"] and Path(r["path"]).suffix == ".xlsx"
    print("Used Data OK  Med", uc["total"], "search smith", q)

    # ---- Import & Map (duplicate rule: skip only when phone AND email both already exist)
    existing_phone, existing_email = api._rows("SELECT (SELECT phone FROM used_contacts WHERE phone IS NOT NULL AND phone <> ? LIMIT 1), (SELECT email FROM used_contacts WHERE email IS NOT NULL AND email <> ? LIMIT 1)", [SEED_PHONE, SEED_EMAIL])[0]
    lead_phone = api._rows("SELECT phone FROM places WHERE used_at IS NULL AND phone IS NOT NULL LIMIT 1")[0][0]
    csv_path = work / "incoming.csv"
    csv_path.write_text(
        "First Name,Last Name,E-mail,Telephone,Sate,Zip,Company\n"
        f"Ann,Lee,ANN@Example.com,+1 (530) 244-4772,ca,95001,Lee LLC\n"
        f"Bob,Ray,,{existing_phone},tx,,Ray Inc\n"                  # phone used, no email -> kept
        f"Cy,Dee,{existing_email},1-212-555-0100,,10001,\n"          # email used, new phone -> kept
        f"Di,Eve,,{lead_phone},,,Eve Co\n"
        "Ed,Fox,,12345,,,Fox\n"
        f"Fay,Gum,{SEED_EMAIL.upper()},{SEED_PHONE},,,Gum Co\n"     # phone AND email used -> duplicate
        "Ann,Lee,ann@example.com,530-244-4772,ca,95001,Lee LLC\n",   # repeats row 1 -> duplicate within file
        encoding="utf-8")
    info = api.file_info(str(csv_path))
    assert info["ok"] and info["rows"] == 7, info
    m = info["mapping"]
    assert m["first_name"] == "First Name" and m["email"] == "E-mail" and m["phone"] == "Telephone" and m["state"] == "Sate" and m["postal_code"] == "Zip" and m["name"] == "Company", m
    meta = {"source": "Test Source", "industry": "Test Industry", "source_type": "Med", "source_category": "medical", "category": ""}
    before_used = api._rows("SELECT count(*) FROM used_contacts")[0][0]
    r = api.run_import(str(csv_path), None, m, meta, {"skip_existing": False, "skip_blank": False})
    assert r["ok"] and r["total"] == 7 and r["dup_existing"] == 1 and r["dup_infile"] == 1 and r["skipped"] == 2 and r["imported"] == 5, r
    assert r["marked"] >= 1, r  # Di's phone matched a lead
    rows = api._rows("SELECT name, email, phone, state, postal_code, source, source_type, import_id FROM used_contacts WHERE source = 'Test Source' ORDER BY id")
    assert rows[0] == ("Ann Lee", "ann@example.com", "5302444772", "CA", "95001", "Test Source", "Med", r["history_id"]), rows[0]
    assert [x[0] for x in rows] == ["Ann Lee", "Bob Ray", "Cy Dee", "Di Eve", "Ed Fox"], rows
    assert rows[3][2] == lead_phone and rows[4][2] is None, rows
    assert api._rows("SELECT count(*) FROM used_contacts")[0][0] == before_used + 5
    assert api._rows("SELECT used_reason FROM places WHERE phone = ?", [lead_phone])[0][0] == f"import:{r['history_id']}"
    assert "Test Industry" in api.catalogs()["industry"]
    assert api.add_catalog("category", "New Cat")["ok"] and "New Cat" in api.catalogs()["category"]
    bad = api.run_import(str(csv_path), None, {"city": "Zip"}, meta)
    assert not bad["ok"]
    # strict option: re-importing the same file now skips everything with a used phone or email
    r_strict = api.run_import(str(csv_path), None, m, meta, {"skip_existing": True, "skip_blank": True})
    assert r_strict["ok"] and r_strict["imported"] == 0 and r_strict["skipped"] == 7, r_strict
    assert api._rows("SELECT count(*) FROM used_contacts")[0][0] == before_used + 5
    print("Import OK", r)

    # ---- Filter File (csv and xlsx)
    r = api.filter_file(str(csv_path), None, "Telephone", "E-mail", True, True)
    assert r["ok"] and r["total"] == 7 and r["removed"] == 6 and r["kept"] == 1, r  # only Ed (invalid phone, no email) survives
    out = Path(r["path"]).read_text(encoding="utf-8").splitlines()
    assert out[0].startswith("First Name,Last Name,E-mail,Telephone") and out[1].split(",")[3] == "", out
    if api.excel_ok:
        xl = work / "incoming.xlsx"
        with api._lock:
            api._con.execute(f"COPY (SELECT * FROM read_csv({fs(csv_path)}, header=true, all_varchar=true)) TO {fs(xl)} WITH (FORMAT xlsx, HEADER true)")
        info = api.file_info(str(xl))
        assert info["ok"] and info["sheets"] and info["rows"] == 7, info
        r = api.filter_file(str(xl), info["sheet"], "Telephone", "", True, False)
        assert r["ok"] and r["removed"] == 6 and r["kept"] == 1 and r["normalized"] >= 3, r
    print("Filter File OK", r)

    # ---- Export: writes the CSV and logs it, but never changes usage (only imports feed Used Data)
    filt = {"states": ["AK"], "has_phone": True, "usage": "unused"}
    n_before = api.count(filt)["total"]
    assert n_before > 0
    used_before = api._rows("SELECT count(*) FROM used_contacts")[0][0]
    r = api.export_csv(filt)
    assert r["ok"] and r["rows"] == n_before, r
    assert api.count(filt)["total"] == n_before, "export must not mark leads as used"
    assert api._rows("SELECT count(*) FROM used_contacts")[0][0] == used_before, "export must not add to Used Data"
    lines = Path(r["path"]).read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",") == app.EXPORT_COLUMNS and len(lines) == n_before + 1
    assert all(len(l.split(",")[2]) == 10 for l in lines[1:6]), "exported phones are canonical"
    # exported phones are not used yet, so a filter-file run keeps them...
    fx = work / "ak.csv"
    fx.write_text("Phone\n" + "\n".join(l.split(",")[2] for l in lines[1:6]) + "\n", encoding="utf-8")
    r2 = api.filter_file(str(fx), None, "Phone", "", True, False)
    assert r2["ok"] and r2["removed"] == 0 and r2["kept"] == 5, r2
    # ...until that file is imported, which marks the matching leads used
    r3 = api.run_import(str(fx), None, {"phone": "Phone"}, {"source": "Leads Explorer", "industry": "AK Test", "source_type": "Not Med", "source_category": "non_medical"})
    assert r3["ok"] and r3["imported"] == 5 and r3["marked"] == 5, r3
    assert api.count(filt)["total"] == n_before - 5
    r4 = api.filter_file(str(fx), None, "Phone", "", True, False)
    assert r4["ok"] and r4["removed"] == 5 and r4["kept"] == 0, r4
    print("Export OK", r)

    # ---- History
    h = api.history(50)
    kinds = [row[h["columns"].index("kind")] for row in h["rows"]]
    assert kinds[:4] == ["filter", "import", "filter", "export"], kinds
    print("History OK", len(h["rows"]), "rows")

    # ---- Start from scratch: empty database created on first launch
    empty = work / "empty.duckdb"
    create_empty_db(empty, "2026-01-01T00:00:00+00:00")
    app.DB = empty
    api2 = app.Api()
    api2._window = FakeWindow()
    api2._save_dialog = lambda filename, kind="csv": str(work / ("e_" + filename))
    o2 = api2.get_options()
    assert o2["total"] == 0 and o2["used"] == 0 and o2["used_contacts"] == 0 and o2["sources"] == [], o2
    assert api2.count({"usage": "unused"})["total"] == 0 and api2.preview({}, 10, 0)["rows"] == []
    assert api2.get_tree(None, [], "") == [] and api2.used_facets({})["source"] == []
    cats = api2.catalogs()
    assert cats["source_type"] == ["Med", "Not Med"] and set(cats["source_category"]) == {"medical", "non_medical"}, cats
    r = api2.run_import(str(csv_path), None, m, meta, {})
    assert r["ok"] and r["imported"] == 6 and r["dup_infile"] == 1 and r["marked"] == 0, r
    assert api2.get_options()["used_contacts"] == 6
    assert api2.used_count({"source": ["Test Source"]})["total"] == 6
    print("Empty DB OK", r)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
