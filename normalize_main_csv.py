from __future__ import annotations

import csv
import json
import shutil
from collections import Counter
from pathlib import Path

import db

INPUT = Path('/home/ubuntu/upload/normalized_merged_all.csv')
DATA_DIR = Path(__file__).resolve().parent / 'data'
ORIGINAL_COPY = DATA_DIR / 'normalized_merged_all_input_original.csv'
OUTPUT = DATA_DIR / 'normalized_merged_all.csv'
REPORT = Path(__file__).resolve().parent / 'main_csv_normalization_report.json'

OUTPUT_HEADERS = [
    'Name', 'First Name', 'Last Name', 'Email', 'Address Line 1', 'Address Line 2', 'City', 'State',
    'Postal Code', 'Country', 'Phone', 'Fax', 'Website', 'Industry', 'Original Industry', 'Category',
    'Alternate Categories', 'Credentials', 'Specialty', 'Rating', 'Reviews', 'NPI', 'Enumeration Date',
    'Original Values', 'source_file', 'source_type', 'source_sheet', 'source_category'
]


def value(row, *names):
    for name in names:
        if name in row and str(row[name] or '').strip():
            return str(row[name]).strip()
    return ''


def source_type_for(source_category: str) -> str:
    low = source_category.strip().lower().replace('-', '_').replace(' ', '_')
    if low in {'medical', 'med'}:
        return 'Med'
    if low in {'non_medical', 'not_med', 'not_medical', 'nonmed'}:
        return 'Not Med'
    return 'Imported'

DATA_DIR.mkdir(parents=True, exist_ok=True)
shutil.copy2(INPUT, ORIGINAL_COPY)
counts = Counter()
phone_changes = Counter()
with INPUT.open('r', encoding='utf-8-sig', newline='') as source, OUTPUT.open('w', encoding='utf-8-sig', newline='') as target:
    reader = csv.DictReader(source)
    writer = csv.DictWriter(target, fieldnames=OUTPUT_HEADERS, lineterminator='\n')
    writer.writeheader()
    for row in reader:
        row = {str(k).strip(): ('' if v is None else str(v).strip()) for k, v in row.items() if k is not None}
        source_category = value(row, 'source_category', 'Source Category')
        raw_phone = value(row, 'Phone', 'Phone Number', 'Telephone', 'Mobile')
        phone = db.canonical_phone(raw_phone)
        if raw_phone and not phone:
            phone_changes['invalid_or_non_10_digit_cleared'] += 1
        elif raw_phone and phone != raw_phone:
            phone_changes['canonicalized'] += 1
        if phone:
            phone_changes['valid_canonical'] += 1
        original_values = json.dumps(row, ensure_ascii=False, separators=(',', ':'))
        first_name = value(row, 'First Name', 'Given Name')
        last_name = value(row, 'Last Name', 'Surname', 'Family Name')
        direct_name = value(row, 'Name', 'Business Name', 'Company Name')
        out = {
            'Name': direct_name or ' '.join(part for part in (first_name, last_name) if part).strip(),
            'First Name': first_name,
            'Last Name': last_name,
            'Email': value(row, 'Email', 'Email Address', 'E-mail'),
            'Address Line 1': value(row, 'Address Line 1', 'Address', 'Street Address', 'street_address'),
            'Address Line 2': value(row, 'Address Line 2', 'Second Line Address'),
            'City': value(row, 'City'),
            'State': value(row, 'State', 'Sate', 'State/Province', 'Region'),
            'Postal Code': value(row, 'Postal Code', 'ZIP', 'Zip', 'ZIP Code', 'postcode', 'postal_code'),
            'Country': value(row, 'Country'),
            'Phone': phone,
            'Fax': value(row, 'Fax'),
            'Website': value(row, 'Website', 'URL'),
            'Industry': value(row, 'Industry'),
            'Original Industry': value(row, 'Original Industry'),
            'Category': value(row, 'Category'),
            'Alternate Categories': value(row, 'Alternate Categories'),
            'Credentials': value(row, 'Credentials'),
            'Specialty': value(row, 'Specialty'),
            'Rating': value(row, 'Rating'),
            'Reviews': value(row, 'Reviews'),
            'NPI': value(row, 'NPI'),
            'Enumeration Date': value(row, 'Enumeration Date', 'EnumerationDate'),
            'Original Values': original_values,
            'source_file': value(row, 'source_file', 'Source File'),
            'source_type': source_type_for(source_category),
            'source_sheet': value(row, 'source_sheet', 'Source Sheet'),
            'source_category': source_category,
        }
        writer.writerow(out)
        counts['rows'] += 1
        counts[f"source_type:{out['source_type']}"] += 1
        if not out['Name']:
            counts['blank_name'] += 1

report = {
    'input': str(INPUT),
    'original_copy': str(ORIGINAL_COPY),
    'output': str(OUTPUT),
    'input_headers_preserved_in_original_values': True,
    'output_headers': OUTPUT_HEADERS,
    'counts': dict(counts),
    'phone_changes': dict(phone_changes),
    'safe_equivalent_mappings': {
        'Address / street_address / Street Address / First Line Address': 'Address Line 1',
        'Second Line Address': 'Address Line 2',
        'ZIP / Zip / ZIP Code / postcode / postal_code': 'Postal Code',
        'State / Sate / State-Provision / Region': 'State',
        'First Name + Last Name': 'Name when source Name is absent',
        'source_category': 'source_category',
        'source_type': 'controlled Med or Not Med derived from source_category; original value retained in Original Values',
    },
    'phone_rule': 'remove non-digits; remove leading 1 from 11-digit values; store only exactly 10 digits; invalid values become blank and remain in Original Values',
}
REPORT.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
