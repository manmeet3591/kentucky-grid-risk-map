"""Vegetation exposure of Kentucky transmission lines.

Each line is clipped to Kentucky, split into ~1 km segments, and buffered by HALF_WIDTH on each side
(flat ends). Within that corridor we take USFS/NLCD 2021 tree canopy cover (30 m):

  canopy  = mean canopy cover, % (the score)
  dense   = share of 30 m cells with canopy >= 50 %

and, when raw/canopy_height/ exists (fetch_canopy_height.py), Meta/WRI 1 m canopy height (Tolan et al. 2024):

  hp95    = 95th percentile height (m) of tree pixels (>= TREE m) in the corridor; 0 if almost no trees
  fall    = fall-in share: fraction of the segment's length (in BIN m steps along the line) where, on either
            side, at least MIN_PX tree pixels are taller than their distance to the centreline
  yr      = year of the imagery behind the height map at the segment midpoint
  (all three null where more than half the corridor is cloud-masked or outside the height data)

Inputs: raw/hifld_lines.geojson, raw/osm_lines_69kv.geojson, raw/counties.geojson, raw/canopy/tcc2021.vrt,
        raw/canopy_height/chm.vrt + acq_date.tif (optional)
Output: docs/data/lines.geojson, docs/data/line_info.json, work/line_county.json
"""
import json
import math
import os
import warnings
import pathlib
from multiprocessing import Pool

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
TREE = 3           # m: canopy-height pixels at least this tall count as trees
BIN = 10           # m: along-line step for the fall-in share
MIN_PX = 5         # tree pixels (~1 m2 each) taller than their distance needed to flag a step
TALL = 0.25        # fall-in share at which a segment counts as "fall-in exposed" in county totals
CHM = RAW / "canopy_height"

to5070 = Transformer.from_crs(4326, 5070, always_xy=True).transform
to4326 = Transformer.from_crs(5070, 4326, always_xy=True).transform
to3857 = Transformer.from_crs(5070, 3857, always_xy=True).transform


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


_R = {}


def _open():
    _R["tcc"] = rasterio.open(RAW / "canopy" / "tcc2021.vrt")
    if (CHM / "chm.vrt").exists():
        _R["chm"] = rasterio.open(CHM / "chm.vrt")
        _R["acq"] = rasterio.open(CHM / "acq_date.tif")


def along_dist(px, py, xy):
    """Distance from points to a polyline and their position along it (same units as the inputs)."""
    a, b = xy[:-1], xy[1:]
    ab = b - a
    seg_len = np.hypot(ab[:, 0], ab[:, 1])
    cum = np.concatenate([[0], np.cumsum(seg_len)])
    best_d = np.full(px.shape, np.inf)
    best_s = np.zeros(px.shape)
    for i in range(len(ab)):
        L2 = max(seg_len[i] ** 2, 1e-9)
        t = np.clip(((px - a[i, 0]) * ab[i, 0] + (py - a[i, 1]) * ab[i, 1]) / L2, 0, 1)
        d = np.hypot(px - (a[i, 0] + t * ab[i, 0]), py - (a[i, 1] + t * ab[i, 1]))
        m = d < best_d
        best_d[m] = d[m]
        best_s[m] = cum[i] + t[m] * seg_len[i]
    return best_d, best_s


def height_metrics(seg):
    """Canopy-height metrics for one segment (EPSG:5070 geometry), or None if not covered."""
    src = _R["chm"]
    s3 = transform(to3857, seg)
    lat = to4326(*seg.interpolate(0.5, normalized=True).coords[0])[1]
    k = 1 / math.cos(math.radians(lat))  # Web Mercator scale: 3857 metres per ground metre
    corridor = s3.buffer(HALF_WIDTH * k, cap_style="flat")
    win = from_bounds(*corridor.bounds, transform=src.transform).round_offsets().round_lengths()
    a = src.read(1, window=win, boundless=True, fill_value=255)
    wt = src.window_transform(win)
    inside = ~geometry_mask([corridor], out_shape=a.shape, transform=wt)
    ok = inside & (a != 255)
    if ok.sum() < 0.5 * inside.sum():
        return None
    h = a[ok].astype(np.float32)
    tree = h >= TREE
    hp95 = float(np.percentile(h[tree], 95)) if tree.sum() >= 100 else 0.0
    # distance to the centreline only for tree pixels; flag those that could reach it
    rr, cc = np.nonzero(ok)
    rr, cc = rr[tree], cc[tree]
    px, py = wt.c + (cc + 0.5) * wt.a, wt.f + (rr + 0.5) * wt.e
    d, along = along_dist(px, py, np.asarray(s3.coords))
    reach = h[tree] > d / k
    nbin = max(1, int(round(seg.length / BIN)))
    b = np.minimum((along[reach] / k / BIN).astype(int), nbin - 1)
    fall = float((np.bincount(b, minlength=nbin) >= MIN_PX).mean()) if reach.any() else 0.0
    yr = None
    acq = _R["acq"]
    x, y = to4326(*seg.interpolate(0.5, normalized=True).coords[0])
    r, c = acq.index(x, y)
    if 0 <= r < acq.height and 0 <= c < acq.width:
        v = float(acq.read(1, window=((r, r + 1), (c, c + 1)))[0, 0])
        yr = int(2000 + v) if v > 0 else None
    return round(hp95, 1), round(fall, 2), yr


