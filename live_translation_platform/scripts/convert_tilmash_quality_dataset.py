from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from scripts.benchmark_tilmash_quality import QualityDatasetRow, parse_quality_dataset, warn_if_dataset_short


def main() -> int:
    args = parse_args()
    source = Path(args.source)
    output = Path(args.output)
    rows = parse_quality_dataset(source.read_text(encoding="utf-8-sig"))
    warning = warn_if_dataset_short(parsed_rows=len(rows), expected_rows=args.expected_rows)
    write_clean_csv(rows, output)
    print(f"extracted {len(rows)} rows")
    print(f"wrote {output}")
    return 1 if warning and args.strict_dataset else 0


def write_clean_csv(rows: list[QualityDatasetRow], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "ru", "kk", "uz", "zh"], quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "id": row.id,
                    "ru": row.ru,
                    "kk": row.kk,
                    "uz": row.uz,
                    "zh": row.zh_cn,
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert a copied Tilmash quality dataset to clean CSV.")
    parser.add_argument("source", help="Path to the copied table, CSV, TSV, or markdown table.")
    parser.add_argument("--output", default="data/tilmash_quality_examples.csv")
    parser.add_argument("--expected-rows", type=int, default=30)
    parser.add_argument("--strict-dataset", action="store_true", help="Exit non-zero when parsed rows are below --expected-rows.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
