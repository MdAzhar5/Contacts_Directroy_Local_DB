from __future__ import annotations

import shutil

from playwright.sync_api import sync_playwright

from dnc_client import DEFAULT_DNC_URL


def main() -> None:
    with sync_playwright() as playwright:
        browser_path = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
        browser = playwright.chromium.launch(headless=True, executable_path=browser_path)
        page = browser.new_page()
        page.goto(DEFAULT_DNC_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector('input[data-testid="stFileUploaderDropzoneInput"]', state="attached", timeout=60000)
        main_uploader = page.locator('section[aria-label="Upload CSV files to clean"] input[type="file"]')
        assert main_uploader.count() == 1, f"expected one main uploader, got {main_uploader.count()}"
        assert main_uploader.first.get_attribute("data-testid") == "stFileUploaderDropzoneInput"
        assert page.locator('input[data-testid="stFileUploaderDropzoneInput"]').count() >= 2
        browser.close()
    print("Hosted DNC main-uploader selector test passed without uploading a file")


if __name__ == "__main__":
    main()
