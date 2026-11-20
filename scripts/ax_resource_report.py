#!/usr/bin/env python3
"""Summarize a VoxWeft AX board-resource CSV produced by monitoring."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from statistics import fmean


def number(value: str) -> float | None:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else None
    except (TypeError, ValueError):
        return None


def stats(rows: list[dict[str, str]], field: str) -> dict[str, float | None]:
    values = [number(row.get(field, "")) for row in rows]
    values = [value for value in values if value is not None]
    if not values:
        return {"first": None, "last": None, "min": None, "max": None, "delta": None}
    return {
        "first": values[0],
        "last": values[-1],
        "min": min(values),
        "max": max(values),
        "delta": values[-1] - values[0],
    }


def edge_average(rows: list[dict[str, str]], field: str, count: int = 10) -> float | None:
    values = [number(row.get(field, "")) for row in rows]
    values = [value for value in values if value is not None]
    if len(values) < count * 2:
        return None
    return fmean(values[-count:]) - fmean(values[:count])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()

    with args.csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit(f"no samples in {args.csv}")

    fields = [
        "os_available_mb", "os_used_mb", "cmm_used_mb",
        "llama_rss_mb", "npu_temp_max_c",
    ]
    errors = sum(bool(row.get("error")) for row in rows)
    process_counts = {row.get("llama_process_count", "") for row in rows}

    if args.markdown:
        print(f"# AX resource report: {args.csv.name}")
        print()
        print(f"- Samples: `{len(rows)}`")
        print(f"- Sampling errors: `{errors}`")
        print(f"- llama process counts: `{', '.join(sorted(process_counts)) or 'n/a'}`")
        print()
        print("| Metric | First | Last | Delta | Min | Max | First/last 10 avg drift |")
        print("|---|---:|---:|---:|---:|---:|---:|")
        for field in fields:
            item = stats(rows, field)
            drift = edge_average(rows, field)
            fmt = lambda value: "n/a" if value is None else f"{value:.1f}"
            print(f"| {field} | {fmt(item['first'])} | {fmt(item['last'])} | {fmt(item['delta'])} | {fmt(item['min'])} | {fmt(item['max'])} | {fmt(drift)} |")
    else:
        print({"samples": len(rows), "errors": errors, "process_counts": sorted(process_counts)})
        for field in fields:
            print(field, stats(rows, field), "edge_drift", edge_average(rows, field))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
