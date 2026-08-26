from __future__ import annotations

import csv
import sys
from pathlib import Path

import pandas as pd

csv.field_size_limit(sys.maxsize)


def supported_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in {".csv", ".xlsx", ".xls"}


def csv_encoding_and_delimiter(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin1", errors="replace")
        encoding = "latin1"
    try:
        dialect = csv.Sniffer().sniff(text[:200000], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        first_line = text.splitlines()[0] if text.splitlines() else ""
        delimiter = max([",", ";", "\t", "|"], key=lambda item: first_line.count(item))
    return encoding, delimiter


def csv_headers(path: Path) -> list[str]:
    encoding, delimiter = csv_encoding_and_delimiter(path)
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        headers = next(reader, [])
    return [str(header).strip() for header in headers]


def preview_file(path: Path, limit: int = 8) -> tuple[list[str], list[dict[str, str]], list[str]]:
    if path.suffix.lower() == ".csv":
        encoding, delimiter = csv_encoding_and_delimiter(path)
        with path.open("r", encoding=encoding, newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            headers = [str(header).strip() for header in (reader.fieldnames or [])]
            sample = []
            for row in reader:
                sample.append({str(k).strip(): "" if v is None else str(v) for k, v in row.items()})
                if len(sample) >= limit:
                    break
        return headers, sample, ["default"]
    sheets = pd.read_excel(path, sheet_name=None, dtype=str, keep_default_na=False, nrows=limit)
    sheet_names = list(sheets.keys())
    if not sheet_names:
        return [], [], []
    first = sheets[sheet_names[0]].fillna("")
    headers = [str(column).strip() for column in first.columns.tolist()]
    sample = []
    for record in first.to_dict(orient="records"):
        sample.append({str(k).strip(): "" if v is None else str(v) for k, v in record.items()})
    return headers, sample, sheet_names


def iter_rows(path: Path):
    if path.suffix.lower() == ".csv":
        encoding, delimiter = csv_encoding_and_delimiter(path)
        with path.open("r", encoding=encoding, newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            for row in reader:
                yield {str(k).strip(): "" if v is None else str(v) for k, v in row.items() if k is not None}
        return
    sheets = pd.read_excel(path, sheet_name=None, dtype=str, keep_default_na=False)
    for sheet_name, frame in sheets.items():
        frame = frame.fillna("")
        for record in frame.to_dict(orient="records"):
            row = {str(k).strip(): "" if v is None else str(v) for k, v in record.items()}
            row.setdefault("source_sheet", str(sheet_name))
            yield row
