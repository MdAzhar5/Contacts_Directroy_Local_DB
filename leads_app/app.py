"""
Leads Explorer - desktop app over two stores in one DuckDB file:

  places         25M USA business leads (Foursquare, Overture Maps, OpenStreetMap)
  used_contacts  contacts already used: everything imported from ContactDirectory,
                 every file imported through "Import & Map", and every export made
                 from "All Leads" with "mark as used" on.

Run:  python app.py      Needs leads.duckdb (schema v2, see migrate_v2.py).
"""
import json
import os
import re
import subprocess
import sys
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import webview

from schema import build_tree, create_empty_db, install_macros

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import sources  # noqa: E402  (release discovery for the Updates tab)

sources.load_dotenv()          # secrets and optional settings from the git-ignored .env
UPDATE_SCRIPT = ROOT / "pipeline" / "update.py"
DB = Path(os.environ.get("LEADS_DB", str(HERE / "leads.duckdb")))
UI = HERE / "ui.html"

EXPORT_COLUMNS = [
    "source", "business_name", "phone", "website", "email", "address", "city", "state", "zip",
    "country", "categories", "latitude", "longitude", "instagram", "twitter",
    "facebook_id", "date_created", "date_refreshed", "date_closed", "source_id",
]
USED_FIELDS = [
    "name", "email", "address_line_1", "address_line_2", "city", "state", "postal_code", "country",
    "phone", "fax", "website", "industry", "original_industry", "category", "alternate_categories",
    "credentials", "specialty", "rating", "reviews", "npi", "enumeration_date", "source", "source_file",
    "source_type", "source_sheet", "source_category", "uploaded_at",
]
USED_PREVIEW = ["name", "phone", "email", "address_line_1", "city", "state", "postal_code", "country",
                "industry", "category", "source", "source_type", "source_file", "uploaded_at"]
USED_FACETS = ["source", "source_type", "source_category", "industry", "category", "state", "city", "country", "source_file"]
CATALOG_KINDS = ["source", "industry", "source_type", "source_category", "category"]

