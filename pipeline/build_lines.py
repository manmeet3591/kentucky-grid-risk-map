"""Vegetation exposure of Kentucky transmission lines.

Each line is clipped to Kentucky, split into ~1 km segments, and buffered by HALF_WIDTH on each side
(flat ends). Within that corridor we take USFS/NLCD 2021 tree canopy cover (30 m):

  canopy  = mean canopy cover, % (the score)
  dense   = share of 30 m cells with canopy >= 50 %

Inputs: raw/hifld_lines.geojson, raw/osm_lines_69kv.geojson, raw/counties.geojson, raw/canopy/tcc2021.vrt
Output: docs/data/lines.geojson, docs/data/line_info.json, work/line_county.json
"""
import json
import warnings
import pathlib

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds
from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString, mapping, shape
from shapely.ops import substring, transform, unary_union

warnings.filterwarnings("ignore", category=DeprecationWarning)
ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW, OUT, WORK = ROOT / "raw", ROOT / "docs" / "data", ROOT / "work"
SEG_M = 1000       # segment length
HALF_WIDTH = 45    # m each side: a typical 69-345 kV right-of-way half-width plus a tree fall-in margin
DENSE = 50         # % canopy for a cell to count as dense

to5070 = Transformer.from_crs(4326, 5070, always_xy=True).transform
to4326 = Transformer.from_crs(5070, 4326, always_xy=True).transform


def load_lines():
    out = []
    for f in json.loads((RAW / "hifld_lines.geojson").read_text())["features"]:
        p = f["properties"]
        if f["geometry"] is None or (p["VOLTAGE"] or 0) < 69:
            continue
        if "UNDERGROUND" in (p["TYPE"] or ""):
            continue  # no tree contact
        owner = p["OWNER"] if p["OWNER"] not in (None, "NOT AVAILABLE") else ""
        out.append({"geom": shape(f["geometry"]), "kv": int(p["VOLTAGE"]), "src": "HIFLD", "id": str(p["ID"]),
                    "owner": owner.title(), "sub": " – ".join(s.title() for s in (p["SUB_1"], p["SUB_2"])
                                                               if s and s != "NOT AVAILABLE")})
    p69 = RAW / "osm_lines_69kv.geojson"
    if p69.exists():
        for f in json.loads(p69.read_text())["features"]:
            p = f["properties"]
            if set(p["voltage"].split(";")) != {"69000"}:
                continue  # shared with a higher-voltage circuit that HIFLD already has
            out.append({"geom": shape(f["geometry"]), "kv": 69, "src": "OSM", "id": p["osm"],
                        "owner": p["operator"], "sub": p["name"]})
    return out


def pieces(g):
    if isinstance(g, LineString):
        return [g]
    if isinstance(g, MultiLineString):
        return list(g.geoms)
    return [p for sub in getattr(g, "geoms", []) for p in pieces(sub)]


def main():
    WORK.mkdir(exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    cty = json.loads((RAW / "counties.geojson").read_text())["features"]
    cgeo = [(c["properties"]["name"], transform(to5070, shape(c["geometry"]))) for c in cty]
    ky = unary_union([g for _, g in cgeo])
    from shapely.strtree import STRtree
    tree = STRtree([g for _, g in cgeo])

    src = rasterio.open(RAW / "canopy" / "tcc2021.vrt")
    feats, per_county = [], {}
    lines = load_lines()
    for li, ln in enumerate(lines):
        g = transform(to5070, ln["geom"]).intersection(ky)
        for part in pieces(g):
            L = part.length
            if L < 50:
                continue
            n = max(1, round(L / SEG_M))
            for k in range(n):
                seg = substring(part, L * k / n, L * (k + 1) / n)
                corridor = seg.buffer(HALF_WIDTH, cap_style="flat")
                win = from_bounds(*corridor.bounds, transform=src.transform).round_offsets().round_lengths()
                a = src.read(1, window=win, boundless=True, fill_value=255)
                m = ~geometry_mask([corridor], out_shape=a.shape, transform=src.window_transform(win))
                v = a[m & (a <= 100)]
                if v.size == 0:
                    continue
                mid = seg.interpolate(0.5, normalized=True)
                hits = [cgeo[i][0] for i in tree.query(mid, predicate="intersects")]
                county = hits[0] if hits else min(cgeo, key=lambda c: c[1].distance(mid))[0]
                canopy, dense, km = float(v.mean()), float((v >= DENSE).mean()), seg.length / 1000
                geo = transform(to4326, seg.simplify(15))
                feats.append({"type": "Feature",
                              "geometry": {"type": "LineString",
                                           "coordinates": [[round(x, 4), round(y, 4)] for x, y in geo.coords]},
                              "properties": {"l": li, "kv": ln["kv"], "county": county, "km": round(km, 2),
                                             "canopy": round(canopy, 1), "dense": round(dense, 2)}})
                c = per_county.setdefault(county, {"km": 0, "canopy_km": 0, "dense_km": 0, "km_hi": 0,
                                                   "km_hv": 0, "canopy_km_hv": 0})
                c["km"] += km
                c["canopy_km"] += canopy * km
                c["dense_km"] += dense * km
                c["km_hi"] += km if canopy >= 40 else 0
                if ln["kv"] >= 100:
                    c["km_hv"] += km
                    c["canopy_km_hv"] += canopy * km
    # higher canopy draws last so it sits on top
    feats.sort(key=lambda f: f["properties"]["canopy"])
    (OUT / "lines.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                                  separators=(",", ":")))
    used = sorted({f["properties"]["l"] for f in feats})
    (OUT / "line_info.json").write_text(json.dumps(
        {i: {k: lines[i][k] for k in ("kv", "src", "id", "owner", "sub")} for i in used}, separators=(",", ":")))
    (WORK / "line_county.json").write_text(json.dumps(per_county))
    km = sum(f["properties"]["km"] for f in feats)
    mean = sum(f["properties"]["canopy"] * f["properties"]["km"] for f in feats) / km
    by_src = {s: round(sum(f["properties"]["km"] for f in feats if lines[f["properties"]["l"]]["src"] == s))
              for s in ("HIFLD", "OSM")}
    print(f"segments: {len(feats)}, km: {km:.0f} {by_src}, length-weighted canopy: {mean:.1f}%")


if __name__ == "__main__":
    main()
