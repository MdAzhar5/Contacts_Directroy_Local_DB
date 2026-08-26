from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import db


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value) -> str:
    return "" if value is None else str(value).strip()


def rows_from_csv(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            yield {str(k).strip(): clean(v) for k, v in row.items() if k is not None}


def mapping_for_normalized_csv(headers: list[str]) -> dict[str, str]:
    aliases = {
        "name": ["Name", "name"], "email": ["Email", "email"], "address_line_1": ["Address Line 1", "Address"],
        "address_line_2": ["Address Line 2"], "city": ["City"], "state": ["State"],
        "postal_code": ["Postal Code", "ZIP Code", "Zip", "postcode"], "country": ["Country"],
        "phone": ["Phone"], "fax": ["Fax"], "website": ["Website"], "industry": ["Industry"],
        "original_industry": ["Original Industry"], "category": ["Category"],
        "alternate_categories": ["Alternate Categories"], "credentials": ["Credentials"],
        "specialty": ["Specialty"], "rating": ["Rating"], "reviews": ["Reviews"],
        "npi": ["NPI"], "enumeration_date": ["Enumeration Date", "EnumerationDate"],
        "source": ["Source", "source"], "source_sheet": ["source_sheet"],
        "source_category": ["source_category"], "first_name": ["First Name"], "last_name": ["Last Name"],
    }
    return {field: next((candidate for candidate in candidates if candidate in headers), "") for field, candidates in aliases.items()}


def main():
    parser = argparse.ArgumentParser(description="Initialize and optionally seed the local SQLite database.")
    parser.add_argument("--csv", default="data/normalized_merged_all.csv", help="CSV to import")
    parser.add_argument("--reset", action="store_true", help="Delete existing contacts and import history before seeding")
    parser.add_argument("--source", default="Initial database seed", help="Source label for seeded rows")
    parser.add_argument("--source-category", default="Initial Dataset", help="Source category for seeded rows")
    args = parser.parse_args()
    db.init_db()
    csv_path = Path(args.csv)
    if not csv_path.is_absolute():
        csv_path = Path(__file__).resolve().parent / csv_path
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")
    if args.reset:
        with db.connection() as conn:
            conn.execute("DELETE FROM contacts")
            conn.execute("DELETE FROM import_history")
            conn.execute("DELETE FROM categories")
            conn.execute("DELETE FROM source_categories")
            conn.execute("DELETE FROM sources")
            conn.execute("DELETE FROM industries")
            conn.execute("DELETE FROM source_types")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        headers = [str(x).strip() for x in next(csv.reader(handle), [])]
    mapping = mapping_for_normalized_csv(headers)
    uploaded_at = utc_now()
    metadata = {
        "source": args.source, "source_category": args.source_category, "category": "",
        "industry": "Imported", "uploaded_at": uploaded_at, "original_filename": csv_path.name,
    }
    imported, failed, errors = db.insert_contacts(rows_from_csv(csv_path), mapping, metadata)
    db.init_db()
    print(json.dumps({"database": str(db.DB_PATH), "uploaded_at": uploaded_at, "imported": imported, "failed": failed, "errors": errors}, indent=2))


if __name__ == "__main__":
    main()
