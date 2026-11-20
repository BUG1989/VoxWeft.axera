#!/usr/bin/env python3
"""Sample an AX board through tempmon and write a resource CSV."""
from __future__ import annotations

import argparse
import csv
import math
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voxweft.runtime.ax8860 import AX_STATS_URL

FIELDS = [
    "timestamp", "epoch", "board_time", "os_total_mb", "os_used_mb",
    "os_available_mb", "cmm_total_mb", "cmm_used_mb", "cmm_delta_mb",
    "llama_process_count", "llama_rss_mb", "llama_rss_delta_mb",
    "npu_temp_max_c", "cpu_load_1m", "rx_mbps", "tx_mbps", "error",
]


def finite(value):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def megabytes(value) -> float | None:
    parsed = finite(value)
    return parsed / 1024 if parsed is not None else None


def sample(url: str) -> dict[str, object]:
    response = requests.get(url, timeout=4)
    response.raise_for_status()
    data = response.json()
    mem = data.get("mem") or {}
    cmm = data.get("cmm") or {}
    llama = data.get("llama") or {}
    zones = data.get("zones") or []
    temps = [finite(zone.get("c")) for zone in zones]
    load = data.get("load") or {}
    net = data.get("net") or {}
    return {
        "board_time": data.get("time"),
        "os_total_mb": megabytes(mem.get("total_kb")),
        "os_used_mb": megabytes(mem.get("used_kb")),
        "os_available_mb": megabytes(mem.get("avail_kb")),
        "cmm_total_mb": megabytes(cmm.get("total_kb")),
        "cmm_used_mb": megabytes(cmm.get("used_kb")),
        "llama_process_count": llama.get("process_count"),
        "llama_rss_mb": finite(llama.get("rss_kb")) / 1024,
        "npu_temp_max_c": max((temp for temp in temps if temp is not None), default=None),
        "cpu_load_1m": finite(load.get("1")),
        "rx_mbps": finite(net.get("rx_mbps")),
        "tx_mbps": finite(net.get("tx_mbps")),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=AX_STATS_URL)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--max-samples", type=int)
    args = parser.parse_args()

    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    first = None
    count = 0
    errors = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        while running and (args.max_samples is None or count < args.max_samples):
            row = {name: "" for name in FIELDS}
            row["timestamp"] = datetime.now().isoformat(timespec="seconds")
            row["epoch"] = int(time.time())
            try:
                current = sample(args.url)
                if first is None:
                    first = current
                row.update({key: value for key, value in current.items() if value is not None})
                if first.get("cmm_used_mb") is not None and current.get("cmm_used_mb") is not None:
                    row["cmm_delta_mb"] = current["cmm_used_mb"] - first["cmm_used_mb"]
                if first.get("llama_rss_mb") is not None and current.get("llama_rss_mb") is not None:
                    row["llama_rss_delta_mb"] = current["llama_rss_mb"] - first["llama_rss_mb"]
            except Exception as exc:
                errors += 1
                row["error"] = repr(exc)
            writer.writerow(row)
            handle.flush()
            count += 1
            time.sleep(max(0.1, args.interval))
    print(f"samples={count} errors={errors} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
