from __future__ import annotations

import re
from pathlib import Path

import requests

from importers import iter_rows

CLEAROUT_BULK_URL = "https://api.clearoutphone.io/v1/phonenumber/bulk"
PHONE_COLUMN_CANDIDATES = ("phone", "telephone", "telephone number", "direct", "mobile")


class ClearoutError(RuntimeError):
    """Raised when the Clearout bulk phone validation API call fails."""


def _detect_row_country(digits: str) -> str | None:
    if len(digits) == 12 and digits.startswith("44"):
        return "gb"
    if len(digits) == 11 and digits.startswith("0"):
        return "gb"
    if len(digits) == 11 and digits.startswith("1"):
        return "us"
    if len(digits) == 10:
        return "us"
    return None


def guess_country_code(file_path: str | Path, default: str = "us") -> str:
    """Guess whether a filtered file's phone numbers are US or UK, like Clearout's own site does.

    Looks at the shape of each phone number (leading 44/0 for UK, leading 1 or bare 10-digit
    NANP for US) rather than asking the user to pick a country up front, then returns whichever
    country the majority of numbers match.
    """
    file_path = Path(file_path)
    phone_column = None
    votes = {"us": 0, "gb": 0}
    for row in iter_rows(file_path):
        if phone_column is None:
            lowered = {str(key).strip().lower(): key for key in row.keys()}
            for candidate in PHONE_COLUMN_CANDIDATES:
                if candidate in lowered:
                    phone_column = lowered[candidate]
                    break
            else:
                continue
        digits = re.sub(r"\D", "", str(row.get(phone_column, "")))
        country = _detect_row_country(digits)
        if country:
            votes[country] += 1
    if votes["gb"] > votes["us"]:
        return "gb"
    if votes["us"] > 0 or votes["gb"] > 0:
        return "us"
    return default


def send_bulk_validation(
    file_path: str | Path,
    api_token: str,
    country_code: str = "us",
    mode: str = "smart",
    timeout: int = 120,
) -> dict:
    """Upload a CSV/XLSX file to Clearout's bulk phone validation API.

    Returns the parsed JSON response, e.g. {"status": "success", "data": {"list_id": "..."}}.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise ClearoutError(f"File not found: {file_path}")
    if not api_token:
        raise ClearoutError("A Clearout API token is required.")
    headers = {"Authorization": f"Bearer:{api_token}"}
    payload = {"country_code": country_code, "settings": f'{{"mode":"{mode}"}}'}
    try:
        with file_path.open("rb") as handle:
            files = {"file": (file_path.name, handle)}
            response = requests.post(CLEAROUT_BULK_URL, files=files, data=payload, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise ClearoutError(f"Could not reach Clearout: {exc}") from exc
    try:
        result = response.json()
    except ValueError:
        raise ClearoutError(f"Clearout returned a non-JSON response (HTTP {response.status_code}): {response.text[:300]}")
    if not response.ok or result.get("status") != "success":
        message = result.get("message") or result.get("error") or response.text[:300]
        raise ClearoutError(f"Clearout request failed (HTTP {response.status_code}): {message}")
    return result
