#!/usr/bin/env python
"""Collect an optional nvidia-smi snapshot for local load-test reports."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from io import StringIO


QUERY = "timestamp,name,memory.used,memory.total,utilization.gpu,utilization.memory,temperature.gpu,power.draw"


def collect() -> dict:
    command = [
        "nvidia-smi",
        f"--query-gpu={QUERY}",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": exc.__class__.__name__, "gpus": []}
    if result.returncode != 0:
        return {"available": False, "error": result.stderr.strip() or "nvidia-smi failed", "gpus": []}
    rows = []
    for row in csv.reader(StringIO(result.stdout)):
        if not row:
            continue
        values = [item.strip() for item in row]
        rows.append(
            {
                "timestamp": values[0] if len(values) > 0 else "",
                "name": values[1] if len(values) > 1 else "",
                "memory_used_mb": _number(values[2] if len(values) > 2 else None),
                "memory_total_mb": _number(values[3] if len(values) > 3 else None),
                "utilization_gpu_percent": _number(values[4] if len(values) > 4 else None),
                "utilization_memory_percent": _number(values[5] if len(values) > 5 else None),
                "temperature_gpu_c": _number(values[6] if len(values) > 6 else None),
                "power_draw_w": _number(values[7] if len(values) > 7 else None),
            }
        )
    return {"available": True, "gpus": rows}


def _number(value: str | None):
    try:
        return float(value) if value not in {None, ""} else None
    except ValueError:
        return None


def main() -> int:
    print(json.dumps(collect(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
