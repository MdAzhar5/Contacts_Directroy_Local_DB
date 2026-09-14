"""
Extract USA places with a phone or email from the Overture Maps places theme.

  python pipeline/overture_usa.py [src_dir] [out.parquet]

Defaults: data/overture_data/*.parquet -> data/overture_usa_contacts.parquet.
The update flow (pipeline/update.py) calls the same extractor on a downloaded release.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import DATA, extract_overture  # noqa: E402

if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA / "overture_data"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DATA / "overture_usa_contacts.parquet"
    extract_overture(src, out)
