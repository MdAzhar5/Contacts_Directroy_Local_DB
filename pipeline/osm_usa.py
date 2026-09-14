"""
Extract named businesses with a phone or email from OpenStreetMap PBF extracts.

  python pipeline/osm_usa.py [file.osm.pbf | folder of *-latest.osm.pbf] [out.parquet]

Defaults: data/osm_data/ -> data/osm_usa_contacts.parquet.
The update flow (pipeline/update.py) calls the same extractor on the downloaded USA extract.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import DATA, extract_osm  # noqa: E402

if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA / "osm_data"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DATA / "osm_usa_contacts.parquet"
    extract_osm(src, out)
