from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from dnc_client import DEFAULT_DNC_URL


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        sample = Path(temp) / "synthetic_upload_test.csv"
        sample.write_text("Name,Phone\nSynthetic Test,(+1) 212.555-0100\n", encoding="utf-8")
        with sync_playwright() as playwright:
            browser_path = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
            browser = playwright.chromium.launch(headless=True, executable_path=browser_path)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(DEFAULT_DNC_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector('input[data-testid="stFileUploaderDropzoneInput"]', state="attached", timeout=30000)
            uploader = page.locator('section[aria-label="Upload CSV files to clean"] input[type="file"]').first
            uploader.evaluate("el => { el.style.display = 'block'; el.style.opacity = '1'; el.style.position = 'fixed'; el.style.width = '2px'; el.style.height = '2px'; }")
            uploader.set_input_files(str(sample))
            page.locator('[data-testid="stFileChip"]').first.wait_for(state="visible", timeout=30000)
            print("Hosted DNC upload-only diagnostic passed")
            browser.close()


if __name__ == "__main__":
    main()
