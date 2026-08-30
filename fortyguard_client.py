"""
FortyGuard Temperature API client - drives the CUI (Corrosion Under Insulation)
screening dashboard.

Primary use: climate_window() pulls a trailing 30-day hourly series for a point
(env_params, filter_type=4) and reduces it to the scalars the CUI climate score
needs - cumulative precipitation, average RH, and freeze-thaw crossings of the
derived hourly dry-bulb. heatmap() is kept for the optional per-site raster.

CONFIRMED (from FortyGuard's live docs at https://docs-api.fortyguard.com
and by running this file against the real API):
  - Base URL: https://api.fortyguard.com
  - Auth header: "api-key": "<your key>"
  - Submit:  POST /v1/env_params
      Payload: {"latitude", "longitude", "temperature",
                "date_time": {"start_date", "start_time", "filter_type"}}
      "temperature" is REQUIRED (submit 422s without it) and is the dry-bulb
      air temperature in deg C - but the API only uses it to compute
      heat_index_celsius (see KEY FINDING below), so for our derived pipeline
      any plausible placeholder works.
      Response: {"error": false, "data": {"activity_id": "..."}}
  - Poll/result:  GET /v1/status/{activity_id}
      This is the ONE unified status+result endpoint for every async
      FortyGuard submission (env_params, heatmap, satellite, ...).
      While running:   {"data": {"status": "Processing"}}
      When finished:    {"data": {"status": "Completed", "result": {...}}}
      On failure:       {"data": {"status": "Failed"}}   (does NOT consume credits)
      env_params jobs typically take a few minutes, so poll with a
      generous timeout.
  - Result shape (data.result):
      metadata.timestamps -> list of ISO timestamps
      locations[0].temperature -> just echoes back the "temperature" you sent
      locations[0].parameters.<field> -> LIST of numbers/nulls, one per
        timestamp (index [0] for a single point-in-time query). Empty list []
        for timestamps with no data yet (the current partial hour).
      locations[0].solar_irradiance.clear_sky.{ghi,dni,dhi}
    Available parameter fields include: heat_index_celsius,
    apparent_temperature_celsius, relative_humidity_percent,
    wet_bulb_temperature_celsius, precipitation_mm, cloud_cover_octas,
    air_quality:idx (+ per-pollutant AQI), methane_ppb, co2_ppm.

KEY FINDING - the "temperature" input IS the dry-bulb air temperature:
  Submitting temperature=25 vs temperature=40 for the same point/time leaves
  wet_bulb_temperature_celsius, relative_humidity_percent and
  apparent_temperature_celsius UNCHANGED (those are the model's own forecast),
  but heat_index_celsius jumps from 25.8 -> 53.7. So the API does NOT return a
  modelled dry-bulb; it expects the caller to supply it and only uses it to
  compute heat_index. For a "current conditions" dashboard we don't have that
  number, so we DERIVE dry-bulb from the model's wet-bulb + RH via
  derive_dry_bulb_c() (invert Stull's wet-bulb approximation) and compute the
  heat index ourselves (see risk.py).

  - POST /v1/heatmap  (visual raster, see heatmap()): polygon_aoi (GeoJSON)
    + date_time + granularity (60/80/100 m tiles). Basic tier caps the AOI at
    ~10 mi^2. Completed result -> {map_data: GeoJSON FeatureCollection with one
    temperature polygon per tile (properties.average_temperature, deg C),
    stats_data.temperature_stats: min/max/mean/std}.

NOTES / still open:
  - The API rejects bursts with a misleading 401 "Invalid or unknown API key"
    and stays locked ~60-90s, re-armed by each rejected request. _request()
    throttles every call and, on 401, waits a fixed cooldown before retrying.
  - env_params / heatmap jobs are slow and queue-dependent (30s to many minutes).
  - Point data is empty ([]) for the current partial hour; request a timestamp
    a couple of hours back.
  - "analysis" field: DO NOT send it to /v1/env_params. It is documented for
    /v1/heatmap; on env_params it is accepted but makes the completed result
    come back with every parameter null. Omit it and all ~15 fields return.
  - No WBGT (wet-bulb GLOBE temperature) field exists. Worker safety uses the
    NWS heat index (see risk.py); apparent_temperature_celsius (sun-inclusive)
    is carried through as an extra column.
  - filter_type: 1 (single hour) and 3 (single day, 24 hourly values) and
    4 (range of days, hourly values over the range, <= 1 month) all work on
    env_params. 2 (range of hours) -> 500. filter_type 4 with a 30-day range
    returns ~744 non-null hourly precip / RH / wet-bulb values in one job.
"""