def score(seg):
    src = _R["tcc"]
    corridor = seg.buffer(HALF_WIDTH, cap_style="flat")
    win = from_bounds(*corridor.bounds, transform=src.transform).round_offsets().round_lengths()
    a = src.read(1, window=win, boundless=True, fill_value=255)
    m = ~geometry_mask([corridor], out_shape=a.shape, transform=src.window_transform(win))
    v = a[m & (a <= 100)]
    if v.size == 0:
        return None
    out = {"canopy": round(float(v.mean()), 1), "dense": round(float((v >= DENSE).mean()), 2)}
    if "chm" in _R:
        hm = height_metrics(seg)
        out |= dict(zip(("hp95", "fall", "yr"), hm)) if hm else {"hp95": None, "fall": None, "yr": None}
    return out


def main():
    WORK.mkdir(exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    cty = json.loads((RAW / "counties.geojson").read_text())["features"]
    cgeo = [(c["properties"]["name"], transform(to5070, shape(c["geometry"]))) for c in cty]
    ky = unary_union([g for _, g in cgeo])
    from shapely.strtree import STRtree
    tree = STRtree([g for _, g in cgeo])

    lines = load_lines()
    segs = []
    for li, ln in enumerate(lines):
        g = transform(to5070, ln["geom"]).intersection(ky)
        for part in pieces(g):
            L = part.length
            if L < 50:
                continue
            n = max(1, round(L / SEG_M))
            for k in range(n):
                segs.append((li, substring(part, L * k / n, L * (k + 1) / n)))
    with Pool(os.cpu_count(), initializer=_open) as pool:
        scores = pool.map(score, [s for _, s in segs], chunksize=64)
    has_h = (CHM / "chm.vrt").exists()

    feats, per_county = [], {}
    for (li, seg), sc in zip(segs, scores):
        if sc is None:
            continue
        ln = lines[li]
        mid = seg.interpolate(0.5, normalized=True)
        hits = [cgeo[i][0] for i in tree.query(mid, predicate="intersects")]
        county = hits[0] if hits else min(cgeo, key=lambda c: c[1].distance(mid))[0]
        canopy, dense, km = sc["canopy"], sc["dense"], seg.length / 1000
        geo = transform(to4326, seg.simplify(15))
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString",
                                   "coordinates": [[round(x, 4), round(y, 4)] for x, y in geo.coords]},
                      "properties": {"l": li, "kv": ln["kv"], "county": county, "km": round(km, 2)} | sc})
        c = per_county.setdefault(county, {"km": 0, "canopy_km": 0, "dense_km": 0, "km_hi": 0,
                                           "km_hv": 0, "canopy_km_hv": 0,
                                           "km_h": 0, "hp95_km": 0, "fall_km": 0, "km_fall_hi": 0})
        c["km"] += km
        c["canopy_km"] += canopy * km
        c["dense_km"] += dense * km
        c["km_hi"] += km if canopy >= 40 else 0
        if ln["kv"] >= 100:
            c["km_hv"] += km
            c["canopy_km_hv"] += canopy * km
        if sc.get("fall") is not None:
            c["km_h"] += km
            c["hp95_km"] += sc["hp95"] * km
            c["fall_km"] += sc["fall"] * km
            c["km_fall_hi"] += km if sc["fall"] >= TALL else 0
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
    if has_h:
        hf = [f["properties"] for f in feats if f["properties"]["fall"] is not None]
        kh = sum(p["km"] for p in hf)
        yrs = np.array([p["yr"] for p in hf if p["yr"]])
        print(f"height: {kh:.0f} km covered, mean p95 {sum(p['hp95'] * p['km'] for p in hf) / kh:.1f} m, "
              f"fall-in share {sum(p['fall'] * p['km'] for p in hf) / kh:.2f}, "
              f"km with fall >= {TALL}: {sum(p['km'] for p in hf if p['fall'] >= TALL):.0f}, "
              f"imagery years {np.percentile(yrs, [5, 50, 95]) if yrs.size else '-'}")


if __name__ == "__main__":
    main()
