from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

CSV_PATH = Path('/home/ubuntu/upload/normalized_merged_all.csv')

def clean(value):
    return '' if value is None else str(value).strip()

def digits(value):
    return re.sub(r'\D', '', clean(value))

def normalized_phone(value):
    d = digits(value)
    if len(d) == 11 and d.startswith('1'):
        d = d[1:]
    return d

with CSV_PATH.open('r', encoding='utf-8-sig', newline='') as handle:
    reader = csv.DictReader(handle)
    headers = reader.fieldnames or []
    rows = 0
    blank = Counter()
    phone_lengths = Counter()
    phone_examples = defaultdict(list)
    source_types = Counter()
    industries = Counter()
    source_categories = Counter()
    duplicate_header_groups = []
    canonical_aliases = {
        'name': ['Name', 'First Name', 'Last Name'],
        'address_line_1': ['Address Line 1', 'Address', 'street_address', 'First Line Address', 'Street Address'],
        'address_line_2': ['Address Line 2', 'Second Line Address'],
        'postal_code': ['Postal Code', 'ZIP', 'Zip', 'ZIP Code', 'Postal', 'postcode', 'postal_code'],
        'phone': ['Phone', 'Phone Number', 'Telephone', 'Mobile', 'Cell Phone'],
        'email': ['Email', 'Email Address', 'E-mail'],
        'state': ['State', 'Sate', 'State/Province', 'Region'],
        'source': ['Source', 'source'],
        'source_category': ['Source Category', 'source_category'],
        'source_type': ['Source Type', 'source_type'],
        'source_sheet': ['Source Sheet', 'source_sheet'],
    }
    for canonical, aliases in canonical_aliases.items():
        found = [h for h in headers if h in aliases]
        if len(found) > 1:
            duplicate_header_groups.append({'canonical': canonical, 'headers': found})
    for row in reader:
        rows += 1
        for h in headers:
            if not clean(row.get(h)):
                blank[h] += 1
        raw_phone = clean(row.get('Phone'))
        d = digits(raw_phone)
        phone_lengths[len(d) if d else 0] += 1
        if raw_phone and len(phone_examples[len(d)]) < 5:
            phone_examples[len(d)].append(raw_phone)
        source_types[clean(row.get('source_type')) or '(blank)'] += 1
        industries[clean(row.get('Industry')) or '(blank)'] += 1
        source_categories[clean(row.get('source_category')) or '(blank)'] += 1

report = {
    'file': str(CSV_PATH),
    'rows': rows,
    'header_count': len(headers),
    'headers': headers,
    'duplicate_or_equivalent_header_groups': duplicate_header_groups,
    'blank_counts': dict(blank),
    'phone_digit_length_counts': dict(phone_lengths),
    'phone_examples_by_digit_length': dict(phone_examples),
    'source_type_counts': dict(source_types),
    'industry_top_30': industries.most_common(30),
    'source_category_top_30': source_categories.most_common(30),
}
Path('/home/ubuntu/contact_directory_desktop/main_csv_audit.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