import collections
import datetime
import math
import os
import threading
import time

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_URL = "https://api.fortyguard.com"
API_KEY = os.environ.get("FORTYGUARD_API_KEY")


def _headers() -> dict:
    """Built lazily so a key loaded after import (e.g. via st.secrets) is picked up."""
    key = os.environ.get("FORTYGUARD_API_KEY", API_KEY)
    if not key:
        raise FortyGuardError(
            "FORTYGUARD_API_KEY is not set. Put it in .env (local) or the "
            "app's Secrets settings (Streamlit Cloud)."
        )
    return {"api-key": key, "Content-Type": "application/json"}


# Kept for reference only - see the docstring note: do NOT pass this to
# /v1/env_params (it nulls out the whole result).
ANALYSIS_FIELDS = None

# The "temperature" field is required but, for our derived pipeline, only feeds
# the API's heat_index (which we don't use - we compute our own). Any plausible
# value works; this is a mild climatological placeholder in deg C.
DEFAULT_REFERENCE_TEMPERATURE_C = 25.0


def derive_dry_bulb_c(wet_bulb_c: float, relative_humidity_pct: float) -> float | None:
    """
    Recover dry-bulb air temperature from the model's wet-bulb + RH by
    inverting Stull's (2011) wet-bulb approximation numerically (bisection).
    Valid roughly for RH 5-99% and standard sea-level pressure.
    """
    if wet_bulb_c is None or relative_humidity_pct is None:
        return None
    rh = max(1.0, min(100.0, relative_humidity_pct))

    def stull_wet_bulb(td: float) -> float:
        return (td * math.atan(0.151977 * math.sqrt(rh + 8.313659))
                + math.atan(td + rh) - math.atan(rh - 1.676331)
                + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh)
                - 4.686035)

    lo, hi = wet_bulb_c, wet_bulb_c + 40.0
    if stull_wet_bulb(hi) < wet_bulb_c:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if stull_wet_bulb(mid) < wet_bulb_c:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 1)


class FortyGuardError(Exception):
    pass


# The API rejects bursts of requests with a (misleading) 401 "Invalid or
# unknown API key" and stays locked for ~60-90s, re-armed by each further
# rejected request - so on 401 we wait a longer FIXED cooldown (retrying
# sooner just keeps the lockout alive). 429/5xx get normal exponential backoff.
_RETRY_STATUS = {401, 429, 500, 502, 503, 504}
_LOCKOUT_COOLDOWN_S = 80.0

_RATE_LOCK = threading.Lock()
_MIN_INTERVAL_S = 1.2  # minimum spacing between ANY two API calls
_last_call = [0.0]

# In-process log of every HTTP call to FortyGuard, newest last, capped. The
# Streamlit backend makes these server-side (Python `requests`), so they never
# appear in the browser's Network tab - this list + the stderr line are how you
# see them. The app renders it in a sidebar expander.
API_CALL_LOG = collections.deque(maxlen=40)


def _log_call(method: str, url: str, status, ms: float, note: str = "") -> None:
    entry = {"at": datetime.datetime.now().strftime("%H:%M:%S"),
             "method": method, "path": url.replace(BASE_URL, ""),
             "status": status if status is not None else "ERR", "ms": round(ms),
             "note": note}
    API_CALL_LOG.append(entry)
    print(f"[fortyguard] {entry['at']} {method} {entry['path']} "
          f"-> {entry['status']} ({entry['ms']} ms) {note}", flush=True)


