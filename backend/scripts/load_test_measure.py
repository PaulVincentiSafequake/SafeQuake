#!/usr/bin/env python3
"""B5 load-test measurement harness — read-only timing of operator surfaces.

Times each surface 3x and reports median + payload size. Read-only: it only
issues GETs. Run against the PREVIEW backend on localhost:8001.

Usage: python load_test_measure.py [--base http://localhost:8001] [--label "stage 1 (100)"]
"""
import argparse
import os
import statistics
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
TOKEN = os.environ["ADMIN_TRIGGER_PASSWORD"]

SURFACES = [
    ("GET /api/devices", "/api/devices?limit=5000"),
    ("GET /api/public/summary", "/api/public/summary"),
    ("Team report PDF (summary)", "/api/admin/casualty-report/operational.pdf?detail=summary"),
    ("Team report PDF (full)", "/api/admin/casualty-report/operational.pdf?detail=full"),
    ("Safe-to-share PDF", "/api/admin/casualty-report/public.pdf"),
    ("Audit CSV", "/api/admin/audit-log/export.csv"),
    ("Audit PDF", "/api/admin/audit-log/export.pdf"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8001")
    ap.add_argument("--label", default="")
    ap.add_argument("--runs", type=int, default=3)
    a = ap.parse_args()

    h = {"X-Admin-Token": TOKEN}
    print(f"# Load-test measurement {a.label}")
    print(f"{'surface':<30} {'median s':>9} {'max s':>8} {'size':>10}  status")
    for name, path in SURFACES:
        times, size, code = [], 0, "?"
        for _ in range(a.runs):
            t = time.perf_counter()
            try:
                r = requests.get(a.base + path, headers=h, timeout=180)
                code = r.status_code
                size = len(r.content)
            except Exception as e:
                code = f"ERR {type(e).__name__}"
            times.append(time.perf_counter() - t)
        print(f"{name:<30} {statistics.median(times):>9.2f} {max(times):>8.2f} "
              f"{size/1024:>9.1f}K  {code}")


if __name__ == "__main__":
    main()
