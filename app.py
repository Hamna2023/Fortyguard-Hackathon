"""
CUI Screening Dashboard - Streamlit app.

Estimates a Corrosion-Under-Insulation rate (mpy) per API RP 581 (2016), Part 2,
Section 16, Table 16.2 - operating temperature x climate "driver" - with linear
interpolation between temperature rows. The API 571 / API RP 583 material
temperature-band gate (~-12 C to 175 C) is applied first. Operating temperatures
are published typical values (an API RP 584 stand-in for per-asset IOW data).

Data:  one FortyGuard env_params job per site pulls a trailing 30-day hourly
series (filter_type=4) -> cumulative precip + mean SO2 index, which classify the
Table 16.2 driver (Marine / Temperate / Arid-Dry / Severe). Jobs are queue-slow;
results cache to data/site_data_cache.json and are served even when stale.
"""

import datetime
import json
import os
import random
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st

from risk import (cui_assessment, classify_driver, cui_corrosion_rate_mpy,
                  f_to_c, INSULATION_CONDITION_MULT, DRIVERS,
                  CUI_SUSCEPTIBLE_MIN_F, CUI_SUSCEPTIBLE_MAX_F,
                  CUI_SUSCEPTIBLE_MIN_C, CUI_SUSCEPTIBLE_MAX_C,
                  SEVERE_SO2_IDX, ARID_PRECIP_MM_PER_30D)

RATE_AXIS_MAX = 20.0  # progress bars / colour ramp scale to the Table 16.2 max

DATA_DIR = Path(__file__).parent / "data"
SITES_CSV = DATA_DIR / "refineries.csv"
CACHE_FILE = DATA_DIR / "site_data_cache.json"

USE_MOCK_DATA = os.environ.get("CUI_MOCK") == "1"  # True = build the UI without hitting the API
STALE_AFTER_SECONDS = 12 * 3600
WINDOW_DAYS = 30

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
try:
    if "FORTYGUARD_API_KEY" in st.secrets:
        os.environ["FORTYGUARD_API_KEY"] = st.secrets["FORTYGUARD_API_KEY"]
except Exception:
    pass

st.set_page_config(page_title="CUI Screening Dashboard", layout="wide",
                   initial_sidebar_state="collapsed")

COLOR_RGB = {
    "green": [46, 160, 67], "yellow": [230, 190, 40], "orange": [230, 130, 30],
    "red": [200, 40, 40], "gray": [140, 140, 140],
}


ASSETS_CSV = DATA_DIR / "assets.csv"


@st.cache_data(ttl=600)
def load_sites() -> pd.DataFrame:
    return pd.read_csv(SITES_CSV)


@st.cache_data(ttl=600)
def load_assets() -> pd.DataFrame:
    df = pd.read_csv(ASSETS_CSV)
    df["compute"] = df["compute"].astype(bool)
    df["note"] = df["note"].fillna("")
    return df


def _pretty(s: str) -> str:
    return s.replace("_", " ").capitalize()


def _pill(text: str, color: str) -> str:
    r, g, b = COLOR_RGB.get(color, COLOR_RGB["gray"])
    return (f'<span style="background:rgb({r},{g},{b});color:#fff;padding:2px 10px;'
            f'border-radius:12px;font-weight:600;font-size:0.85em">{text}</span>')


def risk_pill(label: str, color: str) -> str:
    return _pill(_pretty(label), color)


def mock_window_for_site(row) -> dict:
    """Plausible trailing-window summary (same schema as summarise_window)."""
    rng = random.Random(hash(row["name"]) & 0xFFFF)
    warm = row["state"] in ("TX", "LA", "CA")
    precip = rng.uniform(5, 220)
    rh = rng.uniform(55, 90) if warm else rng.uniform(45, 80)
    ft = 0 if warm else rng.randint(0, 20)
    return {
        "n_hours": WINDOW_DAYS * 24,
        "precip_mm_total": round(precip, 1),
        "avg_rh_pct": round(rh, 1),
        "hours_rh_over_80": int(WINDOW_DAYS * 24 * max(0, (rh - 60) / 40)),
        "freeze_thaw_count": ft,
        "avg_so2_idx": round(rng.uniform(0.5, 6), 1),
        "max_so2_idx": round(rng.uniform(5, 20), 1),
        "min_dry_bulb_c": round(rng.uniform(-8, 5) if not warm else rng.uniform(8, 18), 1),
        "max_dry_bulb_c": round(rng.uniform(28, 40), 1),
        "start_date": "mock-start", "end_date": "mock-end",
    }


