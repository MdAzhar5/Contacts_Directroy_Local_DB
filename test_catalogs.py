from __future__ import annotations

import sqlite3
from pathlib import Path

import db

conn = sqlite3.connect(db.DB_PATH)
counts = {}
for table in ("sources", "industries", "source_types", "source_categories", "categories"):
    counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
source_types = [row[0] for row in conn.execute("SELECT name FROM source_types ORDER BY name").fetchall()]
contacts = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
uploaded = conn.execute("SELECT COUNT(*) FROM contacts WHERE TRIM(uploaded_at) <> ''").fetchone()[0]
source_type_rows = conn.execute("SELECT COUNT(*) FROM contacts WHERE source_type IN ('Med', 'Not Med')").fetchone()[0]
assert contacts == 279522
assert uploaded == contacts
assert counts["sources"] >= 1
assert counts["industries"] >= 11
assert counts["source_types"] >= 2
assert "Med" in source_types and "Not Med" in source_types
assert source_type_rows == contacts
print({"status": "ok", "counts": counts, "source_types": source_types, "contacts": contacts, "uploaded_at_rows": uploaded, "classified_source_type_rows": source_type_rows})
