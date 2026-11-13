"""County multi-hazard risk for Kentucky critical infrastructure.

Hazards come from NASA POWER daily grids (MERRA-2 based, 0.625 x 0.5 deg), 2001-2024, using the same
approach as the DSOC Kenya prototype (including its rainfall QC), with Kentucky thresholds and a cold/ice
hazard in place of drought:

  wind   median annual max of daily max 10 m wind  +  mean days/yr with WS10M_MAX >= WIND_T
  rain   median annual max 1-day rainfall           +  mean days/yr with PRECTOTCORR >= RAIN_T  (flood proxy)
  heat   median annual max Tmax                     +  mean days/yr with T2M_MAX >= HEAT_T
  ice    mean days/yr with wintry precipitation: PRECTOTCORR >= ICE_P mm, T2M_MIN <= 0 C and T2M_MAX <= ICE_TX
         (freezing rain / sleet / wet snow load proxy)  +  mean days/yr with T2M_MIN <= COLD_T

The grid is bilinearly interpolated to points every 0.02 deg inside each county and averaged, so small
counties without a cell centre still get a value. Each metric is ranked across the 120 counties (0-1) and
each hazard index is the mean of its two ranks.

Exposure = weighted count of critical assets per county (OSM facilities from this project, power plants and
customers from the KGFM county table, line km from build_lines.py), ranked across counties.
The dashboard combines Risk = sqrt(Hazard x Exposure), with hazard weights set by the user.

Inputs: raw/power/*.json, raw/counties.geojson, docs/data/counties.geojson, docs/data/facilities.geojson,
        ../kentucky-grid-fm/data/processed/county_static.parquet (read only; optional)
Output: docs/data/risk.json
"""
import datetime as dt
import json
import pathlib

import numpy as np
from shapely.geometry import shape
from shapely import contains_xy

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW, OUT = ROOT / "raw", ROOT / "docs" / "data"
KGFM = ROOT.parent / "kentucky-grid-fm" / "data" / "processed" / "county_static.parquet"
YEARS = list(range(2001, 2025))
MISSING = -900

WIND_T = 12.0      # m/s daily max 10 m wind (~99th percentile of Kentucky cell-days)
RAIN_T = 50.0      # mm/day (~2 in; ~99.9th percentile of cell-days; MERRA-2 cells smooth local cloudbursts)
HEAT_T = 35.0      # deg C (95 F)
ICE_P, ICE_TX = 2.5, 4.0   # mm/day and deg C for a wintry-precipitation day (with Tmin <= 0 C)
COLD_T = -15.0     # deg C (5 F)
RAIN_QC_MAX = 200.0  # mm/day; larger cell-mean values are treated as PRECTOTCORR artefacts (Kenya QC rule)
STEP = 0.02        # deg, county sampling

# Exposure weights per asset (documented in the dashboard)
EXPOSURE_W = {"power_plant": 5, "fuel_terminal": 4, "substation": 3, "hospital": 3, "water_plant": 2,
              "gas_station": 0.5}
HV_KM_PER_UNIT = 50      # 1 unit per 50 km of >= 100 kV line
KV69_KM_PER_UNIT = 100   # 1 unit per 100 km of 69 kV line
CUST_PER_UNIT = 10000    # 1 unit per 10,000 electricity customers


def load(param, qc_max=None):
    """-> lons, lats, array [year][cell][day] (ragged by leap year: list of arrays)"""
    out, dropped, cells = [], 0, None
    for y in YEARS:
        d = json.loads((RAW / "power" / f"{param}_{y}.json").read_text())
        if cells is None:
            cells = [(c["lon"], c["lat"]) for c in d]
        a = np.array([c["v"] for c in d], float)
        a[a <= MISSING] = np.nan
        if qc_max is not None:
            dropped += int((a > qc_max).sum())
            a[a > qc_max] = np.nan
        out.append(a)
    if qc_max is not None:
        print(f"{param}: {dropped} daily values > {qc_max} mm set missing")
    return cells, out