# Fields a user can map from an uploaded file (controlled metadata is chosen separately)
MAPPING_FIELDS = [
    ("name", "Name / Business name"), ("first_name", "First name"), ("last_name", "Last name"),
    ("email", "Email"), ("phone", "Phone"), ("address_line_1", "Address line 1"), ("address_line_2", "Address line 2"),
    ("city", "City"), ("state", "State"), ("postal_code", "Postal / ZIP code"), ("country", "Country"),
    ("fax", "Fax"), ("website", "Website"), ("original_industry", "Original industry"),
    ("alternate_categories", "Alternate categories"), ("credentials", "Credentials"),
    ("specialty", "Specialty"), ("rating", "Rating"), ("reviews", "Reviews"), ("npi", "NPI"),
    ("enumeration_date", "Enumeration date"),
]
MAPPING_ALIASES = {
    "name": ["name", "businessname", "facilityname", "business", "companyname", "company", "fullname"],
    "first_name": ["firstname", "givenname"], "last_name": ["lastname", "surname", "familyname"],
    "email": ["email", "emailaddress", "mail", "emails"],
    "address_line_1": ["address", "streetaddress", "street", "firstlineaddress", "addressline1", "address1"],
    "address_line_2": ["secondlineaddress", "addressline2", "address2", "suite", "unit"],
    "city": ["city", "citytown", "cityname", "town", "locality"],
    "state": ["state", "stateprovince", "region", "sate", "province"],
    "postal_code": ["zip", "zipcode", "postalcode", "postcode", "postal"], "country": ["country", "countrycode"],
    "phone": ["phone", "telephone", "telephonenumber", "phonenumber", "direct", "mobile", "tel", "phones"],
    "fax": ["fax"], "website": ["website", "url", "web", "websites"],
    "original_industry": ["originalindustry", "industry"],
    "alternate_categories": ["alternatecategories", "alternates", "subcategory", "categories"],
    "credentials": ["credentials", "credential"], "specialty": ["specialty", "speciality"],
    "rating": ["rating", "stars"], "reviews": ["reviews", "reviewcount", "numberofreviews"],
    "npi": ["npi", "npinumber"], "enumeration_date": ["enumerationdate", "enumeratedate"],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sql_str(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def sql_in(values) -> str:
    return ", ".join(sql_str(v) for v in values)


def sql_list(values) -> str:
    return "[" + sql_in(values) + "]"


def qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def fs(path) -> str:
    """Path as a DuckDB string literal (forward slashes, quotes escaped)."""
    return sql_str(str(path).replace("\\", "/"))


def suggest_mapping(headers) -> dict:
    normalized = {}
    for h in headers:
        normalized.setdefault(re.sub(r"[^a-z0-9]", "", str(h).lower()), h)
    out = {}
    for field, candidates in MAPPING_ALIASES.items():
        out[field] = next((normalized[c] for c in candidates if c in normalized), "")
    return out


def default_industry(filename: str) -> str:
    low = filename.lower()
    checks = [
        ("immigration.legal-canada", "Immigration Legal Canada"), ("mental_health", "Mental Health"),
        ("mental health", "Mental Health"), ("chiropr", "Chiropractor"), ("multi-spec", "Multi-Specialty"),
        ("family_medicine", "Family Medicine"), ("family medicine", "Family Medicine"),
        ("family practice", "Family Practice"), ("family_doctors", "Family Doctors"),
        ("internel", "Internal & Family Practice"), ("physicians", "Physicians"),
        ("phy-usa-npi", "Physicians"), ("med-npi", "Medical NPI"), ("medical-", "Medical"),
        ("accountant", "Accountant"), ("accounting", "Accountant"),
    ]
    for needle, label in checks:
        if needle in low:
            return label
    stem = Path(filename).stem
    stem = re.sub(r"\s*\([^)]*\)", "", stem)
    stem = re.sub(r"[-_]+", " ", stem)
    stem = re.sub(r"\d+", "", stem)
    return re.sub(r"\s+", " ", stem).strip().title() or "Imported"


def xlsx_sheets(path: Path) -> list:
    try:
        with zipfile.ZipFile(path) as z:
            wb = z.read("xl/workbook.xml").decode("utf-8", errors="replace")
        return re.findall(r'<sheet\b[^>]*?\bname="([^"]+)"', wb) or ["Sheet1"]
    except Exception:
        return ["Sheet1"]


class Api:
    def __init__(self):
        if not DB.exists():
            raise SystemExit(f"Database not found: {DB}\nRun build_db.py (fresh) or migrate_v2.py (upgrade) first.")
        self._con = duckdb.connect(str(DB))
        install_macros(self._con)
        self._con.execute("SET preserve_insertion_order = false")
        try:
            self._con.execute("INSTALL excel; LOAD excel")
            self.excel_ok = True
        except Exception:
            self.excel_ok = False
        ver = self._con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() if self._table_exists("meta") else None
        if not ver or int(ver[0]) < 2:
            raise SystemExit("leads.duckdb is the old layout. Close the app and run migrate_v2.py first.")
        self._lock = threading.Lock()
        self._window = None
        self._csv_mode = {}   # path -> (encoding, lenient)
        if not self._table_exists("dim_tree"):
            # one-time upgrade for databases built before the industry tree existed
            build_tree(self._con)
            self._con.execute("CHECKPOINT")

    # ---------- helpers ----------
    def _table_exists(self, name) -> bool:
        return self._con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()[0] > 0

    def _q(self, sql, params=None):
        with self._lock:
            cur = self._con.execute(sql, params or [])
            cols = [d[0] for d in cur.description]
            return cols, cur.fetchall()

    def _rows(self, sql, params=None):
        return self._q(sql, params)[1]

    def _one(self, sql, params=None):
        return self._rows(sql, params)[0]

    def _dml(self, sql, params=None) -> int:
        """Run INSERT/UPDATE/DELETE (caller holds the lock) and return affected rows."""
        cur = self._con.execute(sql, params or [])
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def _save_dialog(self, filename, kind="csv"):
        types = ("CSV Files (*.csv)",) if kind == "csv" else ("Excel Workbook (*.xlsx)",)
        path = self._window.create_file_dialog(webview.SAVE_DIALOG, directory=str(HERE.parent),
                                               save_filename=filename, file_types=types)
        if not path:
            return None
        if isinstance(path, (list, tuple)):
            path = path[0]
        path = str(path)
        if not path.lower().endswith("." + kind):
            path += "." + kind
        return path

    # ======================================================================
    # ALL LEADS (places)
    # ======================================================================
    def _where(self, f: dict) -> str:
        clauses = []
        sources = [x for x in f.get("sources", []) if x]
        tree = [x for x in f.get("tree", []) if x]
        states = [x for x in f.get("states", []) if x]
        cities = [x for x in f.get("cities", []) if x]
        countries = [x for x in f.get("countries", []) if x]

        if sources:
            clauses.append(f"source IN ({sql_in(sources)})")
        if tree:
            # a selected node covers itself and everything beneath it ("Retail" matches "Retail > Shoe Store");
            # a prefix test per list element is far faster than expanding a node into its leaf categories
            match = " OR ".join(f"c = {sql_str(p)} OR starts_with(c, {sql_str(p + ' > ')})" for p in tree)
            clauses.append(f"len(list_filter(category_list, c -> {match})) > 0")
        if countries:
            clauses.append(f"country IN ({sql_in(countries)})")
        if states:
            clauses.append(f"state IN ({sql_in(states)})")
        if cities:
            pairs = [c.split("||", 1) for c in cities if "||" in c]
            if pairs:
                tuples = ", ".join(f"({sql_str(s)}, {sql_str(c)})" for s, c in pairs)
                clauses.append(f"(state, city) IN ({tuples})")

        name = (f.get("name") or "").strip()
        if name:
            clauses.append(f"business_name ILIKE {sql_str('%' + name + '%')}")
        zipcode = (f.get("zip") or "").strip()
        if zipcode:
            clauses.append(f"zip LIKE {sql_str(zipcode + '%')}")

        if f.get("has_phone"):
            clauses.append("phone IS NOT NULL")
        if f.get("has_email"):
            clauses.append("email IS NOT NULL")
        if f.get("has_website"):
            clauses.append("website IS NOT NULL")
        if f.get("has_facebook"):
            clauses.append("facebook_id IS NOT NULL")
        if f.get("open_only"):
            clauses.append("is_open")
        usage = f.get("usage", "unused")
        if usage == "unused":
            clauses.append("used_at IS NULL")
        elif usage == "used":
            clauses.append("used_at IS NOT NULL")
        return " AND ".join(clauses) if clauses else "TRUE"

    @staticmethod
    def _src_where(sources):
        sources = [s for s in (sources or []) if s]
        return f"source IN ({sql_in(sources)})" if sources else "TRUE"

    def get_tree(self, parent=None, sources=None, search=""):
        """Industry tree nodes. parent=None gives the main industries, a path gives its sub-industries;
        with search, a flat list of every node whose name matches (any level, any parent)."""
        sources = [s for s in (sources or []) if s]
        src_t = f"t.source IN ({sql_in(sources)})" if sources else "TRUE"
        src_c = f"c.source IN ({sql_in(sources)})" if sources else "TRUE"
        if search:
            node = f"t.name ILIKE {sql_str('%' + search + '%')}"
            limit = "LIMIT 300"
        else:
            node = "t.parent IS NULL" if not parent else f"t.parent = {sql_str(parent)}"
            limit = ""
        rows = self._rows(f"""
            SELECT t.path, t.level, t.parent, t.name, sum(t.n) AS n,
                   max(EXISTS (SELECT 1 FROM dim_tree c WHERE c.parent = t.path AND {src_c})) AS kids
            FROM dim_tree t
            WHERE {node} AND {src_t}
            GROUP BY 1, 2, 3, 4 ORDER BY n DESC {limit}
        """)
        return [{"v": p, "level": lv, "parent": pa, "name": nm, "n": int(n), "kids": bool(k)} for p, lv, pa, nm, n, k in rows]

    def get_options(self):
        src = self._rows("SELECT source, n FROM dim_source ORDER BY n DESC")
        co = self._rows("SELECT country, sum(n) FROM dim_country GROUP BY 1 ORDER BY 2 DESC")
        total, used = self._one("SELECT count(*), count(used_at) FROM places")
        used_contacts = self._one("SELECT count(*) FROM used_contacts")[0]
        return {
            "sources": [{"v": a, "n": b} for a, b in src],
            "countries": [{"v": a, "n": int(b)} for a, b in co],
            "total": total, "used": used, "used_contacts": used_contacts, "excel": self.excel_ok,
        }

    def get_industries(self, sources):
        rows = self._rows(f"SELECT industry, sum(n) FROM dim_industry WHERE {self._src_where(sources)} GROUP BY 1 ORDER BY 2 DESC")
        return [{"v": a, "n": int(b)} for a, b in rows]

    def get_states(self, sources):
        rows = self._rows(f"SELECT state, sum(n) FROM dim_state WHERE {self._src_where(sources)} GROUP BY 1 ORDER BY 2 DESC")
        return [{"v": a, "n": int(b)} for a, b in rows]

    def get_categories(self, industries, search="", sources=None):
        where = self._src_where(sources)
        if industries:
            where += f" AND industry IN ({sql_in(industries)})"
        if search:
            where += f" AND category ILIKE {sql_str('%' + search + '%')}"
        rows = self._rows(f"SELECT category, sum(n) AS n FROM dim_category WHERE {where} GROUP BY 1 ORDER BY n DESC LIMIT 400")
        return [{"v": a, "n": int(b)} for a, b in rows]

    def get_cities(self, states, search="", sources=None):
        where = self._src_where(sources)
        if states:
            where += f" AND state IN ({sql_in(states)})"
        if search:
            where += f" AND city ILIKE {sql_str('%' + search + '%')}"
        rows = self._rows(f"SELECT state, city, sum(n) AS n FROM dim_city WHERE {where} GROUP BY 1, 2 ORDER BY n DESC LIMIT 400")
        return [{"v": f"{s}||{c}", "label": f"{c}, {s}", "n": int(n)} for s, c, n in rows]

    def count(self, filters):
        where = self._where(filters or {})
        t, ph, em, we, fb = self._one(f"SELECT count(*), count(phone), count(email), count(website), count(facebook_id) FROM places WHERE {where}")
        per_src = self._rows(f"SELECT source, count(*) FROM places WHERE {where} GROUP BY 1 ORDER BY 2 DESC")
        return {"total": t, "phone": ph, "email": em, "website": we, "facebook": fb,
                "by_source": [{"v": a, "n": b} for a, b in per_src]}

    def preview(self, filters, limit=200, offset=0):
        where = self._where(filters or {})
        cols, rows = self._q(f"""
            SELECT source, business_name, phone, email, website, address, city, state, zip, categories, date_closed, used_at
            FROM places WHERE {where}
            ORDER BY state, city, business_name
            LIMIT {int(limit)} OFFSET {int(offset)}
        """)
        return {"columns": cols, "rows": [list(r) for r in rows]}

    def export_csv(self, filters):
        """Write matching rows to CSV. Exports never change usage: only imports feed Used Data."""
        path = self._save_dialog("leads_export.csv")
        if not path:
            return {"ok": False, "message": "Export cancelled."}
        where = self._where(filters or {})
        stamp = utc_now()
        with self._lock:
            self._con.execute(f"""
                COPY (SELECT {', '.join(EXPORT_COLUMNS)} FROM places WHERE {where} ORDER BY state, city, business_name)
                TO {fs(path)} (HEADER, DELIMITER ',', QUOTE '"', ESCAPE '"')
            """)
            n = self._con.execute(f"SELECT count(*) FROM places WHERE {where}").fetchone()[0]
            self._con.execute("""
                INSERT INTO history (id, kind, filename, occurred_at, source, industry, source_type, source_category, category,
                                     rows, rows_failed, rows_skipped, places_marked, mapping_json, filters_json, error_sample, output_path)
                VALUES (nextval('seq_history'), 'export', ?, ?, NULL, NULL, NULL, NULL, NULL, ?, 0, 0, 0, NULL, ?, NULL, ?)
            """, [Path(path).name, stamp, n, json.dumps(filters or {}), path])
        size = Path(path).stat().st_size
        return {"ok": True, "path": path, "rows": n, "size": size}

    # ======================================================================
    # USED DATA (used_contacts)
    # ======================================================================
    def _used_where(self, f: dict, skip_facet=None) -> str:
        clauses = []
        for facet in USED_FACETS:
            if facet == skip_facet:
                continue
            vals = [x for x in (f.get(facet) or []) if x is not None]
            if vals:
                clauses.append(f"{facet} IN ({sql_in(vals)})")
        if f.get("has_phone"):
            clauses.append("phone IS NOT NULL")
        if f.get("has_email"):
            clauses.append("email IS NOT NULL")
        q = (f.get("q") or "").strip()
        if q:
            like = sql_str("%" + q + "%")
            digits = re.sub(r"\D", "", q)
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            parts = [f"name ILIKE {like}", f"email ILIKE {like}", f"address_line_1 ILIKE {like}",
                     f"website ILIKE {like}", f"npi ILIKE {like}", f"specialty ILIKE {like}"]
            if len(digits) >= 4:
                parts.append(f"phone LIKE {sql_str('%' + digits + '%')}")
            clauses.append("(" + " OR ".join(parts) + ")")
        return " AND ".join(clauses) if clauses else "TRUE"

    def used_facet(self, filters, facet, search="", limit=300):
        if facet not in USED_FACETS:
            return []
        where = self._used_where(filters or {}, skip_facet=facet) + f" AND {facet} IS NOT NULL"
        if search:
            where += f" AND {facet} ILIKE {sql_str('%' + search + '%')}"
        rows = self._rows(f"SELECT {facet}, count(*) AS n FROM used_contacts WHERE {where} GROUP BY 1 ORDER BY n DESC, 1 LIMIT {int(limit)}")
        return [{"v": a, "n": int(b)} for a, b in rows]

    def used_facets(self, filters):
        return {facet: self.used_facet(filters, facet) for facet in USED_FACETS}

    def used_count(self, filters):
        where = self._used_where(filters or {})
        t, ph, em = self._one(f"SELECT count(*), count(phone), count(email) FROM used_contacts WHERE {where}")
        return {"total": t, "phone": ph, "email": em}

    def used_preview(self, filters, limit=200, offset=0):
        where = self._used_where(filters or {})
        cols, rows = self._q(f"SELECT {', '.join(USED_PREVIEW)} FROM used_contacts WHERE {where} ORDER BY id DESC LIMIT {int(limit)} OFFSET {int(offset)}")
        return {"columns": cols, "rows": [list(r) for r in rows]}

    def used_export(self, filters, fmt="csv"):
        fmt = "xlsx" if fmt == "xlsx" and self.excel_ok else "csv"
        path = self._save_dialog("used_contacts_export." + fmt, fmt)
        if not path:
            return {"ok": False, "message": "Export cancelled."}
        where = self._used_where(filters or {})
        opts = "(HEADER, DELIMITER ',', QUOTE '\"', ESCAPE '\"')" if fmt == "csv" else "WITH (FORMAT xlsx, HEADER true)"
        with self._lock:
            self._con.execute(f"COPY (SELECT {', '.join(USED_FIELDS)} FROM used_contacts WHERE {where} ORDER BY id) TO {fs(path)} {opts}")
            n = self._con.execute(f"SELECT count(*) FROM used_contacts WHERE {where}").fetchone()[0]
        return {"ok": True, "path": path, "rows": n, "size": Path(path).stat().st_size}

    # ======================================================================
    # FILE READING (Import & Filter File)
    # ======================================================================
    def open_file(self):
        types = ("Data files (*.csv;*.xlsx;*.txt)", "All files (*.*)")
        res = self._window.create_file_dialog(webview.OPEN_DIALOG, directory=str(HERE.parent), allow_multiple=False, file_types=types)
        if not res:
            return {"ok": False, "message": "No file selected."}
        path = res[0] if isinstance(res, (list, tuple)) else res
        return self.file_info(str(path))

    def _rel(self, path: str, sheet=None) -> str:
        """SQL relation that reads the file with every column as VARCHAR."""
        p = Path(path)
        ext = p.suffix.lower()
        if ext == ".xlsx":
            if not self.excel_ok:
                raise RuntimeError("Excel support is unavailable (DuckDB excel extension failed to load). Save the file as CSV.")
            sheet_opt = f", sheet={sql_str(sheet)}" if sheet else ""
            return f"read_xlsx({fs(path)}{sheet_opt}, header=true, all_varchar=true)"
        if ext == ".xls":
            raise RuntimeError("Old .xls workbooks are not supported. Open it in Excel and save as .xlsx or .csv.")
        enc, lenient = self._csv_mode.get(path, (None, False))
        if enc is None:
            enc, lenient = self._probe_csv(path)
            self._csv_mode[path] = (enc, lenient)
        extra = ", ignore_errors=true, null_padding=true" if lenient else ""
        return f"read_csv({fs(path)}, header=true, all_varchar=true, encoding={sql_str(enc)}{extra})"

    def _probe_csv(self, path):
        for enc, lenient in (("utf-8", False), ("latin-1", False), ("utf-8", True), ("latin-1", True)):
            extra = ", ignore_errors=true, null_padding=true" if lenient else ""
            try:
                with self._lock:
                    self._con.execute(f"SELECT count(*) FROM read_csv({fs(path)}, header=true, all_varchar=true, encoding={sql_str(enc)}{extra})").fetchone()
                return enc, lenient
            except Exception:
                continue
        raise RuntimeError("Could not parse this CSV file.")

    def file_info(self, path: str, sheet=None):
        p = Path(path)
        if not p.exists():
            return {"ok": False, "message": f"File not found: {path}"}
        sheets = xlsx_sheets(p) if p.suffix.lower() == ".xlsx" else []
        if sheets and (not sheet or sheet not in sheets):
            sheet = sheets[0]
        try:
            rel = self._rel(path, sheet)
            cols, rows = self._q(f"SELECT * FROM {rel} LIMIT 8")
            total = self._one(f"SELECT count(*) FROM {rel}")[0]
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        enc, lenient = self._csv_mode.get(path, ("", False))
        return {
            "ok": True, "path": path, "name": p.name, "sheets": sheets, "sheet": sheet,
            "headers": cols, "sample": [list(r) for r in rows], "rows": total,
            "mapping": suggest_mapping(cols), "industry_default": default_industry(p.name),
            "encoding": enc, "lenient": lenient,
        }

    # ======================================================================
    # CATALOGS
    # ======================================================================
    def catalogs(self):
        rows = self._rows("SELECT kind, name FROM catalogs ORDER BY kind, lower(name)")
        out = {k: [] for k in CATALOG_KINDS}
        for k, n in rows:
            out.setdefault(k, []).append(n)
        return out

    def _catalog_add_locked(self, kind, name):
        self._con.execute("INSERT INTO catalogs SELECT ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM catalogs WHERE kind = ? AND name = ?)",
                          [kind, name, utc_now(), kind, name])

    def add_catalog(self, kind, name):
        name = (name or "").strip()
        if kind not in CATALOG_KINDS or not name:
            return {"ok": False, "message": "Enter a value first."}
        with self._lock:
            self._catalog_add_locked(kind, name)
        return {"ok": True, "name": name}

    # ======================================================================
    # IMPORT & MAP
    # ======================================================================
    def run_import(self, path, sheet, mapping, meta, opts=None):
        opts = opts or {}
        mapping = {k: v for k, v in (mapping or {}).items() if v}
        meta = {k: (v or "").strip() for k, v in (meta or {}).items()}
        for req in ("source", "industry", "source_type", "source_category"):
            if not meta.get(req):
                return {"ok": False, "message": f"Please select or add: {req.replace('_', ' ').title()}."}
        if not any(mapping.get(f) for f in ("name", "first_name", "last_name", "email", "phone")):
            return {"ok": False, "message": "Map at least one identifier: Name, First/Last name, Email, or Phone."}
        rel = self._rel(path, sheet)

        def col(field):
            h = mapping.get(field)
            return f"nullif(trim({qident(h)}), '')" if h else "NULL"

        name_expr = f"coalesce(nullif(trim(concat_ws(' ', {col('first_name')}, {col('last_name')})), ''), {col('name')})"
        stamp = utc_now()
        fname = Path(path).name
        category = meta.get("category") or None
        select = f"""
            SELECT {name_expr} AS name, norm_email({col('email')}) AS email,
                   {col('address_line_1')} AS address_line_1, {col('address_line_2')} AS address_line_2,
                   {col('city')} AS city, norm_state({col('state')}) AS state, {col('postal_code')} AS postal_code,
                   upper({col('country')}) AS country, norm_phone({col('phone')}) AS phone, {col('fax')} AS fax,
                   {col('website')} AS website, {sql_str(meta['industry'])} AS industry,
                   {col('original_industry')} AS original_industry, {sql_str(category) if category else 'NULL'} AS category,
                   {col('alternate_categories')} AS alternate_categories, {col('credentials')} AS credentials,
                   {col('specialty')} AS specialty, {col('rating')} AS rating, {col('reviews')} AS reviews,
                   {col('npi')} AS npi, {col('enumeration_date')} AS enumeration_date,
                   {sql_str(meta['source'])} AS source, {sql_str(fname)} AS source_file,
                   {sql_str(meta['source_type'])} AS source_type, {sql_str(sheet) if sheet else 'NULL'} AS source_sheet,
                   {sql_str(meta['source_category'])} AS source_category, {sql_str(stamp)} AS uploaded_at,
                   to_json(__src) AS raw_data, {sql_str(stamp)} AS created_at
            FROM {rel} __src
        """
        with self._lock:
            self._con.execute("BEGIN TRANSACTION")
            try:
                hid = self._con.execute("SELECT nextval('seq_history')").fetchone()[0]
                self._con.execute("DROP TABLE IF EXISTS imp")
                self._con.execute(f"CREATE TEMP TABLE imp AS {select}")
                total = self._con.execute("SELECT count(*) FROM imp").fetchone()[0]
                # Duplicate rule (always on): a row is a duplicate only when BOTH its phone and its
                # email already exist in Used Data. If just one of them matches, the row is kept.
                dup_existing = self._dml("""
                    DELETE FROM imp WHERE phone IS NOT NULL AND email IS NOT NULL
                      AND phone IN (SELECT phone FROM used_contacts WHERE phone IS NOT NULL)
                      AND email IN (SELECT email FROM used_contacts WHERE email IS NOT NULL)
                """)
                # Same rule inside the file: keep the first row of each identical phone+email pair.
                dup_infile = self._dml("""
                    DELETE FROM imp WHERE rowid IN (
                        SELECT rowid FROM (
                            SELECT rowid, row_number() OVER (PARTITION BY phone, email ORDER BY rowid) AS rn
                            FROM imp WHERE phone IS NOT NULL AND email IS NOT NULL
                        ) WHERE rn > 1)
                """)
                skipped_strict = 0
                if opts.get("skip_existing"):
                    skipped_strict += self._dml("DELETE FROM imp WHERE phone IS NOT NULL AND phone IN (SELECT phone FROM used_contacts WHERE phone IS NOT NULL)")
                    skipped_strict += self._dml("DELETE FROM imp WHERE email IS NOT NULL AND email IN (SELECT email FROM used_contacts WHERE email IS NOT NULL)")
                skipped_blank = 0
                if opts.get("skip_blank"):
                    skipped_blank = self._dml("DELETE FROM imp WHERE phone IS NULL AND email IS NULL")
                skipped = dup_existing + dup_infile + skipped_strict + skipped_blank
                imported = self._dml(f"INSERT INTO used_contacts SELECT nextval('seq_used_contacts'), imp.*, {hid} FROM imp")
                marked = self._dml(f"UPDATE places SET used_at = {sql_str(stamp)}, used_reason = 'import:{hid}' "
                                   "WHERE used_at IS NULL AND phone IN (SELECT DISTINCT phone FROM imp WHERE phone IS NOT NULL)")
                marked += self._dml(f"UPDATE places SET used_at = {sql_str(stamp)}, used_reason = 'import:{hid}' "
                                    "WHERE used_at IS NULL AND email IN (SELECT DISTINCT email FROM imp WHERE email IS NOT NULL)")
                self._con.execute("DROP TABLE imp")
                for kind in CATALOG_KINDS:
                    if meta.get(kind):
                        self._catalog_add_locked(kind, meta[kind])
                self._con.execute("""
                    INSERT INTO history (id, kind, filename, occurred_at, source, industry, source_type, source_category, category,
                                         rows, rows_failed, rows_skipped, places_marked, mapping_json, filters_json, error_sample, output_path)
                    VALUES (?, 'import', ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, NULL, ?, ?)
                """, [hid, fname, stamp, meta["source"], meta["industry"], meta["source_type"], meta["source_category"],
                      category, imported, skipped, marked, json.dumps(mapping),
                      f"duplicates of Used Data: {dup_existing}; duplicates within file: {dup_infile}; "
                      f"phone-or-email already used: {skipped_strict}; no phone or email: {skipped_blank}", path])
                self._con.execute("COMMIT")
            except Exception:
                self._con.execute("ROLLBACK")
                raise
        return {"ok": True, "total": total, "imported": imported, "skipped": skipped, "marked": marked, "history_id": hid,
                "dup_existing": dup_existing, "dup_infile": dup_infile, "skipped_strict": skipped_strict, "skipped_blank": skipped_blank}

    # ======================================================================
    # FILTER FILE (remove rows already in Used Data, normalize phones)
    # ======================================================================
    def filter_file(self, path, sheet, phone_col, email_col, match_phone=True, match_email=True):
        phone_col = phone_col or None
        email_col = email_col or None
        rules = []
        if match_phone and phone_col:
            rules.append(f"coalesce(norm_phone({qident(phone_col)}) IN (SELECT phone FROM used_contacts WHERE phone IS NOT NULL), false)")
        if match_email and email_col:
            rules.append(f"coalesce(norm_email({qident(email_col)}) IN (SELECT email FROM used_contacts WHERE email IS NOT NULL), false)")
        if not rules:
            return {"ok": False, "message": "Select at least one match rule and map its column."}
        out = self._save_dialog(f"filtered_{Path(path).stem}.csv")
        if not out:
            return {"ok": False, "message": "Cancelled."}
        rel = self._rel(path, sheet)
        match = " OR ".join(rules)
        replace = f" REPLACE (norm_phone({qident(phone_col)}) AS {qident(phone_col)})" if phone_col else ""
        norm_expr = (f"count(*) FILTER (WHERE trim(coalesce({qident(phone_col)}, '')) <> coalesce(norm_phone({qident(phone_col)}), ''))"
                     if phone_col else "0")
        stamp = utc_now()
        with self._lock:
            total, removed, normalized = self._con.execute(
                f"SELECT count(*), count(*) FILTER (WHERE {match}), {norm_expr} FROM {rel}").fetchone()
            self._con.execute(f"COPY (SELECT *{replace} FROM {rel} WHERE NOT ({match})) TO {fs(out)} (HEADER, DELIMITER ',', QUOTE '\"', ESCAPE '\"')")
            self._con.execute("""
                INSERT INTO history (id, kind, filename, occurred_at, source, industry, source_type, source_category, category,
                                     rows, rows_failed, rows_skipped, places_marked, mapping_json, filters_json, error_sample, output_path)
                VALUES (nextval('seq_history'), 'filter', ?, ?, NULL, NULL, NULL, NULL, NULL, ?, 0, ?, 0, ?, NULL, NULL, ?)
            """, [Path(path).name, stamp, total - removed, removed, json.dumps({"phone": phone_col, "email": email_col}), out])
        return {"ok": True, "path": out, "total": total, "kept": total - removed, "removed": removed, "normalized": normalized}

    # ======================================================================
    # HISTORY
    # ======================================================================
    def history(self, limit=300):
        cols, rows = self._q(f"""
            SELECT id, kind, filename, occurred_at, source, industry, source_type, source_category, category,
                   rows, rows_skipped, places_marked, output_path
            FROM history ORDER BY occurred_at DESC, id DESC LIMIT {int(limit)}
        """)
        return {"columns": cols, "rows": [list(r) for r in rows]}

    def open_path(self, path):
        try:
            os.startfile(path)  # noqa: S606 - Windows desktop app
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    # ======================================================================
    # DATA UPDATES (new Foursquare / Overture / OpenStreetMap releases)
    # ======================================================================
    def _meta(self, key):
        row = self._rows("SELECT value FROM meta WHERE key = ?", [key])
        return row[0][0] if row else None

    def updates_status(self):
        """Installed versions plus the last online check (cached in meta); stale after 24 hours."""
        installed = {k.split(":", 1)[1]: v for k, v in self._rows("SELECT key, value FROM meta WHERE key LIKE 'source_version:%'")}
        updated = {k.split(":", 1)[1]: v for k, v in self._rows("SELECT key, value FROM meta WHERE key LIKE 'source_updated_at:%'")}
        cached = self._meta("update_check")
        data = json.loads(cached) if cached else {"checked_at": None, "results": []}
        latest = {r["source"]: r for r in data.get("results", [])}
        results = []
        for source in sources.SOURCES:
            r = latest.get(source, {})
            cur = installed.get(source)
            ver = r.get("version")
            results.append({
                "source": source, "installed": cur, "updated_at": updated.get(source),
                "version": ver, "size": r.get("size", 0), "size_text": sources.fmt_size(r["size"]) if r.get("size") else "",
                "note": r.get("note", ""), "error": r.get("error"),
                "available": bool(ver) and (cur is None or ver > cur),
            })
        stale = True
        if data.get("checked_at"):
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(data["checked_at"])
                stale = age.total_seconds() > 24 * 3600
            except ValueError:
                stale = True
        return {"checked_at": data.get("checked_at"), "stale": stale, "results": results,
                "available": [r["source"] for r in results if r["available"]]}

    def check_updates(self):
        """Ask each source for its newest release (network) and cache the answer."""
        results = sources.check_all(sources.get_hf_token())
        payload = json.dumps({"checked_at": utc_now(), "results": results})
        with self._lock:
            self._con.execute("DELETE FROM meta WHERE key = 'update_check'")
            self._con.execute("INSERT INTO meta VALUES ('update_check', ?)", [payload])
        return self.updates_status()

    def start_update(self, source):
        """Launch the updater in its own console and close this app so it can take the database."""
        if source not in sources.SOURCES:
            return {"ok": False, "message": f"Unknown source {source}"}
        exe = Path(sys.executable)
        if exe.name.lower() == "pythonw.exe":
            exe = exe.with_name("python.exe")
        try:
            subprocess.Popen([str(exe), str(UPDATE_SCRIPT), "--update", source, "--yes", "--relaunch"],
                             cwd=str(ROOT), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        except Exception as exc:
            return {"ok": False, "message": f"Could not start the updater: {exc}"}
        threading.Timer(1.5, self._shutdown).start()
        return {"ok": True}

    def _shutdown(self):
        try:
            with self._lock:
                self._con.close()
        finally:
            try:
                self._window.destroy()
            except Exception:
                os._exit(0)


def message_box(text: str, title: str = "Leads Explorer", yes_no: bool = False) -> bool:
    """Native Windows dialog (works under pythonw, where the console is invisible). Returns True for Yes/OK."""
    try:
        import ctypes
        flags = (0x04 | 0x20) if yes_no else (0x00 | 0x10)   # MB_YESNO+question / MB_OK+error
        return ctypes.windll.user32.MessageBoxW(0, text, title, flags) in (1, 6)   # IDOK, IDYES
    except Exception:
        print(f"{title}: {text}")
        return False


def main():
    if not DB.exists():
        ok = message_box(
            f"It seems no database was found at:\n{DB}\n\n"
            "Create a new empty database and start from scratch?\n\n"
            "All Leads will be empty until you run build_db.py with the pipeline files. "
            "Used Data fills up through Import & Map.",
            "Leads Explorer - no database", yes_no=True)
        if not ok:
            return
        try:
            create_empty_db(DB, utc_now())
        except Exception as exc:
            message_box(f"Could not create the database:\n{exc}", "Leads Explorer - error")
            return
    try:
        api = Api()
    except SystemExit as exc:
        message_box(str(exc), "Leads Explorer - cannot start")
        return
    except Exception as exc:
        message_box(f"Could not open the database:\n{exc}", "Leads Explorer - cannot start")
        return
    window = webview.create_window(
        "Leads Explorer - USA Business Contacts",
        str(UI),
        js_api=api,
        width=1400,
        height=850,
        min_size=(1000, 650),
        maximized=True,
    )
    api._window = window
    webview.start()


if __name__ == "__main__":
    main()