# ---- disk cache ----

def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text()).get("sites", {})
    except (json.JSONDecodeError, OSError, AttributeError):
        return {}


def _save_cache(sites: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({"sites": sites}, indent=2))


def _is_fresh(entry: dict) -> bool:
    return (datetime.datetime.now().timestamp() - entry.get("fetched_at", 0)) < STALE_AFTER_SECONDS


def fetch_windows(sites: pd.DataFrame, force: bool = False) -> tuple[dict, list[str]]:
    """{site_name: window summary}. Serves the cache; fetches only missing sites."""
    if USE_MOCK_DATA:
        return {r["name"]: mock_window_for_site(r) for _, r in sites.iterrows()}, []

    cache = _load_cache()
    to_fetch = {r["name"]: (r["lat"], r["lon"]) for _, r in sites.iterrows()
                if force or r["name"] not in cache}
    if not to_fetch:
        return cache, []

    from fortyguard_client import submit_window_many, poll_many, summarise_window

    errors: list[str] = []
    with st.spinner(f"Submitting {len(to_fetch)} FortyGuard {WINDOW_DAYS}-day window jobs..."):
        id_map, submit_errs, (start_date, end_date) = submit_window_many(to_fetch, days=WINDOW_DAYS)
    errors += [f"{k}: {v}" for k, v in submit_errs.items()]

    if id_map:
        with st.spinner(f"Waiting for {len(id_map)} jobs (queue-slow, a few minutes)..."):
            results, poll_errs = poll_many(id_map)
        errors += [f"{k}: {v}" for k, v in poll_errs.items()]

        stamp = datetime.datetime.now().timestamp()
        wrote = False
        for name, result in results.items():
            try:
                s = summarise_window(result)
            except (KeyError, IndexError, TypeError) as e:
                errors.append(f"{name}: bad result payload ({e})")
                continue
            if not s.get("n_hours"):
                errors.append(f"{name}: no data for {start_date}..{end_date}")
                continue
            s.update(start_date=start_date, end_date=end_date, fetched_at=stamp)
            cache[name] = s
            wrote = True
        if wrote:
            _save_cache(cache)

    return cache, errors


# ---- scoring ----

def site_driver(window: dict, coastal: bool) -> tuple[str, str]:
    return classify_driver(coastal, window.get("avg_so2_idx"), window.get("precip_mm_total"))


def asset_table(assets: pd.DataFrame, window: dict, coastal: bool,
                insulation: str = "Average") -> pd.DataFrame:
    """One row per asset: computed CUI rate (mpy) + band, sorted worst-first.
    Non-computed assets (e.g. the furnace transfer line) fall to the bottom."""
    driver = site_driver(window, coastal)[0]
    rows = []
    for _, r in assets.iterrows():
        temp_f = float(r["default_temp_f"])   # asset register is in Fahrenheit
        if not r["compute"]:
            rows.append({"asset": r["asset"], "material": r["material"],
                         "temp_f": temp_f, "rate_mpy": None,
                         "band": "not computed", "color": "gray", "note": r["note"],
                         "_sort": -1.0})
            continue
        a = cui_assessment(temp_f, driver, insulation)   # cui_assessment takes Fahrenheit
        rows.append({
            "asset": r["asset"], "material": r["material"],
            "temp_f": temp_f,
            "rate_mpy": a["rate_mpy"] if a["in_range"] else 0.0,
            "band": a["label"] if a["in_range"] else "outside CUI band",
            "color": a["color"], "note": r["note"],
            "_sort": a["rate_mpy"] if a["in_range"] else 0.0,
        })
    return pd.DataFrame(rows).sort_values("_sort", ascending=False).reset_index(drop=True)


MARKER_SLATE = [90, 105, 120]


