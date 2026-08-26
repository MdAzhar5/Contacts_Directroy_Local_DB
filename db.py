from __future__ import annotations

import json
import os
import re
import sqlite3
from decimal import Decimal, InvalidOperation
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("CONTACT_DIRECTORY_DB", str(BASE_DIR / "data" / "contacts.db")))

DISPLAY_FIELDS = [
    "name", "email", "address_line_1", "city", "state", "postal_code", "country", "phone",
    "industry", "specialty", "category", "source", "uploaded_at", "source_file"
]
EXPORT_FIELDS = [
    "name", "email", "address_line_1", "address_line_2", "city", "state", "postal_code", "country",
    "phone", "fax", "website", "industry", "original_industry", "category", "alternate_categories",
    "credentials", "specialty", "rating", "reviews", "npi", "enumeration_date", "source", "source_file",
    "source_type", "source_sheet", "source_category", "uploaded_at"
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def connection():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                address_line_1 TEXT NOT NULL DEFAULT '',
                address_line_2 TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT '',
                postal_code TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                fax TEXT NOT NULL DEFAULT '',
                website TEXT NOT NULL DEFAULT '',
                industry TEXT NOT NULL DEFAULT '',
                original_industry TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                alternate_categories TEXT NOT NULL DEFAULT '',
                credentials TEXT NOT NULL DEFAULT '',
                specialty TEXT NOT NULL DEFAULT '',
                rating TEXT NOT NULL DEFAULT '',
                reviews TEXT NOT NULL DEFAULT '',
                npi TEXT NOT NULL DEFAULT '',
                enumeration_date TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                source_file TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT '',
                source_sheet TEXT NOT NULL DEFAULT '',
                source_category TEXT NOT NULL DEFAULT '',
                uploaded_at TEXT NOT NULL DEFAULT '',
                raw_data TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS import_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_filename TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                uploaded_at TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                source_category TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                rows_imported INTEGER NOT NULL DEFAULT 0,
                rows_failed INTEGER NOT NULL DEFAULT 0,
                mapping_json TEXT NOT NULL DEFAULT '{}',
                error_sample TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS categories (
                name TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_categories (
                name TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sources (
                name TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS industries (
                name TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_types (
                name TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            """
        )
        # Migrate databases created by the earlier project version without deleting data.
        for column, definition in [
            ("email", "TEXT NOT NULL DEFAULT ''"), ("source", "TEXT NOT NULL DEFAULT ''"),
            ("uploaded_at", "TEXT NOT NULL DEFAULT ''"),
        ]:
            _ensure_column(conn, "contacts", column, definition)
        for column, definition in [
            ("uploaded_at", "TEXT NOT NULL DEFAULT ''"), ("source", "TEXT NOT NULL DEFAULT ''"),
            ("source_category", "TEXT NOT NULL DEFAULT ''"), ("category", "TEXT NOT NULL DEFAULT ''"),
            ("industry", "TEXT NOT NULL DEFAULT ''"), ("source_type", "TEXT NOT NULL DEFAULT ''"),
        ]:
            _ensure_column(conn, "import_history", column, definition)
        if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
            rows = conn.execute("SELECT id, phone FROM contacts WHERE TRIM(phone) <> ''").fetchall()
            updates = []
            for row in rows:
                canonical = canonical_phone(row["phone"])
                if canonical != row["phone"]:
                    updates.append((canonical, row["id"]))
            if updates:
                conn.executemany("UPDATE contacts SET phone = ? WHERE id = ?", updates)
            conn.execute("PRAGMA user_version = 2")
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts(name);
            CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
            CREATE INDEX IF NOT EXISTS idx_contacts_phone ON contacts(phone);
            CREATE INDEX IF NOT EXISTS idx_contacts_industry ON contacts(industry);
            CREATE INDEX IF NOT EXISTS idx_contacts_city ON contacts(city);
            CREATE INDEX IF NOT EXISTS idx_contacts_state ON contacts(state);
            CREATE INDEX IF NOT EXISTS idx_contacts_postal_code ON contacts(postal_code);
            CREATE INDEX IF NOT EXISTS idx_contacts_category ON contacts(category);
            CREATE INDEX IF NOT EXISTS idx_contacts_source ON contacts(source);
            CREATE INDEX IF NOT EXISTS idx_contacts_source_category ON contacts(source_category);
            CREATE INDEX IF NOT EXISTS idx_contacts_uploaded_at ON contacts(uploaded_at);
            """
        )
        now = utc_now()
        conn.execute("INSERT OR IGNORE INTO categories(name, created_at) SELECT DISTINCT category, ? FROM contacts WHERE TRIM(category) <> ''", (now,))
        conn.execute("INSERT OR IGNORE INTO source_categories(name, created_at) SELECT DISTINCT source_category, ? FROM contacts WHERE TRIM(source_category) <> ''", (now,))
        conn.execute("INSERT OR IGNORE INTO sources(name, created_at) SELECT DISTINCT source, ? FROM contacts WHERE TRIM(source) <> ''", (now,))
        conn.execute("INSERT OR IGNORE INTO sources(name, created_at) SELECT DISTINCT source, ? FROM import_history WHERE TRIM(source) <> ''", (now,))
        conn.execute("INSERT OR IGNORE INTO industries(name, created_at) SELECT DISTINCT industry, ? FROM contacts WHERE TRIM(industry) <> ''", (now,))
        conn.execute("INSERT OR IGNORE INTO source_types(name, created_at) SELECT DISTINCT source_type, ? FROM contacts WHERE TRIM(source_type) <> ''", (now,))
        conn.execute("INSERT OR IGNORE INTO source_types(name, created_at) VALUES ('Med', ?), ('Not Med', ?)", (now, now))


def count_contacts() -> int:
    with connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0])


def distinct_values(field: str, limit: int = 300) -> list[str]:
    allowed = {"industry", "city", "state", "country", "category", "source_category", "source_file", "source", "source_type"}
    if field not in allowed:
        return []
    with connection() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {field} FROM contacts WHERE TRIM({field}) <> '' ORDER BY {field} COLLATE NOCASE LIMIT ?",
            (limit,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def option_values(table: str) -> list[str]:
    if table not in {"categories", "source_categories", "sources", "industries", "source_types"}:
        return []
    with connection() as conn:
        rows = conn.execute(f"SELECT name FROM {table} ORDER BY name COLLATE NOCASE").fetchall()
    return [str(row[0]) for row in rows]


def add_option(table: str, value: str) -> str:
    if table not in {"categories", "source_categories", "sources", "industries", "source_types"}:
        raise ValueError("Unsupported option catalog")
    value = str(value or "").strip()
    if not value:
        raise ValueError("Option value cannot be blank")
    with connection() as conn:
        conn.execute(f"INSERT OR IGNORE INTO {table}(name, created_at) VALUES (?, ?)", (value, utc_now()))
    return value


def _save_option(conn: sqlite3.Connection, table: str, value: str) -> None:
    value = value.strip()
    if value:
        conn.execute(f"INSERT OR IGNORE INTO {table}(name, created_at) VALUES (?, ?)", (value, utc_now()))


def _filter_clause(filters: dict) -> tuple[str, list[str]]:
    where: list[str] = []
    params: list[str] = []
    exact_fields = ["industry", "city", "state", "country", "category", "source_category", "source_file", "source", "source_type"]
    for field in exact_fields:
        value = str(filters.get(field, "")).strip()
        if value:
            where.append(f"{field} = ?")
            params.append(value)
    q = str(filters.get("q", "")).strip()
    if q:
        phone_query = canonical_phone(q)
        if phone_query:
            where.append("(phone = ? OR name LIKE ? OR email LIKE ? OR address_line_1 LIKE ? OR address_line_2 LIKE ? OR phone LIKE ? OR website LIKE ? OR specialty LIKE ? OR npi LIKE ?)")
            like = f"%{q}%"
            params.extend([phone_query, like, like, like, like, like, like, like, like])
        else:
            where.append("(name LIKE ? OR email LIKE ? OR address_line_1 LIKE ? OR address_line_2 LIKE ? OR phone LIKE ? OR website LIKE ? OR specialty LIKE ? OR npi LIKE ?)")
            like = f"%{q}%"
            params.extend([like] * 8)
    return (" WHERE " + " AND ".join(where) if where else ""), params


def query_contacts(filters: dict, page: int = 1, per_page: int = 50) -> tuple[list[sqlite3.Row], int]:
    clause, params = _filter_clause(filters)
    allowed_sort = set(DISPLAY_FIELDS + ["id", "created_at"])
    sort = filters.get("sort", "id") if filters.get("sort", "id") in allowed_sort else "id"
    direction = "DESC" if filters.get("direction") == "desc" else "ASC"
    offset = max(0, page - 1) * per_page
    with connection() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM contacts{clause}", params).fetchone()[0])
        rows = conn.execute(
            f"SELECT id, {', '.join(DISPLAY_FIELDS)} FROM contacts{clause} ORDER BY {sort} {direction}, id ASC LIMIT ? OFFSET ?",
            [*params, per_page, offset],
        ).fetchall()
    return rows, total


def iter_filtered_contacts(filters: dict, batch_size: int = 1000):
    clause, params = _filter_clause(filters)
    conn = get_connection()
    try:
        cursor = conn.execute(f"SELECT {', '.join(EXPORT_FIELDS)} FROM contacts{clause} ORDER BY id ASC", params)
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            yield from batch
    finally:
        conn.close()


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def canonical_phone(value) -> str:
    """Return a clean US-style 10-digit phone or blank when it is not safely valid.

    Punctuation, spaces, plus signs, hyphens, dots, and parentheses are removed.
    A leading country code 1 is removed only when the result is exactly 10 digits.
    Scientific-notation strings from spreadsheet exports are expanded before validation.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?[eE][+-]?\d+", text):
        try:
            text = format(Decimal(text), "f")
        except (InvalidOperation, ValueError):
            return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def phone_keys(value: str) -> set[str]:
    canonical = canonical_phone(value)
    return {canonical} if canonical else set()


def existing_match_sets(match_fields: list[str]) -> dict[str, set[str]]:
    result = {field: set() for field in match_fields}
    with connection() as conn:
        rows = conn.execute("SELECT email, phone FROM contacts").fetchall()
    for row in rows:
        if "email" in result:
            email = normalize_email(row["email"])
            if email:
                result["email"].add(email)
        if "phone" in result:
            result["phone"].update(phone_keys(row["phone"]))
    return result


def insert_contacts(rows: Iterable[dict], mapping: dict, metadata: dict) -> tuple[int, int, list[str]]:
    columns = [
        "name", "email", "address_line_1", "address_line_2", "city", "state", "postal_code", "country",
        "phone", "fax", "website", "industry", "original_industry", "category", "alternate_categories",
        "credentials", "specialty", "rating", "reviews", "npi", "enumeration_date", "source", "source_file",
        "source_type", "source_sheet", "source_category", "uploaded_at", "raw_data", "created_at"
    ]
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO contacts ({', '.join(columns)}) VALUES ({placeholders})"
    imported = 0
    failed = 0
    errors: list[str] = []
    batch: list[tuple[str, ...]] = []
    upload_source = str(metadata.get("source", "")).strip()
    source_category_default = str(metadata.get("source_category", "")).strip()
    category_default = str(metadata.get("category", "")).strip()
    source_type_default = str(metadata.get("source_type", "")).strip()
    industry_default = str(metadata.get("industry", "")).strip()
    uploaded_at = str(metadata.get("uploaded_at", "")).strip() or utc_now()
    original_filename = str(metadata.get("original_filename", "")).strip()

    def clean(value) -> str:
        return "" if value is None else str(value).strip()

    def make_record(source: dict) -> tuple[str, ...]:
        def mapped(field: str) -> str:
            source_header = mapping.get(field, "")
            return clean(source.get(source_header, "")) if source_header else ""
        first = mapped("first_name")
        last = mapped("last_name")
        source_name = mapped("name")
        name = " ".join(x for x in (first, last) if x).strip() or source_name
        industry = mapped("industry") or industry_default or "Imported"
        category = mapped("category") or category_default
        source_category = mapped("source_category") or source_category_default
        source_value = mapped("source") or upload_source
        source_type = mapped("source_type") or source_type_default
        if not source_type:
            source_type = "Med" if source_category.strip().lower() in {"medical", "med"} else "Not Med" if source_category.strip().lower() in {"non_medical", "not med", "not medical"} else "Imported"
        raw = {k: clean(v) for k, v in source.items() if clean(v) != ""}
        canonical_phone_value = canonical_phone(mapped("phone"))
        return (
            name, mapped("email"), mapped("address_line_1"), mapped("address_line_2"), mapped("city"), mapped("state"),
            mapped("postal_code"), mapped("country"), canonical_phone_value, mapped("fax"), mapped("website"), industry,
            mapped("original_industry"), category, mapped("alternate_categories"), mapped("credentials"), mapped("specialty"),
            mapped("rating"), mapped("reviews"), mapped("npi"), mapped("enumeration_date"), source_value, original_filename,
            source_type, mapped("source_sheet") or clean(source.get("source_sheet", "")), source_category, uploaded_at,
            json.dumps(raw, ensure_ascii=False, separators=(",", ":")), utc_now()
        )

    with connection() as conn:
        _save_option(conn, "categories", category_default)
        _save_option(conn, "source_categories", source_category_default)
        _save_option(conn, "sources", upload_source)
        _save_option(conn, "industries", industry_default)
        _save_option(conn, "source_types", source_type_default)
        for source in rows:
            try:
                batch.append(make_record(source))
                if len(batch) >= 500:
                    conn.executemany(sql, batch)
                    imported += len(batch)
                    batch.clear()
            except Exception as exc:
                failed += 1
                if len(errors) < 10:
                    errors.append(str(exc))
        if batch:
            conn.executemany(sql, batch)
            imported += len(batch)
        conn.execute(
            "INSERT INTO import_history (original_filename, imported_at, uploaded_at, source, source_category, category, industry, source_type, rows_imported, rows_failed, mapping_json, error_sample) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (original_filename, utc_now(), uploaded_at, upload_source, source_category_default, category_default, industry_default, source_type_default, imported, failed, json.dumps(mapping, ensure_ascii=False), "\n".join(errors)),
        )
    return imported, failed, errors


def recent_imports(limit: int = 20) -> list[sqlite3.Row]:
    with connection() as conn:
        return conn.execute("SELECT * FROM import_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
