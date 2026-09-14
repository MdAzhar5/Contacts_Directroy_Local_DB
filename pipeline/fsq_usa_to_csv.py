"""
Download the newest Foursquare OS Places release from Hugging Face and extract USA
businesses with a contact field to data/foursquare_usa_contacts.parquet.

  python pipeline/fsq_usa_to_csv.py [--version 2026-08-11]

Requires a free Hugging Face account that accepted the dataset terms at
https://huggingface.co/datasets/foursquare/fsq-os-places, and a token in the repository
root .env file as HF_TOKEN=... (copy .env.example). The download (~12 GB) is resumable.
(The name is historical: the output is parquet now, not CSV.)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sources  # noqa: E402
from extract import DATA, extract_fsq  # noqa: E402

if __name__ == "__main__":
    info = sources.latest_fsq()
    if "--version" in sys.argv:
        want = sys.argv[sys.argv.index("--version") + 1]
        if want != info["version"]:
            raise SystemExit(f"Only the newest release ({info['version']}) can be fetched from Hugging Face.")
    print(f"Foursquare {info['version']}: downloading {sources.fmt_size(info['size'])} ...")
    raw = sources.download("Foursquare", info, sources.print_progress)
    extract_fsq(raw, DATA / "foursquare_usa_contacts.parquet")
