"""County summaries + dashboard metadata. Run after build_lines.py and build_flood.py.

Output: docs/data/counties.geojson, docs/data/meta.json
"""
import datetime as dt
import json
import pathlib

from shapely.geometry import mapping, shape

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW, OUT, WORK = ROOT / "raw", ROOT / "docs" / "data", ROOT / "work"


def main():
    cty = json.loads((RAW / "counties.geojson").read_text())["features"]
    lines = json.loads((WORK / "line_county.json").read_text())
    flood = json.loads((WORK / "flood_county.json").read_text())
    fmeta = json.loads((WORK / "flood.json").read_text())
    fac = json.loads((OUT / "facilities.geojson").read_text())["features"]
    ev = json.loads((ROOT / "pipeline" / "events.json").read_text())

    feats = []
    for c in cty:
        name = c["properties"]["name"]
        L = lines.get(name, {})
        km = L.get("km", 0)
        p = {"name": name, "fips": c["properties"]["fips"],
             "line_km": round(km, 1),
             "canopy": round(L["canopy_km"] / km, 1) if km else None,
             "dense_km": round(L.get("dense_km", 0), 1),
             "hi_km": round(L.get("km_hi", 0), 1),
             "hv_km": round(L.get("km_hv", 0), 1),
             "h_km": round(L.get("km_h", 0), 1),
             "hp95": round(L["hp95_km"] / L["km_h"], 1) if L.get("km_h") else None,
             "fall_km": round(L.get("fall_km", 0), 1),
             "fall_hi_km": round(L.get("km_fall_hi", 0), 1),
             "fac": {}, "flood": {}}
        for f in fac:
            fp = f["properties"]
            if fp["county"] == name:
                p["fac"][fp["cat"]] = p["fac"].get(fp["cat"], 0) + 1
        for e in ev["events"]:
            eid = e["id"]
            st = {}
            for f in fac:
                fp = f["properties"]
                if fp["county"] == name:
                    s = fp["st"].get(eid, "unobserved")
                    if s in ("flooded", "near", "unobserved"):
                        st[s] = st.get(s, 0) + 1
            p["flood"][eid] = flood[name][eid] | st
        g = shape(c["geometry"]).simplify(0.002, preserve_topology=True)
        gj = json.loads(json.dumps(mapping(g)), parse_float=lambda x: round(float(x), 4))
        feats.append({"type": "Feature", "geometry": gj, "properties": p})
    (OUT / "counties.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                                     separators=(",", ":")))
    events = []
    for e in ev["events"]:
        m = fmeta["events"][e["id"]]
        events.append(e | {"dates": m["dates"], "bounds": m["bounds"], "new_water_km2": m["new_water_km2"]})
    meta = {"built": dt.date.today().isoformat(), "flood_source": fmeta["source"], "events": events,
            "reference": ev["reference"], "reference_dates": fmeta["events"][ev["events"][0]["id"]]["ref_dates"],
            "canopy": "USFS/NLCD Tree Canopy Cover 2021 (v2021-4), 30 m", "corridor_half_width_m": 45}
    lf = json.loads((OUT / "lines.geojson").read_text())["features"]
    yrs = sorted(f["properties"]["yr"] for f in lf if f["properties"].get("yr"))
    if yrs:
        q = lambda x: yrs[min(len(yrs) - 1, int(x * len(yrs)))]
        meta["height"] = {"source": "Meta / WRI global canopy height map v1 (Tolan et al. 2024), 1 m",
                          "years": {"p5": q(0.05), "median": q(0.5), "p95": q(0.95)},
                          "fall_hi": 0.25}
    (OUT / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"counties: {len(feats)}; events: {[e['id'] for e in events]}")


if __name__ == "__main__":
    main()
