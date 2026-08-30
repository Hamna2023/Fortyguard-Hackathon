# CUI Screening Dashboard

A screening-priority tool for **Corrosion Under Insulation (CUI)** on refinery
insulated piping. It estimates a corrosion rate straight from **API RP 581
(2016), Part 2, Section 16, Table 16.2**, using published typical operating
temperatures in place of proprietary per-asset IOW data (**API RP 584**), and
classifies the Table 16.2 climate "driver" from FortyGuard data.

It does not produce an inspection plan - it *ranks* circuits so a limited
inspection budget goes to the worst actors first.

## The model (`risk.py`)

```
if not (10 F <= operating_temp_F <= 350 F):   # CUI-susceptible band (API 571 / RP 581 §16)
    -> "not susceptible", no rate
else:
    rate_mpy = TABLE_16_2[driver] interpolated at operating_temp_F
    rate_mpy *= insulation_condition_multiplier          # API RP 581 Table 16.1
```

### Units

Table 16.2 is defined in **Fahrenheit**, and so is every temperature in
`risk.py` and every operating-temperature input in the UI (asset register,
sliders, the arithmetic trace). `cui_assessment(operating_temp_f, ...)` takes
Fahrenheit and passes it straight to the F-keyed table - **no conversion in the
lookup path**. The only Celsius in the app is FortyGuard's live climate data
(`dry_bulb_c`, `precipitation_mm`, `air_quality_so2:idx`), which feeds
`classify_driver()` only and never the rate lookup. Every displayed temperature
carries an explicit `°F` or `°C`.

### Table 16.2 (hard-coded in `risk.py`, mpy)

| Temp F | Marine / Cooling Tower Drift | Temperate | Arid/Dry | Severe |
|---:|---:|---:|---:|---:|
| 10  | 0 | 0 | 0 | 0 |
| 18  | 1 | 0 | 0 | 3 |
| 43  | 5 | 3 | 1 | 10 |
| 90  | 5 | 3 | 1 | 10 |
| 160 | 10 | 5 | 2 | 20 |
| 225 | 5 | 1 | 1 | 10 |
| 275 | 2 | 1 | 0 | 10 |
| 325 | 1 | 0 | 0 | 5 |
| 350 | 0 | 0 | 0 | 0 |

Linear interpolation between temperature rows (the standard permits this). Rate
peaks near 160 F (~71 C) and tapers above ~225 F - the classic CUI envelope.

### Driver classification (our interpretation - the standard gives no numeric thresholds)

- **Severe** if trailing-window mean `air_quality_so2:idx` >= `SEVERE_SO2_IDX` (25)
- **Marine / Cooling Tower Drift** if the site's `coastal_or_ct_drift` flag is set
- **Arid/Dry** if trailing-30-day cumulative precipitation <= `ARID_PRECIP_MM_PER_30D` (40 mm)
- **Temperate** otherwise

The driver is also overridable per site on the detail screen.

### Insulation condition multiplier (API RP 581 Table 16.1 categories)

`Above Average 0.75 / Average 1.0 / Below Average 1.5`. **NOTE:** the exact
Table 16.1 adjustment factors are not encoded - these are a documented
conservative interpretation; replace `INSULATION_CONDITION_MULT` if you have the
table.

### Rate bands (dashboard colours)

`<= 1` low (green) · `<= 3` moderate (yellow) · `<= 10` high (orange) · `> 10` severe (red) mpy

## Two data needs, kept separate

| | Live snapshot | CUI climate window |
|---|---|---|
| Request | `env_params` `filter_type=1`, most recent complete hour | `env_params` `filter_type=4`, trailing 30 days (one job, 744 hourly readings) |
| Used for | the "Current conditions" panel on the refinery screen (dry/wet-bulb, RH, apparent temp) | cumulative precip + mean SO2 -> Table 16.2 driver; also mean RH and freeze-thaw crossings as context |
| Refresh | on demand ("Load / refresh snapshot" button), cached 1 h | explicit only (sidebar button / `refresh_data.py`), cached to disk, served even when stale |
| Recency shown | the reading's ISO timestamp from `metadata.time_range` | "Climate window: <start> to <end>" from `metadata.time_range` |