def make_map(markers: pd.DataFrame, *, highlight: str | None = None,
             center: tuple[float, float] | None = None, zoom: float = 3.3,
             marker_radius: int | None = None, overlays: list | None = None,
             basemap: str | None = "light") -> pdk.Deck:
    """
    The one map used by every screen. `markers` is a DataFrame with
    name/lat/lon/operator/city/state. `highlight` rings one marker. `center`/`zoom`
    set the view (default: centroid of the markers). `overlays` are pdk.Layers
    drawn UNDER the markers - e.g. a FortyGuard temperature raster.
    `basemap`: "light"/"dark"/"road"/... (a CARTO style) or None for no basemap
    (used when the FortyGuard raster is the map surface).
    Rendered by the caller with st.pydeck_chart(deck, ...); this only builds the Deck.
    """
    d = markers.copy()
    if marker_radius is None:                       # shrink markers as we zoom in
        marker_radius = int(max(300, 34000 / (2 ** (zoom - 3.3))))
    d["radius"] = [int(marker_radius * 1.8) if n == highlight else marker_radius
                   for n in d["name"]]

    layers = list(overlays or [])
    layers.append(pdk.Layer("ScatterplotLayer", id="sites", data=d,
                            get_position=["lon", "lat"], get_fill_color=MARKER_SLATE,
                            get_radius="radius", pickable=True))
    if highlight is not None and (d["name"] == highlight).any():
        layers.append(pdk.Layer("ScatterplotLayer", data=d[d["name"] == highlight],
                                get_position=["lon", "lat"], get_fill_color=[0, 0, 0, 0],
                                get_line_color=[20, 20, 20], get_radius=int(marker_radius * 2.8),
                                stroked=True, line_width_min_pixels=2))

    if center is None:
        center = (float(d["lat"].mean()), float(d["lon"].mean())) if len(d) else (37.0, -96.0)
    return pdk.Deck(
        layers=layers,
        map_provider="carto" if basemap else None,
        map_style=basemap,
        initial_view_state=pdk.ViewState(latitude=center[0], longitude=center[1], zoom=zoom),
        tooltip={"text": "{name} ({operator})\n{city}, {state}"},
    )


RASTER_TTL_S = 300          # re-fetch the site's raster at most once per 5 min
RASTER_HALF_DEG = 0.014     # ~1 mi box - tight on the plant, ~800 tiles
RASTER_DIR = DATA_DIR / "rasters"


def _raster_slug(name: str) -> str:
    return name.lower().replace(" ", "_").replace("/", "-")


def fetch_and_cache_raster(name: str, lat: float, lon: float) -> dict | None:
    """One live /v1/heatmap fetch; writes it as this site's last-good on success."""
    from fortyguard_client import site_raster
    r = site_raster(lat, lon, half_deg=RASTER_HALF_DEG)
    r["fetched_at"] = datetime.datetime.now().timestamp()
    RASTER_DIR.mkdir(parents=True, exist_ok=True)
    (RASTER_DIR / f"{_raster_slug(name)}.json").write_text(json.dumps(r, separators=(",", ":")))
    return r


@st.cache_data(ttl=RASTER_TTL_S, show_spinner="Fetching FortyGuard temperature raster...")
def site_raster_live(name: str, lat: float, lon: float, bucket: int) -> dict | None:
    """
    FortyGuard temperature raster for a site. `bucket` = floor(epoch / 300), so the
    cache entry expires every 5 min and re-fetches. On a fetch failure (the API's
    burst-lockout 401s the heatmap endpoint fairly readily) it falls back to the
    last-good raster on disk; returns None only if there is none.
    """
    if USE_MOCK_DATA:
        return None
    try:
        return fetch_and_cache_raster(name, lat, lon)
    except Exception:  # noqa: BLE001
        try:
            return json.loads((RASTER_DIR / f"{_raster_slug(name)}.json").read_text())
        except (OSError, json.JSONDecodeError):
            return None


# CARTO-style "cool -> warm" temperature ramp (blue -> cyan -> pale yellow -> orange -> red).
_TEMP_STOPS = [
    (0.00, (49, 84, 168)),     # deep blue   (coolest tile in view)
    (0.25, (86, 170, 214)),    # cyan-blue
    (0.50, (247, 236, 176)),   # pale yellow (mid)
    (0.75, (240, 150, 62)),    # orange
    (1.00, (196, 44, 44)),     # deep red    (warmest tile in view)
]


def _ramp(x: float) -> list[int]:
    x = min(1.0, max(0.0, x))
    for (x0, c0), (x1, c1) in zip(_TEMP_STOPS, _TEMP_STOPS[1:]):
        if x <= x1:
            f = 0.0 if x1 == x0 else (x - x0) / (x1 - x0)
            return [round(c0[i] + f * (c1[i] - c0[i])) for i in range(3)]
    return list(_TEMP_STOPS[-1][1])