def pct_rank(v):
    v = np.asarray(v, float)
    s = np.sort(v)
    lo = np.searchsorted(s, v, "left")
    hi = np.searchsorted(s, v, "right")
    return (lo + 0.5 * (hi - lo)) / len(v)


def main():
    cells, W = load("WS10M_MAX")
    _, R = load("PRECTOTCORR", RAIN_QC_MAX)
    _, TX = load("T2M_MAX")
    _, TN = load("T2M_MIN")
    lons = sorted({c[0] for c in cells})
    lats = sorted({c[1] for c in cells})
    idx = {c: i for i, c in enumerate(cells)}

    # per-cell yearly series [cell, year]
    yr = lambda f: np.stack([f(i) for i in range(len(YEARS))], 1)
    S = {
        "wind_annmax": yr(lambda i: np.nanmax(W[i], 1)),
        "wind_days": yr(lambda i: (W[i] >= WIND_T).sum(1)),
        "rain_annmax": yr(lambda i: np.nanmax(R[i], 1)),
        "rain_days": yr(lambda i: (R[i] >= RAIN_T).sum(1)),
        "tmax_annmax": yr(lambda i: np.nanmax(TX[i], 1)),
        "heat_days": yr(lambda i: (TX[i] >= HEAT_T).sum(1)),
        "ice_days": yr(lambda i: ((R[i] >= ICE_P) & (TN[i] <= 0) & (TX[i] <= ICE_TX)).sum(1)),
        "cold_days": yr(lambda i: (TN[i] <= COLD_T).sum(1)),
        "rain_total": yr(lambda i: np.nansum(R[i], 1)),
    }
    M = {  # per-cell climatology
        "wind_annmax": np.median(S["wind_annmax"], 1), "wind_days": S["wind_days"].mean(1),
        "rain_annmax": np.median(S["rain_annmax"], 1), "rain_days": S["rain_days"].mean(1),
        "tmax_annmax": np.median(S["tmax_annmax"], 1), "heat_days": S["heat_days"].mean(1),
        "ice_days": S["ice_days"].mean(1), "cold_days": S["cold_days"].mean(1),
        "rain_annual": S["rain_total"].mean(1),
    }

    def weights(lon, lat):
        """bilinear weights over the regular POWER grid -> {cell index: w}"""
        i = int(np.clip(np.searchsorted(lons, lon) - 1, 0, len(lons) - 2))
        j = int(np.clip(np.searchsorted(lats, lat) - 1, 0, len(lats) - 2))
        tx = np.clip((lon - lons[i]) / (lons[i + 1] - lons[i]), 0, 1)
        ty = np.clip((lat - lats[j]) / (lats[j + 1] - lats[j]), 0, 1)
        return {idx[(lons[i], lats[j])]: (1 - tx) * (1 - ty), idx[(lons[i + 1], lats[j])]: tx * (1 - ty),
                idx[(lons[i], lats[j + 1])]: (1 - tx) * ty, idx[(lons[i + 1], lats[j + 1])]: tx * ty}

    cty = json.loads((RAW / "counties.geojson").read_text())["features"]
    dash = {f["properties"]["fips"]: f["properties"] for f in
            json.loads((OUT / "counties.geojson").read_text())["features"]}
    fac = json.loads((OUT / "facilities.geojson").read_text())["features"]
    kg = None
    if KGFM.exists():
        import pandas as pd
        kg = pd.read_parquet(KGFM)
        kg = kg if kg.index.name == "fips" else kg.set_index("fips")

    fips, cm, cs, n_cells = [], {k: [] for k in M}, {k: [] for k in S}, []
    for f in cty:
        g = shape(f["geometry"])
        x0, y0, x1, y1 = g.bounds
        xs, ys = np.meshgrid(np.arange(x0, x1, STEP) + STEP / 2, np.arange(y0, y1, STEP) + STEP / 2)
        inside = contains_xy(g, xs, ys)
        pts = list(zip(xs[inside], ys[inside])) or [(g.representative_point().x, g.representative_point().y)]
        w = np.zeros(len(cells))
        for lon, lat in pts:
            for k, v in weights(lon, lat).items():
                w[k] += v
        w /= w.sum()
        fips.append(f["properties"]["fips"])
        n_cells.append(int(sum(contains_xy(g, *c) for c in cells)))
        for k in M:
            cm[k].append(float(w @ M[k]))
        for k in S:
            cs[k].append((w @ S[k]).tolist())

    rk = {k: pct_rank(cm[k]) for k in cm}
    H = {"wind": (rk["wind_annmax"] + rk["wind_days"]) / 2,
         "rain": (rk["rain_annmax"] + rk["rain_days"]) / 2,
         "heat": (rk["tmax_annmax"] + rk["heat_days"]) / 2,
         "ice": (rk["ice_days"] + rk["cold_days"]) / 2}

    out = {}
    for i, fp in enumerate(fips):
        d = dash[fp]
        assets = {}
        for f in fac:
            p = f["properties"]
            if p["county"] == d["name"]:
                assets[p["cat"]] = assets.get(p["cat"], 0) + 1
        cust = plants = None
        if kg is not None and fp in kg.index:
            cust = int(kg.loc[fp, "customers"]) if kg.loc[fp, "customers"] == kg.loc[fp, "customers"] else None
            plants = int(kg.loc[fp, "n_power_plant"])
        assets["power_plant"] = plants or 0
        hv, kv69 = d["hv_km"], max(d["line_km"] - d["hv_km"], 0)
        e = (sum(EXPOSURE_W[k] * assets.get(k, 0) for k in EXPOSURE_W) + hv / HV_KM_PER_UNIT
             + kv69 / KV69_KM_PER_UNIT + (cust or 0) / CUST_PER_UNIT)
        out[fp] = {
            "name": d["name"], "n_cells": n_cells[i],
            "m": {k: round(cm[k][i], 2) for k in cm},
            "h": {k: round(float(H[k][i]), 3) for k in H},
            "series": {k: [round(v, 1) for v in cs[k][i]] for k in
                       ("wind_annmax", "rain_annmax", "heat_days", "ice_days", "cold_days", "rain_total")},
            "assets": assets, "customers": cust, "hv_km": round(hv, 1), "kv69_km": round(kv69, 1),
            "exposure_raw": round(e, 2),
        }
    er = pct_rank([v["exposure_raw"] for v in out.values()])
    for v, r in zip(out.values(), er):
        v["exposure"] = round(float(r), 3)

    meta = {
        "built": dt.date.today().isoformat(), "years": [YEARS[0], YEARS[-1]], "grid_deg": [0.625, 0.5],
        "n_grid_cells": len(cells),
        "thresholds": {"wind_ms": WIND_T, "rain_mm": RAIN_T, "heat_c": HEAT_T, "ice_p_mm": ICE_P,
                       "ice_tmax_c": ICE_TX, "cold_c": COLD_T, "rain_qc_mm": RAIN_QC_MAX},
        "exposure_weights": EXPOSURE_W | {"hv_km_per_unit": HV_KM_PER_UNIT, "kv69_km_per_unit": KV69_KM_PER_UNIT,
                                          "customers_per_unit": CUST_PER_UNIT},
        "customers_source": "EAGLE-I modelled customers per county via KGFM county_static" if kg is not None else None,
    }
    (OUT / "risk.json").write_text(json.dumps({"meta": meta, "counties": out}, separators=(",", ":")))
    print(f"risk.json: {len(out)} counties, {(OUT / 'risk.json').stat().st_size / 1e3:.0f} kB")
    for k in H:
        top = sorted(out.values(), key=lambda v: -v["h"][k])[:4]
        print(k, [(t["name"], t["h"][k]) for t in top])
    top = sorted(out.values(), key=lambda v: -v["exposure_raw"])[:6]
    print("exposure", [(t["name"], t["exposure_raw"]) for t in top])
    print("ranges", {k: (round(min(cm[k]), 1), round(max(cm[k]), 1)) for k in cm})


if __name__ == "__main__":
    main()
