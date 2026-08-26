from __future__ import annotations

import csv
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

DEFAULT_DNC_URL = "https://csv-cleaner-app-uc0p.onrender.com/"


class DNCServiceError(RuntimeError):
    """Raised when the hosted DNC cleaner cannot complete a cleaning pass."""


def _safe_phone(value) -> str:
    text = "" if value is None else str(value).strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def _stage_phone_column(input_path: Path, phone_header: str | None) -> tuple[Path, str | None, int]:
    """Create a temporary CSV with canonical phones and a helper phone column if needed."""
    temp_dir = Path(tempfile.mkdtemp(prefix="contact-directory-dnc-"))
    staged = temp_dir / input_path.name
    changed = 0
    helper_header = None
    with input_path.open("r", encoding="utf-8-sig", newline="") as source, staged.open("w", encoding="utf-8-sig", newline="") as target:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise DNCServiceError("The local filtered file has no header row.")
        headers = list(reader.fieldnames)
        selected = phone_header if phone_header in headers else None
        recognized = selected and re.sub(r"[^a-z0-9]", "", selected.lower()) in {
            "phone", "telephone", "phonenumber", "mobile", "cell", "directphone", "contactphone",
        }
        if selected and not recognized:
            helper_header = "Phone"
            while helper_header in headers:
                helper_header = "Phone_for_DNC"
            headers.append(helper_header)
        writer = csv.DictWriter(target, fieldnames=headers, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in reader:
            if selected:
                canonical = _safe_phone(row.get(selected, ""))
                if row.get(selected, "") != canonical:
                    changed += 1
                row[selected] = canonical
                if helper_header:
                    row[helper_header] = canonical
            writer.writerow(row)
    return staged, helper_header, changed


def _remove_helper_column(source_path: Path, output_path: Path, helper_header: str | None) -> int:
    """Copy the hosted result to the requested output while removing any staging-only helper column."""
    with source_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise DNCServiceError("The hosted cleaner returned a file without headers.")
        headers = [h for h in reader.fieldnames if h != helper_header]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with output_path.open("w", encoding="utf-8-sig", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=headers, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            for row in reader:
                if "Phone" in headers:
                    row["Phone"] = _safe_phone(row.get("Phone", ""))
                writer.writerow({field: row.get(field, "") for field in headers})
                count += 1
    return count


def _extract_zip_outputs(downloaded_path: Path) -> dict:
    """Unzip the hosted cleaner's ZIP result and identify its parts.

    The hosted cleaner delivers its result as a ZIP ("Download Cleaned Files (ZIP)") containing
    a CLEAR_*.csv (rows kept after suppression), a DNC_*.csv (rows removed as matches), and a
    _Cleaning_Summary.csv report. The kept-rows file is matched by name rather than assumed to
    be the first ZIP entry, since archive ordering is not guaranteed.
    """
    if not zipfile.is_zipfile(downloaded_path):
        return {"clear": downloaded_path, "dnc": None, "summary": None}
    extract_dir = downloaded_path.parent / "hosted_cleaned_extracted"
    extract_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(downloaded_path) as archive:
        csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not csv_names:
            raise DNCServiceError("The hosted cleaner's ZIP result did not contain a CSV file.")
        archive.extractall(extract_dir, members=csv_names)

    def find(keyword: str) -> Path | None:
        for name in csv_names:
            if keyword in name.lower():
                return extract_dir / name
        return None

    return {
        "clear": find("clear") or (extract_dir / csv_names[0]),
        "dnc": find("dnc"),
        "summary": find("summary"),
    }




def _find_browser_channel(p):
    preferred = os.environ.get("CONTACT_DIRECTORY_BROWSER_CHANNEL", "chrome").strip()
    if preferred.lower() in {"chrome", "msedge", "chromium"}:
        return preferred.lower()
    return "chrome"


def clean_with_hosted_service(
    input_path: str | Path,
    output_path: str | Path,
    phone_header: str | None = None,
    service_url: str = DEFAULT_DNC_URL,
    scrub_tcpa: bool = True,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Upload a local-filtered CSV to the hosted Streamlit cleaner and save its cleaned CSV.

    The hosted service is intentionally invoked only when the caller enables this pass.
    It uses the public browser UI because the service does not expose a documented REST API.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.exists():
        raise DNCServiceError(f"Input file not found: {input_path}")
    staged, helper_header, standardized = _stage_phone_column(input_path, phone_header)
    downloaded = staged.parent / "hosted_cleaned.csv"
    browser = None
    context = None
    try:
        if progress:
            progress("Opening hosted DNC cleaner…")
        base_dir = Path(__file__).resolve().parent
        bundled_browsers = base_dir / "playwright-browsers"
        if bundled_browsers.exists():
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled_browsers))
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise DNCServiceError("Hosted DNC support requires Playwright. Install requirements-desktop.txt and rebuild the EXE.") from exc
        with sync_playwright() as playwright:
            launch_kwargs = {"headless": True}
            channel = _find_browser_channel(playwright)
            try:
                browser = playwright.chromium.launch(channel=channel, **launch_kwargs)
            except Exception:
                system_browser = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome") or shutil.which("chrome")
                try:
                    if system_browser:
                        browser = playwright.chromium.launch(executable_path=system_browser, **launch_kwargs)
                    else:
                        browser = playwright.chromium.launch(**launch_kwargs)
                except Exception as exc:
                    raise DNCServiceError("No compatible Chromium/Chrome browser was found. Install Google Chrome or set CONTACT_DIRECTORY_BROWSER_CHANNEL.") from exc
            context = browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 900})
            page = context.new_page()
            page.goto(service_url, wait_until="domcontentloaded", timeout=60000)
            main_uploader = page.locator('section[aria-label="Upload CSV files to clean"] input[type="file"]')
            if not main_uploader.count():
                main_uploader = page.locator('input[data-testid="stFileUploaderDropzoneInput"]').first
            else:
                main_uploader = main_uploader.first
            try:
                main_uploader.wait_for(state="attached", timeout=60000)
                main_uploader.evaluate("el => { el.style.display = 'block'; el.style.opacity = '1'; el.style.position = 'fixed'; el.style.width = '2px'; el.style.height = '2px'; }")
                main_uploader.set_input_files(str(staged))
            except Exception as exc:
                raise DNCServiceError(f"The hosted cleaner main CSV uploader could not accept the file: {exc}") from exc
            try:
                page.locator('[data-testid="stFileChip"]').first.wait_for(state="visible", timeout=60000)
            except Exception as exc:
                raise DNCServiceError("The hosted cleaner did not confirm the uploaded CSV. Its uploader may have changed or the file may have been rejected.") from exc
            if progress:
                progress("Uploaded remaining rows; preparing hosted DNC pass…")
            try:
                checkbox = page.get_by_role("checkbox", name=re.compile("Scrub phone numbers", re.I))
                if checkbox.count() and checkbox.is_checked() != scrub_tcpa:
                    checkbox.click()
            except Exception:
                pass
            run_button = page.get_by_role("button", name=re.compile("Run Cleaning", re.I))
            if not run_button.count():
                run_button = page.get_by_text(re.compile("Run Cleaning", re.I))
            if not run_button.count():
                raise DNCServiceError("The hosted cleaner did not expose its Run Cleaning control.")

            download_button = page.locator('div[data-testid="stDownloadButton"] button')
            running_icon = page.locator('[data-testid="stStatusWidgetRunningIcon"]')
            started = False
            for attempt in range(3):
                run_button.first.click()
                if progress:
                    progress("Running hosted cleaning pass…" if attempt == 0 else f"Run Cleaning click did not register; retrying (attempt {attempt + 1})…")
                try:
                    running_icon.first.wait_for(state="visible", timeout=8000)
                    started = True
                    break
                except Exception:
                    continue
            if not started:
                raise DNCServiceError("The hosted cleaner did not respond to the Run Cleaning control after several attempts.")

            try:
                download_button.first.wait_for(state="visible", timeout=240000)
            except Exception as exc:
                raise DNCServiceError(f"The hosted cleaner did not produce a download control in time: {exc}") from exc

            if progress:
                progress("Hosted DNC cleaning complete; downloading result…")
            with page.expect_download(timeout=60000) as download_info:
                download_button.first.click()
            download = download_info.value
            download.save_as(str(downloaded))
            if progress:
                progress("Saving final CSV…")
            outputs = _extract_zip_outputs(downloaded)
            count = _remove_helper_column(outputs["clear"], output_path, helper_header)
            dnc_removed_path = None
            summary_path = None
            if outputs.get("dnc") and outputs["dnc"].exists():
                dnc_removed_path = output_path.parent / f"{output_path.stem}_dnc_removed.csv"
                shutil.copyfile(outputs["dnc"], dnc_removed_path)
            if outputs.get("summary") and outputs["summary"].exists():
                summary_path = output_path.parent / f"{output_path.stem}_cleaning_summary.csv"
                shutil.copyfile(outputs["summary"], summary_path)
            return {
                "rows": count,
                "standardized_phones": standardized,
                "service_url": service_url,
                "dnc_removed_path": str(dnc_removed_path) if dnc_removed_path else None,
                "summary_path": str(summary_path) if summary_path else None,
            }
    except DNCServiceError:
        raise
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        raise DNCServiceError(f"Hosted DNC cleaning failed: {message}") from exc
    finally:
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        shutil.rmtree(staged.parent, ignore_errors=True)


def check_service(service_url: str = DEFAULT_DNC_URL, timeout: int = 20) -> tuple[bool, str]:
    """Check only the public health endpoint; this never uploads a file."""
    import requests
    health_url = service_url.rstrip("/") + "/_stcore/health"
    try:
        response = requests.get(health_url, timeout=timeout)
        if response.ok and response.text.strip().lower() == "ok":
            return True, f"Hosted cleaner is online ({health_url})."
        return False, f"Hosted cleaner returned HTTP {response.status_code}."
    except Exception as exc:
        return False, f"Hosted cleaner is unreachable: {exc}"
