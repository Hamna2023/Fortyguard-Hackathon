"""
Corrosion Under Insulation (CUI) screening-rate model.

A screening-priority tool implementing API RP 581 (2016), Part 2, Section 16.
The estimated corrosion rate comes straight from **Table 16.2** (rate in mils
per year vs. operating temperature and climate/exposure "driver"), with linear
interpolation between temperature rows - which the standard permits.

Before returning a rate we gate on the material temperature band from API 571 /
API RP 583 (carbon and low-alloy steel is CUI-susceptible from about -12 C to
175 C); outside that band the mechanism is atypical and no rate is reported.

An optional insulation-condition multiplier (API RP 581 Table 16.1 categories:
Above Average / Average / Below Average) scales the rate. NOTE: the exact
Table 16.1 adjustment factors are not encoded here - the multipliers below are a
documented, conservative interpretation; replace them if you have the table.

Operating temperatures are published typical values (an API RP 584 stand-in for
proprietary per-asset IOW data).

UNITS: Table 16.2 and every function here that touches operating temperature use
**Fahrenheit** (the standard's unit, and the unit of the asset register). The
only Celsius in this app is FortyGuard's live env_params data, which feeds
classify_driver() (precipitation, SO2) - never the temperature lookup. c_to_f /
f_to_c are provided for display conversions.
"""

# ---------------------------------------------------------------------------
# API RP 581 (2016) Part 2, Section 16, Table 16.2
# Estimated CUI corrosion rate (mpy) for ferritic steel.
# ---------------------------------------------------------------------------

TABLE_16_2_TEMP_F = [10, 18, 43, 90, 160, 225, 275, 325, 350]

TABLE_16_2_MPY = {
    "Marine/Cooling Tower Drift": [0, 1, 5, 5, 10, 5, 2, 1, 0],
    "Temperate":                  [0, 0, 3, 3,  5, 1, 1, 0, 0],
    "Arid/Dry":                   [0, 0, 1, 1,  2, 1, 0, 0, 0],
    "Severe":                     [0, 3, 10, 10, 20, 10, 10, 5, 0],
}
DRIVERS = list(TABLE_16_2_MPY)

# CUI-susceptible temperature band for carbon / low-alloy steel. Native unit is
# Fahrenheit here because Table 16.2 (below) is defined in F. API 571 / API RP
# 583 cite ~ -12 C to 175 C; we use the API RP 581 Table 16.2 domain (10 F to
# 350 F), which agrees within a few degrees (175 C = 347 F). Above ~350 F water
# flashes off and steady-state CUI is negligible (Table 16.2 rows go to 0).
CUI_SUSCEPTIBLE_MIN_F = 10.0
CUI_SUSCEPTIBLE_MAX_F = 350.0
CUI_SUSCEPTIBLE_MIN_C = (CUI_SUSCEPTIBLE_MIN_F - 32.0) * 5.0 / 9.0    # -12.2 C
CUI_SUSCEPTIBLE_MAX_C = (CUI_SUSCEPTIBLE_MAX_F - 32.0) * 5.0 / 9.0    # 176.7 C

# ---- driver classification (our documented interpretation) ----
# API RP 581 keys the driver to marine exposure + annual rainfall but gives no
# hard numeric thresholds for the category itself.
SEVERE_SO2_IDX = 25.0        # trailing-window mean air_quality_so2:idx above this -> Severe
ARID_PRECIP_MM_PER_30D = 40.0  # <= this over 30 days -> Arid/Dry, else Temperate

# ---- API RP 581 Table 16.1 insulation condition (interpretation - see docstring) ----
INSULATION_CONDITION_MULT = {
    "Above Average": 0.75,
    "Average": 1.0,
    "Below Average": 1.5,
}

# ---- corrosion-rate bands for the dashboard ----
RATE_BANDS_MPY = [
    (1.0, "low", "green"),
    (3.0, "moderate", "yellow"),
    (10.0, "high", "orange"),
    (float("inf"), "severe", "red"),
]


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def _band(value: float, bands: list) -> tuple[str, str]:
    for threshold, label, color in bands:
        if value <= threshold:
            return label, color
    return bands[-1][1], bands[-1][2]


