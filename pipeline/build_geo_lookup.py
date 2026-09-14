"""
Build lookup tables from the Foursquare USA data so other sources (OSM, Overture)
can fill in a missing state:

  data/geo_lookup.duckdb
    zip3_state : 3-digit ZIP prefix -> most common state
    cell_state : 0.05 degree lat/lon cell -> most common state (and city)

Input: the extracted data/foursquare_usa_contacts.parquet (or pass another parquet/CSV path).
Run once:  python pipeline/build_geo_lookup.py [input]
"""
import sys
import time
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA / "foursquare_usa_contacts.parquet"
OUT = DATA / "geo_lookup.duckdb"

VALID_STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY "
    "PR VI GU AS MP"
).split()


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"Input not found: {SRC}")
    if OUT.exists():
        OUT.unlink()
    con = duckdb.connect(str(OUT))
    t0 = time.time()
    states = ", ".join(f"'{s}'" for s in VALID_STATES)
    src = str(SRC).replace("\\", "/")
    reader = f"read_csv('{src}', header=true, all_varchar=true)" if SRC.suffix.lower() == ".csv" else f"read_parquet('{src}')"

    con.execute(f"""
        CREATE TABLE zip3_state AS
        SELECT zip3, state FROM (
            SELECT substr(zip, 1, 3) AS zip3, state, count(*) AS n,
                   row_number() OVER (PARTITION BY substr(zip, 1, 3) ORDER BY count(*) DESC) AS rn
            FROM {reader}
            WHERE zip IS NOT NULL AND length(zip) >= 5 AND regexp_matches(zip, '^[0-9]{{5}}')
              AND state IN ({states})
            GROUP BY 1, 2
        ) WHERE rn = 1 AND n >= 3
    """)
    print("zip3_state:", con.execute("SELECT count(*) FROM zip3_state").fetchone()[0])

    con.execute(f"""
        CREATE TABLE cell_state AS
        SELECT cell_lat, cell_lon, state, city FROM (
            SELECT round(TRY_CAST(latitude AS DOUBLE) * 20) AS cell_lat,
                   round(TRY_CAST(longitude AS DOUBLE) * 20) AS cell_lon,
                   state, mode(city) AS city, count(*) AS n,
                   row_number() OVER (PARTITION BY round(TRY_CAST(latitude AS DOUBLE) * 20), round(TRY_CAST(longitude AS DOUBLE) * 20)
                                      ORDER BY count(*) DESC) AS rn
            FROM {reader}
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND state IN ({states})
            GROUP BY 1, 2, 3
        ) WHERE rn = 1
    """)
    print("cell_state:", con.execute("SELECT count(*) FROM cell_state").fetchone()[0])
    con.close()
    print(f"Done in {time.time() - t0:.0f}s -> {OUT}")


if __name__ == "__main__":
    main()
