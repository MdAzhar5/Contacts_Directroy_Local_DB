from __future__ import annotations

import csv
import sqlite3
import tempfile
from pathlib import Path

import db
from importers import iter_rows
from desktop_app import ContactDirectoryDesktop

CASES = {
    "+1 (530) 244-4772": "5302444772",
    "530.244.4772": "5302444772",
    "1-530-244-4772": "5302444772",
    " 530 244 4772 ": "5302444772",
    "61533857501": "",
    "+1 0": "",
    "93181500503": "",
    "": "",
}
for raw, expected in CASES.items():
    assert db.canonical_phone(raw) == expected, (raw, db.canonical_phone(raw), expected)

conn = sqlite3.connect(db.DB_PATH)
assert conn.execute('SELECT COUNT(*) FROM contacts').fetchone()[0] == 279522
assert conn.execute("SELECT COUNT(*) FROM contacts WHERE TRIM(phone)<>'' AND (length(phone)<>10 OR phone GLOB '*[^0-9]*')").fetchone()[0] == 0
assert conn.execute("SELECT COUNT(*) FROM contacts WHERE TRIM(uploaded_at)='' ").fetchone()[0] == 0
assert set(conn.execute('SELECT source_type FROM contacts').fetchall()) == {('Med',), ('Not Med',)}
conn.close()

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    source = tmp_path / 'incoming.csv'
    with source.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=['Name', 'Phone', 'Email'])
        writer.writeheader()
        writer.writerow({'Name': 'Canonical Test', 'Phone': '+1 (530) 244-4772', 'Email': 'canonical-test@example.com'})
        writer.writerow({'Name': 'Invalid Test', 'Phone': '+1 0', 'Email': 'invalid-test@example.com'})
    mapping = {'name': 'Name', 'phone': 'Phone', 'email': 'Email'}
    metadata = {'source': 'Final Hardening Test', 'source_category': 'medical', 'category': '', 'source_type': 'Med', 'industry': 'Medical NPI', 'uploaded_at': db.utc_now(), 'original_filename': source.name}
    imported, failed, _ = db.insert_contacts(iter_rows(source), mapping, metadata)
    assert imported == 2 and failed == 0
    conn = sqlite3.connect(db.DB_PATH)
    phone_rows = conn.execute("SELECT phone FROM contacts WHERE source = 'Final Hardening Test' ORDER BY id").fetchall()
    conn.close()
    assert [row[0] for row in phone_rows] == ['5302444772', '']
    try:
        app = ContactDirectoryDesktop()
        output = tmp_path / 'clean.csv'
        mapping_filter = {'phone': 'Phone'}
        result = app._filter_rows(source, output, mapping_filter, ['phone'])
        app.destroy()
        assert result[1] == 1
        assert result[2] == 1
        assert result[3] == 2
    finally:
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("DELETE FROM contacts WHERE source = 'Final Hardening Test'")
        conn.execute("DELETE FROM import_history WHERE source = 'Final Hardening Test'")
        conn.execute("DELETE FROM sources WHERE name = 'Final Hardening Test'")
        conn.commit()
        conn.close()

print({'status': 'ok', 'phone_cases': len(CASES), 'database_phone_invariant': 'all stored phones are blank or 10 digits', 'import_normalization': 'ok', 'filter_normalization': 'ok'})
