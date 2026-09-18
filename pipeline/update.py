r"""
Data updater for Leads Explorer: find newer releases of Foursquare, Overture,
OpenStreetMap and NPI (CMS NPPES), download them, extract USA contacts, and merge them into leads.duckdb.

    python pipeline/update.py --check              print installed vs latest versions
    python pipeline/update.py --check --notify     same, plus a Windows popup when something is newer
    python pipeline/update.py --check --json       machine-readable
    python pipeline/update.py --update Overture    download + extract + merge the newest Overture release
        [--version 2026-08-19.0] [--yes] [--relaunch] [--keep-download] [--from-file path.parquet]

The app must be closed while merging (the database is opened exclusively); the updater
waits for it. A new lead is added only when neither its phone nor its email is already in All
Leads; the summary printed at the end says how many were added and how many were skipped.
Updates never read or change Used Data and never mark leads used (only imports do).
With --relaunch the app is started again when the merge is done (after ~8 seconds, or Enter).
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

merge_done = False   # set once merge() has committed in this run, so failure messages do not claim nothing changed


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(wait: bool = False):
    """Open the database read-write; with wait=True keep retrying while the app still holds it."""
    for attempt in range(600):
        try:
            con = duckdb.connect(str(DB))
            install_macros(con)
            con.execute("SET preserve_insertion_order = false")   # row order is irrelevant; saves memory on 25M-row writes
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
    global merge_done
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

    def saved():
        global merge_done
        merge_done = True

    try:
        # merge_done flips as soon as the data is committed (right after COMMIT, before the checkpoint and the filter
        # rebuild), so a failure or Ctrl+C after that point is reported as "already installed"
        stats = merge(con, source, parquet, version, on_saved=saved)
    finally:
        con.close()
    if raw is not None and not keep_download:
        target = raw if raw.is_dir() else raw.parent
        try:
            for f in Path(target).rglob("*"):
                if f.is_file() and f.suffix in (".parquet", ".pbf", ".zip", ".part") and f.name != parquet.name and f != parquet:
                    f.unlink()
            print("  raw download removed (extracted parquet kept)")
        except OSError as exc:     # e.g. OneDrive or antivirus still holds a file; the update itself is installed
            print(f"  WARNING: could not remove the raw download ({exc}). The update is installed; delete {target} by hand.")
    stats["minutes"] = round((time.time() - t0) / 60, 1)
    print(f"\nUpdate complete in {stats['minutes']} min.")
    print_summary(stats)
    return stats


def print_summary(s: dict) -> None:
    """What the merge added and skipped, in plain words."""
    if s.get("rule") == "every_provider":     # NPI: every provider is added; overlaps are only counted
        lines = [
            ("Providers added (every provider rule):", s["fresh"]),
            ("  added anyway, phone or email already in All Leads:", s.get("phone_in_leads", 0)),
            ("  added anyway, phone shared with another new provider:", s.get("phone_shared_in_release", 0)),
            ("Skipped, no valid phone or email:", s["skipped_no_contact"]),
            ("Deactivated NPIs closed:", s.get("deactivated_closed", 0)),
        ]
    else:
        lines = [
            ("Fresh leads added (neither phone nor email seen before):", s["fresh"]),
            ("Old leads skipped:", s["skipped_total"]),
            ("  phone or email already in All Leads:", s["skipped_in_leads"]),
            ("  duplicate phone/email inside this release:", s["skipped_duplicate"]),
            ("  no valid phone or email:", s["skipped_no_contact"]),
        ]
    width = max(len(label) for label, _ in lines) + 2
    print(f"\n{s['source']} release {s['release_date']} ({s['version']}), installed {s['installed_at'][:10]}")
    for label, n in lines:
        print(f"  {label:<{width}}{n:>12,}")
    print(f"  Businesses already in the database (same source id): {s['existing']:,} "
          f"({s['updated']:,} updated, {s['unchanged']:,} unchanged)")
    print(f"  {s['source']} rows in All Leads: {s['before']:,} -> {s['after']:,}")
    if s.get("dims_error"):
        print(f"  WARNING: the filter lists were not rebuilt ({s['dims_error']}); they keep their previous counts until the next update.")


def pause(seconds: int = 8) -> None:
    """Leave the summary on screen: return after `seconds`, or as soon as Enter is pressed."""
    print(f"\nReopening Leads Explorer in {seconds} seconds (press Enter to reopen now) ...", flush=True)
    try:
        import msvcrt
    except ImportError:
        time.sleep(seconds)
        return
    end = time.time() + seconds
    try:
        while time.time() < end:
            if msvcrt.kbhit() and msvcrt.getwch() in ("\r", "\n"):
                return
            time.sleep(0.1)
    except KeyboardInterrupt:      # the merge is already committed; just reopen
        pass


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
    global merge_done
    merge_done = False
    try:
        stats = run_update(a.update, a.version, a.yes, a.keep_download, a.from_file)
        if a.relaunch and not stats.get("skipped"):
            pause()
    except KeyboardInterrupt:
        if merge_done:
            print("\nStopped. The update is already installed; only the clean-up after it was cut short.")
        else:
            print("\nCancelled. Nothing was changed in the database; re-run the same command to resume the download.")
        if a.relaunch:
            input("Press Enter to reopen Leads Explorer ...")
    except (Exception, SystemExit) as exc:     # SystemExit: e.g. "No parquet files in ...", "Gave up waiting ..."
        if isinstance(exc, SystemExit) and not a.relaunch:
            raise
        print(f"\nUPDATE FAILED: {exc}")
        if merge_done:
            print("The new data was already merged into the database; do not install this release again.")
        else:
            print("The database was not changed. Re-run the same command to try again (downloads resume).")
        if not a.relaunch:
            raise
        input("Press Enter to reopen Leads Explorer ...")
    if a.relaunch:
        print("Starting Leads Explorer ...")
        relaunch_app()


if __name__ == "__main__":
    main()