def heat_overlay(tiles: dict, stats: dict | None = None, opacity: float = 0.78) -> pdk.Layer | None:
    """
    Compact FortyGuard raster (features with properties.t, deg C) -> a GeoJsonLayer
    coloured by the _TEMP_STOPS ramp, normalised to the box's own min..max.
    """
    feats = (tiles or {}).get("features") or []
    vals = [f.get("properties", {}).get("t") for f in feats]
    vals = [t for t in vals if isinstance(t, (int, float))]
    if not vals:
        return None
    lo = (stats or {}).get("min", min(vals))
    hi = (stats or {}).get("max", max(vals))
    span = max(hi - lo, 0.1)
    a = int(255 * opacity)
    scored = {"type": "FeatureCollection", "features": []}
    for f in feats:
        t = f.get("properties", {}).get("t")
        if not isinstance(t, (int, float)):
            continue
        scored["features"].append({
            "type": "Feature", "geometry": f["geometry"],
            "properties": {"_rgba": _ramp((t - lo) / span) + [a]},
        })
    return pdk.Layer("GeoJsonLayer", data=scored, stroked=False, filled=True,
                     get_fill_color="properties._rgba", pickable=False)


def temp_legend_html(lo: float, hi: float) -> str:
    stops = ", ".join(f"rgb{tuple(_ramp(p / 100))} {p}%" for p in range(0, 101, 10))
    return (
        '<div style="display:flex;align-items:center;gap:8px;font-size:0.8em;opacity:0.85">'
        f'<span>{lo:.1f} °C</span>'
        f'<span style="flex:1;height:10px;border-radius:5px;'
        f'background:linear-gradient(to right, {stops})"></span>'
        f'<span>{hi:.1f} °C</span></div>'
    )


@st.cache_data(ttl=3600, show_spinner="Fetching current-conditions snapshot...")
def _live_snapshot(lat: float, lon: float, hour_key: str):
    """Single point-in-time reading, separate from the CUI climate window.
    hour_key busts the cache each hour; the Refresh button clears it explicitly."""
    if USE_MOCK_DATA:
        return {"dry_bulb_c": 31.4, "wet_bulb_c": 25.9, "relative_humidity_pct": 68.0,
                "apparent_temp_c": 37.1, "timestamp": f"{hour_key}:00:00Z (mock)",
                "time_range": {"interval": "1h"}}
    from fortyguard_client import snapshot
    return snapshot(lat, lon)


# ================================ UI ================================

CITATION = "CUI corrosion rate per API RP 581 (2016), §16, Table 16.2."

if USE_MOCK_DATA:
    st.warning("MOCK data - set USE_MOCK_DATA = False (unset CUI_MOCK) for live FortyGuard data.")

sites = load_sites()
assets = load_assets()
options = list(sites["name"])
op_by_name = dict(zip(sites["name"], sites["operator"]))

st.session_state.setdefault("site", None)
st.session_state.setdefault("asset", None)
st.session_state.setdefault("previewed", None)

with st.sidebar:
    st.header("Data")
    force = st.button("Force full refresh (slow: minutes)")
    if force:
        st.cache_data.clear()
    if st.session_state.site is not None and not USE_MOCK_DATA:
        _cache = _load_cache()
        _entry = _cache.get(st.session_state.site) or {}
        if _entry.get("fetched_at"):
            _age_h = (datetime.datetime.now().timestamp() - _entry["fetched_at"]) / 3600
            st.caption(f"{WINDOW_DAYS}-day climate window fetched {_age_h:.1f} h ago.")
            if _age_h * 3600 > STALE_AFTER_SECONDS:
                st.warning("Data is stale. **Force full refresh** or run `python refresh_data.py`.")
    with st.expander("FortyGuard API calls (this session)"):
        if USE_MOCK_DATA:
            st.caption("Mock mode - no API calls.")
        else:
            from fortyguard_client import API_CALL_LOG
            if API_CALL_LOG:
                st.dataframe(pd.DataFrame(list(API_CALL_LOG))[["at", "method", "path", "status", "ms"]],
                             hide_index=True, width="stretch", height=220)
            else:
                st.caption("None yet. Streamlit calls FortyGuard **server-side** (Python "
                           "`requests`), so nothing shows in the browser Network tab. "
                           "Data here is served from `data/site_data_cache.json`; hit "
                           "**Force full refresh** to make live calls appear.")
        st.caption("Also printed to the terminal running `streamlit run` "
                   "(lines prefixed `[fortyguard]`).")

    st.divider()
    st.caption(CITATION + " Climate driver from a FortyGuard 30-day window; the live "
               "snapshot on the refinery screen is separate and not a rate input.")


