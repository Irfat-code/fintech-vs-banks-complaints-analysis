"""Download CFPB complaints (2020-01-01 .. today) one product x calendar year at a time.

Each chunk is streamed to data/raw/<slug>_<year>.csv, then validated:
  - parses as CSV with the expected header (incl. "Complaint ID")
  - row count matches the API hit count for the same filters
Failed / short chunks are retried (up to MAX_TRIES) and reported.
"""
import csv, datetime as dt, json, re, sys, time
from pathlib import Path

import requests

URL = "https://www.consumerfinance.gov/data-research/consumer-complaints/search/api/v1/"
PRODUCTS = [
    "Checking or savings account",
    "Credit card",
    "Credit card or prepaid card",
    "Prepaid card",
    "Money transfer, virtual currency, or money service",
]
START_YEAR = 2020
TODAY = dt.date.today()
MAX_TRIES = 5
PAUSE = 4  # seconds between calls
RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)
csv.field_size_limit(2**31 - 1)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def get(params, stream=False):
    return requests.get(URL, params=params, stream=stream, timeout=(30, 600))


def expected_count(product, dmin, dmax):
    for i in range(MAX_TRIES):
        try:
            r = get({"size": 0, "no_aggs": "true", "product": product,
                     "date_received_min": dmin, "date_received_max": dmax})
            r.raise_for_status()
            return r.json()["hits"]["total"]["value"]
        except Exception as e:
            print(f"   count attempt {i+1} failed: {e}", flush=True)
            time.sleep(PAUSE * (i + 1))
    return None


def count_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        rdr = csv.reader(f)
        header = next(rdr, None)
        if not header or "Complaint ID" not in header:
            raise ValueError(f"bad header: {header!r}"[:200])
        n = 0
        ncol = len(header)
        bad = 0
        for row in rdr:
            n += 1
            if len(row) != ncol:
                bad += 1
        return n, bad


def main():
    log = []
    total_bytes = 0
    for product in PRODUCTS:
        for year in range(START_YEAR, TODAY.year + 1):
            dmin = f"{year}-01-01"
            # date_received_max: use next Jan 1 / tomorrow; boundary overlap is removed by Complaint ID dedupe later
            dmax = f"{year+1}-01-01" if year < TODAY.year else (TODAY + dt.timedelta(days=1)).isoformat()
            out = RAW / f"{slug(product)}_{year}.csv"
            exp = expected_count(product, dmin, dmax)
            time.sleep(PAUSE)
            print(f"[{product} | {year}] expected={exp}", flush=True)
            entry = {"product": product, "year": year, "file": out.name, "expected": exp,
                     "attempts": 0, "issues": []}
            for attempt in range(1, MAX_TRIES + 1):
                entry["attempts"] = attempt
                tmp = out.with_suffix(".part")
                try:
                    with get({"format": "csv", "no_aggs": "true", "product": product,
                              "date_received_min": dmin, "date_received_max": dmax},
                             stream=True) as r:
                        r.raise_for_status()
                        with open(tmp, "wb") as f:
                            for chunk in r.iter_content(1 << 20):
                                f.write(chunk)
                    n, bad = count_csv(tmp)
                    if exp is not None and n != exp:
                        raise ValueError(f"row count {n} != expected {exp} (possibly truncated)")
                    if bad:
                        raise ValueError(f"{bad} rows with wrong column count")
                    tmp.replace(out)
                    size = out.stat().st_size
                    total_bytes += size
                    entry.update(rows=n, bytes=size)
                    print(f"   OK attempt {attempt}: {n:,} rows, {size/1e6:.1f} MB", flush=True)
                    break
                except Exception as e:
                    msg = f"attempt {attempt}: {type(e).__name__}: {e}"[:300]
                    entry["issues"].append(msg)
                    print("   FAIL " + msg, flush=True)
                    time.sleep(PAUSE * attempt * 2)
                finally:
                    time.sleep(PAUSE)
            else:
                entry["failed"] = True
            log.append(entry)
            (RAW / "_download_log.json").write_text(json.dumps(log, indent=2))
    print(f"\nTOTAL downloaded: {total_bytes/1e6:.1f} MB in {sum('rows' in e for e in log)} files")
    for e in log:
        if e["issues"] or e.get("failed"):
            print("ISSUES:", e["file"], "FAILED" if e.get("failed") else "recovered", e["issues"])


if __name__ == "__main__":
    main()
