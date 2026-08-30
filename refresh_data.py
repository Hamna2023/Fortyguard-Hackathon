"""
Pre-warm the CUI dashboard's 30-day climate windows from the command line.

Each site is one FortyGuard env_params job (filter_type=4, 744 hourly readings).
Jobs are queue-slow, so run this before a demo to populate
data/site_data_cache.json; the app then serves the windows instantly.

    python refresh_data.py

The per-site temperature raster is NOT pre-warmed - the app fetches it live and
re-fetches it every 5 minutes (data/rasters/ is not used).

Reads FORTYGUARD_API_KEY from .env (or the environment).
"""

import datetime
import json
from pathlib import Path

import pandas as pd

from fortyguard_client import submit_window_many, poll_many, summarise_window

DATA_DIR = Path(__file__).parent / "data"
SITES_CSV = DATA_DIR / "refineries.csv"
CACHE_FILE = DATA_DIR / "site_data_cache.json"
WINDOW_DAYS = 30


def main() -> None:
    sites = pd.read_csv(SITES_CSV)
    points = {r["name"]: (r["lat"], r["lon"]) for _, r in sites.iterrows()}

    id_map, submit_errs, (start_date, end_date) = submit_window_many(points, days=WINDOW_DAYS)
    for k, v in submit_errs.items():
        print(f"  submit  {k}: {v}")
    print(f"Submitted {len(id_map)} ({start_date}..{end_date}). Polling (several minutes)...")

    results, poll_errs = poll_many(id_map, timeout_s=1800)
    for k, v in poll_errs.items():
        print(f"  poll    {k}: {v}")

    try:
        cache = json.loads(CACHE_FILE.read_text()).get("sites", {})
    except (json.JSONDecodeError, OSError):
        cache = {}

    stamp = datetime.datetime.now().timestamp()
    ok = 0
    for name, result in results.items():
        s = summarise_window(result)
        if not s.get("n_hours"):
            print(f"  no data {name}")
            continue
        s.update(start_date=start_date, end_date=end_date, fetched_at=stamp)
        cache[name] = s
        ok += 1
        print(f"  ok      {name}: {s['n_hours']} h  precip {s['precip_mm_total']} mm  "
              f"RH {s['avg_rh_pct']}%  SO2 {s.get('avg_so2_idx')}")

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({"sites": cache}, indent=2))
    print(f"\nWrote {ok} sites to {CACHE_FILE}")


if __name__ == "__main__":
    main()