def in_cui_range_f(operating_temp_f: float) -> bool:
    """Material temperature-band gate. Input is Fahrenheit (Table 16.2's unit)."""
    return CUI_SUSCEPTIBLE_MIN_F <= operating_temp_f <= CUI_SUSCEPTIBLE_MAX_F


def cui_corrosion_rate_mpy(operating_temp_f: float, driver: str) -> float:
    """
    Table 16.2 lookup with linear interpolation between temperature rows.
    INPUT IS FAHRENHEIT - Table 16.2 (TABLE_16_2_TEMP_F) is defined in F.
    Temperatures outside 10-350 F clamp to the end rows (both 0 mpy).
    """
    if driver not in TABLE_16_2_MPY:
        raise ValueError(f"unknown driver {driver!r}; expected one of {DRIVERS}")
    col = TABLE_16_2_MPY[driver]
    temps = TABLE_16_2_TEMP_F

    if operating_temp_f <= temps[0]:
        return float(col[0])
    if operating_temp_f >= temps[-1]:
        return float(col[-1])
    for i in range(len(temps) - 1):
        lo, hi = temps[i], temps[i + 1]
        if lo <= operating_temp_f <= hi:
            frac = (operating_temp_f - lo) / (hi - lo)
            return round(col[i] + frac * (col[i + 1] - col[i]), 2)
    return float(col[-1])  # unreachable


def classify_driver(coastal_or_ct_drift: bool,
                    avg_so2_idx: float | None,
                    precip_mm_30d: float | None) -> tuple[str, str]:
    """
    Returns (driver, one-line reason). Our interpretation of the API RP 581
    category, since the standard gives no hard numeric thresholds:
      Severe   if the trailing-window mean SO2 index is elevated
      Marine   if the asset is coastal / in cooling-tower drift
      Arid/Dry if trailing-30-day precip is very low
      Temperate otherwise
    """
    if avg_so2_idx is not None and avg_so2_idx >= SEVERE_SO2_IDX:
        return "Severe", f"mean SO2 index {avg_so2_idx:.0f} >= {SEVERE_SO2_IDX:.0f}"
    if coastal_or_ct_drift:
        return "Marine/Cooling Tower Drift", "coastal / cooling-tower drift exposure (set per site)"
    if precip_mm_30d is not None and precip_mm_30d <= ARID_PRECIP_MM_PER_30D:
        return "Arid/Dry", f"30-day precip {precip_mm_30d:.0f} mm <= {ARID_PRECIP_MM_PER_30D:.0f} mm"
    p = f"{precip_mm_30d:.0f} mm" if precip_mm_30d is not None else "n/a"
    return "Temperate", f"inland, 30-day precip {p}"


def cui_assessment(operating_temp_f: float, driver: str,
                   insulation_condition: str = "Average") -> dict:
    """
    Full screening result. INPUT IS FAHRENHEIT - Table 16.2's native unit, and
    the unit our asset register / operator inputs use. FortyGuard's Celsius data
    never reaches this function; it only feeds classify_driver() (precip, SO2).
    If the temperature is outside the CUI-susceptible band, no rate is returned.
    """
    temp_c = f_to_c(operating_temp_f)          # for display only
    mult = INSULATION_CONDITION_MULT.get(insulation_condition, 1.0)

    if not in_cui_range_f(operating_temp_f):
        return {
            "in_range": False, "driver": driver,
            "operating_temp_f": round(operating_temp_f, 0),
            "operating_temp_c": round(temp_c, 0),
            "table_rate_mpy": 0.0, "insulation_condition": insulation_condition,
            "insulation_multiplier": mult,
            "rate_mpy": 0.0, "label": "not susceptible", "color": "gray",
        }

    # operating_temp_f is already Fahrenheit -> pass straight to the F-keyed table.
    table_rate = cui_corrosion_rate_mpy(operating_temp_f, driver)
    rate = round(table_rate * mult, 2)
    label, color = _band(rate, RATE_BANDS_MPY)
    return {
        "in_range": True, "driver": driver,
        "operating_temp_f": round(operating_temp_f, 0),
        "operating_temp_c": round(temp_c, 0),
        "table_rate_mpy": table_rate,
        "insulation_condition": insulation_condition,
        "insulation_multiplier": mult,
        "rate_mpy": rate,
        "label": label, "color": color,
    }
