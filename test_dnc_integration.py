from __future__ import annotations

import csv
import tempfile
from pathlib import Path

import dnc_client


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        source = root / "incoming.csv"
        source.write_text("Name,Phone,Email\nA,(+1) 212.555-0100,a@example.com\nB,212-555-0101,b@example.com\nC,12345,c@example.com\n", encoding="utf-8")
        staged, helper, changed = dnc_client._stage_phone_column(source, "Phone")
        assert helper is None
        assert changed == 3
        with staged.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert [row["Phone"] for row in rows] == ["2125550100", "2125550101", ""]
        output = root / "cleaned.csv"
        count = dnc_client._remove_helper_column(staged, output, None)
        assert count == 3
        with output.open("r", encoding="utf-8-sig", newline="") as handle:
            final_rows = list(csv.DictReader(handle))
        assert final_rows[0]["Phone"] == "2125550100"
        assert final_rows[2]["Phone"] == ""
    ok, text = dnc_client.check_service()
    assert ok, text
    print("DNC integration adapter tests passed")


if __name__ == "__main__":
    main()