The snapshot is **not** an input to the corrosion rate. Neither request fires
until a refinery is opened - the landing page makes no API calls.

**Seeing the calls:** Streamlit runs Python on the server, so FortyGuard calls
(`requests` from the backend) never appear in the browser's Network tab. They
show up in (a) the terminal running `streamlit run`, prefixed `[fortyguard]`,
and (b) the sidebar **"FortyGuard API calls (this session)"** expander. An empty
list means everything was served from `data/site_data_cache.json` - use
**Force full refresh** to make live calls.

## UI - three levels, nothing computed before an explicit choice

1. **Landing** - a US map with **plain slate markers** (no risk colour) and a
   refinery list. Click a marker or pick from the list to see just its
   name / operator / location in an info panel. **No CUI data is fetched or
   shown** until you click **"Open detailed analysis"**.
2. **Refinery detail** - the site's 30-day FortyGuard window is fetched here.
   A **top-level asset table**: one row per insulated circuit
   (Crude Tower Overhead, Desalter, Vacuum Tower Overhead, Atmospheric Side Draw,
   Furnace Transfer Line, Ambient Storage Tank), showing asset / material /
   operating temp / computed CUI rate (mpy) / band, **row-tinted by band and
   sorted worst-first**. The furnace transfer line (~700 F) is flagged
   "above the CUI window at steady state; elevated risk during cycling / outages"
   and gets **no computed rate**. A one-line note says the assets and
   temperatures are published demo values, not a real asset register. Below:
   the site map and the opt-in live current-conditions snapshot (separate job,
   own timestamp, not a rate input).
3. **Asset detail** (click a row, or the "Open asset" picker) - the single-
   circuit view scoped to that asset. **User-editable** (real domain judgement):
   an operating-temperature slider **in °F** (default = the asset's published
   value, shown with its °C equivalent) and an insulation-condition selector.
   **Derived, read-only:** the driver as a badge with a caption naming the rule
   that fired (coastal flag / SO2 / precip); the API RP 581 Table 16.2 citation
   sits in a **"Driver ⓘ" popover**, not in the label. Then the estimated rate +
   band, the `Table 16.2 x multiplier` arithmetic trace, the single Table 16.2 value for
   this driver (the other three behind a "See rates under other conditions"
   expander), and the climate-driver-inputs panel.

### One map component

`app.py`'s `make_map(markers, *, highlight, center, zoom, overlays, basemap)` is
the single map used by every screen - landing calls it with all 10 sites at a
wide zoom (CARTO positron basemap; a US-wide basemap is not something FortyGuard
provides), the refinery screen with the one selected site at a tight zoom.

On the refinery screen the map shows the site's **live FortyGuard temperature
raster** as the surface. `site_raster_live()` submits one `POST /v1/heatmap`
`tcm` job (~1.5 mi box, ~100 m °C tiles), compacts the result (geometry +
1-dp temp, coords to 5 dp) and is `@st.cache_data(ttl=300)` keyed by a
`floor(epoch/300)` bucket - so it **re-fetches every 5 minutes**. `make_map` is
called with `basemap=None` so the raster *is* the map (markers on top); if the
fetch fails it falls back to CARTO with a note. Colours use a clean
blue -> cyan -> pale-yellow -> orange -> red ramp normalised to the box's own
min..max (which is often < 1 °C - the caption says so). Hyperlocal temperature
only - **not a corrosion-rate input.**

## FortyGuard API, as confirmed by hitting it

- Base URL `https://api.fortyguard.com`, header `api-key`.
- **Submit:** `POST /v1/env_params` with
  `{latitude, longitude, temperature, date_time:{start_date, filter_type, ...}}`.
  `temperature` (deg C) is **required**; the API only uses it to compute
  `heat_index_celsius`, everything else is its own model output.
