"""
STEP 1 diagnostic: what date range / granularity does POST /v1/env_params return?
Tries several filter_type values and prints metadata.time_range verbatim.

    python scratch_env_params_probe.py

Safe to delete - this is a one-off diagnostic, not part of the app.
"""
import datetime
import json

from fortyguard_client import submit_job, poll_job

LAT, LON = 29.7355, -94.9774  # Baytown, TX
now = datetime.datetime.now(datetime.UTC)
d30 = (now - datetime.timedelta(days=32)).strftime("%Y-%m-%d")
d2 = (now - datetime.timedelta(days=2)).strftime("%Y-%m-%d")

CASES = [
    ("filter_type=0, start_date only (30d back)", dict(start_date=d30, filter_type=0)),
    ("filter_type=1, start_date+start_time (30d back, 12:00)", dict(start_date=d30, start_time="12:00", filter_type=1)),
    ("filter_type=2, start+end_time same day", dict(start_date=d2, start_time="06:00", end_time="18:00", filter_type=2)),
    ("filter_type=3, single day (2d back)", dict(start_date=d2, filter_type=3)),
    ("filter_type=4, range of days (30d back .. 2d back)", dict(start_date=d30, end_date=d2, filter_type=4)),
]

for label, kw in CASES:
    print("\n" + "=" * 70)
    print(label)
    try:
        aid = submit_job(LAT, LON, **kw)
    except Exception as e:
        print("  submit failed:", e)
        continue
    try:
        result = poll_job(aid, timeout_s=900)
    except Exception as e:
        print("  poll failed:", e)
        continue
    md = result.get("metadata", {})
    ts = md.get("timestamps", [])
    params = result["locations"][0].get("parameters", {})
    non_null = {k: sum(1 for x in v if x is not None) for k, v in params.items()
               if isinstance(v, list) and k in
               ("precipitation_mm", "relative_humidity_percent", "wet_bulb_temperature_celsius")}
    print("  time_range:", json.dumps(md.get("time_range", {})))
    print(f"  timestamps: {len(ts)}"
          + (f"   {ts[0]} .. {ts[-1]}" if ts else ""))
    print("  non-null counts:", non_null)
