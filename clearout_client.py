from __future__ import annotations

from pathlib import Path

import requests

CLEAROUT_BULK_URL = "https://api.clearoutphone.io/v1/phonenumber/bulk"


class ClearoutError(RuntimeError):
    """Raised when the Clearout bulk phone validation API call fails."""


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
