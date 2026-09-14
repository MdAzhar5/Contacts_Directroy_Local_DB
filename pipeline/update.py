r"""
Data updater for Leads Explorer: find newer releases of Foursquare, Overture and
OpenStreetMap, download them, extract USA contacts, and merge them into leads.duckdb.

    python pipeline/update.py --check              print installed vs latest versions
    python pipeline/update.py --check --notify     same, plus a Windows popup when something is newer
    python pipeline/update.py --check --json       machine-readable
    python pipeline/update.py --update Overture    download + extract + merge the newest Overture release
        [--version 2026-08-19.0] [--yes] [--relaunch] [--keep-download] [--from-file path.parquet]

The app must be closed while merging (the database is opened exclusively); the updater
waits for it. With --relaunch the app is started again when the merge is done.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
APP_DIR = ROOT / "leads_app"
DATA = ROOT / "data"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(APP_DIR))

import duckdb  # noqa: E402

import sources  # noqa: E402

sources.load_dotenv()          # secrets and optional settings from the git-ignored .env
DB = Path(os.environ.get("LEADS_DB", str(APP_DIR / "leads.duckdb")))

from extract import EXTRACTORS, OUTPUT_NAMES  # noqa: E402
from merge import get_versions, merge, set_meta  # noqa: E402
from schema import install_macros  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(wait: bool = False):
    """Open the database read-write; with wait=True keep retrying while the app still holds it."""
    for attempt in range(600):
        try:
            con = duckdb.connect(str(DB))
            install_macros(con)
            return con
        except duckdb.IOException as exc:
            if not wait or "being used by another process" not in str(exc):
                raise
            if attempt == 0:
                print("Waiting for Leads Explorer to close (it holds the database) ...")
            time.sleep(2)
    raise SystemExit("Gave up waiting for the database to be released.")


def installed_versions() -> dict:
    if not DB.exists():
        return {}
    con = duckdb.connect(str(DB), read_only=True)
    try:
        return get_versions(con)
    finally:
        con.close()


def check(token: str | None = None) -> list[dict]:
    """Merge 'latest online' with 'installed' for every source."""
    installed = installed_versions()
    out = []
    for r in sources.check_all(token):
        cur = installed.get(r["source"])
        r["installed"] = cur
        r["available"] = bool(r["version"]) and (cur is None or r["version"] > cur)
        out.append(r)
    return out


def print_check(results: list[dict]) -> None:
    print(f"{'Source':<14} {'Installed':<14} {'Latest':<14} {'Size':>8}  Status")
    for r in results:
        status = ("ERROR: " + r["error"]) if r["error"] else ("NEW RELEASE AVAILABLE" if r["available"] else "up to date")
        print(f"{r['source']:<14} {(r['installed'] or 'not recorded'):<14} {(r['version'] or '-'):<14} "
              f"{sources.fmt_size(r['size']) if r['size'] else '':>8}  {status}")


def notify(results: list[dict]) -> None:
    new = [r for r in results if r["available"]]
    if not new:
        return
    text = "New data is available for Leads Explorer:\n\n" + "\n".join(
        f"  {r['source']}: {r['version']}  ({sources.fmt_size(r['size'])})" for r in new) + \
        "\n\nOpen Leads Explorer and go to the Updates tab to download and install."
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, "Leads Explorer - data update", 0x40)
    except Exception:
        print(text)


def run_update(source: str, version: str | None, assume_yes: bool, keep_download: bool, from_file: str | None,
               token: str | None = None) -> dict:
    if source not in sources.SOURCES:
        raise SystemExit(f"Unknown source {source}. Choose one of: {', '.join(sources.SOURCES)}")
    t0 = time.time()
    if from_file:
        parquet = Path(from_file)
        if not version:
            raise SystemExit("--from-file needs --version")
        raw = None
    else:
        print(f"Checking the newest {source} release ...")
        info = sources.latest(source, token)
        if version and version != info["version"]:
            raise SystemExit(f"Only the newest release can be fetched ({info['version']}); {version} is not available.")
        version = info["version"]
        cur = installed_versions().get(source)
        print(f"  installed: {cur or 'not recorded'}   latest: {version}   download: {sources.fmt_size(info['size'])}")
        if cur and version <= cur and not assume_yes:
            print("Already up to date.")
            return {"skipped": True}
        if not assume_yes:
            ans = input(f"Download {sources.fmt_size(info['size'])} and update {source} to {version}? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                return {"skipped": True}
        print(f"Downloading {source} {version} (resumable; re-run if interrupted) ...")
        raw = sources.download(source, info, sources.print_progress)
        print(f"  downloaded to {raw}")
        parquet = DATA / source.lower() / version / OUTPUT_NAMES[source]
        print(f"Extracting USA contacts from {source} {version} ...")
        EXTRACTORS[source](raw, parquet)
        # keep a copy where build_db.py looks for it
        shutil.copyfile(parquet, DATA / OUTPUT_NAMES[source])
    con = open_db(wait=True)
    try:
        stats = merge(con, source, parquet, version)
    finally:
        con.close()
    if raw is not None and not keep_download:
        target = raw if raw.is_dir() else raw.parent
        for f in Path(target).rglob("*"):
            if f.is_file() and f.suffix in (".parquet", ".pbf", ".part") and f.name != parquet.name and f != parquet:
                f.unlink()
        print("  raw download removed (extracted parquet kept)")
    stats["minutes"] = round((time.time() - t0) / 60, 1)
    print(f"\nUpdate complete in {stats['minutes']} min.")
    return stats


def relaunch_app() -> None:
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw) if pyw.exists() else sys.executable
    subprocess.Popen([exe, str(APP_DIR / "app.py")], cwd=str(APP_DIR), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--notify", action="store_true", help="with --check: Windows popup if something is newer")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--update", metavar="SOURCE")
    ap.add_argument("--version")
    ap.add_argument("--yes", action="store_true", help="do not ask before downloading")
    ap.add_argument("--relaunch", action="store_true", help="start the app again when done")
    ap.add_argument("--keep-download", action="store_true")
    ap.add_argument("--from-file", help="skip download/extract and merge this already-extracted parquet")
    ap.add_argument("--set-version", nargs=2, metavar=("SOURCE", "VERSION"), help="record the installed version without updating")
    a = ap.parse_args()

    if a.set_version:
        con = open_db(wait=True)
        set_meta(con, f"source_version:{a.set_version[0]}", a.set_version[1])
        con.close()
        print(f"Recorded {a.set_version[0]} = {a.set_version[1]}")
        return
    if a.check or not a.update:
        results = check()
        if a.json:
            print(json.dumps(results, indent=2))
        else:
            print_check(results)
        if a.notify:
            notify(results)
        return
    # The app closes itself before launching this, so it must be started again on every exit path.
    try:
        run_update(a.update, a.version, a.yes, a.keep_download, a.from_file)
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was changed in the database; re-run the same command to resume the download.")
        if a.relaunch:
            input("Press Enter to reopen Leads Explorer ...")
    except Exception as exc:
        print(f"\nUPDATE FAILED: {exc}")
        print("The database was not changed. Re-run the same command to try again (downloads resume).")
        if not a.relaunch:
            raise
        input("Press Enter to reopen Leads Explorer ...")
    if a.relaunch:
        print("Starting Leads Explorer ...")
        relaunch_app()


if __name__ == "__main__":
    main()