def _throttle() -> None:
    with _RATE_LOCK:
        wait = _MIN_INTERVAL_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def _request(method: str, url: str, *, retries: int = 5, backoff: float = 2.0,
             max_sleep: float = 45.0, **kw):
    """
    Retry on connection errors and _RETRY_STATUS. A 401 means the burst lockout
    is active, so wait a fixed cooldown; 429/5xx get capped exponential backoff.
    """
    kw.setdefault("timeout", 30)
    last = None
    for attempt in range(retries + 1):
        _throttle()
        status = None
        t0 = time.monotonic()
        try:
            resp = requests.request(method, url, headers=_headers(), **kw)
        except requests.RequestException as e:
            last = str(e)
            _log_call(method, url, None, (time.monotonic() - t0) * 1000, str(e)[:60])
        else:
            status = resp.status_code
            _log_call(method, url, status, (time.monotonic() - t0) * 1000,
                      "" if status == 200 else "retry" if status in _RETRY_STATUS else "")
            if status == 200 or status not in _RETRY_STATUS:
                return resp
            last = f"{status} {resp.text[:200]}"

        if attempt == retries:
            raise FortyGuardError(f"{method} {url} failed after {retries} retries: {last}")
        time.sleep(_LOCKOUT_COOLDOWN_S if status == 401
                   else min(backoff * (2 ** attempt), max_sleep))
    raise FortyGuardError(f"{method} {url} failed: {last}")


def submit_job(lat: float, lon: float, start_date: str, start_time: str | None = None,
                filter_type: int = 1, reference_temperature: float | None = None,
                analysis: list[str] | None = None,
                end_date: str | None = None, end_time: str | None = None) -> str:
    """
    Submit an env_params request for a point. Returns an activity_id to poll.
    filter_type: 1 single hour (start_time), 2 range of hours same day
        (start_time + end_time), 3 single day, 4 range of days (start_date +
        end_date, <= 1 month). 3 and 4 return full hourly arrays over the range.
    """
    dt: dict = {"start_date": start_date, "filter_type": filter_type}
    if start_time is not None:
        dt["start_time"] = start_time
    if end_date is not None:
        dt["end_date"] = end_date
    if end_time is not None:
        dt["end_time"] = end_time

    payload = {
        "latitude": lat,
        "longitude": lon,
        "temperature": (reference_temperature if reference_temperature is not None
                        else DEFAULT_REFERENCE_TEMPERATURE_C),
        "date_time": dt,
    }
    if analysis:
        payload["analysis"] = analysis

    resp = _request("POST", f"{BASE_URL}/v1/env_params", json=payload)
    if resp.status_code != 200:
        raise FortyGuardError(f"submit failed: {resp.status_code} {resp.text}")
    return resp.json()["data"]["activity_id"]