def table_16_2_row(temp_f: float) -> pd.DataFrame:
    return pd.DataFrame({
        "Driver": DRIVERS,
        "mpy at this temp": [round(cui_corrosion_rate_mpy(temp_f, d), 2) for d in DRIVERS],
    })


def _style_bands(df: pd.DataFrame):
    def _row(r):
        rgb = COLOR_RGB.get(r["_color"], COLOR_RGB["gray"])
        return [f"background-color: rgba({rgb[0]},{rgb[1]},{rgb[2]},0.16)"] * len(r)
    return df.style.apply(_row, axis=1)


# =========================================================================
# LANDING - no risk data, no colouring, no precompute. Map + names only.
# =========================================================================
if st.session_state.site is None:
    st.title("CUI Screening Dashboard")
    st.caption("Corrosion-Under-Insulation screening for refinery insulated piping. "
               + CITATION + "  Select a refinery to begin - nothing is computed until "
               "you open a site.")

    left, right = st.columns([1, 1.7], gap="large")
    with right:
        ev = st.pydeck_chart(
            make_map(sites, highlight=st.session_state.previewed, zoom=3.3),
            on_select="rerun", selection_mode="single-object", key="landingmap")
        try:
            picked = (ev.selection.get("objects") or {}).get("sites") or []
        except AttributeError:
            picked = []
        if picked and picked[0]["name"] != st.session_state.previewed:
            st.session_state.previewed = picked[0]["name"]
            st.rerun()
        st.caption("Plain markers - no risk data shown here. Click a marker, or use the "
                   "list, then open the detailed analysis.")

    with left:
        st.subheader("Refinery")
        idx = options.index(st.session_state.previewed) if st.session_state.previewed in options else 0
        choice = st.selectbox("Refinery", options, index=idx,
                              format_func=lambda n: f"{n} - {op_by_name.get(n, '')}")
        if choice != st.session_state.previewed:
            st.session_state.previewed = choice
            st.rerun()

        prev = sites[sites["name"] == st.session_state.previewed]
        if not prev.empty:
            pr = prev.iloc[0]
            st.markdown(f"**{pr['name']}**")
            st.write(f"{pr['operator']}")
            st.write(f"{pr['city']}, {pr['state']}")
            st.caption(f"{pr['lat']:.4f}, {pr['lon']:.4f}")
        st.write("")
        if st.button("Open detailed analysis  ->", type="primary", width="stretch"):
            st.session_state.site = st.session_state.previewed
            st.session_state.asset = None
            st.session_state.pop("landingmap", None)
            st.rerun()

    st.stop()

# From here on a site IS selected - now we fetch its 30-day window.
name = st.session_state.site
srow = sites[sites["name"] == name]
s = srow.iloc[0]
coastal = bool(s.get("coastal_or_ct_drift", False))

windows, errors = fetch_windows(srow, force=force)
w = windows.get(name)
for e in errors:
    st.warning(e)

if not w:
    st.title(name)
    if st.button("<-  Back to map"):
        st.session_state.site = None
        st.session_state.asset = None
        st.rerun()
    st.error(f"No FortyGuard climate window for {name}. Check the API key, then "
             "**Force full refresh** or run `python refresh_data.py`.")
    st.stop()

driver, why = site_driver(w, coastal)

