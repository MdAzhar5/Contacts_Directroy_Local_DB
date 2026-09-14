"""
Where each data source publishes new releases, how to find the newest one, and how to
download it with resume support. Pure functions; nothing here touches the database.

  Foursquare OS Places   Hugging Face dataset foursquare/fsq-os-places, folders release/dt=YYYY-MM-DD
                         (gated: needs a free HF token that accepted the dataset terms)
  Overture Maps Places   public S3 bucket overturemaps-us-west-2, folders release/YYYY-MM-DD.N,
                         files under theme=places/type=place/
  OpenStreetMap          Geofabrik us-latest.osm.pbf (rebuilt daily; its Last-Modified date is the version)

Versions are plain strings that sort chronologically: "2026-08-11", "2026-08-19.0", "2026-09-13".
"""
from __future__ import annotations

import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"

SOURCES = ("Foursquare", "Overture", "OpenStreetMap")
UA = {"User-Agent": "leads-explorer/1.0"}

FSQ_REPO = "foursquare/fsq-os-places"
OVERTURE_BUCKET = "https://overturemaps-us-west-2.s3.amazonaws.com/"
OSM_URL = "https://download.geofabrik.de/north-america/us-latest.osm.pbf"
_S3NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


class SourceError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _fetch(url: str, method: str = "GET", timeout: int = 30) -> tuple[dict, bytes]:
    req = urllib.request.Request(url, method=method, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return {k.lower(): v for k, v in r.headers.items()}, (r.read() if method == "GET" else b"")


def load_dotenv(path: Path | None = None) -> dict:
    """Read KEY=VALUE lines from .env into os.environ. A real environment variable always wins,
    so the file never overrides something you set deliberately. Returns what the file contained."""
    path = path or (ROOT / ".env")
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key:
            continue
        values[key] = value
        os.environ.setdefault(key, value)
    return values


def get_hf_token() -> str | None:
    """Token lookup order: HF_TOKEN in the environment, then .env, then the legacy
    hf_token.txt, then a cached `huggingface-cli login`."""
    tok = os.environ.get("HF_TOKEN")
    if tok:
        return tok.strip()
    tok = load_dotenv().get("HF_TOKEN")
    if tok:
        return tok.strip()
    legacy = ROOT / "hf_token.txt"
    if legacy.exists():
        tok = legacy.read_text().strip()
        if tok:
            print(f"Note: reading the token from {legacy.name}. Move it into .env as HF_TOKEN=... and delete that file.")
            return tok
    try:
        from huggingface_hub import get_token
        return get_token()
    except Exception:
        return None


def parse_s3_listing(xml_text: str | bytes) -> tuple[list[str], list[tuple[str, int]], str | None]:
    """(common prefixes, [(key, size)], continuation token) from an S3 ListObjectsV2 response."""
    root = ET.fromstring(xml_text)
    prefixes = [p.find("s3:Prefix", _S3NS).text for p in root.findall("s3:CommonPrefixes", _S3NS)]
    keys = [(c.find("s3:Key", _S3NS).text, int(c.find("s3:Size", _S3NS).text)) for c in root.findall("s3:Contents", _S3NS)]
    tok = root.find("s3:NextContinuationToken", _S3NS)
    return prefixes, keys, (tok.text if tok is not None else None)


def _s3_list(prefix: str, delimiter: bool = False) -> tuple[list[str], list[tuple[str, int]]]:
    prefixes, keys, token = [], [], None
    while True:
        url = f"{OVERTURE_BUCKET}?list-type=2&prefix={urllib.request.quote(prefix)}" + ("&delimiter=/" if delimiter else "")
        if token:
            url += "&continuation-token=" + urllib.request.quote(token)
        _, body = _fetch(url)
        p, k, token = parse_s3_listing(body)
        prefixes += p
        keys += k
        if not token:
            return prefixes, keys


def version_from_last_modified(header_value: str) -> str:
    return parsedate_to_datetime(header_value).strftime("%Y-%m-%d")


def fmt_size(n: int | float) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


# ---------------------------------------------------------------------------
# discovery: what is the newest release of each source?
# ---------------------------------------------------------------------------
def latest_overture() -> dict:
    prefixes, _ = _s3_list("release/", delimiter=True)
    versions = sorted(p.split("/")[1] for p in prefixes if re.fullmatch(r"release/\d{4}-\d{2}-\d{2}\.\d+/", p))
    if not versions:
        raise SourceError("No Overture releases found in the bucket listing.")
    version = versions[-1]
    _, keys = _s3_list(f"release/{version}/theme=places/type=place/")
    files = [(OVERTURE_BUCKET + k, k.rsplit("/", 1)[1], sz) for k, sz in keys if k.endswith(".parquet")]
    if not files:
        raise SourceError(f"Overture release {version} has no place files yet.")
    return {"source": "Overture", "version": version, "size": sum(f[2] for f in files), "files": files,
            "note": f"{len(files)} parquet files"}


def latest_osm() -> dict:
    headers, _ = _fetch(OSM_URL, method="HEAD")
    version = version_from_last_modified(headers["last-modified"])
    size = int(headers.get("content-length", 0))
    return {"source": "OpenStreetMap", "version": version, "size": size,
            "files": [(OSM_URL, f"us-{version}.osm.pbf", size)], "note": "Geofabrik USA extract (rebuilt daily)"}


def latest_fsq(token: str | None = None) -> dict:
    token = token or get_hf_token()
    if not token:
        raise SourceError("Foursquare needs a Hugging Face token. Copy .env.example to .env and put your token in it as "
                          "HF_TOKEN=hf_xxx, after accepting the dataset terms at "
                          "huggingface.co/datasets/foursquare/fsq-os-places.")
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise SourceError("Foursquare needs the huggingface_hub package: pip install -r pipeline/requirements.txt") from exc
    api = HfApi(token=token)
    releases = sorted(e.path for e in api.list_repo_tree(FSQ_REPO, repo_type="dataset", path_in_repo="release")
                      if re.fullmatch(r"release/dt=\d{4}-\d{2}-\d{2}", e.path))
    if not releases:
        raise SourceError("No Foursquare releases found.")
    rel = releases[-1]
    version = rel.split("dt=")[1]
    files = list(api.list_repo_tree(FSQ_REPO, repo_type="dataset", path_in_repo=f"{rel}/places/parquet"))
    size = sum((getattr(f, "size", 0) or 0) for f in files)
    return {"source": "Foursquare", "version": version, "size": size, "release_path": rel,
            "files": [], "note": f"{len(files)} parquet files on Hugging Face"}


def latest(source: str, token: str | None = None) -> dict:
    if source == "Overture":
        return latest_overture()
    if source == "OpenStreetMap":
        return latest_osm()
    if source == "Foursquare":
        return latest_fsq(token)
    raise SourceError(f"Unknown source {source}")


def check_all(token: str | None = None) -> list[dict]:
    """One entry per source: latest version and size, or an error note. Never raises."""
    out = []
    for source in SOURCES:
        try:
            info = latest(source, token)
            out.append({"source": source, "version": info["version"], "size": info["size"], "note": info["note"], "error": None})
        except Exception as exc:
            out.append({"source": source, "version": None, "size": 0, "note": "", "error": str(exc)})
    return out


# ---------------------------------------------------------------------------
# download with resume
# ---------------------------------------------------------------------------
def http_download(url: str, dest: Path, size: int | None = None, progress=None, chunk: int = 4 << 20) -> Path:
    """Download url to dest, resuming a partial file with an HTTP Range request."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    if dest.exists() and (size is None or dest.stat().st_size == size):
        return dest
    have = part.stat().st_size if part.exists() else 0
    headers = dict(UA)
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        if have and r.status != 206:      # server ignored the range: start over
            have = 0
        total = size or (have + int(r.headers.get("Content-Length", 0)))
        mode = "ab" if have else "wb"
        t0 = time.time()
        done = have
        with part.open(mode) as f:
            while True:
                block = r.read(chunk)
                if not block:
                    break
                f.write(block)
                done += len(block)
                if progress:
                    progress(done, total, time.time() - t0)
    if size is not None and part.stat().st_size != size:
        raise SourceError(f"Download incomplete for {dest.name}: {part.stat().st_size} of {size} bytes. Run the update again to resume.")
    part.replace(dest)
    return dest


def download(source: str, info: dict, progress=None) -> Path:
    """Fetch the release described by info into data/<source>/<version>/. Returns the folder (or the .pbf file)."""
    version = info["version"]
    dest_dir = DATA / source.lower() / version
    dest_dir.mkdir(parents=True, exist_ok=True)
    if source == "Foursquare":
        from huggingface_hub import snapshot_download
        rel = info.get("release_path") or f"release/dt={version}"
        local = snapshot_download(repo_id=FSQ_REPO, repo_type="dataset", token=get_hf_token(),
                                  allow_patterns=[f"{rel}/places/parquet/*.parquet"], local_dir=str(dest_dir), max_workers=8)
        return Path(local) / rel / "places" / "parquet"
    files = info["files"]
    total = sum(f[2] for f in files)
    done_before = 0
    for i, (url, name, size) in enumerate(files, 1):
        def cb(done, _total, elapsed, _base=done_before, _i=i):
            if progress:
                progress(_base + done, total, elapsed, f"file {_i}/{len(files)} {name}")
        http_download(url, dest_dir / name, size, cb)
        done_before += size
    if source == "OpenStreetMap":
        return dest_dir / files[0][1]
    return dest_dir


def print_progress(done, total, elapsed, label=""):
    pct = 100 * done / total if total else 0
    rate = done / elapsed / 1e6 if elapsed > 0 else 0
    eta = (total - done) / (done / elapsed) / 60 if done and elapsed > 0 else 0
    print(f"\r  {pct:5.1f}%  {fmt_size(done)} / {fmt_size(total)}  {rate:5.1f} MB/s  ETA {eta:4.0f} min  {label[:60]:<60}", end="", flush=True)
    if done >= total:
        print()