def poll_job(activity_id: str, timeout_s: int = 900, interval_s: float = 10.0) -> dict:
    """
    Poll GET /v1/status/{activity_id} until the job completes.
    Returns data.result. Raises FortyGuardError on failure or timeout.
    Uses a wall-clock deadline (retries/throttle inside _request also count).
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = _request("GET", f"{BASE_URL}/v1/status/{activity_id}")
        if resp.status_code != 200:
            raise FortyGuardError(f"poll failed: {resp.status_code} {resp.text}")
        data = resp.json().get("data", {})
        status = str(data.get("status", "")).lower()
        if status in ("completed", "succeeded", "success"):
            result = data.get("result")
            if result is None:
                raise FortyGuardError(f"completed but no result payload: {data}")
            return result
        if status in ("failed", "error"):
            raise FortyGuardError(f"job failed: {data}")
        time.sleep(interval_s)
    raise FortyGuardError(f"activity {activity_id} timed out after {timeout_s}s")


def bbox_polygon(lat: float, lon: float, half_deg: float = 0.02) -> dict:
    """A small square GeoJSON FeatureCollection centred on a point (~1.4 mi per 0.02 deg)."""
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [[
                [lon - half_deg, lat - half_deg], [lon + half_deg, lat - half_deg],
                [lon + half_deg, lat + half_deg], [lon - half_deg, lat + half_deg],
                [lon - half_deg, lat - half_deg],
            ]]},
        }],
    }


def heatmap(polygon_aoi: dict, start_date: str, start_time: str | None = None,
            granularity: int = 100, filter_type: int = 1,
            end_date: str | None = None, end_time: str | None = None,
            analytic_type: str = "tcm", threshold: float | None = None,
            direction: str | None = None, timeout_s: int = 900) -> dict:
    """
    POST /v1/heatmap for a GeoJSON polygon, then poll.

    AOI cap: ~10 mi^2 (Basic tier), ~50 mi^2 (Premium).
    granularity: tile size in metres - 60 (finest), 80, 100.
    filter_type: 1 single hour (start_time), 2 range of hours same day
        (start_time + end_time), 3 single day (00:00-23:59), 4 range of days
        (start_date + end_date, <= 1 month).
    date range: 2019-01-01 .. now + 12 h.
    analytic_type:
        "tcm"             - temperature snapshot, deg C per tile (default)
        "time_of_measure" - UTC hour (0-23) of the peak temperature
        "exceedance"      - hours the temperature passed `threshold`
        "persistence"     - longest continuous run of hours past `threshold`
    threshold (deg C, default 30) + direction ("above"/"below") apply to
    exceedance / persistence only.

    Returns data.result -> {map_data: GeoJSON FeatureCollection (per-tile
    properties incl. average_temperature), stats_data: {temperature_stats,
    temperature_frequency, normal_temperature_distribution, ...}}.
    """
    dt: dict = {"start_date": start_date, "filter_type": filter_type}
    if start_time is not None:
        dt["start_time"] = start_time
    if end_date is not None:
        dt["end_date"] = end_date
    if end_time is not None:
        dt["end_time"] = end_time

    payload: dict = {"polygon_aoi": polygon_aoi, "date_time": dt, "granularity": granularity}
    if analytic_type and analytic_type != "tcm":
        payload["analytic_type"] = analytic_type
        if threshold is not None:
            payload["threshold"] = threshold
        if direction is not None:
            payload["direction"] = direction

    # A 401 is rejected before any job is created, so retrying the POST is safe.
    # retries=1: the raster is fetched interactively on the refinery screen and
    # re-tried on the next 5-min bucket anyway, so fail fast rather than hang.
    resp = _request("POST", f"{BASE_URL}/v1/heatmap", json=payload, retries=1)
    if resp.status_code != 200:
        raise FortyGuardError(f"heatmap submit failed: {resp.status_code} {resp.text}")
    return poll_job(resp.json()["data"]["activity_id"], timeout_s=timeout_s)


def heatmap_tiles(result: dict) -> tuple[dict | None, dict | None]:
    """
    Pull (map_data, stats_data) out of a heatmap result.

    map_data is a GeoJSON FeatureCollection. Tile properties depend on analytic_type:
      tcm         -> tile_id, average_temperature/min_temperature/max_temperature (deg C)
      exceedance / persistence / time_of_measure -> tile_id, value (hours; hour-of-day for tom)

    stats_data also depends on analytic_type:
      tcm         -> temperature_stats{minimum,maximum,mean,standard_deviation},
                     plus temperature_frequency / *_distribution arrays
      other       -> {analytic_type, units, n_cells, min, max, mean}
    """
    inner = result.get("Result", result)
    return inner.get("map_data"), inner.get("stats_data")


def site_raster(lat: float, lon: float, *, half_deg: float = 0.014,
                granularity: int = 100, lookback_hours: int = 3) -> dict:
    """
    Submit + poll one tcm heatmap for a small box around a point, then return a
    COMPACT payload suitable for caching to disk and overlaying on a map:

        {"for": "<iso hour>", "stats": {"min","max","mean"},
         "tiles": {"type": "FeatureCollection",
                   "features": [{"type":"Feature","geometry":{...},
                                 "properties":{"t": <deg C, 1 dp>}}, ...]}}

    Coordinates are rounded to 5 dp (~1 m) to keep the file small (~150-250 KB).
    """
    ts = (datetime.datetime.now(datetime.UTC)
          - datetime.timedelta(hours=lookback_hours)).replace(minute=0, second=0)
    result = heatmap(bbox_polygon(lat, lon, half_deg=half_deg),
                     ts.strftime("%Y-%m-%d"), ts.strftime("%H:%M"), granularity=granularity)
    map_data, stats = heatmap_tiles(result)
    feats = (map_data or {}).get("features") or []

    def _round_geom(g):
        return {"type": g["type"],
                "coordinates": [[[round(x, 5), round(y, 5)] for x, y in ring]
                                for ring in g["coordinates"]]}

    out_feats = []
    for f in feats:
        t = f.get("properties", {}).get("average_temperature")
        if isinstance(t, (int, float)) and f.get("geometry"):
            out_feats.append({"type": "Feature", "geometry": _round_geom(f["geometry"]),
                              "properties": {"t": round(t, 1)}})
    tstat = (stats or {}).get("temperature_stats", {})
    return {
        "for": ts.strftime("%Y-%m-%dT%H:00Z"),
        "stats": {"min": round(tstat.get("minimum", 0), 1),
                  "max": round(tstat.get("maximum", 0), 1),
                  "mean": round(tstat.get("mean", 0), 1)},
        "tiles": {"type": "FeatureCollection", "features": out_feats},
    }


def submit_many(points: dict, start_date: str, start_time: str,
                filter_type: int = 1, analysis: list[str] | None = None) -> tuple[dict, dict]:
    """
    points: {key: (lat, lon)}  (or {key: (lat, lon, reference_temp)})
    Submits sequentially (the throttle keeps us under the burst limit).
    Returns ({key: activity_id}, {key: error_string}).
    """
    ids, errors = {}, {}
    for key, spec in points.items():
        lat, lon = spec[0], spec[1]
        ref = spec[2] if len(spec) > 2 else None
        try:
            ids[key] = submit_job(lat, lon, start_date, start_time, filter_type,
                                  reference_temperature=ref, analysis=analysis)
        except FortyGuardError as e:
            errors[key] = str(e)
    return ids, errors


def submit_window_many(points: dict, days: int = 30,
                       end_offset_days: int = 2) -> tuple[dict, dict, tuple[str, str]]:
    """
    points: {key: (lat, lon)}. Submits one trailing-window (filter_type=4) job
    per point, sequentially. Returns ({key: activity_id}, {key: error}, (start, end)).
    Pair with poll_many(); pass each result through summarise_window().
    """
    start_date, end_date = window_dates(days, end_offset_days)
    ids, errors = {}, {}
    for key, (lat, lon) in points.items():
        try:
            ids[key] = submit_job(lat, lon, start_date, filter_type=4, end_date=end_date)
        except FortyGuardError as e:
            errors[key] = str(e)
    return ids, errors, (start_date, end_date)


def poll_many(id_map: dict, timeout_s: int = 900, interval_s: float = 5.0) -> tuple[dict, dict]:
    """
    id_map: {key: activity_id}. Round-robin polls all jobs (throttled) until
    each finishes. Returns ({key: result}, {key: error_string}).
    """
    pending = dict(id_map)
    results, errors = {}, {}
    deadline = time.monotonic() + timeout_s
    while pending and time.monotonic() < deadline:
        for key, aid in list(pending.items()):
            try:
                resp = _request("GET", f"{BASE_URL}/v1/status/{aid}")
                data = resp.json().get("data", {})
                status = str(data.get("status", "")).lower()
                if status in ("completed", "succeeded", "success"):
                    if data.get("result") is None:
                        errors[key] = f"completed but no result: {data}"
                    else:
                        results[key] = data["result"]
                    pending.pop(key)
                elif status in ("failed", "error"):
                    errors[key] = f"job failed: {data}"
                    pending.pop(key)
            except FortyGuardError as e:
                errors[key] = str(e)
                pending.pop(key)
        if pending:
            time.sleep(interval_s)
    for key in pending:
        errors[key] = f"timed out after {timeout_s}s"
    return results, errors


def flatten_location(result: dict) -> dict:
    """
    Pull the first location's first-timestamp readings into a flat dict.
    dry_bulb_c is DERIVED (see module docstring) - the API does not return it.
    """
    location = result["locations"][0]
    params = location.get("parameters", {})

    def first(field):
        values = params.get(field)
        if isinstance(values, list):
            return values[0] if values else None
        return values

    wet_bulb_c = first("wet_bulb_temperature_celsius")
    rh_pct = first("relative_humidity_percent")
    return {
        "wet_bulb_c": wet_bulb_c,
        "relative_humidity_pct": rh_pct,
        "apparent_temp_c": first("apparent_temperature_celsius"),
        "dry_bulb_c": derive_dry_bulb_c(wet_bulb_c, rh_pct),
        "air_quality_idx": first("air_quality:idx"),
    }


def get_site_data(lat: float, lon: float, start_date: str, start_time: str,
                   filter_type: int = 1, reference_temperature: float | None = None) -> dict:
    """
    Convenience wrapper: submit + poll one point, return the flattened readings
    (wet_bulb_c, relative_humidity_pct, apparent_temp_c, derived dry_bulb_c).
    """
    activity_id = submit_job(lat, lon, start_date, start_time, filter_type,
                             reference_temperature=reference_temperature,
                             analysis=ANALYSIS_FIELDS)
    return flatten_location(poll_job(activity_id))


def snapshot(lat: float, lon: float, lookback_hours: int = 3) -> dict:
    """
    A single current-conditions reading (filter_type=1) for the most recent
    complete hour. Returns flatten_location() fields plus 'timestamp' (the ISO
    time the reading is for) and 'time_range' (the API's own metadata).
    """
    ts = (datetime.datetime.now(datetime.UTC)
          - datetime.timedelta(hours=lookback_hours)).replace(minute=0, second=0)
    aid = submit_job(lat, lon, ts.strftime("%Y-%m-%d"), ts.strftime("%H:00"), filter_type=1)
    result = poll_job(aid)
    flat = flatten_location(result)
    md = result.get("metadata", {})
    stamps = md.get("timestamps") or []
    flat["timestamp"] = stamps[0] if stamps else None
    flat["time_range"] = md.get("time_range")
    return flat


# ---- trailing climate window (for the CUI model) ----

def window_dates(days: int = 30, end_offset_days: int = 2) -> tuple[str, str]:
    """(start_date, end_date) for a trailing window ending end_offset_days ago
    (the API returns empty arrays for the current hour, so we stop short)."""
    end = datetime.date.today() - datetime.timedelta(days=end_offset_days)
    start = end - datetime.timedelta(days=days)
    return start.isoformat(), end.isoformat()


def summarise_window(result: dict) -> dict:
    """
    Reduce a filter_type=4 env_params result to the scalars the CUI climate
    score needs. Freeze-thaw counts 0 C crossings in the DERIVED hourly
    dry-bulb series (wet-bulb + RH -> dry-bulb per hour).
    """
    loc = result["locations"][0]
    p = loc.get("parameters", {})
    rh = p.get("relative_humidity_percent") or []
    precip = p.get("precipitation_mm") or []
    wet = p.get("wet_bulb_temperature_celsius") or []
    so2 = p.get("air_quality_so2:idx") or []

    rh_vals = [x for x in rh if isinstance(x, (int, float))]
    precip_vals = [x for x in precip if isinstance(x, (int, float))]
    so2_vals = [x for x in so2 if isinstance(x, (int, float))]

    dry = []
    for w, h in zip(wet, rh):
        d = derive_dry_bulb_c(w, h) if isinstance(w, (int, float)) and isinstance(h, (int, float)) else None
        if d is not None:
            dry.append(d)

    crossings, prev = 0, None
    for t in dry:
        if prev is not None and (prev * t) < 0:
            crossings += 1
        prev = t

    n = max(len(rh_vals), 1)
    return {
        "n_hours": len(rh_vals),
        "precip_mm_total": round(sum(precip_vals), 1),
        "avg_rh_pct": round(sum(rh_vals) / n, 1) if rh_vals else None,
        "hours_rh_over_80": sum(1 for x in rh_vals if x >= 80),
        "freeze_thaw_count": crossings,
        "avg_so2_idx": round(sum(so2_vals) / len(so2_vals), 1) if so2_vals else None,
        "max_so2_idx": round(max(so2_vals), 1) if so2_vals else None,
        "min_dry_bulb_c": round(min(dry), 1) if dry else None,
        "max_dry_bulb_c": round(max(dry), 1) if dry else None,
    }


def climate_window(lat: float, lon: float, days: int = 30,
                   end_offset_days: int = 2, timeout_s: int = 900) -> dict:
    """Submit + poll one trailing-window job and summarise it."""
    start_date, end_date = window_dates(days, end_offset_days)
    aid = submit_job(lat, lon, start_date, filter_type=4, end_date=end_date)
    summary = summarise_window(poll_job(aid, timeout_s=timeout_s))
    summary["start_date"], summary["end_date"], summary["days"] = start_date, end_date, days
    return summary


if __name__ == "__main__":
    # Quick manual test:  python fortyguard_client.py
    test_lat, test_lon = 29.8850, -93.9400  # Port Arthur, TX
    print(f"30-day climate window for ({test_lat}, {test_lon}) ...")
    print(climate_window(test_lat, test_lon))