# =========================================================================
# REFINERY DETAIL - asset table (change #1), site map, live snapshot
# =========================================================================
if st.session_state.asset is None:
    if st.button("<-  Back to map"):
        st.session_state.site = None
        st.session_state.pop("landingmap", None)
        st.rerun()

    st.title(name)
    st.caption(f"{s['operator']}  ·  {s['city']}, {s['state']}  ·  {s['lat']:.3f}, {s['lon']:.3f}")
    st.markdown("## Assets - CUI corrosion-rate screening")

    dcap, dhelp = st.columns([4, 1])
    dcap.markdown(f"Climate window **{w.get('start_date')} to {w.get('end_date')}** "
                  f"({w.get('n_hours', 0)} hourly readings) &nbsp;·&nbsp; Driver: "
                  + _pill(driver, "gray")
                  + f" &nbsp;<span style='font-size:0.85em;opacity:0.8'>({why})</span>",
                  unsafe_allow_html=True)
    with dhelp.popover("Driver ⓘ"):
        st.markdown(
            "**Driver** selects the column of **API RP 581 (2016) §16, Table 16.2** "
            "(the CUI corrosion-rate table). It is derived, not chosen:\n\n"
            f"- **Severe** - trailing-window mean SO₂ index ≥ {SEVERE_SO2_IDX:.0f}\n"
            "- **Marine / Cooling Tower Drift** - the site's coastal / CT-drift flag is set\n"
            f"- **Arid/Dry** - 30-day cumulative precipitation ≤ {ARID_PRECIP_MM_PER_30D:.0f} mm\n"
            "- **Temperate** - otherwise\n\n"
            "The category thresholds are our interpretation - the standard names the "
            "categories but gives no hard numbers.")

    st.info("Assets and typical operating temperatures shown here use published values "
            "for this demo. In production, a refinery would onboard its actual asset "
            "register and IOW data at setup.")

    at_df = asset_table(assets, w, coastal, "Average")
    show = at_df.rename(columns={
        "asset": "Asset", "material": "Material", "temp_f": "Operating temp",
        "rate_mpy": "CUI rate (mpy)", "band": "Band", "color": "_color",
    })[["Asset", "Material", "Operating temp", "CUI rate (mpy)", "Band", "_color"]]

    ev = st.dataframe(
        _style_bands(show), hide_index=True, width="stretch",
        on_select="rerun", selection_mode="single-row",
        column_config={
            "Operating temp": st.column_config.NumberColumn(format="%d °F"),
            "CUI rate (mpy)": st.column_config.NumberColumn(format="%.1f"),
            "_color": None,
        },
        key="assettable",
    )
    try:
        sel_rows = ev.selection.get("rows") or []
    except AttributeError:
        sel_rows = []
    if sel_rows:
        st.session_state.asset = at_df.iloc[sel_rows[0]]["asset"]
        st.session_state.pop("assettable", None)
        st.rerun()
    st.caption("Sorted highest-rate first. Click a row for the full Table 16.2 trace. "
               "Insulation condition = Average here; adjust it per asset in the detail view.")

    oc1, oc2 = st.columns([2, 1])
    pick = oc1.selectbox("Open an asset", list(at_df["asset"]), key="assetpick",
                         label_visibility="collapsed")
    if oc2.button("Open asset  ->", width="stretch"):
        st.session_state.asset = pick
        st.session_state.pop("assettable", None)
        st.rerun()

    furnace_notes = at_df[at_df["note"].astype(bool)]
    for _, fr in furnace_notes.iterrows():
        st.caption(f"**{fr['asset']}** ({fr['temp_f']:.0f} °F): {fr['note']}")

    # ---- site map + live FortyGuard temperature raster (auto-refreshes every 5 min) ----
    mcol, scol = st.columns([1.4, 1], gap="large")
    with mcol:
        st.markdown("#### Site - FortyGuard temperature raster")
        bucket = int(datetime.datetime.now().timestamp() // RASTER_TTL_S)
        raster = site_raster_live(name, float(s["lat"]), float(s["lon"]), bucket)
        overlays, raster_on = [], False
        if raster and raster.get("tiles", {}).get("features"):
            lyr = heat_overlay(raster["tiles"], raster.get("stats"))
            if lyr is not None:
                overlays.append(lyr)
                raster_on = True
        st.pydeck_chart(make_map(
            srow, highlight=name, center=(float(s["lat"]), float(s["lon"])),
            zoom=13 if raster_on else 10, overlays=overlays,
            basemap=None if raster_on else "light",   # the raster IS the surface
        ))
        if raster_on:
            stt = raster.get("stats", {})
            age_min = (datetime.datetime.now().timestamp() - raster.get("fetched_at", 0)) / 60
            freshness = ("live" if age_min < RASTER_TTL_S / 60 + 1
                         else f"last good, {age_min:.0f} min old - live fetch is rate-limited")
            st.markdown(temp_legend_html(stt.get("min", 0), stt.get("max", 0)),
                        unsafe_allow_html=True)
            st.caption(f"`/v1/heatmap` tcm ({freshness}) - {len(raster['tiles']['features'])} "
                       f"~100 m tiles for {raster.get('for', '?')}, spanning only "
                       f"{stt.get('max', 0) - stt.get('min', 0):.1f} °C across the box "
                       "(colours are stretched to that range). Shows *where* it runs warmer / "
                       "cooler - hyperlocal temperature, not a corrosion-rate input. "
                       f"Re-fetched every {RASTER_TTL_S // 60} min.")
        else:
            st.caption("Temperature raster unavailable (FortyGuard rate-limit or busy, and no "
                       "cached copy yet). Plain basemap shown; CUI screening is unaffected. "
                       "Retries every 5 min.")
    with scol:
        st.markdown("#### Current conditions (live snapshot)")
        hour_key = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H")
        _snap_key = f"snap::{name}::{hour_key}"
        if st.button("Load / refresh snapshot"):
            _live_snapshot.clear()
            st.session_state[_snap_key] = True
            st.rerun()
        if not st.session_state.get(_snap_key):
            st.caption("A single point-in-time reading (dry/wet-bulb, RH), separate from "
                       "the 30-day window and *not* a rate input. One FortyGuard job, "
                       "~1-3 min - click to load.")
        else:
            try:
                snap = _live_snapshot(float(s["lat"]), float(s["lon"]), hour_key)
            except Exception as e:  # noqa: BLE001
                snap = None
                st.warning(f"Snapshot unavailable: {e}")
            if snap:
                tr = snap.get("time_range") or {}
                st.caption(f"Reading for **{snap.get('timestamp') or tr.get('start') or 'n/a'}** "
                           f"(interval {tr.get('interval', '1h')}). FortyGuard reports in °C.")
                db, wb = snap.get("dry_bulb_c"), snap.get("wet_bulb_c")
                rh, ap = snap.get("relative_humidity_pct"), snap.get("apparent_temp_c")
                qa, qb = st.columns(2)
                qa.metric("Dry-bulb", f"{db:.1f} °C" if db is not None else "n/a")
                qb.metric("Wet-bulb", f"{wb:.1f} °C" if wb is not None else "n/a")
                qa.metric("Humidity", f"{rh:.0f} %" if rh is not None else "n/a")
                qb.metric("Apparent", f"{ap:.1f} °C" if ap is not None else "n/a")

# =========================================================================
# ASSET DETAIL - the single-circuit UI, scoped to one asset
# =========================================================================
else:
    asset_name = st.session_state.asset
    arow = assets[assets["asset"] == asset_name]
    if arow.empty:
        st.session_state.asset = None
        st.rerun()
    ar = arow.iloc[0]
    default_f = float(ar["default_temp_f"])   # asset register is in Fahrenheit

    if st.button(f"<-  Back to {name} assets"):
        st.session_state.asset = None
        st.rerun()

    st.title(f"{asset_name}")
    st.caption(f"{name}  ·  {s['operator']}, {s['city']} {s['state']}  ·  {ar['material']}")

    if not bool(ar["compute"]):
        st.warning(f"**{asset_name}** ({default_f:.0f} °F): {ar['note']} "
                   "No live corrosion rate is computed for this asset.")
        st.stop()

    st.markdown("## CUI corrosion-rate screening")
    st.caption(f"Climate window **{w.get('start_date')} to {w.get('end_date')}** "
               f"({w.get('n_hours', 0)} hourly readings, FortyGuard).")

    ctl1, ctl2 = st.columns([1.5, 1])
    with ctl1:
        # Slider is in Fahrenheit - Table 16.2's unit and the asset register's unit,
        # so the value flows straight into the lookup with no conversion.
        op_f = st.slider("Asset operating temperature (°F)", 0.0, 500.0, default_f, 5.0,
                         help=f"Default = published typical value for {asset_name} "
                              f"({default_f:.0f} °F = {f_to_c(default_f):.0f} °C). API RP 584 "
                              "stand-in, editable per asset. CUI-susceptible band "
                              f"{CUI_SUSCEPTIBLE_MIN_F:.0f}–{CUI_SUSCEPTIBLE_MAX_F:.0f} °F "
                              f"({CUI_SUSCEPTIBLE_MIN_C:.0f}–{CUI_SUSCEPTIBLE_MAX_C:.0f} °C).")
        st.caption(f"= {f_to_c(op_f):.1f} °C")
    with ctl2:
        insul = st.radio("Insulation condition", list(INSULATION_CONDITION_MULT), index=1,
                         help="API RP 581 Table 16.1 categories, used as a rate multiplier "
                              f"{INSULATION_CONDITION_MULT}. (Interpretation - see risk.py.)")

    dh1, dh2 = st.columns([4, 1])
    dh1.markdown("**Driver** &nbsp; " + _pill(driver, "gray")
                 + f" &nbsp;<span style='font-size:0.85em;opacity:0.8'>({why})</span>",
                 unsafe_allow_html=True)
    with dh2.popover("Driver ⓘ"):
        st.markdown(
            "Selects the column of **API RP 581 (2016) §16, Table 16.2**. Derived, "
            "not chosen:\n\n"
            f"- **Severe** - mean SO₂ index ≥ {SEVERE_SO2_IDX:.0f}\n"
            "- **Marine / Cooling Tower Drift** - site coastal / CT-drift flag set\n"
            f"- **Arid/Dry** - 30-day precip ≤ {ARID_PRECIP_MM_PER_30D:.0f} mm\n"
            "- **Temperate** - otherwise")

    # op_f is already Fahrenheit; cui_assessment takes Fahrenheit -> no conversion here.
    a = cui_assessment(op_f, driver, insul)

    if not a["in_range"]:
        st.info(f"Operating temperature **{op_f:.0f} °F ({a['operating_temp_c']:.0f} °C)** is "
                f"outside the CUI-susceptible band "
                f"({CUI_SUSCEPTIBLE_MIN_F:.0f}–{CUI_SUSCEPTIBLE_MAX_F:.0f} °F / "
                f"{CUI_SUSCEPTIBLE_MIN_C:.0f}–{CUI_SUSCEPTIBLE_MAX_C:.0f} °C, per API 571 / "
                "API RP 581 §16). The mechanism is atypical here - no steady-state rate.")
    else:
        st.markdown(f"### Estimated CUI corrosion rate &nbsp; {a['rate_mpy']:.1f} mpy &nbsp;&nbsp;"
                    + risk_pill(a["label"], a["color"]), unsafe_allow_html=True)
        st.progress(min(a["rate_mpy"] / RATE_AXIS_MAX, 1.0))
        st.caption(f"Table 16.2 lookup: **{a['table_rate_mpy']:.1f} mpy** at "
                   f"{a['operating_temp_f']:.0f} °F / {driver} (interpolated between rows)  x  "
                   f"{a['insulation_multiplier']} ({insul} insulation)  =  {a['rate_mpy']:.1f} mpy.")

    p1, p2 = st.columns(2)
    with p1:
        st.markdown("#### Table 16.2 - this asset")
        st.metric(f"{driver} @ {a['operating_temp_f']:.0f} °F", f"{a['table_rate_mpy']:.1f} mpy")
        st.caption("Interpolated between the bracketing temperature rows. Rate peaks near "
                   "160 °F (~71 °C) and tapers above ~225 °F as water flashes off.")
        with st.expander("See rates under other conditions"):
            others = table_16_2_row(op_f)   # op_f is already Fahrenheit
            others = others[others["Driver"] != driver].reset_index(drop=True)
            st.dataframe(others, hide_index=True, width="stretch",
                         column_config={"mpy at this temp": st.column_config.NumberColumn(format="%.2f")})
            st.caption(f"Rates at {op_f:.0f} °F. Reference only - the driver is fixed by the "
                       "data, not selectable.")
    with p2:
        st.markdown("#### Climate driver inputs (30-day window)")
        d1, d2, d3 = st.columns(3)
        d1.metric("30d precip", f"{w.get('precip_mm_total', 0):.0f} mm")
        d2.metric("Mean RH", f"{w.get('avg_rh_pct', 0):.0f} %")
        d3.metric("Mean SO2 idx",
                  f"{w.get('avg_so2_idx') if w.get('avg_so2_idx') is not None else 'n/a'}")
        st.caption(f"Coastal / cooling-tower drift flag: **{'yes' if coastal else 'no'}** (set per site).")
        ft = w.get("freeze_thaw_count", 0)
        st.caption(f"Freeze-thaw crossings in window: **{ft}** "
                   f"(ambient {w.get('min_dry_bulb_c', '?')}–{w.get('max_dry_bulb_c', '?')} °C, "
                   "FortyGuard). Not a Table 16.2 input - an aggravating-factor flag.")

st.divider()
st.caption(
    CITATION + " Rate = Table 16.2 (operating temperature × climate driver, linear "
    "interpolation between temperature rows) × an API RP 581 Table 16.1 insulation-"
    "condition multiplier (interpretation). Table 16.2 and every temperature in the "
    "lookup are in **°F**; the only °C is FortyGuard's climate data, which feeds the "
    "driver classification only. The CUI-susceptible band "
    f"({CUI_SUSCEPTIBLE_MIN_F:.0f}–{CUI_SUSCEPTIBLE_MAX_F:.0f} °F / "
    f"{CUI_SUSCEPTIBLE_MIN_C:.0f}–{CUI_SUSCEPTIBLE_MAX_C:.0f} °C, API 571 / API RP 581 §16) "
    "gates the lookup. Driver thresholds are our interpretation. Assets and operating "
    "temperatures are published typical values (API RP 584 stand-in). Screening priority "
    "only, not an inspection plan."
)