- **`filter_type`** (diagnosed live - `scratch_env_params_probe.py`): only
  `1, 2, 3, 4` are accepted (`0` -> 422). It changes the *range*, never the
  granularity - **`metadata.time_range.interval` is always `1h`**:
  - `1` one hour (`start_date` + `start_time`)
  - `2` hours within one day (`start_time` + `end_time`; needs both, else 500)
  - `3` one full day, 24 hourly readings
  - `4` a multi-day range (`start_date` + `end_date`, <= 1 month)
  A 30-day `filter_type=4` request returns **744 non-null hourly** `precipitation_mm`
  / `relative_humidity_percent` / `wet_bulb_temperature_celsius` / `air_quality_so2:idx`
  in **one job**. History goes back to 2019-01-01.
- **Poll / result:** `GET /v1/status/{activity_id}` - one unified endpoint for
  every async job. `Processing` -> `Completed` (`result`) / `Failed`. Jobs are
  queue-slow: 30 s to many minutes.
- **No dry-bulb field.** Dry-bulb is *derived* by inverting Stull's wet-bulb
  approximation from the model's wet-bulb + RH (`derive_dry_bulb_c`); the
  freeze-thaw count runs on that derived hourly series.
- The API rejects **bursts** with a misleading `401 "Invalid or unknown API
  key"` and stays locked ~60-90 s. `_request()` throttles every call (1.2 s)
  and, on 401, waits a fixed cooldown before retrying.
- **`POST /v1/heatmap`** (optional raster): GeoJSON `polygon_aoi` +
  `granularity` (60/80/100 m tiles). AOI cap ~10 mi^2 Basic / ~50 mi^2 Premium.
  `analytic_type` `tcm` (deg C snapshot) / `time_of_measure` / `exceedance` /
  `persistence`. Result -> `map_data` (GeoJSON, tile prop `average_temperature`
  for tcm) + `stats_data`. Opacity is applied client-side in the pydeck layer.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env             # then edit .env: FORTYGUARD_API_KEY=your_key  (no quotes)
python refresh_data.py           # pre-warm the 30-day climate windows (queue-slow, ~10 min)
streamlit run app.py
```

`app.py` loads the key from `.env` automatically. The 30-day windows are served
from `data/site_data_cache.json`; the per-site temperature raster is fetched
**live and re-fetched every 5 min**. Set `CUI_MOCK=1` to run the UI on synthetic
data with no API calls.

## Deploy (Streamlit Community Cloud)

Push to a GitHub repo, "New app" at <https://share.streamlit.io> pointing at
`app.py`, add `FORTYGUARD_API_KEY = "your_key"` in the app's Secrets. Commit the
warm `data/site_data_cache.json` so the first page load is instant (the raster
loads live on the refinery screen).

## Data

`data/refineries.csv` - 10 US refineries: `name, operator, city, state, lat, lon,
coastal_or_ct_drift`. Coordinates are the **plant locations** (Wikipedia / Global
Energy Monitor), not city centres. `coastal_or_ct_drift` (`true`/`false`) drives
the Marine Table 16.2 category - set per site (Gulf Coast + SF Bay + LAX).

`data/assets.csv` - the shared asset template applied to every refinery:
`asset, default_temp_f, material, compute, note`. `compute=false` (furnace
transfer line) shows the flag instead of a rate. **Demo values** - in production
a refinery onboards its real asset register and IOW data at setup.

## Project structure

```
app.py                        Streamlit dashboard: landing -> refinery (asset table) -> asset detail
risk.py                       CUI model: Table 16.2 lookup + interp, driver classifier,
                              CUI-susceptible-band gate, insulation multiplier, rate bands
fortyguard_client.py          FortyGuard client: climate_window (30-day series),
                              snapshot (single reading), heatmap, throttle+retry
refresh_data.py               CLI: pre-warm the 30-day climate windows before a demo
scratch_env_params_probe.py   one-off diagnostic of env_params filter_type behaviour
data/refineries.csv           10 US refinery sites: plant coords, coastal flag
data/assets.csv               6-asset template applied to every site
data/site_data_cache.json     pre-warmed 30-day climate windows (committed)
```

## Pitch line

> A screening-priority tool implementing **API RP 581 (2016) §16, Table 16.2**'s
> CUI corrosion-rate table, using published typical operating temperatures in
> place of proprietary per-asset IOW data (API RP 584).
